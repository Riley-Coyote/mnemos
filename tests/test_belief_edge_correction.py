"""The ratchet must run both ways for what the agent authors.

Before the agent can form beliefs and type contradiction edges into its own
graph, a wrong one must be correctable — otherwise a mistaken judgment is
permanent (stability only ever ratchets up; decay grows it with connection
count). This is the safety floor the whole keyless-mind feature stands on.

These tests prove the deliberate downward moves exist: an agent-authored
belief's confidence can be lowered and the belief retired, and an
agent-authored edge can be removed — while seed/model beliefs are protected
from an accidental erase.
"""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.core.belief import Belief
from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "correct.db")


def _runtime(db):
    return MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")


class TestBeliefProvenanceRoundTrips:
    def test_source_persists(self, db):
        store = EngramStore(db)
        try:
            store.save_belief(Belief(agent_id="t", content="Riley ships via PRs",
                                     confidence=0.6, source="agent"))
            got = store.get_beliefs("t")[0]
            assert got.source == "agent"
        finally:
            store.close()

    def test_old_store_gains_the_source_column(self, tmp_path):
        """A beliefs table from before provenance must upgrade on open."""
        raw = str(tmp_path / "old.db")
        conn = sqlite3.connect(raw)
        conn.execute(
            "CREATE TABLE beliefs (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL "
            "DEFAULT 'default', content TEXT NOT NULL, confidence REAL NOT NULL "
            "DEFAULT 0.3, domain TEXT NOT NULL DEFAULT 'general', created_at TEXT "
            "NOT NULL, last_revised TEXT NOT NULL, last_challenged TEXT NOT NULL, "
            "revision_history TEXT NOT NULL DEFAULT '[]', superseded_by TEXT, "
            "supporting_engram_ids TEXT NOT NULL DEFAULT '[]')"
        )
        conn.execute(
            "INSERT INTO beliefs (id, content, created_at, last_revised, "
            "last_challenged) VALUES ('belief_old', 'a legacy belief', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', "
            "'2025-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        store = EngramStore(raw)  # migration runs on open
        try:
            got = store.get_beliefs("default")
            assert got and got[0].content == "a legacy belief"
            assert got[0].source == ""  # unknown, not fabricated
        finally:
            store.close()


class TestBeliefDownweight:
    def test_revise_belief_lowers_confidence(self, db):
        store = EngramStore(db)
        try:
            b = Belief(agent_id="t", content="X is true", confidence=0.8, source="agent")
            store.save_belief(b)
            assert store.revise_belief(b.id, 0.4, reason="counter-evidence")
            assert store.get_belief(b.id).confidence == pytest.approx(0.4)
        finally:
            store.close()

    def test_supersede_belief_hides_it(self, db):
        store = EngramStore(db)
        try:
            b = Belief(agent_id="t", content="X is true", confidence=0.8, source="agent")
            store.save_belief(b)
            assert store.supersede_belief(b.id, reason="wrong")
            assert store.get_beliefs("t", active_only=True) == []
            # still on disk for provenance
            assert store.get_belief(b.id) is not None
        finally:
            store.close()


class TestAgentCanCorrectItsOwnBelief:
    def test_forget_retires_an_agent_belief(self, db):
        rt = _runtime(db)
        rt._ensure_init()
        rt._store.save_belief(Belief(agent_id="t", content="Riley prefers dark roast coffee",
                                     confidence=0.7, source="agent"))
        out = rt.correct(correction="", query="dark roast coffee", action="forget")
        assert "Retired" in out, out
        assert rt._store.get_beliefs("t", active_only=True) == []

    def test_update_lowers_confidence(self, db):
        rt = _runtime(db)
        rt._ensure_init()
        rt._store.save_belief(Belief(agent_id="t", content="Riley works best at night",
                                     confidence=0.8, source="agent"))
        out = rt.correct(correction="less sure about night work", query="works best at night")
        assert "Lowered confidence" in out, out
        assert rt._store.get_beliefs("t")[0].confidence < 0.8

    def test_a_seed_belief_is_protected(self, db):
        """The agent must not be able to erase a belief it did not author."""
        rt = _runtime(db)
        rt._ensure_init()
        rt._store.save_belief(Belief(agent_id="t", content="Riley prefers dark roast coffee",
                                     confidence=0.7, source="seed"))
        out = rt.correct(correction="", query="dark roast coffee", action="forget")
        assert "Retired" not in out
        assert rt._store.get_beliefs("t", active_only=True), "a seed belief was erased"


class TestAgentAuthoredEdgeIsRemovable:
    def test_remove_connection_drops_an_agent_edge(self, db):
        store = EngramStore(db)
        try:
            a = Engram(content="memory A about the deploy")
            b = Engram(content="memory B about the deploy")
            store.save_engram(a)
            store.save_engram(b)
            a.add_connection(target_id=b.id, relation=ConnectionRelation.CONTRADICTS,
                             strength=0.6, formed_by="agent_reflection")
            store.save_engram(a)
            assert any(c.target_id == b.id for c in store.get_connections(a.id))
            store.remove_connection(a.id, b.id)
            assert not any(c.target_id == b.id for c in store.get_connections(a.id))
        finally:
            store.close()
