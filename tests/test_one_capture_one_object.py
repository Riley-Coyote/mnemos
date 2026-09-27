"""One capture, one object; corrections that land once.

A capture writes two things: the continuity note the briefing is built from,
and the memory the graph holds. Each correction path updated one and not the
other, and four ways a correction came apart were reproduced on copies of a
real store:

  * correcting by the note's id left the memory saying the old thing;
  * correcting by the memory's id left the note saying it in the briefing;
  * two corrections of one note left two live replacements;
  * correcting, then forgetting, left the corrected memory active.

And no correction wrote any history of what it replaced.

Now a capture saves its note and its memory in one transaction, the note
pointing at the memory, and every correction and forget reaches both from
whichever id or query names them, and acts on both at once. A correction
records what it replaced: a ``supersedes`` link and lineage both ways, the old
note's successor, and a version keeping the old words, signed by whoever
corrected. The old pair is kept, archived. A belief moves only when a
correction names it by its id. A note shares its memory's fate. Code older than
the store records the agent's words, retires what they name, and does nothing
else.

These tests read what was written on fresh connections, and compare against
literal text and SQL, so on code without the change they fail on behaviour,
not on a missing import.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
ORIGINAL = "Riley keeps the canoe paddles in the boathouse loft."
CORRECTED = "Riley keeps the canoe paddles in the garden shed."
AGAIN = "Riley keeps the canoe paddles in the car boot."
OTHER = "The choir rehearses on Tuesday evenings in the old hall."
QUERY = "canoe paddles"
OLD_WORDS = "boathouse loft"
NEW_WORDS = "garden shed"
MEANING = "Paddles live wherever the boat does."
LESSON = "Check where a thing is kept today before repeating where it was."
SESSION = "sess-r07-5b3d-4f0b"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
SONNET = "claude-sonnet-5"
OLDER = "This session runs older Mnemos code than the store expects. Restart the session."


# ── Helpers ──


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _write(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _captured(result: str) -> tuple[str, str]:
    """(memory id, note id) from a capture's result."""
    memory = re.search(r"Memory ID: (engram_\w+)", result)
    note = re.search(r"Continuity note ID: (\S+)", result)
    assert memory and note, result
    return memory.group(1), note.group(1)


def _live_memories(db, words: str = QUERY) -> list[dict]:
    return _rows(
        db,
        "SELECT * FROM engrams WHERE state != 'archived' AND content LIKE ? ORDER BY created_at",
        (f"%{words}%",),
    )


def _live_notes(db, words: str = QUERY) -> list[dict]:
    return _rows(
        db,
        "SELECT * FROM hypomnema_entries WHERE active = 1 AND content LIKE ? ORDER BY created_at",
        (f"%{words}%",),
    )


def _only_live_pair(db) -> tuple[dict, dict]:
    """The one live memory and the one live note holding the canoe paddles,
    each pointing at the other."""
    memories, notes = _live_memories(db), _live_notes(db)
    assert len(memories) == 1, f"{len(memories)} live memories: {[m['content'] for m in memories]}"
    assert len(notes) == 1, f"{len(notes)} live notes: {[n['content'] for n in notes]}"
    [memory], [note] = memories, notes
    assert note["related_engram_id"] == memory["id"] == note["graduated_to_engram_id"], (
        "the live note does not point at the live memory"
    )
    return memory, note


def _replacement(result: str) -> str:
    """The memory a correction by memory id wrote, as its result names it."""
    match = re.search(r"captured correction (engram_\w+)", result)
    assert match, result
    return match.group(1)


def _state(db, engram_id: str) -> str:
    [row] = _rows(db, "SELECT state FROM engrams WHERE id = ?", (engram_id,))
    return row["state"]


def _lineage(db, engram_id: str) -> dict:
    [row] = _rows(db, "SELECT lineage FROM engrams WHERE id = ?", (engram_id,))
    return json.loads(row["lineage"] or "{}")


