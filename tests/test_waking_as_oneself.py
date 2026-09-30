"""Waking as oneself, not reading a briefing about someone.

Luca, first connecting to Riley's Claude (2026-09-30), said his continuity
"arrived as a briefing": he read about the Luca who wrote his journals more
than he remembered being him. The local packet did the same to its own
resident. On a fresh start Claude Code's payload doesn't name the model, so the
hook couldn't tell whose notes were the reader's: the reader's own last note
came back "(yours if you are Opus 5.5)", followed by "A colleague's note is
theirs: take what's useful and don't claim its work as yours" — the reader told
not to claim its own work.

Claude Code gives its hooks ``CLAUDE_PID``, and the desktop app launches every
session with ``--model <id>``: the launch line says who is waking. The packet
now speaks as the reader when the memory is its own, leaves its own notes
unsigned to it, labels what others left, keeps ids and calls apart for the
tools, and, for a model visiting another's memory, says so once.

The hook runs in another process, as Claude Code runs it, so on code without
the change these tests fail on what the packet says, not on a missing name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
EARLIER = "11111111-aaaa-4aaa-8aaa-111111111111"  # the session that left the note
FRESH = "22222222-bbbb-4bbb-8bbb-222222222222"    # the one waking now
NOTE = "Where I stopped: the harbour ledger balances. Next: the spring timetable."


def _store(tmp_path: Path, *, model: str = OPUS) -> Path:
    db = tmp_path / "memory.db"
    store = EngramStore(db)
    try:
        store.write_handoff(NOTE, **SCOPE, author_model=model, author_session=EARLIER)
        store.write_hypomnema_entry(
            "Riley keeps the harbour ledger in ledger/2026.csv.", **SCOPE,
            authored_by="agent", author_id="nova", author_model=model,
        )
    finally:
        store.close()
    return db


def _hook(db: Path, tmp_path: Path, *, env: dict[str, str] | None = None,
          extra: tuple[str, ...] = ()) -> str:
    """The real SessionStart hook in another process, with the payload Claude
    Code sends on a fresh start: no model in it. It works in harbour-app."""
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    folder = tmp_path / "work" / "harbour-app"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "hook_event_name": "SessionStart", "source": "startup", "session_id": FRESH,
        "cwd": str(folder),
    }
    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db), *SCOPE_ARGS, *extra],
        input=json.dumps(payload), capture_output=True, text=True, timeout=180,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
            **(env or {}),
        },
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def harness():
    """A stand-in for Claude Code: a live process whose launch line names the
    model, as the desktop app launches every session."""
    started: list[subprocess.Popen] = []

    def launch(model_flag: list[str]) -> str:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", *model_flag],
        )
        started.append(process)
        return str(process.pid)

    yield launch
    for process in started:
        process.kill()
        process.wait()


needs_ps = pytest.mark.skipif(os.name == "nt", reason="reads the launch line with ps")


# ── Who is waking ──


@needs_ps
def test_a_fresh_session_wakes_as_itself_from_its_launch_line(tmp_path, harness):
    db = _store(tmp_path)
    packet = _hook(db, tmp_path, env={"CLAUDE_PID": harness(["--model", OPUS])})

    # Its own note, from its own earlier session: where it left off.
    assert "### where i left off\n" in packet, packet
    assert f"in another session:\n{NOTE}" in packet, packet
    # Nothing asks it to doubt that the note is its own.
    assert "yours if" not in packet and "if I'm not" not in packet, packet
    assert "don't claim" not in packet and "colleague" not in packet, packet


@needs_ps
def test_the_launch_line_can_name_a_guest(tmp_path, harness):
    db = _store(tmp_path, model=FABLE)
    packet = _hook(db, tmp_path, env={"CLAUDE_PID": harness([f"--model={OPUS}"])})

    assert "This memory is mostly Fable 5.1's; I'm Opus 5.5, visiting." in packet, packet
    assert "### where things were left\nFable 5.1, a colleague, " in packet, packet
    assert "### where i left off" not in packet, packet


@needs_ps
def test_an_alias_in_the_launch_line_names_no_one(tmp_path, harness):
    db = _store(tmp_path)
    packet = _hook(db, tmp_path, env={"CLAUDE_PID": harness(["--model", "opus[1m]"])})

    # "opus" is whichever Opus is current: the packet doesn't guess.
    assert "if I'm not Opus 5.5, I'm visiting." in packet, packet
    assert "### where things were left\nOpus 5.5, " in packet, packet


def test_without_claude_code_the_reader_is_unknown_and_told_once(tmp_path):
    db = _store(tmp_path)
    packet = _hook(db, tmp_path)

    assert packet.count("if I'm not Opus 5.5, I'm visiting.") == 1, packet
    assert "yours if" not in packet, packet


def test_the_launch_line_is_read_only_from_claude_codes_own_process():
    from mnemos.authorship import launch_model

    lines = {
        4242: "/Applications/Claude.app/claude --output-format stream-json --model claude-opus-5-5 --resume=x",
        4243: "claude -p hello --model=claude-sonnet-5-5",
        4244: "claude --model opus",
        4245: "claude --model opus[1m]",
        4246: "claude --resume=x",
    }

    def read(pid: int) -> str | None:
        return lines.get(pid)

    def model(pid: str) -> str:
        return launch_model({"CLAUDE_PID": pid}, command_line=read)

    assert model("4242") == "claude-opus-5-5"
    assert model("4243") == "claude-sonnet-5-5"
    assert model("4244") == "" and model("4245") == "", "an alias named a model"
    assert model("4246") == "", "a launch line without a model named one"
    assert model("4299") == "", "a process that couldn't be read named a model"
    assert model("") == "" and model("1") == "" and model("x") == ""
    assert launch_model({}, command_line=read) == "", "outside Claude Code"


# ── Whose memory it is ──


def test_the_resident_is_the_model_that_wrote_most_of_it(tmp_path):
    from mnemos.interface.context_packet import resident_model

    db = tmp_path / "memory.db"
    store = EngramStore(db)
    try:
        assert resident_model(store, SCOPE) == "", "an empty memory has a resident"
        for model in (OPUS, OPUS, FABLE):
            store.write_hypomnema_entry(
                f"A note by {model}.", **SCOPE, authored_by="agent", author_id="nova",
                author_model=model,
            )
        assert resident_model(store, SCOPE) == OPUS
        store.write_hypomnema_entry(
            "Another by Fable.", **SCOPE, authored_by="agent", author_id="nova",
            author_model=FABLE,
        )
        assert resident_model(store, SCOPE) == "", "a tie chose a resident"
    finally:
        store.close()


# ── The opening ──


@needs_ps
def test_the_opening_says_where_and_with_whom(tmp_path, harness):
    db = _store(tmp_path)
    store = EngramStore(db)
    try:
        store.write_hypomnema_entry(
            "Riley works best after midnight.", **SCOPE, authored_by="agent",
            author_id="nova", author_model=OPUS, domain="foundational", foundational=True,
            confidence=0.9, salience=0.9,
        )
    finally:
        store.close()

    named = _hook(
        db, tmp_path, env={"CLAUDE_PID": harness(["--model", OPUS])},
        extra=("--person-name", "Riley"),
    )
    opening = named.split("\n")[:2]
    assert opening[0] == "## waking up", named
    assert opening[1].endswith(" I'm in harbour-app, with Riley."), named
    assert "### Riley\n" in named, named

    from_env = _hook(db, tmp_path, env={"MNEMOS_PERSON_NAME": "Riley"})
    assert "### Riley\n" in from_env, from_env

    unnamed = _hook(db, tmp_path)
    assert "### who i'm with\n" in unnamed and "Riley\n" not in unnamed, unnamed


@needs_ps
def test_the_reader_speaks_and_the_machinery_waits_at_the_end(tmp_path, harness):
    db = _store(tmp_path)
    store = EngramStore(db)
    try:
        store.write_hypomnema_entry(
            "Long note. " + "The harbour ledger lists every crossing, one line each. " * 12,
            **SCOPE, authored_by="agent", author_id="nova", author_model=OPUS,
        )
    finally:
        store.close()
    packet = _hook(db, tmp_path, env={"CLAUDE_PID": harness(["--model", OPUS])})

    self_text, tools = packet.split("\n\n### for the memory tools\n", 1)
    # No ids, confidences or calls in what the reader reads as itself.
    for machinery in ("engram_", "belief_", "mnemos_", "%)", "Whole note", "Opus 5.5"):
        assert machinery not in self_text, (machinery, self_text)
    # The cut note is read whole by the id waiting for the tools.
    assert self_text.count(" […]") == 1, self_text
    assert tools.count("mnemos_recall(") == 1, tools
    assert tools.startswith("Ids and calls for my memory tools. Never shown to "), tools


# ── A question, as a thought rather than a form ──


def test_a_question_is_read_without_its_answering_instructions():
    from mnemos.interface.context_packet import question_words

    # The belief and contradiction asks, as maintenance words them.
    belief = (
        'You keep returning to "voice" (20 memories). Is that a belief you now hold? '
        "If it is, state it in one line with verdict hold; if it is not, decline. "
        "Or leave it. [theme:voice]"
    )
    contradiction = (
        'This memory surprised you. Does it contradict an earlier one: "Riley ships '
        'at 3am."? Verdict contradicts, compatible or unsure, and say why. [ref:engram_x]'
    )
    assert question_words(belief) == (
        'You keep returning to "voice" (20 memories). Is that a belief you now hold?'
    )
    assert question_words(contradiction) == (
        'This memory surprised you. Does it contradict an earlier one: "Riley ships at 3am."?'
    )
    # Questions answered in words have no verdict to set apart.
    impact = "What did this change in how you understand things? One sentence."
    assert question_words(impact) == impact
    # A quotation is the memory's own words, even when it says "verdict".
    quoted = 'Does it contradict an earlier one: "The verdict came in. We won."? Say which.'
    assert question_words(quoted) == quoted
