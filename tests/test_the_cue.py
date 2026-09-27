"""Experience that comes to the cue (WP-R16).

In the lab, with a notebook present, the agent searched Mnemos about four
times a question instead of seven, and lessons for new situations fell from
82% to 35%. Telling it to search for what it learned changed nothing (L05c).
So a UserPromptSubmit hook, ``mnemos hook prompt``, brings the memories that
may bear on each message to the model: at most three, one line each, behind a
relevance gate, never twice in a session, never counted as a use, fast with
meaning from the session's own warm server and from words alone without it.

None of these tests needs sentence-transformers or torch: a fake model gives
each text a vector by the concepts it names, so every cosine is exactly
controlled. Sockets live under a HOME short enough for a unix socket path.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.embedding_index import EmbeddingIndex
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore


class _Lazy:
    """The module under test, imported where a test first uses it, so that on
    code without it each test fails by itself, at the line that needs it,
    rather than the whole file failing to import (the fail-on-base proof)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        import importlib

        return getattr(importlib.import_module(self._name), attr)


cue = _Lazy("mnemos.cue")


def cue_memories(*args, **kwargs):
    from mnemos.simple_runtime import cue_memories as query

    return query(*args, **kwargs)

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
MODEL = "claude-opus-5-5"


# ── A model whose meaning is controlled ──

_CONCEPTS = (
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
    {"garden", "marigolds", "greenhouse", "bloom"},
)


def concept_vector(text: str) -> list[float]:
    """Counts of each concept's words, and a little of nothing in particular:
    "beacon" is near "lighthouse" without sharing a word."""
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in _CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


class _Vector(list):
    def tolist(self):
        return list(self)


class _ConceptModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector(concept_vector(texts))
        return [_Vector(concept_vector(text)) for text in texts]


class _ConceptEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _ConceptModel()
        return self._model


@pytest.fixture
def meaning(monkeypatch):
    """A working local backend, for every index made while the test runs."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME in the test's folder, reached by a path short enough for a unix
    socket under it (a symlink in /tmp when the test's folder is too deep)."""
    real = tmp_path / "home"
    real.mkdir()
    link = None
    probe = real / ".mnemos" / "run" / "cue-4194304-0123456789ab.sock"
    path = real
    if len(os.fsencode(str(probe))) >= 100:
        link = Path("/tmp") / f"mnq-{uuid.uuid4().hex[:10]}"
        os.symlink(real, link)
        path = link
    monkeypatch.setenv("HOME", str(path))
    yield path
    if link is not None:
        os.unlink(link)


# The message every test sends, and what the fake model makes of it: two
# lighthouse words. Its distinctive words are lighthouse, keeper, storm,
# shutters, close and tonight; "fog" is searchable but too short to be one.
MESSAGE = "The lighthouse keeper asked whether the storm shutters close tonight in the fog"


def _memory(content: str, **fields) -> Engram:
    return Engram(content=content, kind=fields.pop("kind", "semantic"), owner_agent_id="nova",
                  person_id="riley", project_scope="demo", **fields)


def _store(db: Path, *memories: Engram) -> Path:
    """A store holding ``memories``, each findable by meaning (the fake model)."""
    store = EngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db))
    for engram in memories:
        store.save_engram(engram)
        index.index_engram(engram.id, engram.content)
    index.close()
    store.close()
    return db


def _cue(db: Path, text: str = MESSAGE, *, meaning_on: bool = True, **kwargs) -> list[dict]:
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True) if meaning_on else None
    try:
        return cue_memories(store, index, text, **SCOPE, **kwargs)
    finally:
        store.close()


