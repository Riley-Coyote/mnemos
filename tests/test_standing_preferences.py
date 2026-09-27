"""Standing preferences, marked by the agent (WP-R04c).

A standing rule is obeyed, not recalled. On a copy of a real store, nothing
marked the human's standing preferences: the ``preference`` tag and the
``foundational`` domain are substring labels ("prefer", "always"), and the
plain-words rule (captured 2026-08-26, tagged "continuity" only) had never been
reinforced, corrected or versioned, so it never reached the briefing and decay
was fading it for disuse. Every usage signal works against a rule that is
followed, and guessing from words would put guesses in the one block every
session reads.

So the agent marks a standing preference itself, as a typed choice: when it
captures it (``standing=true``), or later by the memory's id
(``mnemos_correct`` with ``mark_standing`` / ``unmark_standing``, which change
no words and write no version). The mark is signed: who, in which session,
when. "Who you're with" opens with the newest marks, one line each in the
memory's own words, then says how to list the rest (``mnemos_recall`` with
``standing=true``). A marked memory is exempt from decay until it is unmarked.
Code older than the store records the words and ignores the flag.

These tests read what was written on fresh connections and compare against
literal text and SQL, so on code without the change they fail on behaviour,
not on a missing import. The briefing is read from the real SessionStart hook,
in another process from the one that captured.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import SCHEMA_VERSION, SQL_CREATE_TABLES, EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
SESSION = "sess-r04c-7c1e-4d2a"
READER = "22222222-bbbb-4bbb-8bbb-222222222222"
OLDER = "This session runs older Mnemos code than the store expects. Restart the session."

# What the briefing and the tools say, as literal text.
LABEL = "Standing, how the human wants you to work in every session:"
LIST_CALL = 'mnemos_recall(query="", standing=true)'
DESCRIPTION = "True when this is how the human wants you to work in every session, not just now"

# The plain-words rule, shaped like the real one: several sentences, longer
# than one briefing line.
PLAIN = (
    "HARD RULE from Riley (2026-08-26, after having to ask again): always answer in "
    "plain words anyone would understand. No jargon, code names, branch names or "
    "technical structure unless he asks. Verbose or highly technical only when he "
    "explicitly asks for it. The reply he pointed to as the model was four short "
    "lines and a yes-or-no question at the end. Every reply to him should sound "
    "like that one."
)
RULES = (
    "Riley wants the plan posted before anything is built.",
    "Riley wants one direction committed to, always, not three safe options.",
    "Riley wants to see the result running before he reads any code.",
    "Riley wants every claim checked against the real thing first.",
    "Riley wants the workspace kept tidy, with no stray files.",
    "Riley wants disagreement said plainly, with the reason.",
    PLAIN,
)
RULE_CONTEXT = "Said during the lab review."
CANOE = "Riley keeps the canoe paddles in the boathouse loft."
CANOE_RULE = "Riley wants the canoe paddles put back in the loft after every trip."
CORRECTED = "Riley wants the canoe paddles put back in the garden shed after every trip."
FOUNDATION = "Riley always prefers the ferry to the bridge."
OTHER = "The choir rehearses on Tuesday evenings in the old hall."
QUIET_RULE = "Riley wants dates written as 2026-09-27, never as 27/9."
# A capture's words that say "always", "rule", "prefers", "every session":
# words never mark anything.
SOUNDS_STANDING = (
    "HARD RULE: Riley always prefers short replies, in every session, as a "
    "standing preference."
)


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


def _engram(db, engram_id: str) -> dict:
    [row] = _rows(db, "SELECT * FROM engrams WHERE id = ?", (engram_id,))
    return row


def _mark(db, engram_id: str) -> tuple:
    """A memory's mark as stored: (standing, by, session, at)."""
    row = _engram(db, engram_id)
    return (
        row.get("standing"), row.get("standing_by"),
        row.get("standing_session"), row.get("standing_at"),
    )


def _standing_ids(db) -> set[str]:
    columns = {row["name"] for row in _rows(db, "PRAGMA table_info(engrams)")}
    if "standing" not in columns:
        return set()
    return {row["id"] for row in _rows(db, "SELECT id FROM engrams WHERE standing = 1")}


