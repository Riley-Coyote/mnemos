"""The substrate tick decays memory the way maintenance does.

`mnemos substrate-tick` decayed by one raw UPDATE: the same flat amount off
every active memory in the file, every agent's and every scope's, standing
memories included, once per tick however little time had passed. It now
runs the store's own decay pass (run_decay_pass), as maintenance does: by
elapsed time on the curve, never a memory the agent marked standing, only
the tick's own agent, on the clock maintenance keeps, and not at all from
code older than the store.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.simple_runtime import MnemosRuntime
from mnemos.substrate.config import SubstrateConfig
from mnemos.substrate.tick import Substrate

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
TWO_DAYS_AGO = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def _capture(db, words: str, agent_id: str = "nova") -> str:
    runtime = MnemosRuntime(
        db_path=str(db), use_dedicated_model=False, **{**SCOPE, "agent_id": agent_id},
    )
    try:
        said = runtime.capture(words, impact="Kept for the tick test.")
    finally:
        runtime.close()
    return said.split("Memory ID: ")[1].split()[0]


def _last_used_two_days_ago(db, *ids: str) -> None:
    """The memories were last used, and the store last maintained (the
    captures ran a cycle), two days ago."""
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "UPDATE engrams SET last_accessed = ?, accessibility = 0.9, strength = 0.9 "
            "WHERE id = ?",
            [(TWO_DAYS_AGO, engram_id) for engram_id in ids],
        )
        conn.execute(
            "UPDATE consolidation_log SET started_at = ?, completed_at = ?",
            (TWO_DAYS_AGO, TWO_DAYS_AGO),
        )
        conn.commit()
    finally:
        conn.close()


def _levels(db, engram_id: str) -> tuple[float, float]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return tuple(conn.execute(
            "SELECT accessibility, strength FROM engrams WHERE id = ?", (engram_id,)
        ).fetchone())
    finally:
        conn.close()


def _tick(db, agent_id: str = "nova") -> dict:
    substrate = Substrate(SubstrateConfig(agent_id=agent_id, db_path=str(db)))
    try:
        return substrate.tick()
    finally:
        substrate.store.close()


@pytest.fixture
def store_of_three(tmp_path):
    """A standing memory, an ordinary one, and another agent's, all last
    used two days ago."""
    db = tmp_path / "memory.db"
    ids = {
        "standing": _capture(db, "Riley wants every change to land through a pull request."),
        "ordinary": _capture(db, "The harbour office opens at eight on weekdays."),
        "other_agent": _capture(db, "Vega keeps the tide tables in the blue binder.", "vega"),
    }
    runtime = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    try:
        runtime._ensure_init()
        assert runtime._store.set_standing(ids["standing"], True, by="claude-opus-5-5")
    finally:
        runtime.close()
    _last_used_two_days_ago(db, *ids.values())
    return db, ids


def test_a_tick_leaves_a_standing_memory_as_it_stands(store_of_three):
    db, ids = store_of_three
    _tick(db)
    assert _levels(db, ids["standing"]) == (0.9, 0.9)
    accessibility, _ = _levels(db, ids["ordinary"])
    assert accessibility < 0.9, "the ordinary memory should fade"


def test_a_tick_decays_only_its_own_agents_memories(store_of_three):
    db, ids = store_of_three
    _tick(db)
    assert _levels(db, ids["other_agent"]) == (0.9, 0.9)


def test_ticks_in_a_row_do_not_decay_the_same_hours_twice(store_of_three):
    db, ids = store_of_three
    _tick(db)
    after_one = _levels(db, ids["ordinary"])
    _tick(db)
    after_two = _levels(db, ids["ordinary"])
    assert after_two == pytest.approx(after_one, abs=1e-3)


def test_a_tick_by_code_older_than_the_store_decays_nothing(store_of_three):
    db, ids = store_of_three
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(MAINTENANCE_CODE_VERSION + 1),),
        )
        conn.commit()
    finally:
        conn.close()
    summary = _tick(db)
    for engram_id in ids.values():
        assert _levels(db, engram_id) == (0.9, 0.9)
    assert summary["engrams_decayed"] == 0