# The gate's four cases, each with a cosine the fake model fixes exactly.
def _gate_memories() -> dict[str, Engram]:
    return {
        # Meaning alone: two lighthouse words, none of the message's (cos 1.00).
        "near": _memory("Trim the lamp and the beacon wick each evening."),
        # A distinctive word it shares ("keeper") and cos 0.32: a lesson the agent wrote.
        "worded": _memory("The keeper met the ferry at the pier by the boat.",
                          impact="Meet the ferry early when a storm is coming.",
                          impact_source="agent"),
        # Found by "fog" (not distinctive), cos 0.32: under the floor.
        "under": _memory("Fog rolled over the ferry, the pier, the boat and the beacon."),
        # Shares "keeper", but cos 0.24: under the word path's floor too.
        "faint": _memory("The keeper read the ferry timetable on the pier by the boat."),
        # Nothing to do with it.
        "garden": _memory("The marigolds in the greenhouse bloom in June."),
    }


# ── The gate and the cap ──


def test_the_gate_offers_only_what_clears_the_floor_or_a_distinctive_word(tmp_path, meaning):
    """At the starting floor (0.40): the memory near in meaning, and the one
    sharing a distinctive word at cosine 0.32. Not the one at 0.32 found only by
    a common short word, nor the one sharing a word at 0.24, nor the unrelated.
    At 0.30 the one under the floor comes too."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    names = {engram.id: name for name, engram in found.items()}

    offered = [names[line["id"]] for line in _cue(db)]
    assert offered == ["worded", "near"]
    assert [names[line["id"]] for line in _cue(db, floor=0.30)] == ["worded", "near", "under"]

    lines = {names[line["id"]]: line for line in _cue(db, floor=0.30)}
    assert lines["near"]["similarity"] == pytest.approx(1.0, abs=0.01)
    assert lines["worded"]["similarity"] == pytest.approx(0.316, abs=0.01)
    assert lines["worded"]["shared"] == ["keeper", "storm"]
    assert lines["under"]["shared"] == []
    assert cue.CUE_FLOOR == 0.40 and cue.CUE_WORD_FLOOR == 0.25


def test_at_most_three_lines_lessons_first_each_one_line_under_200(tmp_path, meaning):
    long_words = " ".join([
        "The lamp room at the top of the lighthouse holds the spare beacon and the keeper's log.",
        "Every entry since the spring storms is written in pencil, because ink runs in the damp.",
        "The keeper wants the log copied before the autumn inspection comes round again this year.",
    ])
    memories = [
        _memory("Trim the lamp and the beacon wick each evening."),
        _memory(long_words),
        _memory("The lamp keeper tends the beacon."),
        _memory("A beacon lamp for the keeper's lighthouse.",
                impact="A spare lamp saved the night the main one failed.", impact_source="agent"),
        _memory("Lighthouse lamp beacon keeper.", tags=["lesson", "distilled"]),
    ]
    db = _store(tmp_path / "memory.db", *memories)

    lines = _cue(db)
    assert len(lines) == 3
    assert [line["lesson"] for line in lines] == [True, True, False]
    assert {lines[0]["text"], lines[1]["text"]} == {
        "A spare lamp saved the night the main one failed.",  # the agent's impact
        "Lighthouse lamp beacon keeper.",  # a lesson's own words
    }

    everything = _cue(db, limit=10)
    cut = next(line for line in everything if line["id"] == memories[1].id)
    assert len(cut["text"]) < cue.CUE_LINE_CHARS
    assert cut["text"].endswith("in the damp. […]"), cut["text"]
    assert cut["date"] == memories[1].created_at[:10]

    block = cue.format_block(lines)
    rows = block.splitlines()
    assert rows[0] == cue.HEADING and len(rows) == 4
    assert rows[1] == f"- {lines[0]['date']}, lesson: {lines[0]['text']} ({lines[0]['id']})"
    assert all(line["id"] in row for line, row in zip(lines, rows[1:]))


def test_the_same_words_come_once_under_the_memory_they_were_drawn_from(tmp_path, meaning):
    """A lesson copies its memory's impact word for word. Both match; the line
    comes once, under the memory's id, which recalled gives the lesson and
    what happened."""
    lesson_words = "Check the lamp before the storm, every time."
    story = _memory("The keeper found the lighthouse lamp dead just as the storm came in.",
                    impact=lesson_words, impact_source="agent")
    drawn = _memory(lesson_words, tags=["lesson", "distilled"])
    db = _store(tmp_path / "memory.db", drawn, story)

    lines = _cue(db, limit=10)
    assert [line["text"] for line in lines].count(lesson_words) == 1
    assert next(line["id"] for line in lines if line["text"] == lesson_words) == story.id


def test_standing_memories_never_come_to_the_cue(tmp_path, meaning):
    standing = _memory("Trim the lamp and the beacon wick each evening.")
    db = _store(tmp_path / "memory.db", standing, _memory("The lamp keeper tends the beacon."))
    store = EngramStore(str(db))
    store.set_standing(standing.id, True, by=MODEL)
    store.close()

    assert standing.id not in [line["id"] for line in _cue(db)]
    assert _cue(db), "the other memory still comes"


def test_messages_with_fewer_than_four_content_words_get_nothing(tmp_path, meaning, home):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())

    for short in ("ok", "yes", "beautiful", "sounds good to me", "thanks, lighthouse keeper!"):
        assert _cue(db, short) == []
        assert _hook(db, short) == ""
    assert _hook(db, "lighthouse keeper storm shutters") != ""


# ── The hook ──


def _hook(db: Path, prompt: str, session: str = "session-one", **environ) -> str:
    payload = {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": prompt,
               "cwd": "/tmp", "transcript_path": "/tmp/t.jsonl"}
    return cue.prompt_hook(payload, db_path=str(db), environ=environ, **SCOPE)


def _seen(session: str, db: Path) -> dict:
    return cue.SeenFile(session, cue.scope_key(str(db), **SCOPE)).read()


def test_the_hook_prints_one_block_and_records_it_as_offered(tmp_path, meaning, home):
    """Without an answerer the hook answers from words: a memory sharing two
    distinctive words with the message. The block is recorded as offered."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())

    block = _hook(db, MESSAGE)

    assert block.splitlines()[0] == cue.HEADING
    assert found["worded"].id in block and "Meet the ferry early when a storm is coming." in block
    assert found["near"].id not in block  # meaning only: words alone can't see it
    seen = _seen("session-one", db)
    assert seen["offered"] == 1 and seen["offers"][0]["via"] == "words"
    assert seen["offers"][0]["ids"] == [found["worded"].id]