def _captured(result: str) -> tuple[str, str]:
    """(memory id, note id) from a capture's result."""
    memory = re.search(r"Memory ID: (engram_\w+)", result)
    note = re.search(r"Continuity note ID: (\S+)", result)
    assert memory and note, result
    return memory.group(1), note.group(1)


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _claim_for_newer_code(db, version: int = 999) -> None:
    """What a newer server's startup does to a store: raise its minimum."""
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)", (str(version),))


def _in_session(monkeypatch, tmp_path: Path) -> None:
    """A Claude Code session with no transcript to read: every signature
    comes from signed_as."""
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def _home(folder: Path) -> Path:
    home = folder / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


def _env(folder: Path, session: str = "") -> dict[str, str]:
    env = {
        "HOME": str(_home(folder)),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "MNEMOS_DISABLE_DOTENV": "1",
        "PYTHONPATH": ":".join(sys.path),
    }
    if session:
        # A Claude Code session with no transcript to read.
        config = folder / "claude-config"
        config.mkdir(exist_ok=True)
        env.update({"CLAUDE_CODE_SESSION_ID": session, "CLAUDE_CONFIG_DIR": str(config)})
    return env


_CAPTURE = """
import json, sys
from mnemos.simple_runtime import MnemosRuntime

runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    for item in json.loads(sys.argv[2]):
        options = {key: item[key] for key in ("context", "signed_as", "standing") if key in item}
        print(runtime.capture(item["content"], **options))
finally:
    runtime.close()
"""


def _capture_elsewhere(
    db: Path, folder: Path, items: list[dict], session: str = "",
) -> tuple[list[str], str]:
    """Capture in another process, as another session's server would: the
    memory ids in order, and what the captures said."""
    done = subprocess.run(
        [sys.executable, "-c", _CAPTURE, str(db), json.dumps(items)],
        capture_output=True, text=True, timeout=180, env=_env(folder, session),
    )
    assert done.returncode == 0, done.stderr
    ids = re.findall(r"Memory ID: (engram_\w+)", done.stdout)
    assert len(ids) == len(items), done.stdout
    return ids, done.stdout


_MARK_COLUMNS = (
    ("standing", "standing INTEGER NOT NULL DEFAULT 0"),
    ("standing_by", "standing_by TEXT NOT NULL DEFAULT ''"),
    ("standing_session", "standing_session TEXT NOT NULL DEFAULT ''"),
    ("standing_at", "standing_at TEXT"),
)


def _mark_by_hand(
    db, engram_id: str, *, hours_ago: float = 0.0, by: str = OPUS, session: str = "",
) -> tuple:
    """Mark a memory standing in the file itself, as a mark made earlier left
    it, and return the mark. A store without the columns gets them, so code
    without the change is judged on what it does with a mark, not on how it
    would make one."""
    conn = sqlite3.connect(str(db))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(engrams)")}
        for name, ddl in _MARK_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE engrams ADD COLUMN {ddl}")
        at = _ago(hours_ago)
        conn.execute(
            "UPDATE engrams SET standing = 1, standing_by = ?, standing_session = ?, "
            "standing_at = ? WHERE id = ?",
            (by, session, at, engram_id),
        )
        conn.commit()
    finally:
        conn.close()
    return (1, by, session, at)


def _hook(db: Path, folder: Path) -> str:
    """The briefing from the real SessionStart hook, in another process."""
    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "session_id": READER, "model": OPUS,
    }
    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db), *SCOPE_ARGS],
        input=json.dumps(payload), capture_output=True, text=True, timeout=180,
        env=_env(folder),
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def _who(packet: str) -> list[str]:
    """The lines of "Who you're with", without its heading."""
    assert "### Who you're with\n" in packet, packet
    section = packet.split("### Who you're with\n", 1)[1].split("\n\n### ", 1)[0]
    return section.split("\n")


