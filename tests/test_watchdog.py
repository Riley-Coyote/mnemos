"""A watchdog for silent failure.

Every serious failure this memory has had looked like success: a maintenance
report nobody saw for weeks, 161 questions nobody answered, lesson questions
starved behind stale ones, old servers writing by old rules. Each layer said
it was healthy.

These tests build a store where one thing has stalled, and one where nothing
has, and check what the health card and ``mnemos doctor`` say: one plain
sentence and a command for what stalled, and nothing at all when all is well.
The watchdog reads only, so the last tests check that looking changes nothing.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.core.engram import Engram
from mnemos.dream_journal import write_dream_entry
from mnemos.simple_runtime import MnemosRuntime, format_health_card, recall_index_meta_key
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
CURRENT_SESSION = "11111111-aaaa-4bbb-8ccc-000000000001"
OLDER_SESSION = "22222222-bbbb-4ccc-8ddd-000000000002"
# A store opened by code far newer than this one.
AHEAD = 999


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Recall by words only, whatever this machine has installed; the tests of
    # the meaning index give it a backend of their own (``meaning``).
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)


def _runtime(db: Path, **kwargs) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE, **kwargs)


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _day(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).date().isoformat()


def _write(db: Path, sql: str, params: tuple = ()) -> None:
    """Set up a fixture store the way time or older code would have left it."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _healthy(tmp_path: Path) -> Path:
    """Memory in ordinary use: two captures, maintenance after each, and a
    briefing that delivered them."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
        runtime.capture("The staging server restarts every Sunday night.")
        runtime.context()
    finally:
        runtime.close()
    return db


def _memory_id(db: Path) -> str:
    return _read(db, "SELECT id FROM engrams WHERE owner_agent_id = 'nova' ORDER BY created_at LIMIT 1")[0][0]


def _ask(db: Path, kind: str, target: str, *, created: str, shown: int,
         answered: bool = False, expires: str | None = None) -> None:
    """One question in the queue, as maintenance proposed it and the briefings
    showed it."""
    _write(
        db,
        "INSERT INTO reflection_queue (id, agent_id, person_id, project_scope, kind, "
        "target_id, prompt, surfaced_count, created_at, expires_at, answered_at, answer) "
        "VALUES (?, 'nova', 'riley', 'demo', ?, ?, 'What did this change?', ?, ?, ?, ?, ?)",
        (
            f"ask-{kind}-{created}", kind, target, shown, created,
            expires or (datetime.fromisoformat(created) + timedelta(days=30)).isoformat(),
            _ago(hours=1) if answered else None, "It changed the plan." if answered else None,
        ),
    )


def _cycle(db: Path, completed: str, **passes) -> None:
    """A maintenance cycle in the log, with what each pass reported."""
    store = EngramStore(str(db))
    try:
        store.log_consolidation(
            f"cycle_{completed}", "cycle", completed, completed,
            stats={"cycle_type": "shallow", "passes_run": list(passes), **passes},
            **SCOPE,
        )
    finally:
        store.close()


def _card(db: Path) -> tuple[str, dict]:
    runtime = _runtime(db)
    try:
        data = runtime.health()
        return format_health_card(data), data
    finally:
        runtime.close()


def _doctor(db: Path, capsys) -> str:
    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    return capsys.readouterr().out


def _flags(card: str) -> list[str]:
    return [line for line in card.splitlines() if line.startswith("ATTENTION — ") and " Run: " in line]


# ── Silent when healthy ──


def test_a_healthy_store_raises_no_flag_and_prints_nothing(tmp_path, capsys):
    db = _healthy(tmp_path)

    card, data = _card(db)
    out = _doctor(db, capsys)

    assert data["watchdog"]["flags"] == [], data["watchdog"]["flags"]
    assert "ATTENTION" not in card, card
    assert "ATTENTION" not in out, out
    # Every check still says what it expected and what it saw.
    for name, check in data["watchdog"]["checks"].items():
        assert check["expected"] and check["seen"], name
        assert check["stalled"] is False, (name, check)


# ── Questions nobody answers ──


def test_questions_shown_three_times_and_never_answered_are_flagged(tmp_path, capsys):
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    _ask(db, "impact", memory, created=_ago(days=3), shown=3)
    _ask(db, "belief", memory, created=_ago(days=2), shown=3)
    _ask(db, "lesson", memory, created=_ago(days=4), shown=1, answered=True)

    card, data = _card(db)
    out = _doctor(db, capsys)

    said = (
        "2 questions were shown three times and never answered, and 1 of 3 were "
        "ever answered (33%)."
    )
    assert f"ATTENTION — {said} Run: mnemos_reflect(target_id=\"{memory}\"" in card, card
    assert said in out, out
    check = data["watchdog"]["checks"]["questions"]
    assert (check["asked"], check["answered"], check["answer_rate"]) == (3, 1, 33)
    assert check["unanswered_after_showings"] == check["unanswered_after_showings_ever"] == 2


def test_following_the_printed_call_answers_a_belief_question(tmp_path):
    """A belief question answered without a verdict keeps the words and stays
    open, and the flag with it. The call printed has to be one that settles it."""
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    _ask(db, "belief", memory, created=_ago(days=2), shown=3)

    _card_text, data = _card(db)
    [flag] = [flag for flag in data["watchdog"]["flags"] if flag["check"] == "questions"]
    command = flag["command"]

    # Follow it as printed: the id it names, and a verdict it lists.
    target = re.search(r'target_id="([^"]+)"', command).group(1)
    listed = re.search(r"verdict: (.+)$", command)
    verdicts = [v.strip() for v in re.split(r",| or ", listed.group(1))] if listed else []
    runtime = _runtime(db)
    try:
        runtime.reflect(
            target_id=target, text="A habit of this project, not a belief of mine.",
            verdict="decline" if "decline" in verdicts else "",
        )
    finally:
        runtime.close()

    _card_text, after = _card(db)
    assert after["watchdog"]["checks"]["questions"]["stalled"] is False, (
        f"following {command!r} left the question open"
    )
    assert command == (
        f'mnemos_reflect(target_id="{memory}", text="…", verdict="…"); '
        "verdict: hold, decline or not_now"
    )


def test_a_question_is_not_stalled_before_a_day_or_once_answered(tmp_path):
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    _ask(db, "impact", memory, created=_ago(hours=5), shown=3)
    _ask(db, "belief", memory, created=_ago(days=3), shown=3, answered=True)
    # Past its lifetime a question is gone, whatever happened to it.
    _ask(db, "contradiction", memory, created=_ago(days=40), shown=3, expires=_ago(days=10))

    card, data = _card(db)

    assert data["watchdog"]["checks"]["questions"]["stalled"] is False
    assert "never answered" not in card, card


# ── The report the briefing can find ──


def test_maintenance_the_briefing_never_reports_is_flagged(tmp_path, capsys):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM hypomnema_entries WHERE entry_kind = 'maintenance_report'")
    _write(db, "DELETE FROM consolidation_log")
    # Two days ago a cycle linked three memories, and no report of it exists
    # (`mnemos consolidate`, the scheduled job, logs cycles but writes none).
    _cycle(db, _ago(days=2), connection_discovery={"connections_created": 3})

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"Maintenance changed memory on {_day(days=2)}, but the briefing finds no "
        "report of it. Run: mnemos_context"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out


def test_a_missing_report_stays_flagged_after_a_week(tmp_path, capsys):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM hypomnema_entries WHERE entry_kind = 'maintenance_report'")
    _write(db, "DELETE FROM consolidation_log")
    # Eight days ago a cycle linked three memories, and no report of it exists.
    _cycle(db, _ago(days=8), connection_discovery={"connections_created": 3})

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"Maintenance changed memory on {_day(days=8)}, but the briefing finds no "
        "report of it. Run: mnemos_context"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out


def test_a_report_older_than_the_maintenance_it_missed_is_flagged(tmp_path):
    db = _healthy(tmp_path)
    runtime = _runtime(db)
    try:
        runtime.health()  # opens the store
        write_dream_entry(runtime._store, runtime.scope,
                          "Mnemos connected 2 memories that belong together.")
    finally:
        runtime.close()
    _write(db, "UPDATE hypomnema_entries SET created_at = ?, last_revised_at = ? "
               "WHERE entry_kind = 'maintenance_report'", (_ago(days=5), _ago(days=5)))
    _write(db, "DELETE FROM consolidation_log")
    _cycle(db, _ago(days=3), connection_discovery={"connections_created": 1})

    card, _data = _card(db)

    assert (
        f"Maintenance changed memory on {_day(days=3)}, but the briefing's newest "
        f"report is from {_day(days=5)}."
    ) in card, card


def test_a_report_written_after_the_maintenance_is_not_flagged(tmp_path):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM consolidation_log")
    _cycle(db, _ago(days=2), connection_discovery={"connections_created": 3})
    runtime = _runtime(db)
    try:
        runtime.health()  # opens the store
        write_dream_entry(runtime._store, runtime.scope,
                          "Mnemos connected 3 memories that belong together.")
    finally:
        runtime.close()

    _card_text, data = _card(db)

    assert data["watchdog"]["checks"]["report"]["stalled"] is False


# ── Maintenance that changes nothing, or never runs ──


def _stable(tmp_path: Path) -> tuple[Path, str]:
    """A store whose memories have sat still for days: written five days ago,
    each already asked what it taught (its question long since answered)."""
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "UPDATE engrams SET created_at = ?", (_ago(days=5),))
    _write(db, "DELETE FROM reflection_queue")
    _write(db, "DELETE FROM consolidation_log")
    _ask(db, "lesson", memory, created=_ago(days=4), shown=1, answered=True)
    return db, memory


def _idle(memory: str | None = None, *, processed: int = 2) -> dict:
    """What a cycle that changed nothing logs: it read the memories, found
    nothing to link, decay moved nothing past its floor, and softening named
    ``memory`` as waiting for its lesson question, when given."""
    return {
        "decay": {"engrams_processed": processed, "engrams_decayed": 0},
        "connection_discovery": {"engrams_processed": processed, "connections_created": 0},
        "softening": {"engrams_evaluated": processed, "engrams_softened": 0,
                      **({"awaiting_impact": [memory]} if memory else {})},
    }


def test_a_stable_store_whose_cycles_find_nothing_prints_nothing(tmp_path, capsys):
    db, memory = _stable(tmp_path)
    _cycle(db, _ago(hours=60), decay={"engrams_decayed": 2})
    for hours in (50, 26, 2):
        # Its fading memory was asked what it taught long ago: nothing is owed.
        _cycle(db, _ago(hours=hours), **_idle(memory))

    card, data = _card(db)
    out = _doctor(db, capsys)

    check = data["watchdog"]["checks"]["maintenance"]
    assert (check["cycles"], check["cycles_changed"], check["idle_cycles"]) == (4, 1, 3)
    assert check["stalled"] is False, check
    assert "ATTENTION" not in card, card
    assert "ATTENTION" not in out, out


def test_idle_cycles_that_leave_a_lesson_question_unasked_are_flagged(tmp_path, capsys):
    db, memory = _stable(tmp_path)
    _write(db, "DELETE FROM reflection_queue")  # its lesson question was never asked
    for hours in (40, 3):
        _cycle(db, _ago(hours=hours), **_idle(memory))

    card, data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"Maintenance has run twice since {_day(hours=40)} without changing anything; "
        "1 fading memory it found has never been asked what it taught. Run: mnemos_maintain"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out
    assert data["watchdog"]["checks"]["maintenance"]["idle_evidence"]["lessons_never_asked"] == [memory]


def test_idle_cycles_that_never_read_the_memories_are_flagged(tmp_path):
    db, _memory = _stable(tmp_path)
    for hours in (40, 3):
        _cycle(db, _ago(hours=hours), **_idle(processed=0))

    card, _data = _card(db)

    assert (
        f"Maintenance has run twice since {_day(hours=40)} without changing anything; "
        "it never read the 2 memories it keeps. Run: mnemos consolidate"
    ) in card, card


def test_idle_maintenance_names_the_passes_that_failed(tmp_path):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM consolidation_log")
    for hours in (40, 3):
        _cycle(db, _ago(hours=hours), decay_error="database is locked")

    card, _data = _card(db)

    assert (
        f"Maintenance has run twice since {_day(hours=40)} without changing anything; "
        "its log shows these passes failing: decay."
    ) in card, card


def test_captures_that_no_maintenance_followed_are_flagged(tmp_path, capsys):
    db = _healthy(tmp_path)
    # Older code records the agent's words and runs no maintenance: two
    # captures, two days ago, and no cycle since.
    _write(db, "DELETE FROM consolidation_log")
    _write(db, "UPDATE engrams SET created_at = ? WHERE json_extract(source, '$.type') = 'session'",
           (_ago(days=2),))

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"2 memories captured since {_day(days=2)} have had no maintenance cycle; "
        "no cycle is recorded. Run: mnemos consolidate"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out


def test_a_capture_just_after_the_last_cycle_counts_once_a_day_passes(tmp_path, capsys):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM consolidation_log")
    _cycle(db, _ago(hours=49), decay={"engrams_decayed": 2})
    # Captured half an hour after that cycle, when the activity gate can hold
    # a cycle back; then no cycle for two days.
    _write(db, "UPDATE engrams SET created_at = ? WHERE json_extract(source, '$.type') = 'session'",
           (_ago(hours=48, minutes=30),))

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"2 memories captured since {_day(hours=48, minutes=30)} have had no maintenance "
        f"cycle; the last ran {_day(hours=49)}. Run: mnemos consolidate"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out


def test_a_cycle_that_changed_something_today_is_not_flagged(tmp_path):
    db = _healthy(tmp_path)
    _write(db, "DELETE FROM consolidation_log")
    for hours in (50, 26):
        _cycle(db, _ago(hours=hours), decay={"engrams_decayed": 0})
    _cycle(db, _ago(hours=1), decay={"engrams_decayed": 4})

    _card_text, data = _card(db)

    assert data["watchdog"]["checks"]["maintenance"]["stalled"] is False


# ── Lesson questions waiting their turn ──


def test_lesson_questions_waiting_more_than_two_weeks_are_flagged(tmp_path, capsys):
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    # Shown once, then always behind other questions.
    _ask(db, "lesson", memory, created=_ago(days=15), shown=1)

    card, data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"1 lesson question has waited more than 14 days to be shown, the oldest "
        f'since {_day(days=15)}. Run: mnemos_reflect(target_id="{memory}", text="…")'
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out
    assert data["watchdog"]["checks"]["lesson_questions"]["waiting"] == 1


def test_a_lesson_question_younger_than_two_weeks_is_not_flagged(tmp_path):
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    _ask(db, "lesson", memory, created=_ago(days=13), shown=0)

    card, data = _card(db)

    assert data["watchdog"]["checks"]["lesson_questions"]["stalled"] is False
    assert "lesson question" not in card, card


# ── Sessions on older code ──


def _older_session_writes(db: Path, monkeypatch) -> None:
    """A current session leaves a handoff, then a newer Mnemos opens the store,
    and a session whose code is now older than the store leaves one too."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CURRENT_SESSION)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()
    _write(db, "UPDATE meta SET value = ? WHERE key = 'min_code_version'", (str(AHEAD),))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OLDER_SESSION)
    runtime = _runtime(db)
    try:
        runtime.handoff("Halfway through the migration; the backup is taken.")
    finally:
        runtime.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")