def test_never_twice_in_a_session(tmp_path, meaning, home):
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())

    first = _hook(db, MESSAGE)
    again = _hook(db, MESSAGE + " again, the keeper and the storm")
    elsewhere = _hook(db, MESSAGE, session="session-two")

    assert found["worded"].id in first
    assert again == "", "what this session was shown is not shown again"
    assert found["worded"].id in elsewhere, "another session (or one after /clear) starts fresh"
    assert _seen("session-one", db)["shown"] == [found["worded"].id]


def test_what_the_briefing_showed_is_never_offered_in_that_session(tmp_path, home, monkeypatch):
    """The session-start hook records what its briefing showed: a capture's
    note, and the memory it is one object with. The cue then leaves that
    memory out of the session; another session is offered it."""
    db = tmp_path / "memory.db"
    rt = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    said = rt.capture("The lighthouse keeper closes the storm shutters before the fog comes in.",
                      signed_as=MODEL)
    rt.close()
    memory_id = re.search(r"(engram_[A-Za-z0-9]+)", said).group(1)
    payload = json.dumps({"session_id": "briefed-session", "hook_event_name": "SessionStart",
                          "source": "startup", "model": MODEL, "cwd": str(tmp_path)})

    started = _run_hook(db, "session-start", payload)
    assert started.returncode == 0 and b"lighthouse keeper closes" in started.stdout

    seen = _seen("briefed-session", db)
    assert memory_id in seen["shown"], seen
    assert seen["offered"] == 0, "the briefing showing something is not an offer"
    assert _hook(db, MESSAGE, session="briefed-session") == ""
    assert memory_id in _hook(db, MESSAGE, session="another-session")