def _decay_pass(db) -> dict:
    from mnemos.consolidation.decay import run_decay_pass

    store = EngramStore(str(db))
    try:
        return run_decay_pass(store, {}, **SCOPE)
    finally:
        store.close()


def _fade(db, engram_id: str, *, accessibility: float, hours: float, state: str = "active") -> None:
    """Leave a memory untouched for ``hours`` at ``accessibility``."""
    _write(
        db,
        "UPDATE engrams SET state = ?, accessibility = ?, strength = 0.5, stability = 0.0, "
        "last_accessed = ? WHERE id = ?",
        (state, accessibility, _ago(hours), engram_id),
    )


def _dynamics(db, engram_id: str) -> tuple:
    row = _engram(db, engram_id)
    return row["state"], row["accessibility"], row["strength"], row["stability"]


# ── 1. A capture marks it, as a typed and signed choice ──


def test_a_capture_marked_standing_is_signed_and_words_never_mark_one(tmp_path):
    db = tmp_path / "memory.db"
    # Captured in one process, in a Claude Code session ...
    (marked, sounds), said = _capture_elsewhere(db, tmp_path, [
        {"content": PLAIN, "standing": True, "signed_as": OPUS},
        {"content": SOUNDS_STANDING, "signed_as": OPUS},
    ], session=SESSION)

    standing, by, session, at = _mark(db, marked)
    assert (standing, by, session) == (1, OPUS, SESSION), "the capture was not marked and signed"
    assert abs(datetime.now(timezone.utc) - datetime.fromisoformat(at)) < timedelta(minutes=5)
    assert "Standing: yes." in said, said
    # Words that sound like a standing rule mark nothing: only the choice does.
    assert _mark(db, sounds) == (0, "", "", None)
    assert _standing_ids(db) == {marked}

    # ... and read back in another: the next session's briefing opens with it.
    # The capture whose words only sound like a rule is an ordinary note.
    who = _who(_hook(db, tmp_path))
    assert who[0] == LABEL, who
    assert who[1].startswith("- HARD RULE from Riley") and who[1].endswith(f" ({marked})"), who
    assert who[2] == "Other notes:" and SOUNDS_STANDING in who[3], who


def test_the_tool_descriptions_carry_the_mark_and_the_instructions_do_not():
    from mnemos.simple_mcp import SERVER_INSTRUCTIONS, simple_mcp

    tools = {tool.name: tool for tool in asyncio.run(simple_mcp.list_tools())}
    capture, correct, recall = (
        tools["mnemos_capture"], tools["mnemos_correct"], tools["mnemos_recall"],
    )
    for tool in (capture, recall):
        standing = tool.inputSchema["properties"].get("standing")
        assert standing is not None and standing.get("type") == "boolean", tool.inputSchema
        assert standing.get("default") is False
    assert f"standing: {DESCRIPTION}" in " ".join(capture.description.split())
    correct_text = " ".join(correct.description.split())
    assert "mark_standing" in correct_text and "unmark_standing" in correct_text
    assert "standing: True to list every memory marked standing" in " ".join(recall.description.split())
    # The instructions stay as WP-R04b left them: models see only their first
    # 2,048 characters, and the parameter descriptions, which they see in
    # full, carry the mark.
    assert len(SERVER_INSTRUCTIONS) == 1816
    assert re.search(r"\bstanding\b", SERVER_INSTRUCTIONS, re.IGNORECASE) is None


# ── 2. Marking and unmarking change the mark and nothing else ──


def _everything_but_the_mark(db) -> dict:
    marks = {"standing", "standing_by", "standing_session", "standing_at"}
    return {
        "engrams": [
            {key: value for key, value in row.items() if key not in marks}
            for row in _rows(db, "SELECT * FROM engrams ORDER BY id")
        ],
        "notes": _rows(db, "SELECT * FROM hypomnema_entries ORDER BY id"),
        "versions": _rows(db, "SELECT * FROM versions ORDER BY engram_id, version_num"),
        "links": _rows(db, "SELECT * FROM connections ORDER BY source_id, target_id, relation"),
        "archive": _rows(db, "SELECT * FROM archive ORDER BY id"),
    }