def test_sessions_writing_with_older_code_are_named_with_their_last_write(
    tmp_path, capsys, monkeypatch
):
    db = _healthy(tmp_path)
    _older_session_writes(db, monkeypatch)

    card, data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"1 session still writes with code older than the store (below version {AHEAD}) "
        f"and needs a restart: {OLDER_SESSION} last wrote just now. "
        "Run: claude --resume <session id>"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out
    sessions = data["watchdog"]["checks"]["older_code"]["sessions"]
    assert [item["session"] for item in sessions] == [OLDER_SESSION]
    assert sessions[0]["below_version"] == AHEAD
    assert sessions[0]["writes_without_trace"] == 1


def test_a_session_on_current_code_is_not_named(tmp_path, monkeypatch):
    db = _healthy(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CURRENT_SESSION)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
        runtime.capture("Riley reviews releases on Mondays.")
    finally:
        runtime.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    card, data = _card(db)

    assert data["watchdog"]["checks"]["older_code"]["sessions"] == []
    assert "older than the store" not in card, card


# ── Recall's meaning index ──


class _Vector(list):
    def tolist(self):
        return list(self)


class _WordModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        def vector(text: str) -> _Vector:
            return _Vector([float(len(text) % 7) + 1.0, 1.0, 0.5])
        if isinstance(texts, str):
            return vector(texts)
        return [vector(text) for text in texts]