def test_what_the_briefings_graph_showed_is_never_offered_in_that_session(tmp_path, meaning, home):
    """With --include-graph the briefing also shows long-term graph recall for
    its cue. What that section showed is recorded with the rest: the cue never
    offers it again in that session; another session is offered it."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    payload = json.dumps({"session_id": "graph-session", "hook_event_name": "SessionStart",
                          "source": "startup", "model": MODEL, "cwd": str(tmp_path)})

    started = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start", "--db-path", str(db),
         *SCOPE_ARGS, "--include-graph", "--query", "the keeper met the ferry at the pier"],
        input=payload.encode(), capture_output=True, timeout=60,
        env=_env(Path(os.environ["HOME"])),
    )

    assert started.returncode == 0, started.stderr
    assert b"Meet the ferry early when a storm is coming." in started.stdout, started.stdout
    assert found["worded"].id in _seen("graph-session", db)["shown"]
    assert _hook(db, MESSAGE, session="graph-session") == ""
    assert found["worded"].id in _hook(db, MESSAGE, session="another-session")


def test_offered_is_not_used_nothing_is_reinforced(tmp_path, meaning, home):
    """The cue reads. Offering a memory changes nothing about it: not its
    strength, access count or state, no link, no version, no reinforcement."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    before = _state(db)

    block = _hook(db, MESSAGE)
    answerer = _answerer(db)
    try:
        by_meaning = _hook(db, MESSAGE, session="warm-session", CLAUDE_PID=str(os.getpid()))
    finally:
        answerer.stop()

    assert block and by_meaning
    assert _state(db) == before
    assert _seen("session-one", db)["offered"] == 1
    assert _seen("warm-session", db)["offers"][0]["via"] == "meaning"


def _state(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "engrams": conn.execute(
                "SELECT id, state, strength, stability, accessibility, access_count, "
                "last_accessed, content, impact FROM engrams ORDER BY id").fetchall(),
            "connections": conn.execute("SELECT COUNT(*) FROM connections").fetchone(),
            "reinforced": conn.execute("SELECT COUNT(*) FROM session_reinforcements").fetchone(),
            "versions": conn.execute("SELECT COUNT(*) FROM versions").fetchone(),
            "meta": conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
        }
    finally:
        conn.close()


def test_a_store_newer_than_the_hook_gets_nothing_printed(tmp_path, meaning, home):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    store = EngramStore(str(db))
    store.raise_min_code_version(MAINTENANCE_CODE_VERSION + 1)
    store.close()

    assert _hook(db, MESSAGE) == ""


# ── The warm answerer ──


def _answerer(db: Path, claude_pid: int | None = None) -> cue.CueAnswerer:
    """A warm answerer, as a server starts one where the prompt hook is in use."""
    cue.mark_hook_in_use()
    answerer = cue.CueAnswerer(str(db), claude_pid=claude_pid or os.getpid(), **SCOPE)
    assert answerer.start()
    assert answerer.ready.wait(10), "the answerer never came up"
    assert answerer.warm.is_set(), "where the hook is in use, the model loads first"
    return answerer


