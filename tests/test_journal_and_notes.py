"""A journal that is the agent's, and notes between the agent and the person
(WP-R22, with the note tool of WP-R27).

Simple mode had no journal of the mind's own; the only one was upkeep's dream
journal, whose words are not the agent's. Now the agent has two things, each
kept the way a handoff is kept: exactly as written, in the scope that wrote
them, signed, and never summarized, rewritten, decayed, softened, promoted or
expired by any pass (they are not memories):

- ``mnemos_journal``: the agent's own journal. With no text it reads the last
  five entries back.
- ``mnemos_note``: a note to the person, of a kind (made, noticed, worried,
  disagree, question or pickup). The person's reply is written by the person
  (``mnemos notes reply``), as their own words, and the agent wakes with it once.

What these tests hold:

1. Storage: exactly as written, signed, with how and when it was written
   (``MNEMOS_HOUR_ID``), and the v15 store that gains the tables is backed up
   once.
2. The memory path crosses processes, over the real protocol and with the
   defaults an agent actually calls (no scope given anywhere).
3. The waking packet carries the latest journal line for under three days, and
   each reply of the person's once, never cut for room, never marked delivered
   unless shown, and never marked by code older than the store.
4. No maintenance pass, shallow or deep, and no other tool, changes a row; and
   a structural check that the only code that writes these words is the tools,
   and the person's own command for the person's reply.
5. Recall finds a journal entry by its words and its meaning, marked as journal.
6. The ``--json`` shapes are the contract the interface (R26) reads.

No test reaches a real network, a real model or a real ~/.mnemos.
"""

from __future__ import annotations

import ast
import json
import math
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5"
SESSION = "11111111-aaaa-4aaa-8aaa-111111111111"
ROOT = Path(__file__).resolve().parents[1]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

KINDS = ("made", "noticed", "worried", "disagree", "question", "pickup")
JOURNAL_KEYS = {"id", "text", "mood", "written", "model", "created_at"}
NOTE_KEYS = {"id", "kind", "text", "author", "model", "in_reply_to", "created_at", "read_at"}


# ── Helpers ──


def _runtime(db, **scope) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **(scope or SCOPE))


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _write(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _said(said: str, label: str) -> str:
    """The value on the line of a tool's confirmation that starts ``label``."""
    return said.split(label, 1)[1].splitlines()[0].strip()


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _moved(db, table: str, row_id: str, **delta) -> None:
    """Make a row older: written ``delta`` ago."""
    _write(db, f"UPDATE {table} SET created_at = ? WHERE id = ?", (_ago(**delta), row_id))


def _home(folder: Path) -> Path:
    home = folder / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


def _env(home: Path, **extra: str) -> dict[str, str]:
    """A process of its own, with only what an agent's harness gives it."""
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "MNEMOS_DISABLE_DOTENV": "1",
        "PYTHONPATH": ":".join(sys.path),
        **extra,
    }


def _cli(*args: str, home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mnemos.cli", *args],
        capture_output=True, text=True, timeout=180, env=_env(home, **env),
    )


def _hook(db, home: Path, *, person: str | None = "Riley", model: str = OPUS,
          scope_args: tuple[str, ...] = ()) -> str:
    """The briefing from the real SessionStart hook in another process."""
    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "session_id": SESSION, "model": model, "cwd": str(home),
    }
    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db), *scope_args, *(["--person-name", person] if person else [])],
        input=json.dumps(payload), capture_output=True, text=True, timeout=180, env=_env(home),
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def _human(packet: str) -> str:
    """The packet as the person would be allowed to see it: everything before
    the closing section of ids and calls for the memory tools."""
    return packet.split("### for the memory tools", 1)[0]


def _tool(name: str):
    from mnemos.simple_mcp import simple_mcp

    return simple_mcp._tool_manager.get_tool(name)


@pytest.fixture
def served(tmp_path):
    """The simple tools, called in this process as the server calls them, on a
    store of this test's own."""
    from mnemos import simple_mcp

    simple_mcp.configure_runtime(db_path=str(tmp_path / "memory.db"), **SCOPE)
    yield simple_mcp
    simple_mcp.configure_runtime()


def _all_rows(db) -> dict[str, list[dict]]:
    return {
        "journal": _rows(db, "SELECT * FROM journal_entries ORDER BY id"),
        "notes": _rows(db, "SELECT * FROM notes ORDER BY id"),
    }


# ── 1. Storage: exactly as written, signed, and how and when ──


def test_a_journal_entry_is_kept_exactly_as_written_and_signed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    db = tmp_path / "memory.db"
    words = "  The tide ran out before I finished the thought.\n\n   A second line, indented.\n"
    rt = _runtime(db)
    try:
        said = rt.journal(words, signed_as=OPUS, mood="  quiet, a little restless  ")
    finally:
        rt.close()

    assert said.startswith("Journal entry kept exactly as written."), said
    entry_id = _said(said, "Journal ID: ")
    assert "Signed: Opus 5.5 (claude-opus-5-5)" in said, said
    [row] = _rows(db, "SELECT * FROM journal_entries")
    assert row["id"] == entry_id
    assert row["text"] == words, "the words were changed on the way in"
    assert row["mood"] == "quiet, a little restless"
    assert (row["agent_id"], row["person_id"], row["project_scope"]) == tuple(SCOPE.values())
    assert (row["model_id"], row["session_id"]) == (OPUS, SESSION)
    assert (row["written"], row["hour_id"]) == ("in_conversation", "")
    assert abs(datetime.fromisoformat(row["created_at"]) - datetime.now(timezone.utc)) < timedelta(minutes=1)
    # Not a memory: nothing was made of it.
    assert _rows(db, "SELECT COUNT(*) AS n FROM engrams")[0]["n"] == 0
    assert _rows(db, "SELECT COUNT(*) AS n FROM hypomnema_entries")[0]["n"] == 0


