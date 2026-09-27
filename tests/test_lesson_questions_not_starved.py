"""A fading memory is asked what it taught, even behind memories already asked.

When the agent's own memories fade without a lesson, the softening pass names
them in ``awaiting_impact``, the most accessible first, and maintenance asks
about two of them each cycle. It took the first two and only then asked. The
queue refuses a second lesson question about a memory however the first one
ended (answered, skipped, shown out or expired), so once the two most
accessible had been asked they took both places in every cycle after. Nothing
further was asked, each memory behind them faded without its question, and
every cycle reported success.

The memories already asked are now set aside first, and up to two of the rest
are asked.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}

# Three of the agent's own memories with the accessibility each is left at,
# most accessible first: the order the softening pass names them in.
HARBOUR = (
    ("The harbour pilot boards every ship at the outer buoy, whatever the weather.", 0.30),
    ("Brass lamps on the jetty are polished each Sunday by the harbourmaster's niece.", 0.25),
    ("Tide tables for the estuary are posted at the chandlery door each morning.", 0.20),
)
LONG_AGO = "2020-01-01T00:00:00+00:00"


def _runtime(db: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _fading(db: Path) -> list[str]:
    """Capture the three, then leave them old and faint enough to fade.

    Returns their ids in the order the softening pass names them."""
    rt = _runtime(db)
    try:
        ids = [
            re.search(r"Memory ID: (engram_\w+)", rt.capture(text)).group(1)
            for text, _ in HARBOUR
        ]
    finally:
        rt.close()
    conn = sqlite3.connect(str(db))
    try:
        for memory, (_, accessibility) in zip(ids, HARBOUR):
            conn.execute(
                "UPDATE engrams SET created_at = ?, last_accessed = ?, accessibility = ? "
                "WHERE id = ?",
                (LONG_AGO, LONG_AGO, accessibility, memory),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _lessons(db: Path) -> dict[str, int]:
    """How many lesson questions each memory has been asked."""
    return dict(_read(db, """
        SELECT target_id, COUNT(*) FROM reflection_queue
        WHERE kind = 'lesson' AND agent_id = ? AND person_id = ? AND project_scope = ?
        GROUP BY target_id
    """, tuple(SCOPE.values())))


def _waiting_for(db: Path, memory: str) -> list[str]:
    """The kinds of question still waiting for an answer about ``memory``."""
    return [kind for (kind,) in _read(db, """
        SELECT kind FROM reflection_queue
        WHERE target_id = ? AND answered_at IS NULL
          AND agent_id = ? AND person_id = ? AND project_scope = ?
        ORDER BY kind
    """, (memory, *SCOPE.values()))]


def _end(rt: MnemosRuntime, memory: str, ended: str) -> None:
    """Let the lesson question already put to ``memory`` end as ``ended``."""
    store = rt._store
    assert store is not None
    lesson = "target_id = ? AND kind = 'lesson'"
    if ended == "shown out":
        store._get_conn().execute(
            f"UPDATE reflection_queue SET surfaced_count = ? WHERE {lesson}",
            (store.MAX_SURFACINGS, memory),
        )
    elif ended == "expired":
        store._get_conn().execute(
            f"UPDATE reflection_queue SET expires_at = ? WHERE {lesson}", (LONG_AGO, memory)
        )
    elif ended == "skipped":
        said = rt.reflect(memory, "Nothing true comes to mind.", verdict="skip")
        assert said.startswith("Skipped."), said
        closed = store._get_conn().execute(
            f"SELECT answered_at FROM reflection_queue WHERE {lesson}", (memory,)
        ).fetchone()
        assert closed[0] is not None, "the skip closed some other question"
    store._commit()


def _mark_for(db: Path, version: int) -> None:
    """What a server of ``version`` leaves in a store it opened."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(version),),
        )
        conn.commit()
    finally:
        conn.close()


def _running(monkeypatch, version: int) -> None:
    """Run this process as Mnemos code of ``version``."""
    for module in (
        "mnemos.code_version",
        "mnemos.simple_runtime",
        "mnemos.store.sqlite_store",
        "mnemos.retrieval.reactive",
    ):
        monkeypatch.setattr(f"{module}.MAINTENANCE_CODE_VERSION", version, raising=False)


# ── The two at the head no longer take both places ──