def test_the_warm_answerer_answers_on_a_private_socket(tmp_path, meaning, home):
    """Found by the Claude Code process's pid: the socket is 0600 in a 0700
    folder under ~/.mnemos/run, and it answers with meaning, which finds the
    memory words alone cannot."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    answerer = _answerer(db)
    try:
        path = answerer.path
        assert path == Path(str(home)) / ".mnemos" / "run" / f"cue-{os.getpid()}-{answerer.key}.sock"
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700

        reply = cue.ask_answerer(answerer.key, MESSAGE, environ={"CLAUDE_PID": str(os.getpid())})
        assert reply is not None and reply["v"] == cue.CUE_PROTOCOL
        assert [line["id"] for line in reply["lines"]] == [found["worded"].id, found["near"].id]

        block = _hook(db, MESSAGE, CLAUDE_PID=str(os.getpid()))
        assert found["near"].id in block, "meaning found it"
        assert _seen("session-one", db)["offers"][0]["via"] == "meaning"
    finally:
        answerer.stop()
    assert not path.exists(), "a stopped answerer takes its socket with it"


def test_an_answerer_never_takes_a_live_socket_and_replaces_a_dead_one(tmp_path, meaning, home):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    first = _answerer(db, claude_pid=4242)
    try:
        second = cue.CueAnswerer(str(db), claude_pid=4242, **SCOPE)
        second.start()
        second.thread.join(10)
        assert not second.ready.is_set(), "a live answerer keeps its socket"
        assert first.path.exists() and cue._answers(str(first.path))
    finally:
        first.stop()

    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(first.path))
    dead.close()  # the file stays, and nothing listens: a server that crashed
    third = _answerer(db, claude_pid=4242)
    try:
        assert cue._answers(str(third.path))
    finally:
        third.stop()


def test_where_the_hook_is_not_in_use_the_model_loads_only_when_a_cue_asks(tmp_path, meaning, home):
    """A server where the prompt hook never ran (it is off by default) loads no
    model at start: the socket opens, and the model loads when the first cue
    asks. That cue, and any before the model is ready, are answered from
    words by the hook; after that, by meaning."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    made = []

    def factory(path):
        made.append(path)
        return EmbeddingIndex(db_path=path, read_only=True)

    assert not cue.hook_in_use()
    answerer = cue.CueAnswerer(str(db), claude_pid=os.getpid(), index_factory=factory, **SCOPE)
    assert answerer.start()
    try:
        assert answerer.ready.wait(10)
        time.sleep(0.2)
        assert made == [] and not answerer.warm.is_set(), "no model at start"

        first = _hook(db, MESSAGE, CLAUDE_PID=str(os.getpid()))
        assert found["near"].id not in first, "the first cue is answered from words"
        assert _seen("session-one", db)["offers"][0]["via"] == "words"
        assert answerer.warm.wait(10) and len(made) == 1, "the first cue started the model"

        later = _hook(db, MESSAGE, session="session-two", CLAUDE_PID=str(os.getpid()))
        assert found["near"].id in later
        assert _seen("session-two", db)["offers"][0]["via"] == "meaning"
        assert cue.hook_in_use(), "the hook marked itself in use"
    finally:
        answerer.stop()