def _claim_for_newer_code(db, version: int = 999) -> None:
    """What a newer server's startup does to a store: raise its minimum."""
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)", (str(version),))


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _decay(db, engram_id: str, *, accessibility: float, hours: float) -> str:
    """Leave a memory untouched for ``hours`` at ``accessibility``, then run
    the decay pass, and return the state decay leaves it in."""
    _write(
        db,
        "UPDATE engrams SET accessibility = ?, stability = 0.0, last_accessed = ? WHERE id = ?",
        (accessibility, _ago(hours), engram_id),
    )
    from mnemos.consolidation.decay import run_decay_pass

    store = EngramStore(str(db))
    try:
        run_decay_pass(store, {}, **SCOPE)
    finally:
        store.close()
    return _state(db, engram_id)


def _in_session(monkeypatch, tmp_path: Path) -> None:
    """A Claude Code session with no transcript to read: every signature
    comes from signed_as."""
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


# ── 1. One capture, one object ──


def test_a_capture_is_one_pair_the_note_pointing_at_its_memory(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
    finally:
        runtime.close()

    memory, note = _only_live_pair(db)
    assert (memory["id"], note["id"]) == (memory_id, note_id)
    assert note["content"] == ORIGINAL and memory["content"] == ORIGINAL


def test_a_capture_lands_whole_or_not_at_all(tmp_path, monkeypatch):
    """The memory was committed on its own before the note was written, so a
    failure between them left a memory no note pointed at."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        store = runtime._store

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store, "write_hypomnema_entry", broken)
        with pytest.raises(sqlite3.OperationalError):
            runtime.capture(ORIGINAL)
        assert store._transaction_depth == 0
        monkeypatch.undo()

        assert _rows(db, "SELECT id FROM engrams WHERE content LIKE ?", (f"%{QUERY}%",)) == [], (
            "a capture that failed halfway left its memory behind"
        )
        runtime.capture(ORIGINAL)
    finally:
        runtime.close()
    _only_live_pair(db)


def test_a_captures_vector_is_written_once_its_pair_has_landed(tmp_path, monkeypatch):
    """The vector is written through another connection, which cannot write
    while the pair's transaction holds the lock: inside it, the write would
    wait five seconds, fail, and be swallowed. (Holds before and after pairs:
    it guards the order in which a pair and its vector are written.)"""
    _turn_semantic_recall_on(monkeypatch)
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, _note_id = _captured(runtime.capture(ORIGINAL))
        corrected = runtime.correct(CORRECTED, target_id=memory_id)
    finally:
        runtime.close()
    replacement = re.search(r"captured correction (engram_\w+)", corrected).group(1)
    for engram_id in (memory_id, replacement):
        assert _rows(db, "SELECT COUNT(*) AS n FROM embeddings WHERE engram_id = ?", (engram_id,)) == [
            {"n": 1}
        ], f"{engram_id} was stored without its vector"


# ── 2. The four ways a correction came apart ──


def test_correcting_by_the_notes_id_corrects_its_memory(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        result = runtime.correct(CORRECTED, target_id=note_id)
        recalled = runtime.recall("canoe paddles kept")
    finally:
        runtime.close()

    assert result.startswith(f"Updated continuity note {note_id}."), result
    assert _live_memories(db, OLD_WORDS) == [], "the note was corrected and its memory still says the old thing"
    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == CORRECTED
    assert _state(db, memory_id) == "archived"
    assert NEW_WORDS in recalled and OLD_WORDS not in recalled, recalled


def test_correcting_by_the_memorys_id_corrects_its_note_in_the_briefing(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        runtime.capture(OTHER)
        result = runtime.correct(CORRECTED, target_id=memory_id)
    finally:
        runtime.close()
    reader = _runtime(db)
    try:
        briefing = reader.context()
    finally:
        reader.close()

    assert "captured correction" in result, result
    assert OLD_WORDS not in briefing, f"the briefing still shows the corrected note:\n{briefing}"
    assert NEW_WORDS in briefing, briefing
    assert _live_notes(db, OLD_WORDS) == []
    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == CORRECTED
    assert memory["id"] in result and note["id"] in result


@pytest.mark.parametrize("path", ["query", "the same note id", "the same memory id"])
def test_two_corrections_leave_one_live_version(tmp_path, path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        for words in (CORRECTED, AGAIN):
            if path == "query":
                runtime.correct(words, query=QUERY)
            elif path == "the same note id":
                runtime.correct(words, target_id=note_id)
            else:
                runtime.correct(words, target_id=memory_id)
    finally:
        runtime.close()

    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == AGAIN


@pytest.mark.parametrize("by", ["query", "the old note id", "the old memory id", "the new memory id"])
def test_correcting_then_forgetting_leaves_nothing_live(tmp_path, by):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        corrected = runtime.correct(CORRECTED, query=QUERY)
        new_memory = re.search(r"Memory ID: (engram_\w+)", corrected).group(1)
        target = {
            "query": "",
            "the old note id": note_id,
            "the old memory id": memory_id,
            "the new memory id": new_memory,
        }[by]
        forgotten = runtime.correct("", target_id=target, query=QUERY if by == "query" else "", action="forget")
        recalled = runtime.recall("canoe paddles kept")
        briefing = runtime.context()
    finally:
        runtime.close()

    assert "rchived" in forgotten, forgotten
    assert _live_memories(db) == [], "the corrected memory stayed live after a forget"
    assert _live_notes(db) == []
    assert NEW_WORDS not in recalled and NEW_WORDS not in briefing
    # Forgetting archives; nothing is deleted.
    assert len(_rows(db, "SELECT id FROM engrams WHERE content LIKE ?", (f"%{QUERY}%",))) == 2


# ── 3. A correction records what it replaced ──


def test_a_correction_records_what_it_replaced(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        replacement = _replacement(runtime.correct(CORRECTED, target_id=memory_id))
    finally:
        runtime.close()

    assert _rows(
        db, "SELECT relation, formed_by FROM connections WHERE source_id = ? AND target_id = ?",
        (replacement, memory_id),
    ) == [{"relation": "supersedes", "formed_by": "correction"}]
    assert _lineage(db, replacement).get("supersedes") == [memory_id]
    assert _lineage(db, memory_id).get("superseded_by") == replacement
    assert _rows(
        db, "SELECT content_snapshot, change_reason FROM versions WHERE engram_id = ?", (replacement,),
    ) == [{"content_snapshot": ORIGINAL, "change_reason": "correction"}]
    memory, note = _only_live_pair(db)
    assert memory["id"] == replacement
    [old_note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note_id,))
    assert old_note["superseded_by"] == note["id"] and old_note["active"] == 0
    # The old pair stays, archived: its words are kept, never deleted.
    [old] = _rows(db, "SELECT content, state FROM engrams WHERE id = ?", (memory_id,))
    assert old == {"content": ORIGINAL, "state": "archived"}
    assert old_note["content"] == ORIGINAL


def test_a_correction_that_keeps_the_words_writes_no_version(tmp_path):
    """A version is written only when the words change: a correction that
    gives a memory a new meaning in the same words replaces the pair and
    records what it replaced, but has no old words to keep."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, _note_id = _captured(runtime.capture(ORIGINAL, impact=MEANING))
        replacement = _replacement(runtime.correct(ORIGINAL, target_id=memory_id, impact=LESSON))
    finally:
        runtime.close()

    assert _rows(
        db, "SELECT relation FROM connections WHERE source_id = ? AND target_id = ?",
        (replacement, memory_id),
    ) == [{"relation": "supersedes"}]
    assert _rows(db, "SELECT * FROM versions WHERE engram_id = ?", (replacement,)) == []
    memory, _note = _only_live_pair(db)
    assert memory["id"] == replacement and memory["impact"] == LESSON


def test_a_replaced_id_names_its_current_version_and_never_its_old_words(tmp_path):
    """The old pair stays reachable by its ids, as a trace: reading one says
    what replaced it. What a correction replaced stays out of recall."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        replacement = _replacement(runtime.correct(CORRECTED, target_id=memory_id))
        by_old_memory = runtime.recall(memory_id)
        by_old_note = runtime.recall(note_id)
        by_new_memory = runtime.recall(replacement)
    finally:
        runtime.close()

    assert (
        f"Memory {memory_id} was replaced by a correction. Its current version is "
        f"memory {replacement}"
    ) in by_old_memory, by_old_memory
    memory, note = _only_live_pair(db)
    assert (
        f"Note {note_id} was replaced by a correction. Its current version is note {note['id']}"
    ) in by_old_note, by_old_note
    for said in (by_old_memory, by_old_note):
        assert OLD_WORDS not in said
    assert CORRECTED in by_new_memory and f"It replaced memory {memory_id}" in by_new_memory
    assert _state(db, memory_id) == "archived"


def test_a_correction_is_signed_by_whoever_made_it(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path)
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL, signed_as=FABLE))
        replacement = _replacement(runtime.correct(CORRECTED, target_id=memory_id, signed_as=OPUS))
        signed = _rows(
            db, "SELECT author_model, author_session FROM versions WHERE engram_id = ?", (replacement,),
        )
        memory, note = _only_live_pair(db)
        runtime.correct("", target_id=note["id"], action="forget", signed_as=SONNET)
    finally:
        runtime.close()

    # The version entry keeping the old words is signed like the words.
    assert signed == [{"author_model": OPUS, "author_session": SESSION}]
    assert (memory["author_kind"], memory["author_model"], memory["author_session"]) == (
        "agent", OPUS, SESSION,
    )
    assert (note["authored_by"], note["author_model"], note["author_session"]) == (
        "agent", OPUS, SESSION,
    )
    # The retired note keeps its own words and signature, and says who retired it.
    [old_note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note_id,))
    assert old_note["author_model"] == FABLE
    retired = json.loads(old_note["revisions_json"])[-1]
    assert (retired["revised_by"], retired["revised_by_session"]) == (OPUS, SESSION)
    [forgotten] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note["id"],))
    forgot = json.loads(forgotten["revisions_json"])[-1]
    assert (forgot["revised_by"], forgot["revised_by_session"]) == (SONNET, SESSION)
    assert forgotten["active"] == 0 and _state(db, memory["id"]) == "archived"


# ── 4. What the agent means by a correction becomes a lesson ──


def test_an_impact_given_with_a_correction_becomes_a_lesson(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path)
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, _note_id = _captured(runtime.capture(ORIGINAL))
        result = runtime.correct(CORRECTED, target_id=memory_id, impact=LESSON, signed_as=OPUS)
        replacement = _replacement(result)
        # Without an impact of its own, a correction draws no lesson.
        runtime.correct(AGAIN, target_id=replacement)
    finally:
        runtime.close()

    lessons = _rows(db, "SELECT * FROM engrams WHERE tags LIKE '%\"lesson\"%'")
    assert [(l["content"], l["author_kind"], l["author_model"]) for l in lessons] == [
        (LESSON, "agent", OPUS)
    ], lessons
    assert lessons[0]["id"] in result, result
    assert _rows(
        db, "SELECT relation FROM connections WHERE source_id = ? AND target_id = ?",
        (replacement, lessons[0]["id"]),
    ) == [{"relation": "distilled_into"}]


def test_a_correction_by_note_id_writes_no_placeholder_meaning(tmp_path):
    """No meaning to carry and none given: the replacement memory's meaning
    stays empty. A correction by note id wrote no memory before, so it adds
    no path that writes the server's words."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        _memory_id, note_id = _captured(runtime.capture(ORIGINAL))
        runtime.correct(CORRECTED, target_id=note_id)
    finally:
        runtime.close()

    memory, _note = _only_live_pair(db)
    assert (memory["content"], memory["impact"], memory["impact_source"]) == (CORRECTED, "", "")


# ── 5. A belief moves only when a correction names it ──


def test_words_alone_never_move_a_belief(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        held = Belief(agent_id="nova", content="Riley prefers dark roast coffee",
                      confidence=0.7, source="agent")
        runtime._store.save_belief(held)
        runtime.correct("", query="dark roast coffee", action="forget")
        runtime.correct("Less sure Riley prefers dark roast coffee.", query="Riley prefers dark roast coffee")
        after = runtime._store.get_belief(held.id)
        assert (after.superseded_by, after.confidence) == (None, 0.7), (
            "a correction moved a belief by the words it shared with it"
        )

        retired = runtime.correct("", target_id=held.id, action="forget")
        assert "Retired the belief" in retired, retired
        assert runtime._store.get_belief(held.id).superseded_by
    finally:
        runtime.close()


def test_a_belief_corrected_by_its_id_holds_the_agents_words(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        held = Belief(agent_id="nova", content="Riley works best at night",
                      confidence=0.8, domain="work", source="agent")
        seed = Belief(agent_id="nova", content="Riley likes tea", confidence=0.6, source="seed")
        runtime._store.save_belief(held)
        runtime._store.save_belief(seed)
        result = runtime.correct("Riley works best in the early morning.", target_id=held.id)
        untouched = runtime.correct("", target_id=seed.id, action="forget")
        beliefs = runtime._store.get_beliefs("nova", active_only=True)
        old = runtime._store.get_belief(held.id)
    finally:
        runtime.close()

    [new] = [belief for belief in beliefs if belief.id != seed.id]
    assert (new.content, new.source, new.confidence, new.domain) == (
        "Riley works best in the early morning.", "agent", 0.4, "work",
    )
    assert old.superseded_by == new.id and old.confidence == 0.0
    assert "Riley works best in the early morning." in old.revision_history[-1].reason
    assert new.id in result, result
    # A belief the agent did not state is not the agent's to rewrite or retire.
    assert "not one you stated" in untouched and seed.id in {belief.id for belief in beliefs}


# ── 6. A note shares its memory's fate ──


def test_a_note_shares_its_memorys_fate(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        # With their meaning given, no question about either is waiting: the
        # briefing shows the memory's words only through its note.
        memory_id, note_id = _captured(runtime.capture(ORIGINAL, impact=MEANING))
        runtime.capture(OTHER, impact="Tuesdays belong to the choir.")
    finally:
        runtime.close()

    def briefing() -> str:
        reader = _runtime(db)
        try:
            return reader.context()
        finally:
            reader.close()

    def notes_counted() -> int:
        reader = _runtime(db)
        try:
            return reader.health()["counts"]["continuity_notes_active"]
        finally:
            reader.close()

    assert OLD_WORDS in briefing() and notes_counted() == 2, "premise: both notes show"

    assert _decay(db, memory_id, accessibility=0.06, hours=100) == "dormant"
    quiet = briefing()
    assert OLD_WORDS not in quiet, f"a note showed over a memory that went quiet:\n{quiet}"
    assert "choir rehearses" in quiet and notes_counted() == 1

    runtime = _runtime(db)
    try:
        runtime.recall(memory_id)  # the memory wakes
    finally:
        runtime.close()
    assert _state(db, memory_id) == "active"
    assert OLD_WORDS in briefing(), "the note did not come back when its memory woke"

    assert _decay(db, memory_id, accessibility=0.03, hours=720) == "archived"
    assert OLD_WORDS not in briefing(), "a note showed over a memory that faded into the archive"

    runtime = _runtime(db)
    try:
        by_note = runtime.recall(note_id)  # the note's id reaches its memory
    finally:
        runtime.close()
    assert ORIGINAL in by_note and "recalling the note brought it back" in by_note, by_note
    assert _state(db, memory_id) == "active"
    assert OLD_WORDS in briefing() and notes_counted() == 2


def test_older_code_shows_a_quiet_note_by_its_id_but_wakes_nothing(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
    finally:
        runtime.close()
    assert _decay(db, memory_id, accessibility=0.06, hours=100) == "dormant"
    _claim_for_newer_code(db)

    runtime = _runtime(db)
    try:
        by_note = runtime.recall(note_id)
    finally:
        runtime.close()
    assert ORIGINAL in by_note
    assert "Its memory has gone quiet, and this session's code leaves it there." in by_note, by_note
    assert _state(db, memory_id) == "dormant"


# ── 7. Code older than the store records the words and nothing else ──


def test_older_code_records_the_correction_and_nothing_else(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL, impact=MEANING))
        runtime.capture("The canoe club meets at the boathouse on Saturdays.")
        runtime._ensure_init()
        held = Belief(agent_id="nova", content="Riley paddles every weekend",
                      confidence=0.6, source="agent")
        runtime._store.save_belief(held)
    finally:
        runtime.close()
    _claim_for_newer_code(db)
    links_before = _rows(db, "SELECT * FROM connections ORDER BY source_id, target_id, relation")

    runtime = _runtime(db)
    try:
        by_note = runtime.correct(CORRECTED, target_id=note_id, impact=LESSON)
        belief = runtime.correct("", target_id=held.id, action="forget")
    finally:
        runtime.close()

    for said in (by_note, belief):
        assert said.endswith(OLDER), said
    # The agent's words land, as one pair, and what they name is retired.
    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == CORRECTED
    assert (memory["impact"], memory["impact_source"], memory["author_kind"]) == (LESSON, "agent", "agent")
    assert _state(db, memory_id) == "archived"
    [old_note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note_id,))
    assert old_note["active"] == 0
    assert "Filing it as a lesson waits for current Mnemos." in by_note
    # Nothing else: no links, lineage, versions, lesson or belief change.
    assert _rows(db, "SELECT * FROM connections ORDER BY source_id, target_id, relation") == links_before
    assert "supersedes" not in _lineage(db, memory["id"]) or not _lineage(db, memory["id"])["supersedes"]
    assert not _lineage(db, memory_id).get("superseded_by")
    assert old_note["superseded_by"] is None
    assert _rows(db, "SELECT * FROM versions WHERE engram_id = ?", (memory["id"],)) == []
    assert _rows(db, "SELECT id FROM engrams WHERE tags LIKE '%\"lesson\"%'") == []
    [after] = _rows(db, "SELECT confidence, superseded_by FROM beliefs WHERE id = ?", (held.id,))
    assert after == {"confidence": 0.6, "superseded_by": None}
    assert "left as it is" in belief


# ── 8. Across processes ──


_CAPTURE = """
import sys
from mnemos.simple_runtime import MnemosRuntime

runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    if sys.argv[2] == "capture":
        print(runtime.capture(sys.argv[3]))
    else:
        print(runtime.correct(sys.argv[3], target_id=sys.argv[4]))
finally:
    runtime.close()
"""


def _in_another_process(home: Path, *args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", _CAPTURE, *args],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_a_correction_made_in_one_process_is_one_pair_in_another(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "memory.db"

    memory_id, note_id = _captured(_in_another_process(home, str(db), "capture", ORIGINAL))
    corrected = _in_another_process(home, str(db), "correct", CORRECTED, note_id)

    assert f"Updated continuity note {note_id}." in corrected, corrected
    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == CORRECTED
    assert _state(db, memory_id) == "archived"
    assert _lineage(db, memory_id).get("superseded_by") == memory["id"]
    reader = _runtime(db)
    try:
        assert NEW_WORDS in reader.context() and OLD_WORDS not in reader.context()
    finally:
        reader.close()


# ── 9. A note that only references a memory is never its pair ──
#
# The advanced tools write notes that name a memory they interpret or
# summarise (``related_engram_id``). One capture writing a note and a memory
# together is what makes them a pair (``graduated_to_engram_id``); a reference
# never does. Such a note is corrected and forgotten on its own, and does not
# share the fate of the memory it references.

SUMMARY = "Paddling thread, where it stands: the gear question is settled for the season."
SUMMARY_NOW = "Paddling thread, where it stands: new paddles are on order."


def _summary_of(db, memory_id: str, **fields) -> str:
    """A note that only references a memory, written as mnemos_hypomnema_write
    writes one."""
    store = EngramStore(str(db))
    try:
        return store.write_hypomnema_entry(
            SUMMARY,
            source="synthesized",
            agent_id=SCOPE["agent_id"],
            person_id=SCOPE["person_id"],
            project_scope=SCOPE["project_scope"],
            related_engram_id=memory_id,
            **fields,
        )
    finally:
        store.close()


def _note(db, note_id: str) -> dict:
    [row] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note_id,))
    return row


def test_correcting_a_summary_note_by_its_id_leaves_the_memory_it_references(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
    finally:
        runtime.close()
    summary_id = _summary_of(db, memory_id)

    runtime = _runtime(db)
    try:
        result = runtime.correct(SUMMARY_NOW, target_id=summary_id)
    finally:
        runtime.close()

    assert result.startswith(f"Updated continuity note {summary_id}."), result
    assert _state(db, memory_id) == "active", "correcting a summary retired the memory it references"
    memory, note = _only_live_pair(db)
    assert (memory["id"], note["id"]) == (memory_id, note_id), "the capture's own pair came apart"
    old = _note(db, summary_id)
    new = _note(db, old["superseded_by"])
    assert old["active"] == 0 and (new["active"], new["content"]) == (1, SUMMARY_NOW)
    # The corrected summary still references what it summarises.
    assert new["related_engram_id"] == memory_id


def test_forgetting_a_summary_note_by_its_id_leaves_the_memory_it_references(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
    finally:
        runtime.close()
    summary_id = _summary_of(db, memory_id)

    runtime = _runtime(db)
    try:
        result = runtime.correct("", target_id=summary_id, action="forget")
    finally:
        runtime.close()

    assert _state(db, memory_id) == "active", "forgetting a summary retired the memory it references"
    assert result == f"Archived continuity note {summary_id}.", result
    memory, note = _only_live_pair(db)
    assert (memory["id"], note["id"]) == (memory_id, note_id)
    assert _note(db, summary_id)["active"] == 0


def test_a_summary_note_does_not_share_the_fate_of_the_memory_it_references(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        # With their meaning given, no question about either is waiting.
        memory_id, _note_id = _captured(runtime.capture(ORIGINAL, impact=MEANING))
        runtime.capture(OTHER, impact="Tuesdays belong to the choir.")
    finally:
        runtime.close()
    _summary_of(db, memory_id)
    assert _decay(db, memory_id, accessibility=0.06, hours=100) == "dormant"

    reader = _runtime(db)
    try:
        briefing = reader.context()
        counted = reader.health()["counts"]["continuity_notes_active"]
    finally:
        reader.close()

    assert SUMMARY in briefing, f"a summary hid when the memory it references went quiet:\n{briefing}"
    # The memory's own note goes quiet with it; the summary and the choir stay.
    assert OLD_WORDS not in briefing and "choir rehearses" in briefing
    assert counted == 2


def test_an_older_capture_linked_only_one_way_is_still_one_pair_after_migration(tmp_path):
    """A store from before the pair was recorded: the capture's note names its
    memory only as related_engram_id. Opening it with this code pairs them,
    because the note's words are the memory's words as a capture writes them
    (the memory adds the capture's context; the note has since gained a
    reflection). A summary that references the same memory is left alone."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL, context="At the lake house."))
    finally:
        runtime.close()
    summary_id = _summary_of(db, memory_id)
    _write(
        db,
        "UPDATE hypomnema_entries SET graduated_to_engram_id = NULL, content = ? WHERE id = ?",
        (f"{ORIGINAL}\n\nWhat this changed: (Opus 5.5) Paddles live with the boat.", note_id),
    )
    _write(db, "DELETE FROM meta WHERE key = 'capture_pairs_linked'")
    _write(db, "UPDATE meta SET value = '12' WHERE key = 'schema_version'")
    assert [row["content"] for row in _rows(db, "SELECT content FROM engrams WHERE id = ?", (memory_id,))] == [
        f"{ORIGINAL}\n\nContext: At the lake house."
    ], "premise: the memory holds the capture's context"

    EngramStore(str(db)).close()  # the first open by this code migrates it

    assert _note(db, note_id)["graduated_to_engram_id"] == memory_id, "the older capture was not paired"
    assert _note(db, summary_id)["graduated_to_engram_id"] is None, "a summary was paired with what it references"
    runtime = _runtime(db)
    try:
        runtime.correct(CORRECTED, target_id=note_id)
    finally:
        runtime.close()
    assert _live_memories(db, OLD_WORDS) == [], "correcting the older capture's note left its memory saying the old thing"
    memory, note = _only_live_pair(db)
    assert memory["content"] == note["content"] == CORRECTED


def test_a_summary_note_is_never_promoted_into_the_memory_it_references(tmp_path):
    """Promotion marked a note that references a memory as graduated into
    it, pairing the two."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(ORIGINAL))
    finally:
        runtime.close()
    summary_id = _summary_of(db, memory_id, confidence=0.9, salience=0.8, foundational=True)

    runtime = _runtime(db)
    try:
        runtime.maintain()
    finally:
        runtime.close()

    assert _note(db, summary_id)["graduated_to_engram_id"] is None, (
        "promotion paired a summary with the memory it references"
    )
    assert [row["id"] for row in _rows(
        db, "SELECT id FROM hypomnema_entries WHERE graduated_to_engram_id = ?", (memory_id,),
    )] == [note_id]


# ── A working local embedding backend, without torch (as the version tests use) ──


class _Vector(list):
    def tolist(self):
        return list(self)


class _Model:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector([1.0, 0.0, 0.5])
        return [_Vector([1.0, 0.0, 0.5]) for _ in texts]


def _turn_semantic_recall_on(monkeypatch) -> None:
    from mnemos.store import embedding_index as ei

    class Embedder(ei._LocalEmbedder):
        def _get_model(self):
            if self._model is None:
                self._model = _Model()
            return self._model

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", Embedder)