def test_marking_and_unmarking_change_only_the_mark_and_its_signature(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path)
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        memory_id, note_id = _captured(runtime.capture(
            CANOE_RULE, impact="Put things back where Riley keeps them.", signed_as=OPUS,
        ))
        runtime.capture(OTHER, signed_as=OPUS)
    finally:
        runtime.close()
    before = _everything_but_the_mark(db)

    runtime = _runtime(db)
    try:
        marked = runtime.correct("", target_id=memory_id, action="mark_standing", signed_as=OPUS)
        again = runtime.correct("", target_id=note_id, action="mark_standing", signed_as=FABLE)
    finally:
        runtime.close()
    standing, by, session, at = _mark(db, memory_id)
    assert (standing, by, session) == (1, OPUS, SESSION), marked
    assert f"Marked memory {memory_id} standing." in marked, marked
    # Marking what is already marked writes nothing: the mark in force keeps
    # its signature.
    assert "already standing" in again and _mark(db, memory_id) == (1, OPUS, SESSION, at), again
    assert _everything_but_the_mark(db) == before, "marking changed more than the mark"

    runtime = _runtime(db)
    try:
        # By the note's id: the note and its memory are one capture. The text
        # a correction would use changes nothing here, and the result says so.
        unmarked = runtime.correct(
            "Riley wants the paddles in the shed.", target_id=note_id,
            action="unmark_standing", signed_as=FABLE,
        )
    finally:
        runtime.close()
    standing, by, session, when = _mark(db, memory_id)
    assert (standing, by, session) == (0, FABLE, SESSION), unmarked
    assert when >= at
    assert f"Unmarked memory {memory_id}" in unmarked, unmarked
    assert "Your words weren't used" in unmarked, unmarked
    assert _everything_but_the_mark(db) == before, "unmarking changed more than the mark"