def _fake_answerer(path: Path, reply: bytes | None) -> threading.Thread:
    """Something listening at ``path`` that answers ``reply`` to anything, or
    never answers at all (None)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(5)

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            if reply is None:
                time.sleep(1)
            else:
                conn.sendall(reply)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("reply", [
    None,  # slow: never answers
    json.dumps({"v": 99, "ok": True, "lines": [  # a protocol this code doesn't speak
        {"id": "engram_FROMANEWERSERVER", "text": "x", "date": "2026-09-27", "key": "k"}]}).encode() + b"\n",
    b"not json\n",
])
def test_the_hook_answers_from_words_when_the_answerer_is_slow_or_speaks_another_protocol(
    tmp_path, meaning, home, reply,
):
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    key = cue.scope_key(str(db), **SCOPE)
    thread = _fake_answerer(cue.answerer_path(os.getpid(), key), reply)

    started = time.monotonic()
    block = _hook(db, MESSAGE, CLAUDE_PID=str(os.getpid()))
    took = time.monotonic() - started
    thread.join(5)

    assert found["worded"].id in block and "FROMANEWERSERVER" not in block
    assert _seen("session-one", db)["offers"][0]["via"] == "words"
    assert took < cue.CONNECT_SECONDS + cue.ANSWER_SECONDS + 0.3, took


def test_the_hook_answers_from_words_when_no_answerer_is_there(tmp_path, meaning, home):
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())

    block = _hook(db, MESSAGE, CLAUDE_PID="999999")

    assert found["worded"].id in block
    assert _seen("session-one", db)["offers"][0]["via"] == "words"


def test_a_server_that_cannot_start_its_answerer_runs_the_same(monkeypatch):
    import mnemos.simple_mcp as simple_mcp

    def broken(*args, **kwargs):
        raise RuntimeError("no sockets today")

    monkeypatch.setattr("mnemos.cue.CueAnswerer", broken)
    assert simple_mcp.start_cue_answerer() is None


# ── Silence on every failure, and the hard cap ──


def _env(home: Path, **extra) -> dict[str, str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
           "PYTHONPATH": ":".join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


def _run_hook(db: Path, which: str, payload: str | bytes, *, scope_first: bool = False,
              **extra) -> subprocess.CompletedProcess:
    """The hook as Claude Code runs it: its own process, the payload on stdin.
    The scope goes after the subcommand, as the installer writes it, or before
    it (``scope_first``), which the prompt hook takes too."""
    data = payload if isinstance(payload, bytes) else payload.encode()
    scope = ["--db-path", str(db), *SCOPE_ARGS]
    command = [*scope, "hook", which] if scope_first else ["hook", which, *scope]
    return subprocess.run(
        [sys.executable, "-m", "mnemos.cli", *command],
        input=data, capture_output=True, timeout=60, env=_env(Path(os.environ["HOME"]), **extra),
    )


def _prompt_payload(prompt: str = MESSAGE, **fields) -> str:
    return json.dumps({"session_id": "silent-session", "hook_event_name": "UserPromptSubmit",
                       "prompt": prompt, **fields})


@pytest.mark.parametrize("payload", [
    b"{not json",
    b"[1, 2, 3]",
    b"",
    json.dumps({"hook_event_name": "Stop", "prompt": MESSAGE}).encode(),
    json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": ["not", "text"]}).encode(),
])
def test_a_payload_it_does_not_expect_prints_nothing(tmp_path, meaning, home, payload):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    done = _run_hook(db, "prompt", payload)
    assert (done.returncode, done.stdout) == (0, b"")


def test_errors_print_nothing_and_never_create_a_store(tmp_path, home):
    missing = tmp_path / "nowhere.db"
    done = _run_hook(missing, "prompt", _prompt_payload())
    assert (done.returncode, done.stdout) == (0, b"")
    assert not missing.exists()

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database" * 100)
    done = _run_hook(garbage, "prompt", _prompt_payload())
    assert (done.returncode, done.stdout) == (0, b"")


def test_an_error_inside_prints_nothing_in_process_too(tmp_path, meaning, home, monkeypatch, capsys):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    payload = tmp_path / "payload.json"
    payload.write_text(_prompt_payload())

    def boom(*args, **kwargs):
        raise RuntimeError("the cue broke")

    monkeypatch.setattr("mnemos.cue.prompt_hook", boom)
    with open(payload) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        code = main(["--db-path", str(db), *SCOPE_ARGS, "hook", "prompt"])
    out = capsys.readouterr()
    assert code == 0 and out.out == ""
    assert "the cue broke" in out.err


def test_the_hook_prints_its_block_as_one_json_object(tmp_path, meaning, home):
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())

    done = _run_hook(db, "prompt", _prompt_payload(), scope_first=True)

    assert done.returncode == 0
    printed = json.loads(done.stdout)
    assert printed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert found["worded"].id in printed["hookSpecificOutput"]["additionalContext"]


def test_a_locked_store_holds_the_hook_no_longer_than_its_cap(tmp_path, meaning, home):
    """Another process holds the store locked. The hook stops at its deadline,
    prints nothing and exits 0; the prompt goes on."""
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        done = _run_hook(db, "prompt", _prompt_payload())
        took = time.monotonic() - started
    finally:
        conn.rollback()
        conn.close()

    assert (done.returncode, done.stdout) == (0, b"")
    # The process's own start (the interpreter and imports) comes on top of
    # the hook's budget; a loaded test machine gets some slack.
    assert took < cue.HOOK_BUDGET + 1.0, took


def test_the_hard_stop_ends_the_process_silently_whatever_it_waits_on(tmp_path):
    code = (
        "import time\n"
        "from mnemos import cue\n"
        "cue.arm_hard_stop(0.3)\n"
        "time.sleep(10)\n"
        "print('never')\n"
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                          env=_env(tmp_path))
    took = time.monotonic() - started
    assert (done.returncode, done.stdout) == (0, "")
    assert took < 5, took


def test_the_hook_imports_nothing_heavy(tmp_path, meaning, home):
    db = _store(tmp_path / "memory.db", *_gate_memories().values())
    code = (
        "import sys\n"
        "from mnemos.cli import main\n"
        f"main(['--db-path', {str(db)!r}, *{SCOPE_ARGS!r}, 'hook', 'prompt'])\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('torch', 'sentence_transformers', 'transformers', 'numpy', 'mcp'))\n"
        "print('HEAVY', heavy, file=sys.stderr)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], input=_prompt_payload(), capture_output=True,
                          text=True, timeout=60, env=_env(Path(os.environ["HOME"])))
    assert done.returncode == 0, done.stderr
    assert "HEAVY []" in done.stderr, done.stderr
    assert "additionalContext" in done.stdout


# ── Never twice: the per-session files ──


def test_per_session_files_are_private_and_old_ones_go(tmp_path, home):
    old = cue.SeenFile("old-session-0001", "0123456789ab")
    old.record(["engram_OLD"])
    week_ago = time.time() - 8 * 86400
    os.utime(old.path, (week_ago, week_ago))
    dead = cue.run_dir() / "cue-12345-0123456789ab.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(dead))
    listener.close()
    os.utime(dead, (week_ago, week_ago), follow_symlinks=False)

    fresh = cue.SeenFile("new-session-0001", "0123456789ab")
    fresh.record(["engram_NEW"], ["key"], offer={"ids": ["engram_NEW"], "via": "words"})

    assert not old.path.exists() and not dead.exists()
    assert fresh.read()["shown"] == ["engram_NEW"] and fresh.read()["offered"] == 1
    assert stat.S_IMODE(os.stat(fresh.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cue.run_dir()).st_mode) == 0o700
    assert cue.offered_summary()["offered"] == 1


# ── A real server answers the hook, and /clear doesn't lose it ──

_SERVER = r'''
import math, re, sys
import mnemos.store.embedding_index as ei
CONCEPTS = CONCEPTS_HERE

def vector(text):
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]

class Vector(list):
    def tolist(self):
        return list(self)

class Model:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return Vector(vector(texts))
        return [Vector(vector(t)) for t in texts]

class Embedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = Model()
        return self._model

ei._check_local_deps = lambda: True
ei._LocalEmbedder = Embedder
from mnemos.cli import main
sys.exit(main(sys.argv[1:]))
'''.replace("CONCEPTS_HERE", repr(tuple(sorted(group) for group in _CONCEPTS)))


def test_a_real_server_answers_the_hook_across_processes_and_after_clear(tmp_path, meaning, home):
    """`mnemos serve` (the simple MCP server) is started the way Claude Code
    starts it: as a child of this process, which stands in for Claude Code,
    with the session's id at start. Its answerer loads the model and opens its
    socket. The hook, in its own process, finds it by CLAUDE_PID alone.

    /clear gives the session a new id and keeps its MCP servers: the hook then
    runs with the new id, and still reaches the same server. What the old
    session was offered can be offered again, since /clear emptied the
    model's context, and never twice within the new one."""
    found = _gate_memories()
    db = _store(tmp_path / "memory.db", *found.values())
    before = _state(db)
    cue.mark_hook_in_use()  # the hook is in use here, so the server warms at start
    # The scope after `serve`: given before it, the subcommand's own defaults
    # replace the person and project (an older CLI quirk, logged for R14).
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER, "serve", "--db-path", str(db), *SCOPE_ARGS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_env(Path(os.environ["HOME"]), CLAUDE_CODE_SESSION_ID="session-before-clear"),
    )
    key = cue.scope_key(str(db), **SCOPE)
    path = cue.answerer_path(os.getpid(), key)
    try:
        deadline = time.monotonic() + 60
        while not path.exists() and time.monotonic() < deadline and server.poll() is None:
            time.sleep(0.05)
        assert path.exists(), server.stderr.read().decode() if server.poll() is not None else "no socket"

        claude = {"CLAUDE_PID": str(os.getpid())}
        before_clear = _run_hook(db, "prompt", _prompt_payload(session_id="session-before-clear"),
                                 CLAUDE_CODE_SESSION_ID="session-before-clear", **claude)
        after_clear = _run_hook(db, "prompt", _prompt_payload(session_id="session-after-clear"),
                                CLAUDE_CODE_SESSION_ID="session-after-clear", **claude)
        repeat = _run_hook(db, "prompt", _prompt_payload(session_id="session-after-clear"),
                           CLAUDE_CODE_SESSION_ID="session-after-clear", **claude)
    finally:
        server.stdin.close()
        server.wait(timeout=30)

    for done in (before_clear, after_clear):
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        assert found["near"].id in context, "meaning found it: the server answered"
    assert repeat.stdout == b"", "never twice within a session"
    assert _seen("session-before-clear", db)["offers"][0]["via"] == "meaning"
    assert _seen("session-after-clear", db)["offers"][0]["via"] == "meaning"
    assert server.returncode == 0
    assert not path.exists(), "the server took its socket with it when it stopped"
    assert _state(db) == before, "the server's answerer and the hook read only"