@pytest.mark.parametrize("ended", ["waiting", "shown out", "expired", "skipped"])
def test_two_memories_asked_before_leave_room_for_the_third(tmp_path, ended):
    db = tmp_path / "memory.db"
    first, second, third = _fading(db)
    rt = _runtime(db)
    try:
        rt._ensure_init()
        assert rt._enqueue_lesson_reflections({"awaiting_impact": [first, second]}) == 2
        for memory in (first, second):
            _end(rt, memory, ended)
        asked = rt._enqueue_lesson_reflections(
            {"awaiting_impact": [first, second, third]}
        )
    finally:
        rt.close()

    assert _lessons(db) == {first: 1, second: 1, third: 1}, (
        f"two memories whose lesson questions were {ended} took both places, and "
        f"the third fading memory was never asked what it taught: {_lessons(db)}"
    )
    assert asked == 1


def test_the_next_cycle_asks_the_memory_the_first_two_left_waiting(tmp_path):
    """Nothing made by hand: the first cycle asks the two most accessible, and
    before this change every cycle after it asked nothing at all."""
    db = tmp_path / "memory.db"
    first, second, third = _fading(db)
    rt = _runtime(db)
    try:
        rt.maintain()
        assert _lessons(db) == {first: 1, second: 1}, "the first cycle asks the two at the head"

        rt.maintain()
        assert _lessons(db) == {first: 1, second: 1, third: 1}, (
            "the second cycle asked nothing: the two asked in the first took both "
            f"places again, and the third memory faded unasked: {_lessons(db)}"
        )
        # Its plain "what did this change?" is folded into the lesson question:
        # one question about one memory, not two.
        assert _waiting_for(db, third) == ["lesson"]

        rt.maintain()
        assert _lessons(db) == {first: 1, second: 1, third: 1}, "a memory was asked twice"
    finally:
        rt.close()


# ── Read back in another process ──

_MAINTAIN = """
import sys
from mnemos.simple_runtime import MnemosRuntime

runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    print(runtime.maintain())
finally:
    runtime.close()
"""


def test_a_question_asked_in_one_process_is_answered_in_another(tmp_path):
    db = tmp_path / "memory.db"
    first, second, third = _fading(db)
    rt = _runtime(db)
    try:
        rt.maintain()
    finally:
        rt.close()

    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", _MAINTAIN, str(db)],
        capture_output=True, text=True, timeout=180,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
             "PYTHONPATH": ":".join(sys.path)},
    )
    assert done.returncode == 0, done.stderr
    assert "Cycle: shallow" in done.stdout, done.stdout

    rt = _runtime(db)
    try:
        rt._ensure_init()
        assert rt._store is not None
        waiting = {
            item["target_id"]: item
            for item in rt._store.pending_reflections(**SCOPE, limit=10)
            if item["kind"] == "lesson"
        }
        assert third in waiting, f"the other process asked nothing about it: {waiting}"
        assert waiting[third]["excerpt"].startswith("Tide tables for the estuary")
        said = rt.reflect(third, "Notices posted where people already stop are the ones read.")
    finally:
        rt.close()
    assert said.startswith("Lesson recorded."), said


# ── Older code asks nothing ──


def test_this_code_asks_nothing_once_newer_code_has_opened_the_store(tmp_path):
    """The code written here becomes the old code later."""
    db = tmp_path / "memory.db"
    _fading(db)
    _mark_for(db, MAINTENANCE_CODE_VERSION + 1)
    rt = _runtime(db)
    try:
        said = rt.maintain()
    finally:
        rt.close()
    assert "Cycle: skipped" in said, said
    assert _lessons(db) == {}, "code older than the store chose memories to ask about"


def test_the_code_before_this_change_stands_down_once_this_code_opens_the_store(
    tmp_path, monkeypatch,
):
    """Servers started before this change still choose the old way, and must
    stop choosing once this code has opened the store."""
    assert MAINTENANCE_CODE_VERSION >= 9, (
        "which memories are asked for a lesson changed, and the code version was not raised"
    )
    db = tmp_path / "memory.db"
    _fading(db)
    assert _read(db, "SELECT value FROM meta WHERE key = 'min_code_version'") == [
        (str(MAINTENANCE_CODE_VERSION),)
    ]

    _running(monkeypatch, MAINTENANCE_CODE_VERSION - 1)
    rt = _runtime(db)
    try:
        said = rt.maintain()
    finally:
        rt.close()
    assert "Cycle: skipped" in said, said
    assert _lessons(db) == {}
