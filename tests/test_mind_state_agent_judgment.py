"""The mind-state report counts agent-authored beliefs and contradictions.

This metric is the launch claim's evidence — a keyless install that forms
beliefs and types contradiction edges through the agent. The readiness script
exercises it end to end but does not run in CI, so this guards the counter
directly: only agent-authored, still-active content is counted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks.continuity_eval import mind_state  # noqa: E402

from mnemos.core.belief import Belief  # noqa: E402
from mnemos.core.engram import Engram  # noqa: E402
from mnemos.core.types import ConnectionRelation  # noqa: E402
from mnemos.store.sqlite_store import EngramStore  # noqa: E402


def test_agent_judgment_counts_only_agent_authored_active(tmp_path):
    db = str(tmp_path / "mind.db")
    store = EngramStore(db)
    try:
        # An agent belief (counted) and a superseded one (not) and a seed (not).
        store.save_belief(Belief(agent_id="default", content="held", confidence=0.6,
                                 source="agent"))
        store.save_belief(Belief(agent_id="default", content="retired", confidence=0.6,
                                 source="agent", superseded_by="retired"))
        store.save_belief(Belief(agent_id="default", content="seeded", confidence=0.6,
                                 source="seed"))
        # An agent contradiction edge (counted) and a machine one (not).
        a, b, c = Engram(content="A"), Engram(content="B"), Engram(content="C")
        for e in (a, b, c):
            store.save_engram(e)
        a.add_connection(target_id=b.id, relation=ConnectionRelation.CONTRADICTS,
                         strength=0.7, formed_by="agent_reflection")
        a.add_connection(target_id=c.id, relation=ConnectionRelation.CONTRADICTS,
                         strength=0.7, formed_by="encoding")
        store.save_engram(a)
    finally:
        store.close()

    j = mind_state(db)["agent_judgment"]
    assert j["beliefs"] == 1, j
    assert j["contradictions"] == 1, j
    assert j["alive"] is True


def test_agent_judgment_dead_on_an_empty_store(tmp_path):
    db = str(tmp_path / "empty.db")
    EngramStore(db).close()
    j = mind_state(db)["agent_judgment"]
    assert j == {"beliefs": 0, "contradictions": 0, "alive": False}
