"""Small follow-ups (WP-R19), gathered from the reports of R07, R08, R09, R16,
R16b and R17.

1. A skip holds across both passes: a memory whose lesson question was
   skipped is not asked "what did this change?", and the reverse, until a
   correction writes its words anew.
2. A briefing fetched by mnemos_context counts as shown, as the session-start
   hook's does, so the cue doesn't bring those lines again.
3. The watchdog reads the cue's offers: offers, silences, by meaning or by
   words, and failures.
4. The scheduled `mnemos consolidate` writes a maintenance report when there
   is something worth reporting, and reports older than the newest five are
   retired, never deleted.
5. A reflection lands in the memory's own note, never in one that only
   references the memory.
6. No placeholder impacts: the correction paths leave the meaning empty when
   the agent gives none (tests/test_correct_keeps_meaning.py).
7. Network waits are bounded: every network embedding call waits at most 2 s,
   the maintenance cycle's link lookup is bounded the same way, and a timeout
   skips the meaning step quietly and is counted.
8. The embedding model loads offline when cached: already true
   (tests/test_recall_by_meaning.py, section 9).
9. The Jev gate in doctor and across sessions.

No test reaches a real network, a real Jev or a real ~/.mnemos.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mnemos.store.embedding_index as ei
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.simple_runtime import MnemosRuntime, format_health_card
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
LONG_AGO = "2020-01-01T00:00:00+00:00"


def _runtime(db: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _memory_id(said: str) -> str:
    return re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", said).group(1)


def _note_id(said: str) -> str:
    return re.search(r"Continuity note ID: (\S+)", said).group(1)


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _write(db: Path, sql: str, params: tuple = ()) -> None:
    """Set a fixture store up the way time or older code would have left it."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _env(home: Path, **extra) -> dict[str, str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
           "PYTHONPATH": ":".join(sys.path), "PYTHONDONTWRITEBYTECODE": "1",
           "MNEMOS_JEV_KEY_FILE": os.devnull}
    env.update(extra)
    return env


# ── 1. A skip holds across both passes ──

HARBOUR = "The harbour pilot boards every ship at the outer buoy, whatever the weather."


def _fade(db: Path, memory: str, accessibility: float = 0.2) -> None:
    """Leave a memory old and faint enough for the softening pass to name it."""
    _write(db, "UPDATE engrams SET created_at = ?, last_accessed = ?, accessibility = ? WHERE id = ?",
           (LONG_AGO, LONG_AGO, accessibility, memory))


def _asked(db: Path, memory: str) -> list[tuple[str, bool]]:
    """Each question asked about ``memory``: its kind, and whether it ended in
    an answer (words or a skip)."""
    return [(kind, bool(answered)) for kind, answered in _read(db, """
        SELECT kind, answered_at FROM reflection_queue
        WHERE target_id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
        ORDER BY created_at
    """, (memory, *SCOPE.values()))]


def _awaiting_impact(db: Path) -> list[str]:
    """Every memory the logged cycles' softening named as waiting for a lesson."""
    named = []
    for (stats,) in _read(db, "SELECT stats FROM consolidation_log WHERE pass_name = 'cycle'"):
        named += (json.loads(stats).get("softening") or {}).get("awaiting_impact") or []
    return named


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