# ── Installing the hook ──


def _install(tmp_path, *extra: str) -> tuple[int, Path]:
    settings = tmp_path / "claude" / "settings.json"
    code = main(["hooks", "install", "claude-code", "--settings", str(settings),
                 "--db-path", str(tmp_path / "memory.db"), "--agent-id", "nova", "--write", *extra])
    return code, settings


def test_the_prompt_hook_is_installed_only_when_asked(tmp_path, capsys):
    code, settings = _install(tmp_path)
    assert code == 0
    written = json.loads(settings.read_text())
    assert "UserPromptSubmit" not in written["hooks"], "off by default"

    code, _ = _install(tmp_path, "--prompt")
    assert code == 0
    written = json.loads(settings.read_text())
    [entry] = written["hooks"]["UserPromptSubmit"]
    [handler] = entry["hooks"]
    parts = handler["command"].split()
    assert parts[1:3] == ["hook", "prompt"]
    assert ["--db-path", str(tmp_path / "memory.db")] == parts[parts.index("--db-path"):parts.index("--db-path") + 2]
    assert "--agent-id" in parts and handler["timeout"] >= 1
    assert len(written["hooks"]["SessionStart"]) == 1

    # A prompt hook that exits 2 blocks the prompt. A Mnemos that can't run
    # `hook prompt` (an older one) exits 2 from argparse; the command doesn't.
    older = tmp_path / "older-mnemos"
    older.write_text("#!/bin/sh\necho 'mnemos: error: invalid choice' >&2\nexit 2\n")
    older.chmod(0o755)
    command = handler["command"].replace(parts[0], str(older), 1)
    assert subprocess.run(["/bin/sh", "-c", command], capture_output=True).returncode == 0