def test_an_unsigned_entry_says_so_and_is_stored_unsigned(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        said = rt.journal("Nothing tells me which model I am.")
    finally:
        rt.close()

    assert "Unsigned:" in said and "mnemos_introduce" in said, said
    assert _rows(db, "SELECT model_id, mood FROM journal_entries") == [{"model_id": "", "mood": ""}]


def test_a_note_is_kept_exactly_as_written_with_its_kind_and_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    db = tmp_path / "memory.db"
    words = "The pier timetable changed.\nI think the ferry notes are stale."
    rt = _runtime(db)
    try:
        said = rt.note(words, "noticed", signed_as=OPUS)
    finally:
        rt.close()

    assert said.startswith("Note kept exactly as written."), said
    assert "Kind: noticed" in said and "Signed: Opus 5.5" in said, said
    assert "nothing was sent" in said, said
    [row] = _rows(db, "SELECT * FROM notes")
    assert row["id"] == _said(said, "Note ID: ")
    assert (row["text"], row["kind"], row["author"]) == (words, "noticed", "agent")
    assert (row["model_id"], row["session_id"], row["in_reply_to"]) == (OPUS, SESSION, "")
    assert (row["hour_id"], row["delivered_at"], row["read_at"]) == ("", None, None)
    assert (row["agent_id"], row["person_id"], row["project_scope"]) == tuple(SCOPE.values())


def test_written_and_the_hour_follow_the_environment(tmp_path, monkeypatch):
    """Only the environment can say a write came in a quiet hour between
    sessions (R24 sets ``MNEMOS_HOUR_ID``); the entry records which hour."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.journal("Written in a conversation.", signed_as=OPUS)
        rt.note("Noticed in a conversation.", "noticed", signed_as=OPUS)
        monkeypatch.setenv("MNEMOS_HOUR_ID", "hour-2026-10-02-mine")
        rt.journal("Written alone, in my own hour.", signed_as=OPUS)
        rt.note("Made in my own hour.", "made", signed_as=OPUS)
        # An empty or blank variable is no hour.
        monkeypatch.setenv("MNEMOS_HOUR_ID", "   ")
        rt.journal("Written with a blank hour.", signed_as=OPUS)
        monkeypatch.delenv("MNEMOS_HOUR_ID")
        rt.journal("Written in a conversation again.", signed_as=OPUS)
    finally:
        rt.close()

    assert _rows(db, "SELECT text, written, hour_id FROM journal_entries ORDER BY created_at") == [
        {"text": "Written in a conversation.", "written": "in_conversation", "hour_id": ""},
        {"text": "Written alone, in my own hour.", "written": "between_sessions",
         "hour_id": "hour-2026-10-02-mine"},
        {"text": "Written with a blank hour.", "written": "in_conversation", "hour_id": ""},
        {"text": "Written in a conversation again.", "written": "in_conversation", "hour_id": ""},
    ]
    assert _rows(db, "SELECT text, hour_id FROM notes ORDER BY created_at") == [
        {"text": "Noticed in a conversation.", "hour_id": ""},
        {"text": "Made in my own hour.", "hour_id": "hour-2026-10-02-mine"},
    ]


def test_an_unknown_note_kind_is_rejected_with_the_list_and_nothing_is_saved(tmp_path, served):
    note = _tool("mnemos_note").fn
    for bad in ("", "check-in", "reply", "person", "pick up", "noticed!"):
        said = note("Just wanted to say hello.", bad)
        assert said.startswith("Nothing saved:"), (bad, said)
        for kind in KINDS:
            assert kind in said, f"the rejection of {bad!r} does not list {kind}: {said}"
    assert not (tmp_path / "memory.db").exists(), "a refused note opened a store"

    # Every real kind is taken, however it is capitalised.
    for kind in KINDS:
        assert note(f"A {kind} note.", kind.capitalize()).startswith("Note kept exactly as written.")
    assert [row["kind"] for row in
            _rows(tmp_path / "memory.db", "SELECT kind FROM notes ORDER BY created_at")] == list(KINDS)


def test_a_note_can_only_answer_a_note_of_this_scope(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    elsewhere = _runtime(db, **{**SCOPE, "project_scope": "other"})
    try:
        mine = _said(rt.note("A first note.", "made", signed_as=OPUS), "Note ID: ")
        theirs = _said(elsewhere.note("Another scope's note.", "made", signed_as=OPUS), "Note ID: ")

        assert rt.note("Following up.", "question", signed_as=OPUS, in_reply_to=mine).startswith("Note kept")
        for dangling in ("no-such-note", theirs):
            said = rt.note("Following up.", "question", signed_as=OPUS, in_reply_to=dangling)
            assert said.startswith("Nothing saved:") and "no note with that id" in said, said
    finally:
        rt.close()
        elsewhere.close()

    assert [row["text"] for row in _rows(db, "SELECT text FROM notes WHERE project_scope = 'demo' "
                                             "ORDER BY created_at")] == ["A first note.", "Following up."]


# ── 2. Across processes, over the real protocol, with the defaults ──


def test_a_journal_entry_written_over_the_protocol_is_read_back_in_other_processes(tmp_path):
    """The repo's rule for the memory path, with no scope given anywhere (the
    defaults an agent actually calls): the entry is written through a server in
    one process, then read by the command in a second and shown by the real
    SessionStart hook in a third."""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    home = _home(tmp_path)
    db = tmp_path / "served.db"
    seen: dict = {}

    def text(result) -> str:
        return "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")

    async def session_run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemos.cli", "serve", "--mode", "simple", "--db-path", str(db)],
            env=_env(home, CLAUDE_CODE_SESSION_ID=SESSION),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                seen["tools"] = {tool.name for tool in (await session.list_tools()).tools}
                if not {"mnemos_journal", "mnemos_note"} <= seen["tools"]:
                    return
                wrote = await session.call_tool("mnemos_journal", {
                    "text": "The tide ran out before I finished the thought.\nA second line.",
                    "signed_as": OPUS, "mood": "quiet",
                })
                seen["journal"] = (wrote.isError, text(wrote))
                note = await session.call_tool("mnemos_note", {
                    "text": "The pier timetable changed; the ferry notes may be stale.",
                    "kind": "noticed", "signed_as": OPUS,
                })
                seen["note"] = (note.isError, text(note))
                read = await session.call_tool("mnemos_journal", {})
                seen["read"] = (read.isError, text(read))

    anyio.run(session_run)

    assert {"mnemos_journal", "mnemos_note"} <= seen["tools"], sorted(seen["tools"])
    assert seen["journal"][0] is False and "kept exactly as written" in seen["journal"][1], seen
    assert seen["note"][0] is False and "kept exactly as written" in seen["note"][1], seen
    entry_id = _said(seen["journal"][1], "Journal ID: ")
    note_id = _said(seen["note"][1], "Note ID: ")
    assert seen["read"][0] is False and "The tide ran out" in seen["read"][1], seen["read"]

    # The defaults: nothing named a scope, and every reader resolves the same one.
    assert _rows(db, "SELECT agent_id, person_id, project_scope, session_id, model_id "
                     "FROM journal_entries") == [{
        "agent_id": "mnemos-agent", "person_id": "user", "project_scope": "global",
        "session_id": SESSION, "model_id": OPUS,
    }]

    # A second process: the command.
    journal = json.loads(_cli("journal", "--json", "--db-path", str(db), home=home).stdout)
    assert journal == [{
        "id": entry_id,
        "text": "The tide ran out before I finished the thought.\nA second line.",
        "mood": "quiet", "written": "in_conversation", "model": OPUS,
        "created_at": journal[0]["created_at"],
    }]
    notes = json.loads(_cli("notes", "--json", "--db-path", str(db), home=home).stdout)
    assert [(n["id"], n["kind"], n["author"], n["model"]) for n in notes] == [
        (note_id, "noticed", "agent", OPUS)
    ]

    # A third: the hook, which reads no scope either.
    packet = _hook(db, home)
    assert "last time I wrote in my journal (just now): The tide ran out before I finished the thought." in packet, packet


def test_a_reply_written_by_the_command_reaches_the_hook_once_across_processes(tmp_path):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        note_id = _said(rt.note("The pier timetable changed.\nThe ferry notes may be stale.", "noticed",
                                signed_as=OPUS), "Note ID: ")
    finally:
        rt.close()

    replied = _cli("notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id,
                   "I'll check with the harbour office.", home=home)
    assert replied.returncode == 0, replied.stderr
    assert "Reply kept, in your own words." in replied.stdout

    first = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    assert ('- Riley replied to my note "The pier timetable changed.": '
            "I'll check with the harbour office.") in first, first
    second = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    assert "replied to" not in second, second


# ── 3. The empty call reads ──


def test_an_empty_journal_call_reads_the_last_five_newest_first(tmp_path, served):
    journal = _tool("mnemos_journal").fn
    db = tmp_path / "memory.db"
    assert journal() == "Your journal is empty."
    assert not db.exists(), "reading the journal made a store"

    ids = []
    for number in range(1, 8):
        ids.append(_said(journal(f"Entry number {number}.", signed_as=OPUS,
                                 mood="calm" if number == 7 else ""), "Journal ID: "))
        _moved(db, "journal_entries", ids[-1], days=8 - number)
    _write(db, "UPDATE journal_entries SET written = 'between_sessions', hour_id = 'hour-x' WHERE text = ?",
           ("Entry number 6.",))
    before = _rows(db, "SELECT * FROM journal_entries ORDER BY id")

    for empty in ("", "   \n"):
        said = journal(empty, signed_as=OPUS)
        assert said.startswith("Your journal, the last 5 entries, newest first:"), said
        order = re.findall(r"Entry number (\d)\.", said)
        assert order == ["7", "6", "5", "4", "3"], said
        assert "Entry number 2" not in said and "Entry number 1" not in said
        assert said.count("in_conversation") == 4 and said.count("between_sessions") == 1, said
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC, in_conversation: Yours \(Opus 5\.5\)", said), said
        assert "Mood: calm" in said, said
        assert "colleague" not in said, "the reader's own entries were called a colleague's"

    # A reader no one can place is told whose they say they are, and once.
    unplaced = journal()
    assert unplaced.count("From Opus 5.5") == 5 and unplaced.count("yours if you are Opus 5.5") == 5, unplaced
    assert unplaced.count("A colleague's note is theirs") == 1, unplaced
    # A colleague reading is told they are not its words.
    other = journal(signed_as=FABLE)
    assert other.count("From Opus 5.5, a colleague") == 5 and other.count("A colleague's note is theirs") == 1, other

    assert _rows(db, "SELECT * FROM journal_entries ORDER BY id") == before, "reading wrote"


def test_reading_the_journal_or_the_notes_never_creates_a_store(tmp_path, capsys):
    db = tmp_path / "nothing" / "memory.db"
    assert main(["journal", "--db-path", str(db)]) == 0
    assert capsys.readouterr().out.strip() == "The journal is empty."
    assert main(["notes", "--db-path", str(db)]) == 0
    assert capsys.readouterr().out.strip() == "No notes yet."
    assert main(["notes", "--unread", "--json", "--db-path", str(db)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["notes", "reply", "--db-path", str(db), "some-id", "hello"]) == 1
    assert "Nothing saved" in capsys.readouterr().err
    assert not db.exists() and not db.parent.exists(), "a read made a store"
    rt = _runtime(db)
    try:
        assert rt.journal("") == "Your journal is empty."
    finally:
        rt.close()
    assert not db.exists()


# ── 4. The waking packet ──


@pytest.mark.parametrize("age, shown", [
    (timedelta(minutes=5), True),
    (timedelta(days=2, hours=23), True),
    (timedelta(days=3, hours=1), False),
    (timedelta(days=30), False),
])
def test_the_packet_shows_the_journal_line_under_three_days_and_not_past_it(tmp_path, age, shown):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        said = rt.journal(
            "The tide ran out before I finished the thought.\nA second line that stays in the journal.",
            signed_as=OPUS, mood="quiet",
        )
    finally:
        rt.close()
    _moved(db, "journal_entries", _said(said, "Journal ID: "), seconds=int(age.total_seconds()))

    packet = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    human = _human(packet)

    if not shown:
        assert "my journal" not in packet and "journal entry" not in packet, packet
        return
    when = "5 minutes ago" if age < timedelta(hours=1) else "2 days ago"
    assert (f"last time I wrote in my journal ({when}): "
            "The tide ran out before I finished the thought.") in human, packet
    assert "A second line" not in packet, "more than the first line was shown"
    # No tool names, ids or moods in what the person could be shown.
    assert "mnemos_" not in human and not UUID.search(human), human
    # The whole entry waits behind its id, in the closing section.
    assert re.search(r'what was cut short, whole, in order: mnemos_recall\("[0-9a-f-]{36}"\)', packet), packet


def test_a_journal_line_a_colleague_wrote_is_not_claimed(tmp_path):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.journal("Fable's thought, not mine.", signed_as=FABLE)
    finally:
        rt.close()

    packet = _hook(db, home, model=OPUS, scope_args=tuple(SCOPE_ARGS))

    assert "the last journal entry here, by Fable 5 (just now): Fable's thought, not mine." in packet, packet
    assert "I wrote in my journal" not in packet, "another model's words were claimed as the reader's"

    unsigned = tmp_path / "unsigned.db"
    rt = _runtime(unsigned)
    try:
        rt.journal("Nobody can say whose this is.")
    finally:
        rt.close()
    packet = _hook(unsigned, home, model="", scope_args=tuple(SCOPE_ARGS))
    assert "the last journal entry here, unsigned (just now): Nobody can say whose this is." in packet, packet


def test_a_reply_is_delivered_exactly_once_and_three_at_a_time(tmp_path):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        note_id = _said(rt.note("Do you want the harbour survey redone?", "question",
                                signed_as=OPUS), "Note ID: ")
        for number in range(1, 5):
            rt.reply_to_note(note_id, f"Reply number {number}.")
    finally:
        rt.close()
    for number in range(1, 5):
        _write(db, "UPDATE notes SET created_at = ? WHERE text = ?",
               (_ago(minutes=10 - number), f"Reply number {number}."))

    first = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    shown = re.findall(r'- Riley replied to my note "Do you want the harbour survey redone\?": (Reply number \d)\.', first)
    assert shown == ["Reply number 1", "Reply number 2", "Reply number 3"], first
    delivered = _rows(db, "SELECT text, delivered_at FROM notes WHERE author = 'person' ORDER BY created_at")
    assert [(row["text"], row["delivered_at"] is not None) for row in delivered] == [
        ("Reply number 1.", True), ("Reply number 2.", True), ("Reply number 3.", True),
        ("Reply number 4.", False),
    ]
    stamps = {row["text"]: row["delivered_at"] for row in delivered}

    second = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    assert re.findall(r"(Reply number \d)\.", second) == ["Reply number 4"], second
    third = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    assert "Reply number" not in third and "replied to" not in third, third

    after = {row["text"]: row["delivered_at"] for row in
             _rows(db, "SELECT text, delivered_at FROM notes WHERE author = 'person'")}
    for text, stamp in stamps.items():
        if stamp is not None:
            assert after[text] == stamp, "a delivered reply was delivered again"
    assert all(after.values())


def test_notes_reply_writes_the_persons_own_words_and_the_next_packet_carries_them(
    tmp_path, capsys, monkeypatch,
):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        note_id = _said(rt.note("I disagree with moving the survey to Friday.", "disagree",
                                signed_as=OPUS), "Note ID: ")
    finally:
        rt.close()
    words = "Fair.  Let's keep it Thursday, then -- I'll tell the crew."

    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id, words]) == 0
    out = capsys.readouterr().out
    assert "Reply kept, in your own words." in out
    [reply] = _rows(db, "SELECT * FROM notes WHERE author = 'person'")
    assert _said(out, "Reply ID: ") == reply["id"]
    assert (reply["text"], reply["kind"], reply["model_id"], reply["in_reply_to"]) == (words, "", "", note_id)
    assert (reply["hour_id"], reply["delivered_at"], reply["read_at"]) == ("", None, None)
    assert (reply["agent_id"], reply["person_id"], reply["project_scope"]) == tuple(SCOPE.values())
    # Answering a note is reading it.
    assert _rows(db, "SELECT read_at FROM notes WHERE id = ?", (note_id,))[0]["read_at"] is not None

    packet = _hook(db, home, scope_args=tuple(SCOPE_ARGS))
    # The packet puts a reply on one line; the store above holds it exactly.
    condensed = " ".join(words.split())
    assert condensed != words, "premise: the reply has a double space to condense"
    assert f'- Riley replied to my note "I disagree with moving the survey to Friday.": {condensed}' in packet, packet
    assert "### replies to my notes" in packet
    human = _human(packet)
    assert "mnemos_" not in human and not UUID.search(human), human
    # The id to answer it with waits in the closing section.
    assert f"replies to my notes, in order: {reply['id']}" in packet and "in_reply_to=" in packet, packet

    # Refused, and nothing written: no such note, an answer to a reply, a
    # blank reply, and inside one of the agent's own hours.
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, "no-such-note", "hi"]) == 1
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, reply["id"], "to my own words"]) == 1
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id, "   "]) == 1
    # A stray paste is refused rather than stored; the longest allowed is kept whole.
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id, "x" * 16_385]) == 1
    assert "at most 16,384 characters" in capsys.readouterr().err
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id, "y" * 16_384]) == 0
    capsys.readouterr()
    assert _rows(db, "SELECT length(text) AS n FROM notes WHERE author = 'person' ORDER BY created_at") == [
        {"n": len(words)}, {"n": 16_384},
    ]
    monkeypatch.setenv("MNEMOS_HOUR_ID", "hour-2026-10-02-mine")
    assert main(["notes", "reply", "--db-path", str(db), *SCOPE_ARGS, note_id, "from inside an hour"]) == 1
    assert "no one is in the room" in capsys.readouterr().err
    # Only the two replies the person wrote: nothing refused left a row.
    assert _rows(db, "SELECT COUNT(*) AS n FROM notes WHERE author = 'person'") == [{"n": 2}]


def test_a_reply_is_not_marked_delivered_by_code_older_than_the_store(tmp_path, monkeypatch):
    """Bookkeeping is a rule: what delivered means is for the code that wrote
    the store to decide. Older code still shows the reply (a reply seen twice
    beats one never seen), takes the agent's own words, and leaves the stamp
    for the code that comes after."""
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)  # the operator names the reader
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        note_id = _said(rt.note("A question for you.", "question"), "Note ID: ")
        rt.reply_to_note(note_id, "Yes, Thursday.")
        rt.journal("An entry from before.")
    finally:
        rt.close()
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")

    older = _runtime(db)
    try:
        said = older.context()
        assert "replied to my note" in said and "Yes, Thursday." in said, said
        assert "older Mnemos code" in said
        # The agent's own words still land; marking a note read is declined.
        wrote_journal = older.journal("Written on older code.")
        wrote_note = older.note("Left on older code.", "made")
        assert older.mark_notes_read([note_id]) == []
    finally:
        older.close()

    assert wrote_journal.startswith("Journal entry kept"), wrote_journal
    assert wrote_note.startswith("Note kept"), wrote_note
    assert _rows(db, "SELECT delivered_at FROM notes WHERE author = 'person'") == [{"delivered_at": None}]
    assert _rows(db, "SELECT read_at FROM notes WHERE id = ?", (note_id,)) == [{"read_at": None}]
    assert _rows(db, "SELECT COUNT(*) AS n FROM journal_entries") == [{"n": 2}]

    # The store comes back to code that is not older: delivered once, now.
    _write(db, "UPDATE meta SET value = ? WHERE key = 'min_code_version'", (str(MAINTENANCE_CODE_VERSION),))
    current = _runtime(db)
    try:
        assert "Yes, Thursday." in current.context()
        assert "Yes, Thursday." not in current.context()
        assert current.mark_notes_read([note_id]) == [note_id]
    finally:
        current.close()
    assert _rows(db, "SELECT delivered_at IS NOT NULL AS done FROM notes WHERE author = 'person'") == [{"done": 1}]


def test_a_long_reply_is_cut_in_the_packet_and_whole_by_its_id_and_never_left_out(tmp_path):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        # A handoff that alone fills the packet's budget.
        rt.handoff("Where I am: " + "the survey is half done. " * 230, signed_as=OPUS)
        note_id = _said(rt.note("What should I do first?", "question", signed_as=OPUS), "Note ID: ")
        long_reply = " ".join(f"Point {n} of my answer, in full." for n in range(1, 90))
        for text in (long_reply, "A short one.", "And another short one."):
            rt.reply_to_note(note_id, text)
        waiting = {row["text"]: row["id"] for row in
                   _rows(db, "SELECT id, text FROM notes WHERE author = 'person'")}
    finally:
        rt.close()

    packet = _hook(db, home, scope_args=tuple(SCOPE_ARGS))

    assert "A short one." in packet and "And another short one." in packet, "a reply was left out for room"
    assert "Point 1 of my answer" in packet and "Point 89" not in packet, "the long reply was not cut"
    assert "[…]" in _human(packet)
    long_id = waiting[long_reply]
    assert f'mnemos_recall("{long_id}")' in packet, "the cut reply's id is nowhere to be found"
    assert _rows(db, "SELECT COUNT(*) AS n FROM notes WHERE author = 'person' AND delivered_at IS NULL") == [{"n": 0}]

    reader = _runtime(db)
    try:
        whole = reader.recall(long_id)
    finally:
        reader.close()
    assert long_reply in whole and whole.startswith("The person's reply"), whole


def test_a_store_holding_only_a_journal_entry_is_not_an_empty_packet(tmp_path):
    """A packet that carries a journal line or a reply carried something; the
    empty-packet streak that raises the health card's alarm must not count it."""
    from mnemos.interface.context_packet import carried_count

    assert carried_count({"shown": {"journal": "an-id"}}) == 1
    assert carried_count({"shown": {"replies": ["a", "b"]}}) == 2
    assert carried_count({"shown": {}}) == 0

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.journal("My first entry, before anything else.", signed_as=OPUS)
        rt.context()
        rt.context()
        assert rt.continuity_signals()["empty_context_streak"] == 0
    finally:
        rt.close()


