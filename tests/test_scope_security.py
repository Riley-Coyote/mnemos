"""Security boundaries for person and project scoped durable memory."""

from __future__ import annotations

import re

from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore


def _memory_id(capture_result: str) -> str:
    match = re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", capture_result)
    assert match
    return match.group(1)


def test_identity_graph_isolated_between_people(tmp_path):
    db = str(tmp_path / "scope.db")
    bob = MnemosRuntime(db_path=db, agent_id="shared", person_id="bob", project_scope="global")
    alice = MnemosRuntime(db_path=db, agent_id="shared", person_id="alice", project_scope="global")
    try:
        bob.capture("Bob's private launch word is ZEPHYR.", importance="high")
        graph = alice.identity_graph()
    finally:
        bob.close()
        alice.close()

    assert "ZEPHYR" not in str(graph)


def test_out_of_scope_recall_cannot_reconsolidate_memory(tmp_path):
    db = str(tmp_path / "scope.db")
    bob = MnemosRuntime(db_path=db, agent_id="shared", person_id="bob", project_scope="global")
    alice = MnemosRuntime(db_path=db, agent_id="shared", person_id="alice", project_scope="global")
    try:
        memory_id = _memory_id(bob.capture("Bob alone knows the QUASAR ORCHID launch phrase."))
        store = EngramStore(db)
        before = store.get_engram(memory_id)
        assert before is not None
        before_counts = (before.access_count, before.reconsolidation_count)
        store.close()

        result = alice.recall("QUASAR ORCHID")

        store = EngramStore(db)
        after = store.get_engram(memory_id)
        assert after is not None
        after_counts = (after.access_count, after.reconsolidation_count)
        store.close()
    finally:
        bob.close()
        alice.close()

    assert "QUASAR ORCHID" not in result
    assert after_counts == before_counts


def test_out_of_scope_id_cannot_archive_memory(tmp_path):
    db = str(tmp_path / "scope.db")
    bob = MnemosRuntime(db_path=db, agent_id="shared", person_id="bob", project_scope="global")
    alice = MnemosRuntime(db_path=db, agent_id="shared", person_id="alice", project_scope="global")
    try:
        memory_id = _memory_id(bob.capture("Bob's memory must remain active."))
        response = alice.correct("", target_id=memory_id, action="forget")
        store = EngramStore(db)
        memory = store.get_engram(memory_id)
        store.close()
    finally:
        bob.close()
        alice.close()

    assert "Archived memory" not in response
    assert memory is not None and memory.state == "active"


def test_project_scope_isolated_before_retrieval(tmp_path):
    db = str(tmp_path / "scope.db")
    alpha = MnemosRuntime(db_path=db, agent_id="agent", person_id="riley", project_scope="alpha")
    beta = MnemosRuntime(db_path=db, agent_id="agent", person_id="riley", project_scope="beta")
    try:
        alpha.capture("Project alpha contains the private COBALT FERN decision.")
        result = beta.recall("COBALT FERN")
    finally:
        alpha.close()
        beta.close()

    assert "COBALT FERN" not in result


def test_unscoped_legacy_engram_is_quarantined(tmp_path):
    db = str(tmp_path / "scope.db")
    store = EngramStore(db)
    legacy = Engram(
        content="ambiguous legacy secret",
        owner_agent_id="agent",
        person_id="",
        project_scope="",
    )
    store.save_engram(legacy)

    assert not store.engram_visible_in_scope(
        legacy.id, agent_id="agent", person_id="riley", project_scope="global"
    )
    assert store.search_fts(
        '"ambiguous"', agent_id="agent", person_id="riley", project_scope="global"
    ) == []
    store.close()