def test_installing_again_replaces_its_own_hook_and_keeps_others(tmp_path, capsys):
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    theirs = {"hooks": [{"type": "command", "command": "echo someone else's hook"}]}
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [theirs]}, "model": "opus"}))

    _install(tmp_path, "--prompt")
    _install(tmp_path, "--prompt")

    written = json.loads(settings.read_text())
    prompts = written["hooks"]["UserPromptSubmit"]
    assert prompts[0] == theirs and len(prompts) == 2
    assert written["model"] == "opus"


def test_printing_without_write_shows_the_prompt_hook_only_when_asked(tmp_path, capsys):
    main(["hooks", "install", "claude-code", "--settings", str(tmp_path / "s.json")])
    assert "UserPromptSubmit" not in capsys.readouterr().out
    main(["hooks", "install", "claude-code", "--prompt", "--settings", str(tmp_path / "s.json")])
    assert "UserPromptSubmit" in capsys.readouterr().out
    assert not (tmp_path / "s.json").exists()


def test_the_prompt_hook_is_for_claude_code_only(tmp_path, capsys):
    settings = tmp_path / "hooks.json"
    code = main(["hooks", "install", "codex", "--prompt", "--settings", str(settings), "--write"])
    assert code == 1 and not settings.exists()
    assert "Claude Code" in capsys.readouterr().err