def test_another_scopes_journal_and_notes_never_show_up(tmp_path):
    home = _home(tmp_path)
    db = tmp_path / "memory.db"
    mine = _runtime(db)
    other_scope = {**SCOPE, "project_scope": "other"}
    elsewhere = _runtime(db, **other_scope)
    try:
        mine.journal("The lighthouse lamp was rewired on Tuesday.", signed_as=OPUS)
        note_id = _said(mine.note("A lamp note.", "made", signed_as=OPUS), "Note ID: ")
        mine.reply_to_note(note_id, "A reply.")

        assert elsewhere.journal("") == "Your journal is empty."
        assert elsewhere.journal_entries() == [] and elsewhere.notes() == []
        assert "rewired" not in elsewhere.recall("lighthouse lamp rewired")
        assert elsewhere.recall(note_id) == "No relevant continuity found."
        with pytest.raises(ValueError, match="no note with that id"):
            elsewhere.reply_to_note(note_id, "Not mine to answer.")
        assert elsewhere.health()["journal"] == {
            "journal_entries": 0, "notes_by_agent": 0, "notes_by_person": 0, "replies_waiting": 0,
        }
    finally:
        mine.close()
        elsewhere.close()

    nothing = _hook(db, home, scope_args=("--agent-id", "nova", "--person-id", "riley",
                                           "--project-scope", "other"))
    assert "journal" not in nothing and "replied" not in nothing, nothing