class _WordEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _WordModel()
        return self._model


@pytest.fixture
def meaning(monkeypatch):
    """A working local backend for every index made while the test runs."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _WordEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


def _unindexed_memory(db: Path, content: str, *, written: str) -> str:
    """A memory recall can return that nothing has indexed, written ``written``."""
    store = EngramStore(str(db))
    try:
        engram = Engram(content=content, kind="semantic", owner_agent_id="nova",
                        person_id="riley", project_scope="demo")
        store.save_engram(engram)
    finally:
        store.close()
    _write(db, "UPDATE engrams SET created_at = ? WHERE id = ?", (written, engram.id))
    return engram.id


def test_what_waits_for_the_meaning_index_over_a_day_is_flagged(tmp_path, capsys, meaning):
    db = _healthy(tmp_path)
    _card_text, before = _card(db)
    assert before["watchdog"]["checks"]["recall_index"]["waiting"] == 0, (
        "the captures were not indexed as they were written"
    )
    _unindexed_memory(db, "The deploy key rotates on the first of the month.", written=_ago(days=2))

    card, data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"1 memory or note has waited more than a day for recall's meaning index, "
        f"the oldest since {_day(days=2)}. Run: mnemos embeddings index"
    )
    assert f"ATTENTION — {said}" in card, card
    assert said in out, out
    assert data["watchdog"]["checks"]["recall_index"]["waiting_over_a_day"] == 1


def test_the_index_flag_carries_the_last_pass_reason_and_says_it_once(tmp_path, capsys, meaning):
    db = _healthy(tmp_path)
    _unindexed_memory(db, "The deploy key rotates on the first of the month.", written=_ago(days=2))
    reason = (
        "the local model all-MiniLM-L6-v2 could not be loaded from this machine; "
        "run: mnemos embeddings download"
    )
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (
        recall_index_meta_key(**SCOPE),
        '{"at": "%s", "by": "mnemos consolidate", "items": 0, "passages": 0, '
        '"waiting": 1, "skipped": "%s"}' % (_ago(hours=3), reason),
    ))

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = f"the last pass could not embed anything: {reason}. Run: mnemos embeddings index"
    assert said in card, card
    assert said in out, out
    # R08's own line said the same thing; now it is said once.
    assert "Recall index:" not in card, card
    assert "Recall index:" not in out, out


def test_the_last_pass_line_stays_while_nothing_has_waited_a_day(tmp_path, capsys, meaning):
    db = _healthy(tmp_path)
    _unindexed_memory(db, "The deploy key rotates on the first of the month.", written=_ago(hours=2))
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (
        recall_index_meta_key(**SCOPE),
        '{"at": "%s", "by": "mnemos consolidate", "items": 0, "passages": 0, '
        '"waiting": 1, "skipped": "the model failed"}' % _ago(hours=1),
    ))

    card, data = _card(db)
    out = _doctor(db, capsys)

    assert data["watchdog"]["checks"]["recall_index"]["stalled"] is False
    assert "Recall index:  not updated (mnemos consolidate" in card, card
    assert "Recall index: not updated (mnemos consolidate" in out, out
    assert "meaning index," not in card


def test_a_note_rewritten_in_place_waits_from_its_rewrite(tmp_path, meaning):
    """A note rewritten in place drops its passages with its old words, so it
    waits for the index from the rewrite, not from when it was first written."""
    db = _healthy(tmp_path)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()
    handoff = _read(db, "SELECT id FROM hypomnema_entries WHERE entry_kind = 'handoff'")[0][0]
    _write(db, "UPDATE hypomnema_entries SET created_at = ? WHERE id = ?", (_ago(days=3), handoff))
    store = EngramStore(str(db))
    try:
        store.revise_hypomnema_entry(
            handoff, "Left off wiring the release checklist; the tests pass.",
            reason="corrected by its id", **SCOPE,
        )
    finally:
        store.close()
    _write(db, "UPDATE hypomnema_entries SET last_revised_at = ? WHERE id = ?", (_ago(hours=1), handoff))

    card, data = _card(db)

    check = data["watchdog"]["checks"]["recall_index"]
    assert (check["waiting"], check["waiting_over_a_day"], check["stalled"]) == (1, 0, False), check
    assert "meaning index" not in card, card


# ── Continuity lines: a stall, never the ordinary state between sessions ──


def _delivered(db: Path, at: str) -> None:
    """When a starting session was last handed continuity."""
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
           ("simple:nova:riley:demo:last_context_delivery_at", at))


def _sessions(db: Path, count: int) -> None:
    """How many sessions have used this memory."""
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
           ("simple:nova:riley:demo:session_counter", str(count)))


def _warnings(db: Path) -> list[str]:
    runtime = _runtime(db)
    try:
        return runtime.continuity_signals()["warnings"]
    finally:
        runtime.close()


def test_a_handoff_waiting_for_the_next_session_prints_nothing(tmp_path, capsys, monkeypatch):
    db = _healthy(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CURRENT_SESSION)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    card, data = _card(db)
    out = _doctor(db, capsys)

    # Between sessions a handoff waits: that is its ordinary state.
    assert data["continuity"]["warnings"] == [], data["continuity"]["warnings"]
    assert "ATTENTION" not in card, card
    assert "ATTENTION" not in out, out

    # The next session is handed it, and leaves its own for the one after.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OLDER_SESSION)
    runtime = _runtime(db)
    try:
        assert "Left off wiring the release checklist." in runtime.context()
        runtime.handoff("Finished the checklist; the release notes are next.")
    finally:
        runtime.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    card, data = _card(db)
    assert data["continuity"]["warnings"] == [], data["continuity"]["warnings"]
    assert "ATTENTION" not in card, card


def test_a_handoff_no_session_was_handed_for_a_day_is_flagged(tmp_path, capsys, monkeypatch):
    db = _healthy(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CURRENT_SESSION)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    _write(db, "UPDATE hypomnema_entries SET created_at = ? WHERE entry_kind = 'handoff'",
           (_ago(days=2),))
    # A session started a day ago and was handed continuity, but not the handoff.
    _delivered(db, _ago(days=1))

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"A handoff left on {_day(days=2)} has not reached any session, though "
        "sessions have started since. Run: mnemos_context"
    )
    assert f"  - {said}" in card, card
    assert f"ATTENTION:  {said}" in out, out


def test_a_handoff_no_session_has_come_for_is_not_flagged_however_old(tmp_path):
    db = _healthy(tmp_path)
    runtime = _runtime(db)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()
    _write(db, "UPDATE hypomnema_entries SET created_at = ? WHERE entry_kind = 'handoff'",
           (_ago(days=3),))
    _delivered(db, _ago(days=4))  # the last session started before it was left

    assert not any("handoff" in warning.lower() for warning in _warnings(db)), _warnings(db)


def test_a_new_store_in_its_first_session_prints_nothing(tmp_path, capsys):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
        runtime.handoff("Set up memory; nothing else yet.")
    finally:
        runtime.close()

    card, data = _card(db)
    out = _doctor(db, capsys)

    assert data["continuity"]["warnings"] == [], data["continuity"]["warnings"]
    assert "ATTENTION" not in card, card
    assert "ATTENTION" not in out, out


def test_continuity_no_starting_session_was_handed_is_flagged(tmp_path, capsys):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
    finally:
        runtime.close()
    _write(db, "UPDATE hypomnema_entries SET created_at = ?", (_ago(days=2),))
    _sessions(db, 3)  # sessions came and captured; none was handed anything

    card, _data = _card(db)
    out = _doctor(db, capsys)

    said = (
        f"No starting session has been handed the continuity kept here since "
        f"{_day(days=2)}, though 3 sessions have used this memory. "
        "Run: mnemos hooks install --write"
    )
    assert f"  - {said}" in card, card
    assert f"ATTENTION:  {said}" in out, out


def test_a_week_away_prints_nothing(tmp_path):
    db = _healthy(tmp_path)
    for table in ("engrams", "hypomnema_entries"):
        _write(db, f"UPDATE {table} SET created_at = ?", (_ago(days=9),))
    _delivered(db, _ago(days=8))

    assert _warnings(db) == []


def test_a_week_without_handing_continuity_while_memory_is_written_is_flagged(tmp_path):
    db = _healthy(tmp_path)  # memory written just now
    _delivered(db, _ago(days=8))

    assert (
        "No starting session has been handed continuity in 8 days, though memory "
        "has been written since. Run: mnemos hooks install --write"
    ) in _warnings(db)


def test_a_run_of_short_sessions_without_a_capture_prints_nothing(tmp_path):
    db = _healthy(tmp_path)  # captured today
    _sessions(db, 6)  # five sessions since, each with nothing durable to keep

    assert _warnings(db) == []


def test_sessions_without_a_capture_for_over_a_day_are_flagged(tmp_path):
    db = _healthy(tmp_path)
    for table in ("engrams", "hypomnema_entries"):
        _write(db, f"UPDATE {table} SET created_at = ?", (_ago(days=2),))
    _sessions(db, 6)

    assert (
        f"No capture has reached this scope in 5 sessions, since {_day(days=2)}: "
        "either nothing durable has come up, or captures are landing in another "
        "store or scope. Run: mnemos doctor"
    ) in _warnings(db)


# ── Hidden notes are not missing ones ──


def test_a_note_hidden_by_its_memory_fading_is_not_called_missing(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
        runtime.context()
    finally:
        runtime.close()
    _write(db, "DELETE FROM hypomnema_entries WHERE entry_kind = 'maintenance_report'")
    # Decay took the memory into quiet: its note stops reaching the briefing.
    _write(db, "UPDATE engrams SET state = 'dormant'")

    runtime = _runtime(db)
    try:
        signals = runtime.continuity_signals()
        data = runtime.health()
    finally:
        runtime.close()
    card = format_health_card(data)

    # Something was captured; its note is only hidden.
    assert "Nothing has been captured" not in card, card
    assert (
        "No note reaches the briefing: the only one is hidden, because the memory "
        "it belongs to went quiet or faded."
    ) in card, card
    assert signals["notes_active"] == 0
    assert signals["notes_hidden"] == 1
    assert data["counts"]["continuity_notes_hidden"] == 1
    assert data["watchdog"]["checks"]["notes"]["hidden"] == 1


def test_a_scope_whose_notes_were_all_forgotten_is_not_called_empty(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
        runtime.correct("", query="release notes docs releases", action="forget")
        _write(db, "DELETE FROM hypomnema_entries WHERE entry_kind = 'maintenance_report'")
        signals = runtime.continuity_signals()
    finally:
        runtime.close()

    assert signals["notes_active"] == 0
    assert not any("Nothing has been captured" in w for w in signals["warnings"]), signals
    assert any("though memory was captured to this scope" in w for w in signals["warnings"])


def test_an_empty_scope_still_says_nothing_was_captured(tmp_path):
    runtime = _runtime(tmp_path / "memory.db")
    try:
        runtime.context()
        signals = runtime.continuity_signals()
    finally:
        runtime.close()

    assert signals["notes_hidden"] == 0
    assert any("Nothing has been captured" in w for w in signals["warnings"])


# ── Reads only ──


def _stalled(tmp_path: Path, monkeypatch) -> Path:
    """A store where every flag the watchdog has is raised at once."""
    db = _healthy(tmp_path)
    memory = _memory_id(db)
    _write(db, "DELETE FROM reflection_queue")
    _ask(db, "impact", memory, created=_ago(days=3), shown=3)
    _ask(db, "lesson", memory, created=_ago(days=20), shown=1)
    _write(db, "DELETE FROM hypomnema_entries WHERE entry_kind = 'maintenance_report'")
    _write(db, "DELETE FROM consolidation_log")
    _cycle(db, _ago(days=2), connection_discovery={"connections_created": 3})
    _write(db, "UPDATE engrams SET created_at = ? WHERE json_extract(source, '$.type') = 'session'",
           (_ago(hours=36),))
    _older_session_writes(db, monkeypatch)
    return db


def test_a_check_that_cannot_run_says_so_and_fails_nothing(tmp_path, capsys, monkeypatch):
    import mnemos.watchdog as watchdog

    def unreadable(*_args):
        raise sqlite3.OperationalError("no such table: reflection_queue")

    monkeypatch.setattr(watchdog, "_CHECKS", tuple(
        (name, unreadable if name == "questions" else check)
        for name, check in watchdog._CHECKS
    ))
    db = _healthy(tmp_path)

    card, data = _card(db)
    out = _doctor(db, capsys)

    check = data["watchdog"]["checks"]["questions"]
    assert check["stalled"] is False
    assert check["seen"] == "not checked (OperationalError: no such table: reflection_queue)"
    # The read path fails silent: the other checks ran, and nothing is printed.
    assert data["watchdog"]["checks"]["maintenance"]["cycles"] >= 1
    assert "ATTENTION" not in card and "ATTENTION" not in out


def test_the_watchdog_runs_on_a_store_that_refuses_every_write(tmp_path, monkeypatch):
    from mnemos.watchdog import _rows, watch

    db = _stalled(tmp_path, monkeypatch)
    store = ReadOnlyEngramStore(db)
    try:
        watched = watch(store, **SCOPE)
        with pytest.raises(ValueError, match="only reads"):
            _rows(store._get_conn(), "UPDATE reflection_queue SET surfaced_count = 0")
    finally:
        store.close()

    assert {flag["check"] for flag in watched["flags"]} == {
        "questions", "report", "maintenance", "lesson_questions", "older_code",
    }, watched["flags"]
    assert not any("error" in check for check in watched["checks"].values()), watched["checks"]


def _raw(path: Path) -> tuple[str | None, str | None]:
    """The database file's bytes and its write-ahead log's, as they are on
    disk, with nothing folded in first."""
    def digest(p: Path) -> str | None:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return digest(path), digest(Path(f"{path}-wal"))


def test_health_and_doctor_leave_the_database_and_its_log_byte_identical(
    tmp_path, capsys, monkeypatch
):
    source = _stalled(tmp_path, monkeypatch)
    db = tmp_path / "copy.db"
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(str(db))
    try:
        reader.backup(copy)
    finally:
        copy.close()
        reader.close()

    # A server holds the store open, as the MCP server does; opening it is the
    # server's own write, done before anything is measured.
    server = _runtime(db)
    try:
        server.health()
        before = _raw(db)
        assert before[1] is not None, "the open store has no write-ahead log to compare"
        for _ in range(2):
            card = format_health_card(server.health())
            watched = server.watchdog()
        after_health = _raw(db)
        out = _doctor(db, capsys)
        after_doctor = _raw(db)
    finally:
        server.close()

    assert watched["flags"], "the fixture raised no flag, so nothing was exercised"
    assert "ATTENTION — " in card and "ATTENTION:" in out
    assert after_health == before, "health changed the store it was reporting on"
    assert after_doctor == before, "doctor changed the store it was checking"


def test_every_flag_is_one_sentence_and_a_command(tmp_path, monkeypatch):
    db = _stalled(tmp_path, monkeypatch)

    card, data = _card(db)

    lines = _flags(card)
    assert len(lines) == len(data["watchdog"]["flags"]) >= 5, card
    for line in lines:
        sentence, command = line[len("ATTENTION — "):].split(" Run: ")
        assert sentence.endswith(".") and sentence.count(". ") == 0, sentence
        assert command and "\n" not in command
        # The sentence is for a human; the command may carry an id.
        for word in ("engram", "hypomnema", "surfaced", "consolidation"):
            assert word not in sentence.lower(), (word, sentence)