def test_a_skipped_lesson_question_is_never_asked_again_as_what_it_changed(tmp_path):
    """Skipped in one process, and maintenance in another asks nothing more
    about the memory."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(HARBOUR))
    finally:
        rt.close()
    _fade(db, memory)
    rt = _runtime(db)
    try:
        rt.maintain()
        assert _asked(db, memory) == [("lesson", False)], "premise: it was asked what it taught"
        said = rt.reflect(memory, "Nothing true comes to mind.", verdict="skip")
    finally:
        rt.close()
    assert said.startswith("Skipped."), said

    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run([sys.executable, "-c", _MAINTAIN, str(db)], capture_output=True,
                          text=True, timeout=180, env=_env(home))
    assert done.returncode == 0, done.stderr
    assert "Cycle: shallow" in done.stdout, done.stdout

    assert _read(db, "SELECT state FROM engrams WHERE id = ?", (memory,)) == [("active",)], (
        "premise: the impact pass could ask about it"
    )
    assert _asked(db, memory) == [("lesson", True)], (
        "the lesson question was skipped, and maintenance asked what the memory changed: "
        f"{_asked(db, memory)}"
    )


def test_a_skipped_what_did_this_change_is_never_asked_again_as_a_lesson(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(HARBOUR))
        rt.maintain()
        assert ("impact", False) in _asked(db, memory), "premise: it was asked what it changed"
        said = rt.reflect(memory, "Nothing true comes to mind.", verdict="skip")
    finally:
        rt.close()
    assert said.startswith("Skipped."), said

    _fade(db, memory)
    rt = _runtime(db)
    try:
        rt.maintain()
    finally:
        rt.close()

    assert memory in _awaiting_impact(db), "premise: the softening pass named it as fading"
    assert _asked(db, memory) == [("impact", True)], (
        f"the skip did not hold: it was asked what it taught: {_asked(db, memory)}"
    )


def test_a_correction_lets_either_question_ask_once_more(tmp_path):
    """A skip holds until the memory's words change. A correction writes them
    as a new memory (with a version keeping the old words), which may be asked
    again; the skipped one stays as it was."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(HARBOUR))
        rt.maintain()
        rt.reflect(memory, "Nothing true comes to mind.", verdict="skip")
        corrected = rt.correct(
            correction="The harbour pilot boards every ship at the inner buoy, whatever the weather.",
            target_id=memory,
        )
        replacement = re.search(r"captured correction (engram_[A-Za-z0-9]+)", corrected).group(1)
        rt.maintain()
    finally:
        rt.close()

    assert _asked(db, memory) == [("impact", True)]
    assert _asked(db, replacement) == [("impact", False)], (
        "the corrected memory was not asked what it changed"
    )