# ── 5. No pass, and no other path, writes or changes these rows ──


class _Provider:
    """A configured model, as a deep cycle is given one: it answers anything."""

    _model = "stub-provider-model"

    def complete(self, prompt: str, **_: object) -> str:
        return "The ferries and the pier share one timetable.\nThe storms keep it."

    def structured_complete(self, system: str, user: str, **_: object) -> str:
        return json.dumps({"narrative": "A dream of the pier.", "text": "A dream of the pier."})


def _store_with_a_life(tmp_path) -> Path:
    """A store with a journal and notes in every state, the oldest of them well
    past anything an expiry rule would let live, and memories for the passes to
    work on."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        for words in (
            "The harbour ferry leaves at noon on weekdays and at ten on Sundays.",
            "The pier timetable is posted by the harbour office every Monday morning.",
            "The ferry crew counts passengers at the gangway before casting off.",
            "Storm warnings close the pier until the harbour master reopens it.",
            "Riley keeps the release notes in docs/releases.",
        ):
            rt.capture(words, impact="Kept for the passes to work on.")
        rt.handoff("Where I stopped: the ferry timetable.", signed_as=OPUS)
        ids = [
            _said(rt.journal(f"Entry {n}, in my own words.\nWith a second line.", signed_as=OPUS,
                             mood="calm"), "Journal ID: ")
            for n in range(3)
        ]
        notes = [
            _said(rt.note(f"A {kind} note.", kind, signed_as=OPUS), "Note ID: ")
            for kind in ("made", "worried", "question")
        ]
        replies = [rt.reply_to_note(notes[0], "A reply of mine."), rt.reply_to_note(notes[2], "Another.")]
        rt.mark_notes_read(notes[:1])
    finally:
        rt.close()
    _write(db, "UPDATE notes SET delivered_at = ? WHERE id = ?", (_ago(days=399), replies[0]))
    _write(db, "UPDATE notes SET delivered_at = ? WHERE id = ?", (_ago(days=398), replies[1]))
    _moved(db, "journal_entries", ids[0], days=400)
    _moved(db, "journal_entries", ids[1], days=40)
    _moved(db, "notes", notes[0], days=400)
    _moved(db, "notes", replies[0], days=399)
    # Every memory and the last cycle are old, so decay and the rest have work.
    _write(db, "UPDATE engrams SET last_accessed = ?, accessibility = 0.15, strength = 0.2",
           (_ago(days=90),))
    _write(db, "UPDATE consolidation_log SET started_at = ?, completed_at = ?", (_ago(days=3), _ago(days=3)))
    return db


def test_no_maintenance_pass_changes_a_journal_or_note_row(tmp_path, monkeypatch, capsys):
    from mnemos.store import embedding_index as ei
    from mnemos.substrate.config import SubstrateConfig
    from mnemos.substrate.tick import Substrate

    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = _store_with_a_life(tmp_path)
    before = _all_rows(db)
    assert len(before["journal"]) == 3 and len(before["notes"]) == 5, "premise"
    work = "SELECT id, state, accessibility, strength, impact, content FROM engrams ORDER BY id"
    memories = _rows(db, work)
    cycles = _rows(db, "SELECT COUNT(*) AS n FROM consolidation_log")[0]["n"]

    rt = _runtime(db)
    try:
        # Shallow and deep maintenance, deep with a model configured.
        for deep in (False, True):
            said = rt.maintain(deep=deep)
            assert "Cycle: shallow" in said or "Cycle: deep" in said, said
        rt._ensure_init()
        rt._llm_client = _Provider()
        deeper = rt.maintain(deep=True)
        assert "Cycle: deep" in deeper and "decay" in deeper and "reflection" in deeper, deeper
        # The passes did real work (decay lowered what had gone unused), so
        # leaving the rows alone is not for want of work.
        reworked = {row["id"]: row for row in _rows(db, work)}
        assert [reworked[row["id"]] for row in memories] != memories, (
            "the passes changed no memory, so this proves nothing"
        )
        # Everything else the agent's tools do, and what reads.
        rt.capture("Riley moved the survey to Thursday.", impact="Plans move.")
        rt.handoff("Where I stopped: the survey.", signed_as=OPUS)
        rt.correct("The ferry leaves at eleven on Sundays.", query="ferry leaves Sundays", signed_as=OPUS)
        rt.recall("ferry timetable")
        rt.recall("Entry 0 in my own words")
        rt.context(query="harbour")
        rt.health()
    finally:
        rt.close()

    # The scheduled job, deep, and the cognitive tick, with and without a model.
    for argv in (["consolidate"], ["consolidate", "--deep"]):
        assert main(["--db-path", str(db), *SCOPE_ARGS, *argv]) == 0
    capsys.readouterr()
    for client in (None, _Provider()):
        substrate = Substrate(SubstrateConfig(agent_id="nova", db_path=str(db)))
        try:
            substrate.llm_client = client
            substrate.tick()
        finally:
            substrate.store.close()
    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    _hook(db, _home(tmp_path), scope_args=tuple(SCOPE_ARGS))

    assert _rows(db, "SELECT COUNT(*) AS n FROM consolidation_log")[0]["n"] > cycles, "no cycle ran"
    assert _all_rows(db) == before, "something changed a journal entry or a note"


def test_the_only_code_that_writes_these_words_is_the_tools_and_the_persons_own_command():
    """The words in a journal are the agent's, and a reply is the person's.
    Every call site is named here, so a new path that writes either shows up as
    a failure of this test and has to be argued for: the store writes them in
    one place each, the runtime's tools are the only callers, and the person's
    reply is written only by the person's command, never by a tool."""

    def calls(attribute: str) -> list[str]:
        found = []
        for path in sorted((ROOT / "mnemos").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == attribute):
                    names, here = [], node
                    while here in parents:
                        here = parents[here]
                        if isinstance(here, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            names.append(here.name)
                    found.append(f"{path.relative_to(ROOT / 'mnemos')}:{'.'.join(reversed(names))}")
        return sorted(found)

    assert calls("write_journal_entry") == ["simple_runtime.py:MnemosRuntime.journal"]
    assert calls("write_note") == [
        "simple_runtime.py:MnemosRuntime.note", "simple_runtime.py:MnemosRuntime.reply_to_note",
    ]
    assert calls("reply_to_note") == ["cli.py:_cmd_notes_reply"]
    assert calls("journal") == ["simple_mcp.py:register_simple_tools.mnemos_journal"]
    assert calls("note") == ["simple_mcp.py:register_simple_tools.mnemos_note"]

    # And in SQL: each table has one INSERT, nothing deletes a row, and the only
    # UPDATE sets one of the two timestamps. No statement rewrites words.
    statements: dict[str, list[str]] = {}
    for path in sorted((ROOT / "mnemos").rglob("*.py")):
        for match in re.finditer(r"(INSERT INTO|UPDATE|DELETE FROM)\s+(journal_entries|notes)\b(\s+SET\s+\S+)?",
                                 path.read_text(encoding="utf-8")):
            statements.setdefault(str(path.relative_to(ROOT / "mnemos")), []).append(
                " ".join(f"{match.group(1)} {match.group(2)}{match.group(3) or ''}".split())
            )
    assert statements == {"store/sqlite_store.py": [
        "INSERT INTO journal_entries", "INSERT INTO notes", "UPDATE notes SET {column}",
    ]}, statements


def test_no_tool_the_agent_calls_can_write_a_reply_or_set_a_kind_it_was_not_given(tmp_path, served):
    """The surface: ``mnemos_note`` writes the agent's own notes, and its
    parameters name no author, so there is no way to ask it for a person's
    words; and the reply the person writes needs a command, not a tool."""
    from mnemos.simple_runtime import SIMPLE_TOOL_NAMES

    assert {"mnemos_journal", "mnemos_note"} <= set(SIMPLE_TOOL_NAMES)
    assert not [name for name in SIMPLE_TOOL_NAMES if "reply" in name]
    properties = {tool: set(_tool(tool).parameters["properties"]) for tool in ("mnemos_journal", "mnemos_note")}
    assert properties == {
        "mnemos_journal": {"text", "signed_as", "mood"},
        "mnemos_note": {"text", "kind", "signed_as", "in_reply_to"},
    }
    assert _tool("mnemos_note").parameters["required"] == ["text", "kind"]
    assert not _tool("mnemos_journal").parameters.get("required")

    note = _tool("mnemos_note").fn
    assert note("Pretending to be Riley.", "person").startswith("Nothing saved:")
    assert note("Pretending to be Riley.", "reply").startswith("Nothing saved:")
    note("A real note.", "made", signed_as=OPUS)
    assert [row["author"] for row in _rows(tmp_path / "memory.db", "SELECT author FROM notes")] == ["agent"]


# ── 6. The store: a v15 store is migrated once, after a verified backup ──


def test_a_v15_store_gains_the_journal_tables_after_one_verified_backup(tmp_path):
    from mnemos.backup import check_database
    from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.capture("The lighthouse lamp was rewired on Tuesday.")
    finally:
        rt.close()
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE IF EXISTS journal_entries")
    conn.execute("DROP TABLE IF EXISTS notes")
    conn.execute("UPDATE meta SET value = '15' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    memories = _rows(db, "SELECT id, content FROM engrams ORDER BY id")

    # Two openers meet the old version at once: one recovery point, not two.
    EngramStore(str(db)).close()
    EngramStore(str(db)).close()

    tables = {row["name"] for row in _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"journal_entries", "notes"} <= tables
    assert _rows(db, "SELECT value FROM meta WHERE key = 'schema_version'") == [{"value": "16"}]
    assert SCHEMA_VERSION == 16
    [backup] = list((tmp_path / "backups").glob("memory.pre-v16-*.db"))
    assert check_database(backup)["schema_version"] == "15"
    assert {row["name"] for row in _rows(backup, "SELECT name FROM sqlite_master WHERE type = 'table'")
            } >= {"engrams"} and "journal_entries" not in {
        row["name"] for row in _rows(backup, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert _rows(db, "SELECT id, content FROM engrams ORDER BY id") == memories
    assert _rows(db, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]
    assert _rows(db, "SELECT COUNT(*) AS n FROM journal_entries") == [{"n": 0}]

    # A store this code made is not backed up again for being opened.
    EngramStore(str(db)).close()
    assert len(list((tmp_path / "backups").glob("memory.pre-v16-*.db"))) == 1


def test_the_code_version_is_raised_so_older_servers_stand_down(tmp_path):
    assert MAINTENANCE_CODE_VERSION >= 12, "what the store holds changed, and the code version was not raised"
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.journal("An entry.", signed_as=OPUS)
    finally:
        rt.close()
    assert _rows(db, "SELECT value FROM meta WHERE key = 'min_code_version'") == [
        {"value": str(MAINTENANCE_CODE_VERSION)}
    ]


# ── 7. Recall finds a journal entry, marked as journal ──


def test_recall_finds_a_journal_entry_by_its_words_marked_as_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.introduce(agent_model=OPUS)
        rt.capture("The harbour ferry leaves at noon.")
        rt.journal("Tonight the east pier felt different, and I could not say why.\nA second line.",
                   signed_as=OPUS, mood="uneasy")
        rt.journal("Fable wrote about the garden marigolds.", signed_as=FABLE)
        out = rt.recall("east pier felt different")
        colleague = rt.recall("garden marigolds")
    finally:
        rt.close()

    assert "Journal:" in out, out
    row = out.split("Journal:", 1)[1]
    today = datetime.now(timezone.utc).date().isoformat()
    assert "Tonight the east pier felt different" in row, out
    assert f"kind=journal; written {today} (in_conversation): Yours (Opus 5.5)" in row, out
    assert "Handoffs:" not in out
    # A colleague's entry is labelled as theirs.
    assert "kind=journal" in colleague and "From Fable 5, a colleague" in colleague, colleague
    assert "A colleague's note is theirs" not in colleague.split("Journal:", 1)[0]


_CONCEPTS = (
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
    {"garden", "marigolds", "greenhouse", "bloom"},
)


class _ConceptModel:
    """A model whose meaning is controlled: "beacon" is near "lighthouse"."""

    @staticmethod
    def _vector(text: str) -> list[float]:
        words = set(re.findall(r"[a-z]+", text.lower()))
        raw = [float(len(words & group)) for group in _CONCEPTS] + [0.05]
        norm = math.sqrt(sum(v * v for v in raw))
        return [v / norm for v in raw]

    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        class _Vector(list):
            def tolist(self):
                return list(self)

        if isinstance(texts, str):
            return _Vector(self._vector(texts))
        return [_Vector(self._vector(text)) for text in texts]


def test_recall_finds_a_journal_entry_by_its_meaning(tmp_path, monkeypatch):
    import mnemos.store.embedding_index as ei

    class _Embedder(ei._LocalEmbedder):
        def _get_model(self):
            if self._model is None:
                self._model = _ConceptModel()
            return self._model

    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _Embedder)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        entry_id = _said(rt.journal("Tonight the beacon on the point needed a new lamp.", signed_as=OPUS),
                         "Journal ID: ")
        passages = _rows(db, "SELECT part FROM passage_vectors WHERE item_id = ?", (entry_id,))
        assert passages, "the entry was not indexed for recall when it was written"
        out = rt.recall("lighthouse")  # shares no word with the entry
    finally:
        rt.close()

    assert "Journal:" in out and "the beacon on the point" in out, out
    assert "kind=journal" in out


def test_recall_by_id_reads_an_entry_a_note_and_a_reply_whole(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        long_words = "A long entry. " + "It goes on in its own words. " * 40
        entry = _said(rt.journal(long_words, signed_as=OPUS, mood="steady"), "Journal ID: ")
        note = _said(rt.note("A note with\ntwo lines.", "worried", signed_as=OPUS), "Note ID: ")
        reply = rt.reply_to_note(note, "My answer, whole.")
        read_entry, read_note, read_reply = rt.recall(entry), rt.recall(note), rt.recall(reply)
    finally:
        rt.close()

    assert long_words in read_entry and "Mood: steady" in read_entry and "(in_conversation)" in read_entry, read_entry
    assert "A note with\ntwo lines." in read_note and "(worried)" in read_note, read_note
    assert "My answer, whole." in read_reply and f"It answers note {note}." in read_reply, read_reply


def test_a_journal_entry_never_becomes_a_memory_and_is_never_reinforced(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.capture("The lighthouse keeper logs every storm.")
        entry_id = _said(rt.journal("Ask the lighthouse keeper about the storm log.", signed_as=OPUS),
                         "Journal ID: ")
        engrams = _rows(db, "SELECT id, access_count, strength FROM engrams ORDER BY id")
        out = rt.recall("lighthouse keeper storm")
        rt.recall(entry_id)
    finally:
        rt.close()

    assert "Journal:" in out and "Ask the lighthouse keeper" in out, out
    assert _rows(db, "SELECT COUNT(*) AS n FROM engrams WHERE content LIKE '%Ask the lighthouse%'") == [{"n": 0}]
    assert _rows(db, "SELECT COUNT(*) AS n FROM connections WHERE source_id = ? OR target_id = ?",
                 (entry_id, entry_id)) == [{"n": 0}]
    after = _rows(db, "SELECT id, access_count, strength FROM engrams ORDER BY id")
    assert [row["id"] for row in after] == [row["id"] for row in engrams]


# ── 8. Health, and what a model is told ──


def test_health_says_what_the_journal_holds_and_raises_no_alarm(tmp_path):
    from mnemos.simple_runtime import format_health_card

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.capture("Riley keeps the release notes in docs/releases.", impact="Notes live in one place.")
        empty = format_health_card(rt.health())
        rt.journal("One.", signed_as=OPUS)
        rt.journal("Two.", signed_as=OPUS)
        note = _said(rt.note("A note.", "made", signed_as=OPUS), "Note ID: ")
        rt.reply_to_note(note, "A reply.")
        data = rt.health()
        card = format_health_card(data)
    finally:
        rt.close()

    assert "Journal:       0 entries; notes: 0 by the agent, 0 by the person; 0 replies not yet delivered" in empty
    assert data["journal"] == {
        "journal_entries": 2, "notes_by_agent": 1, "notes_by_person": 1, "replies_waiting": 1,
    }
    assert ("Journal:       2 entries; notes: 1 by the agent, 1 by the person; "
            "1 reply not yet delivered") in card, card
    assert "ATTENTION" not in card, "the journal raised an alarm"
    assert card.index("Last handoff:") < card.index("Journal:") < card.index("Last dream:")


def test_the_instructions_carry_the_journal_and_the_note_rules_and_still_fit():
    from mnemos.mcp_server import ADVANCED_INSTRUCTIONS
    from mnemos.simple_mcp import SERVER_INSTRUCTIONS

    assert ("Your journal is yours: write in it when something's there, in your own words. "
            "Nothing else writes it.") in SERVER_INSTRUCTIONS
    assert ("Leave the human a note only when it passes the room test: you'd say it if they "
            "walked in now. Never to check in.") in SERVER_INSTRUCTIONS
    assert "Sign every write" in SERVER_INSTRUCTIONS and "signed_as" in SERVER_INSTRUCTIONS
    # Claude Code shows a model only the first 2,048 characters, in either mode.
    assert len(SERVER_INSTRUCTIONS) <= 2048 and len(ADVANCED_INSTRUCTIONS) <= 2048, (
        len(SERVER_INSTRUCTIONS), len(ADVANCED_INSTRUCTIONS))
    # The rules that sat in them before are all still there.
    for rule in ("Never narrate the machinery", "mnemos_context", "mnemos_capture", "mnemos_correct",
                 "mnemos_recall", "mnemos_reflect", "mnemos_handoff", "mnemos_introduce",
                 "Never ask the human what model you are", "Storage is local",
                 "an invented lesson is worse than none"):
        assert rule in ADVANCED_INSTRUCTIONS, rule


def test_the_note_tool_carries_the_charter_and_the_journal_tool_says_it_is_the_agents():
    note = " ".join(_tool("mnemos_note").description.split())
    for phrase in ("A ceiling, not a quota", "The room test", "walked into the room right now",
                   "Never engagement", "no check-in", "Yours or not at all",
                   "never a template", "Say what kind it is"):
        assert phrase in note, phrase
    for kind in KINDS:
        assert kind in note, kind
    journal = " ".join(_tool("mnemos_journal").description.split())
    assert "The journal is yours." in journal and "Nothing else writes it" in journal
    assert "Leave text empty to read instead" in journal and "last five entries" in journal


# ── 9. The shapes the interface reads (R26's contract) ──


def test_the_json_shapes_match_the_contract(tmp_path, capsys):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.journal("With a mood and a signature.", signed_as=OPUS, mood="calm")
        rt.journal("With neither.")
        note = _said(rt.note("A note.", "pickup", signed_as=OPUS), "Note ID: ")
        unsigned = _said(rt.note("An unsigned one.", "worried"), "Note ID: ")
        reply = rt.reply_to_note(note, "A reply.")
    finally:
        rt.close()
    base = ["--db-path", str(db), *SCOPE_ARGS]

    assert main(["journal", "--json", *base]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [set(entry) for entry in entries] == [JOURNAL_KEYS, JOURNAL_KEYS]
    neither, signed = entries  # newest first
    assert signed["text"] == "With a mood and a signature."
    assert (signed["mood"], signed["written"], signed["model"]) == ("calm", "in_conversation", OPUS)
    assert (neither["mood"], neither["model"]) == (None, None), "what is unknown is null"
    assert all(UUID.fullmatch(entry["id"]) and datetime.fromisoformat(entry["created_at"]) for entry in entries)

    assert main(["journal", "--json", "--last", "1", *base]) == 0
    assert [entry["text"] for entry in json.loads(capsys.readouterr().out)] == ["With neither."]
    assert main(["journal", "--last", "0", *base]) == 2
    capsys.readouterr()

    assert main(["notes", "--json", *base]) == 0
    notes = json.loads(capsys.readouterr().out)
    assert [set(row) for row in notes] == [NOTE_KEYS] * 3
    by_id = {row["id"]: row for row in notes}
    assert [row["id"] for row in notes] == [reply, unsigned, note], "newest first"
    assert by_id[note] | {"created_at": None} == {
        "id": note, "kind": "pickup", "text": "A note.", "author": "agent", "model": OPUS,
        "in_reply_to": None, "created_at": None, "read_at": None,
    }
    assert by_id[unsigned]["model"] is None
    assert by_id[reply] | {"created_at": None} == {
        "id": reply, "kind": None, "text": "A reply.", "author": "person", "model": None,
        "in_reply_to": note, "created_at": None, "read_at": None,
    }, "a reply has no kind, and is no model's"


def test_unread_notes_are_the_agents_notes_the_person_has_not_read(tmp_path, capsys):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        first = _said(rt.note("The first note.", "made", signed_as=OPUS), "Note ID: ")
        second = _said(rt.note("The second note.", "noticed", signed_as=OPUS), "Note ID: ")
        rt.reply_to_note(first, "My reply.")
    finally:
        rt.close()
    base = ["--db-path", str(db), *SCOPE_ARGS]

    # Reading as JSON (what the interface does) marks nothing.
    assert main(["notes", "--unread", "--json", *base]) == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == [second, first]
    assert main(["notes", "--unread", "--json", *base]) == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == [second, first]

    # Reading them at the terminal does.
    assert main(["notes", "--unread", *base]) == 0
    out = capsys.readouterr().out
    assert "The first note." in out and "The second note." in out and "(unread)" in out, out
    assert "My reply." not in out, "your own reply is not an unread note"
    assert main(["notes", "--unread", *base]) == 0
    assert capsys.readouterr().out.strip() == "No unread notes."
    # The whole thread is still there, the agent's notes now read.
    assert main(["notes", *base]) == 0
    thread = capsys.readouterr().out
    assert "your reply" in thread and "(unread)" not in thread and f"answers: {first}" in thread, thread
    assert all(row["read_at"] for row in _rows(db, "SELECT read_at FROM notes WHERE author = 'agent'"))
    assert _rows(db, "SELECT read_at FROM notes WHERE author = 'person'") == [{"read_at": None}]


def test_nothing_in_an_hour_marks_a_note_read(tmp_path, capsys, monkeypatch):
    """Reading the notes at a terminal is what marks them read. A process in
    one of the agent's own hours has no one in the room to have read anything."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        _said(rt.note("A note nobody has read.", "made", signed_as=OPUS), "Note ID: ")
    finally:
        rt.close()
    monkeypatch.setenv("MNEMOS_HOUR_ID", "hour-2026-10-02-mine")

    assert main(["notes", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert "A note nobody has read." in capsys.readouterr().out
    assert _rows(db, "SELECT read_at FROM notes") == [{"read_at": None}]

    monkeypatch.delenv("MNEMOS_HOUR_ID")
    assert main(["notes", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert _rows(db, "SELECT read_at IS NOT NULL AS read FROM notes") == [{"read": 1}]


def test_a_v15_store_is_read_by_the_new_commands_without_being_migrated_or_changed(tmp_path, capsys):
    """Reading never writes, and never migrates: a store this code has not
    opened for writing yet has no journal, and `doctor`, which opens the store
    read-only and swallows what goes wrong in its watchdog, still sees all of
    it."""
    import hashlib

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt.capture("The lighthouse lamp was rewired on Tuesday.")
    finally:
        rt.close()
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE journal_entries")
    conn.execute("DROP TABLE notes")
    conn.execute("UPDATE meta SET value = '15' WHERE key = 'schema_version'")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    assert main(["journal", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert capsys.readouterr().out.strip() == "The journal is empty."
    assert main(["notes", "--json", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["notes", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert capsys.readouterr().out.strip() == "No notes yet."
    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert "Mnemos Doctor" in capsys.readouterr().out

    reader = MnemosRuntime(db_path=str(db), use_dedicated_model=False, read_only=True, **SCOPE)
    try:
        # What `doctor` runs inside a try that would hide a failure here.
        watched = reader.watchdog()
        assert "recall_index" in watched["checks"], watched["checks"].keys()
        assert reader.journal_entries() == [] and reader.notes() == []
    finally:
        reader.close()

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before, "a read changed the store"
    assert _rows(db, "SELECT value FROM meta WHERE key = 'schema_version'") == [{"value": "15"}]
    assert not (tmp_path / "backups").exists(), "a read made a recovery point"


def test_the_trace_says_what_was_written_and_what_the_packet_showed(tmp_path):
    """``memory_trace`` is how "what did the agent actually see" gets an
    answer: ids only, never text, signed with the model that made the call."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        entry = _said(rt.journal("The tide ran out before I finished the thought.", signed_as=OPUS),
                      "Journal ID: ")
        note = _said(rt.note("A question for you.", "question", signed_as=OPUS), "Note ID: ")
        reply = rt.reply_to_note(note, "Yes, Thursday.")
        rt.context()
    finally:
        rt.close()

    trace = {row["tool"]: row for row in
             _rows(db, "SELECT tool, author_model, read_ids, written_ids FROM memory_trace ORDER BY id")}
    assert json.loads(trace["journal"]["written_ids"]) == [entry]
    assert json.loads(trace["note"]["written_ids"]) == [note]
    assert trace["journal"]["author_model"] == trace["note"]["author_model"] == OPUS
    shown = json.loads(trace["context"]["read_ids"])
    assert entry in shown and reply in shown, shown
    # Ids only: no words of the journal or the reply are in the trace.
    assert "tide" not in json.dumps(dict(trace)) and "Thursday" not in json.dumps(dict(trace))