def test_only_a_memory_in_use_named_by_its_id_is_marked(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        kept_id, _ = _captured(runtime.capture(CANOE_RULE))
        gone_id, _ = _captured(runtime.capture(OTHER))
        runtime.correct("", target_id=gone_id, action="forget")
        handoff = runtime.handoff("Where I stopped: the canoe inventory.")
        handoff_id = re.search(r"Handoff ID: (\S+)", handoff).group(1)
        runtime._ensure_init()
        belief = Belief(agent_id="nova", content="Plain words carry further.",
                        confidence=0.5, source="agent")
        runtime._store.save_belief(belief)
        counts = (len(_rows(db, "SELECT id FROM engrams")), len(_rows(db, "SELECT id FROM hypomnema_entries")))
        said = {
            "no id": runtime.correct("", action="mark_standing"),
            "a query": runtime.correct("", query="canoe paddles loft", action="mark_standing"),
            "a belief": runtime.correct("", target_id=belief.id, action="mark_standing"),
            "a handoff": runtime.correct("", target_id=handoff_id, action="mark_standing"),
            "forgotten": runtime.correct("", target_id=gone_id, action="mark_standing"),
            "unknown": runtime.correct(
                "", target_id="engram_01ZZZZZZZZZZZZZZZZZZZZZZZZ", action="mark_standing",
            ),
        }
        assert (len(_rows(db, "SELECT id FROM engrams")), len(_rows(db, "SELECT id FROM hypomnema_entries"))) == counts
        for case, text in said.items():
            assert text.startswith("Nothing was marked"), (case, text)
        assert _standing_ids(db) == set()

        # An id a correction replaced reaches its current version.
        replaced = runtime.correct(CORRECTED, target_id=kept_id)
        current = re.search(r"captured correction (engram_\w+)", replaced).group(1)
        followed = runtime.correct("", target_id=kept_id, action="mark_standing")
    finally:
        runtime.close()
    assert "had been replaced by a correction" in followed, followed
    assert _standing_ids(db) == {current}


# ── 3. The briefing opens with them ──


def test_the_briefing_opens_with_the_newest_standing_marks(tmp_path):
    db = tmp_path / "memory.db"
    items = [
        {"content": rule, "signed_as": OPUS, **({"context": RULE_CONTEXT} if index == 3 else {})}
        for index, rule in enumerate(RULES)
    ]
    items += [
        {"content": FOUNDATION, "signed_as": OPUS},
        {"content": OTHER, "signed_as": OPUS},
    ]
    ids, _ = _capture_elsewhere(db, tmp_path, items)
    marked = ids[: len(RULES)]
    # Marked an hour apart, in the order of RULES: the plain-words rule is
    # the newest mark.
    for hour, engram_id in enumerate(marked):
        _mark_by_hand(db, engram_id, hours_ago=len(RULES) - hour)

    packet = _hook(db, tmp_path)
    who = _who(packet)

    newest = list(reversed(range(len(RULES))))[:5]
    assert who[0] == LABEL, who
    shown = who[1:6]
    for line, index in zip(shown, newest):
        assert line.startswith("- ") and line.endswith(f" ({marked[index]})"), line
    # One line each, in the memory's own words: whole when it is short, cut at
    # a sentence boundary when it is long, never with the capture's context.
    assert shown[0].startswith("- HARD RULE from Riley"), shown[0]
    words = shown[0][2:].rsplit(" (", 1)[0]
    assert words.endswith(". […]") and len(words) <= 330 and words[:-4] in PLAIN, words
    assert shown[3] == f"- {RULES[3]} ({marked[3]})", shown[3]
    assert RULE_CONTEXT not in packet
    assert who[6] == f"And 2 more: {LIST_CALL}", who
    # The rest of the section follows, and a standing memory's own note is not
    # said a second time.
    assert who[7] == "Other notes:", who
    assert any(FOUNDATION in line for line in who[8:]), who
    for rule in RULES[:6]:
        assert packet.count(rule) <= 1, f"said twice: {rule}"


def test_standing_lines_are_the_last_to_leave_a_full_packet(tmp_path):
    db = tmp_path / "memory.db"
    rules = [{"content": rule, "signed_as": OPUS} for rule in RULES[:5]]
    foundations = [
        {"content": f"Riley always keeps ledger {n} in order. " + "The harbour ledger lists every crossing. " * 6,
         "signed_as": OPUS}
        for n in range(6)
    ]
    ids, _ = _capture_elsewhere(db, tmp_path, rules + foundations)
    for hour, engram_id in enumerate(ids[:5]):
        _mark_by_hand(db, engram_id, hours_ago=5 - hour)
    store = EngramStore(str(db))
    try:
        store.write_handoff(
            "Where I stopped, at length. " + "The ferry ledger was reconciled line by line. " * 95,
            **SCOPE, author_model=OPUS, author_session=READER,
        )
        for n in range(4):
            store.save_belief(Belief(agent_id="nova", content=f"Belief {n}: plain words carry.",
                                     confidence=0.5, source="agent"))
    finally:
        store.close()

    packet = _hook(db, tmp_path)
    assert len(packet) < 6000, len(packet)
    who = _who(packet)
    assert who[0] == LABEL
    assert [line.rsplit(" (", 1)[-1][:-1] for line in who[1:6]] == list(reversed(ids[:5])), who
    # Premise: the packet was full, and the foundational notes that follow
    # the standing lines gave way first.
    assert sum("Riley always keeps ledger" in line for line in who) < 3, who


# ── 4. Recall lists them all ──


def test_recall_lists_every_standing_memory_whatever_max_results_says(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        marked = [_captured(runtime.capture(rule))[0] for rule in RULES[:6]]
        canoe_rule, _ = _captured(runtime.capture(CANOE_RULE))
        canoe, _ = _captured(runtime.capture(CANOE))
        runtime.capture(OTHER)
    finally:
        runtime.close()
    for hour, engram_id in enumerate([*marked, canoe_rule]):
        _mark_by_hand(db, engram_id, hours_ago=10 - hour)
    links = _rows(db, "SELECT * FROM connections ORDER BY source_id, target_id, relation")
    returns = {row["id"]: row["access_count"] for row in _rows(db, "SELECT id, access_count FROM engrams")}

    runtime = _runtime(db)
    try:
        listed = runtime.recall("", max_results=1, standing=True)
        named = runtime.recall("canoe paddles", standing=True)
    finally:
        runtime.close()

    order = [*reversed([*marked, canoe_rule])]
    assert re.findall(r"id=(engram_\w+)", listed) == order, listed
    assert canoe not in listed and canoe not in named, "a memory not marked standing was listed"
    named_ids = re.findall(r"id=(engram_\w+)", named)
    assert named_ids[0] == canoe_rule and sorted(named_ids) == sorted(order), named
    # A listing is not a return for a cue: nothing is reinforced or linked.
    assert _rows(db, "SELECT * FROM connections ORDER BY source_id, target_id, relation") == links
    assert {row["id"]: row["access_count"] for row in _rows(db, "SELECT id, access_count FROM engrams")} == returns


def test_the_recall_tool_takes_an_empty_query_only_for_the_standing_list(tmp_path):
    from mnemos import simple_mcp as surface

    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        marked, _ = _captured(runtime.capture(CANOE_RULE))
    finally:
        runtime.close()
    _mark_by_hand(db, marked)
    recall = surface.simple_mcp._tool_manager.get_tool("mnemos_recall").fn
    surface.configure_runtime(db_path=str(db), **SCOPE)
    try:
        listed = recall(query="", standing=True)
        with pytest.raises(ValueError, match="query cannot be empty"):
            recall(query="")
    finally:
        surface.configure_runtime()
    assert f"id={marked}" in listed and CANOE_RULE in listed, listed


# ── 5. A standing memory is exempt from decay while it is marked ──


def test_a_standing_memory_does_not_fade_until_it_is_unmarked(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        standing, _ = _captured(runtime.capture(CANOE_RULE))
        quiet, _ = _captured(runtime.capture(QUIET_RULE))
        ordinary, _ = _captured(runtime.capture(OTHER))
    finally:
        runtime.close()
    _mark_by_hand(db, standing)
    _mark_by_hand(db, quiet)
    _fade(db, standing, accessibility=0.06, hours=100)
    _fade(db, ordinary, accessibility=0.06, hours=100)
    # Gone quiet before it was marked: it fades no further while marked.
    _fade(db, quiet, accessibility=0.03, hours=720, state="dormant")
    held = {engram_id: _dynamics(db, engram_id) for engram_id in (standing, quiet)}

    _decay_pass(db)

    assert _engram(db, ordinary)["state"] == "dormant", "premise: this long unused, a memory fades"
    for engram_id, before in held.items():
        assert _dynamics(db, engram_id) == before, f"a standing memory decayed: {engram_id}"
    reader = _runtime(db)
    try:
        briefing = reader.context()
    finally:
        reader.close()
    assert CANOE_RULE in briefing and QUIET_RULE in briefing, briefing

    runtime = _runtime(db)
    try:
        runtime.correct("", target_id=standing, action="unmark_standing")
    finally:
        runtime.close()
    _decay_pass(db)
    assert _engram(db, standing)["state"] == "dormant", "an unmarked memory did not decay again"
    assert _dynamics(db, quiet) == held[quiet]


# ── 6. Code older than the store records the words and ignores the flag ──


def test_older_code_records_the_words_and_ignores_the_flag(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        kept, _ = _captured(runtime.capture(CANOE_RULE, standing=True))
    finally:
        runtime.close()
    _claim_for_newer_code(db)

    runtime = _runtime(db)
    try:
        captured = runtime.capture(PLAIN, standing=True)
        memory_id, note_id = _captured(captured)
        marked = runtime.correct("", target_id=memory_id, action="mark_standing")
        unmarked = runtime.correct("", target_id=kept, action="unmark_standing")
        corrected = runtime.correct(CORRECTED, target_id=kept)
    finally:
        runtime.close()

    for said in (captured, marked, unmarked, corrected):
        assert said.endswith(OLDER), said
    # The words land, as one pair; the flag does not.
    [memory] = _rows(db, "SELECT * FROM engrams WHERE id = ?", (memory_id,))
    [note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (note_id,))
    assert memory["content"] == note["content"] == PLAIN
    assert note["graduated_to_engram_id"] == memory_id
    assert "Standing: not marked." in captured, captured
    assert marked.startswith("Nothing was marked") and unmarked.startswith("Nothing was unmarked")
    # A correction retires what it names; the mark stays with it, and the
    # replacement is not marked.
    current = re.search(r"captured correction (engram_\w+)", corrected).group(1)
    assert _engram(db, kept)["state"] == "archived"
    assert _standing_ids(db) == {kept}
    assert current not in _standing_ids(db)
    assert "It was standing" in corrected, corrected


# ── A correction keeps a standing memory standing ──


def test_correcting_a_standing_memory_keeps_it_standing(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path)
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        old_id, note_id = _captured(runtime.capture(CANOE_RULE, signed_as=OPUS))
    finally:
        runtime.close()
    mark = _mark_by_hand(db, old_id, hours_ago=30, by=OPUS, session=SESSION)

    runtime = _runtime(db)
    try:
        corrected = runtime.correct(CORRECTED, target_id=note_id, signed_as=FABLE)
    finally:
        runtime.close()
    new_id = re.search(r"Memory ID: (engram_\w+)", corrected).group(1)
    assert _engram(db, old_id)["state"] == "archived"
    # The mark in force moves to the replacement, signed as it was made.
    assert _mark(db, new_id) == mark, corrected
    assert "It stays standing" in corrected, corrected

    who = _who(_hook(db, tmp_path))
    assert who[:2] == [LABEL, f"- {CORRECTED} ({new_id})"], who
    assert CANOE_RULE not in "\n".join(who)


# ── The store from before gains the mark, unmarked, after a backup ──


def _without_the_mark(sql: str) -> str:
    """The schema script as a v13 store had it: engrams without the standing
    columns."""
    head, rest = sql.split("CREATE TABLE IF NOT EXISTS engrams (", 1)
    body, tail = rest.split(");", 1)
    kept = [
        line for line in body.splitlines()
        if not line.strip().startswith(("standing", "--"))
    ]
    return f"{head}CREATE TABLE IF NOT EXISTS engrams ({chr(10).join(kept).rstrip().rstrip(',')}\n);{tail}"


def test_a_store_from_before_gains_the_mark_unmarked_after_a_backup(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_without_the_mark(SQL_CREATE_TABLES))
    assert "standing" not in {row[1] for row in conn.execute("PRAGMA table_info(engrams)")}
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '13')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('min_code_version', '6')")
    stamp = "2026-08-26T08:27:15+00:00"
    conn.execute(
        "INSERT INTO engrams (id, content, content_at_encoding, tags, owner_agent_id, person_id, "
        "project_scope, created_at, last_accessed) VALUES (?, ?, ?, ?, 'nova', 'riley', 'demo', ?, ?)",
        ("engram_before", PLAIN, PLAIN, json.dumps(["continuity"]), stamp, stamp),
    )
    conn.commit()
    conn.close()

    EngramStore(str(db)).close()

    columns = {row["name"] for row in _rows(db, "PRAGMA table_info(engrams)")}
    assert {"standing", "standing_by", "standing_session", "standing_at"} <= columns
    assert _mark(db, "engram_before") == (0, "", "", None), "a memory from before came out marked"
    meta = {row["key"]: row["value"] for row in _rows(db, "SELECT key, value FROM meta")}
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert meta["min_code_version"] == str(MAINTENANCE_CODE_VERSION)
    [backup] = list((tmp_path / "backups").glob(f"old.pre-v{SCHEMA_VERSION}-*.db"))
    assert "standing" not in {row["name"] for row in _rows(backup, "PRAGMA table_info(engrams)")}
    assert [row["id"] for row in _rows(backup, "SELECT id FROM engrams")] == ["engram_before"]
    EngramStore(str(db)).close()
    assert len(list((tmp_path / "backups").glob("old.pre-v*.db"))) == 1