def test_the_watchdog_does_not_count_a_skipped_memory_as_never_asked(tmp_path, monkeypatch):
    """Its idle evidence named fading memories never asked what they taught;
    one whose "what did this change?" was skipped is never asked that, and is
    owed nothing."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(HARBOUR))
    finally:
        rt.close()
    _write(db, "UPDATE engrams SET created_at = ?", (_ago(days=5),))
    _write(db, "DELETE FROM reflection_queue")
    _write(db, "DELETE FROM consolidation_log")
    _write(db, "INSERT INTO reflection_queue (id, agent_id, person_id, project_scope, kind, target_id, "
               "prompt, surfaced_count, created_at, expires_at, answered_at, answer) "
               "VALUES ('ask-skip', 'nova', 'riley', 'demo', 'impact', ?, 'What did this change?', 1, ?, ?, ?, "
               "'Nothing true comes to mind.')",
           (memory, _ago(days=4), _ago(days=-26), _ago(days=4)))
    store = EngramStore(str(db))
    try:
        for hours in (40, 3):
            at = _ago(hours=hours)
            store.log_consolidation(f"cycle_{hours}", "cycle", at, at, stats={
                "cycle_type": "shallow",
                "decay": {"engrams_processed": 1, "engrams_decayed": 0},
                "connection_discovery": {"engrams_processed": 1, "connections_created": 0},
                "softening": {"engrams_evaluated": 1, "engrams_softened": 0, "awaiting_impact": [memory]},
            }, **SCOPE)
    finally:
        store.close()

    rt = _runtime(db)
    try:
        data = rt.health()
    finally:
        rt.close()
    check = data["watchdog"]["checks"]["maintenance"]
    assert check["idle_evidence"]["lessons_never_asked"] == [], check
    assert "ATTENTION" not in format_health_card(data)


def test_the_code_before_this_change_stands_down_once_this_code_opens_the_store(
    tmp_path, monkeypatch,
):
    """Servers started before this change still ask about a skipped memory,
    and must stop maintaining once this code has opened the store."""
    assert MAINTENANCE_CODE_VERSION >= 10, (
        "which memories are asked changed, and the code version was not raised"
    )
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(HARBOUR))
        rt.maintain()
        rt.reflect(memory, "Nothing true comes to mind.", verdict="skip")
    finally:
        rt.close()
    assert _read(db, "SELECT value FROM meta WHERE key = 'min_code_version'") == [
        (str(MAINTENANCE_CODE_VERSION),)
    ]
    _fade(db, memory)

    for module in ("mnemos.code_version", "mnemos.simple_runtime", "mnemos.store.sqlite_store",
                   "mnemos.retrieval.reactive"):
        monkeypatch.setattr(f"{module}.MAINTENANCE_CODE_VERSION", MAINTENANCE_CODE_VERSION - 1,
                            raising=False)
    rt = _runtime(db)
    try:
        said = rt.maintain()
    finally:
        rt.close()
    assert "Cycle: skipped" in said, said
    assert _asked(db, memory) == [("impact", True)]


# ── 5. A reflection lands in the memory's own note ──


def _note(db: Path, note_id: str) -> str:
    return _read(db, "SELECT content FROM hypomnema_entries WHERE id = ?", (note_id,))[0][0]


def test_a_reflection_lands_in_the_memorys_own_note_not_one_that_references_it(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        captured = rt.capture(HARBOUR)
        memory, own = _memory_id(captured), _note_id(captured)
        # A note interpreting the memory, written after it (as the advanced
        # mnemos_hypomnema_write writes one): it names the memory, and is not it.
        interpreting = rt._store.write_hypomnema_entry(
            "Reading of the pilot memory: the outer buoy is the fixed point.", **SCOPE,
            source="synthesized", related_engram_id=memory,
        )
        rt.maintain()
        assert ("impact", False) in _asked(db, memory), "premise: asked what it changed"
        said = rt.reflect(memory, "A fixed meeting point is what makes the handover safe.")
    finally:
        rt.close()

    assert said.startswith("Reflection recorded."), said
    assert "What this changed: A fixed meeting point" in _note(db, own), _note(db, own)
    assert "What this changed" not in _note(db, interpreting), (
        "the reflection landed in a note that only references the memory"
    )


def test_a_correction_that_keeps_a_reference_still_gets_its_own_reflection(tmp_path):
    """A note that only referenced a memory, corrected, becomes a pair of its
    own that still names the memory it referenced. A reflection on the new
    memory lands in its own note."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        captured = rt.capture(HARBOUR)
        memory, own = _memory_id(captured), _note_id(captured)
        interpreting = rt._store.write_hypomnema_entry(
            "Reading of the pilot memory: the outer buoy is the fixed point.", **SCOPE,
            source="synthesized", related_engram_id=memory,
        )
        corrected = rt.correct(
            correction="Reading of the pilot memory: the inner buoy is the fixed point.",
            target_id=interpreting,
        )
        replacement, replacement_note = _memory_id(corrected), _note_id(corrected)
        assert _read(db, "SELECT related_engram_id, graduated_to_engram_id FROM hypomnema_entries "
                         "WHERE id = ?", (replacement_note,)) == [(memory, replacement)], "premise"
        rt._store.enqueue_reflection("impact", replacement, "What did this change?", **SCOPE)
        said = rt.reflect(replacement, "The fixed point moved, and the handover with it.")
    finally:
        rt.close()

    assert said.startswith("Reflection recorded."), said
    assert "What this changed: The fixed point moved" in _note(db, replacement_note), (
        "the reflection never reached the corrected memory's own note"
    )
    assert "What this changed" not in _note(db, own)
