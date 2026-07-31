"""The agent forms and judges its own beliefs and contradictions, keyless.

Two reflection kinds were legal in the schema but never produced or applied.
Wiring them completes the inversion: a keyless install now does the last two
judgment tasks — belief formation and contradiction detection — through the
agent's own turns, no provider. The server only ever *proposes*; the agent's
answer is what becomes a belief or a contradiction edge.
"""

from __future__ import annotations

import re

import pytest

from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "kinds.db")


def _runtime(db):
    return MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")


def _pending(rt, kind):
    return [
        i for i in rt.pending_reflections(limit=10) if i["kind"] == kind
    ]


class TestBeliefFormation:
    def test_a_recurring_theme_is_offered_as_a_belief(self, db):
        rt = _runtime(db)
        # Five captures sharing a real (non-structural) theme tag.
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}",
                       importance="high")
        rt.maintain()
        beliefs = _pending(rt, "belief")
        assert beliefs, "no belief candidate surfaced for a strongly recurring theme"
        assert "belief you now hold" in beliefs[0]["prompt"].lower()

    def test_answering_forms_an_agent_belief(self, db):
        rt = _runtime(db)
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}")
        rt.maintain()
        item = _pending(rt, "belief")[0]
        out = rt.reflect(item["target_id"], "Vektor's performance is never quite finished.")
        assert "Belief recorded" in out, out
        beliefs = rt._store.get_beliefs("t", active_only=True)
        mine = [b for b in beliefs if b.source == "agent"]
        assert any("never quite finished" in b.content for b in mine)

    def test_an_empty_answer_forms_nothing(self, db):
        rt = _runtime(db)
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}")
        rt.maintain()
        item = _pending(rt, "belief")[0]
        before = len(rt._store.get_beliefs("t"))
        rt.reflect(item["target_id"], "   ")
        assert len(rt._store.get_beliefs("t")) == before


class TestBeliefReaffirmation:
    def test_no_retires_a_belief(self, db):
        rt = _runtime(db)
        rt._ensure_init()
        # Capture with an impact so no impact reflection is queued on this
        # engram — the belief reaffirmation must own its target uncontended
        # (the tool answers by target_id alone).
        rt.capture(content="anchor memory for the belief", impact="grounds a belief")
        eid = rt._store.get_active_engrams(agent_id="t", limit=1)[0].id
        rt._store.save_belief(Belief(agent_id="t", content="An outdated belief",
                                     confidence=0.6, source="agent",
                                     supporting_engram_ids=[eid]))
        # Reaffirmation reflection targets the supporting engram with a marker.
        rt._store.enqueue_reflection(
            "belief", eid,
            "You hold this belief: \"An outdated belief\". Still true? "
            f"[belief:{[b.id for b in rt._store.get_beliefs('t')][0]}]",
            agent_id="t", person_id="p", project_scope="g",
        )
        item = _pending(rt, "belief")[0]
        out = rt.reflect(item["target_id"], "no")
        assert "Retired" in out
        assert rt._store.get_beliefs("t", active_only=True) == []


class TestContradiction:
    def _surprising_pair(self, rt):
        """A capture with a surprising sibling, so a candidate can surface."""
        rt._ensure_init()
        rt.capture(content="Riley always ships through pull requests, never to main")
        # A directly conflicting later capture; force a surprise signal so the
        # enqueuer offers it (surprise detection is store-state dependent).
        eng = rt._encoder.encode(
            content="Riley now pushes small fixes straight to main without a PR",
            agent_id="t",
        )
        eng.encoding_context.surprise_level = 0.7
        rt._store.save_engram(eng)
        return eng

    def test_a_surprising_capture_surfaces_a_contradiction_candidate(self, db):
        rt = _runtime(db)
        self._surprising_pair(rt)
        rt.maintain()
        cand = _pending(rt, "contradiction")
        assert cand, "no contradiction candidate surfaced for a surprising conflicting capture"
        assert re.search(r"\[ref:engram_", cand[0]["prompt"])

    def test_yes_writes_a_contradicts_edge_and_downweights(self, db):
        rt = _runtime(db)
        eng = self._surprising_pair(rt)
        rt.maintain()
        item = _pending(rt, "contradiction")[0]
        other_id = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", item["prompt"]).group(1)
        before = rt._store.get_engram(other_id).strength

        out = rt.reflect(item["target_id"], "yes, he changed his workflow")
        assert "Contradiction recorded" in out, out

        edges = rt._store.get_connections(item["target_id"])
        assert any(
            c.target_id == other_id
            and str(getattr(c.relation, "value", c.relation)) == "contradicts"
            and c.formed_by == "agent_reflection"
            for c in edges
        ), "no agent-authored CONTRADICTS edge was written"
        # The older memory was downweighted — the deliberate downward move.
        assert rt._store.get_engram(other_id).strength < before

    def test_no_records_no_conflict(self, db):
        rt = _runtime(db)
        self._surprising_pair(rt)
        rt.maintain()
        item = _pending(rt, "contradiction")[0]
        other_id = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", item["prompt"]).group(1)
        out = rt.reflect(item["target_id"], "no, those are about different things")
        assert "not a contradiction" in out.lower()
        edges = rt._store.get_connections(item["target_id"])
        assert not any(c.target_id == other_id for c in edges)


class TestRestraintHolds:
    def test_the_packet_never_shows_more_than_two_reflections(self, db):
        rt = _runtime(db)
        for i in range(6):
            rt.capture(content=f"The vektor project needs another perf pass note {i}",
                       importance="high")
        rt.maintain()
        assert len(rt.pending_reflections(limit=2)) <= 2
