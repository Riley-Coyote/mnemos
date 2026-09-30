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
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime, format_health_card
from mnemos.simple_scope import MnemosScope
from mnemos.store.sqlite_store import EngramStore


class _Lazy:
    """A module under test, imported where a test first uses it, so that on
    code without what it needs each test fails by itself (the fail-on-base
    proof)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        import importlib

        return getattr(importlib.import_module(self._name), attr)


cue = _Lazy("mnemos.cue")
dream_journal = _Lazy("mnemos.dream_journal")

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
MODEL = "claude-opus-5-5"
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


def _engram(content: str, **fields) -> Engram:
    return Engram(content=content, kind=fields.pop("kind", "semantic"), owner_agent_id="nova",
                  person_id="riley", project_scope="demo", **fields)


def _env(home: Path, **extra) -> dict[str, str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
           "PYTHONPATH": ":".join(sys.path), "PYTHONDONTWRITEBYTECODE": "1",
           "MNEMOS_JEV_KEY_FILE": os.devnull}
    env.update(extra)
    return env


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME in the test's folder, by a path short enough for a unix socket."""
    real = tmp_path / "home"
    real.mkdir()
    link = None
    path = real
    if len(os.fsencode(str(real / ".mnemos" / "run" / "cue-4194304-0123456789ab.sock"))) >= 100:
        link = Path("/tmp") / f"mnr-{uuid.uuid4().hex[:10]}"
        os.symlink(real, link)
        path = link
    monkeypatch.setenv("HOME", str(path))
    yield path
    if link is not None:
        os.unlink(link)


# A model whose meaning is controlled: each text's vector counts the words it
# holds of each concept, so every cosine is fixed by the words.
_CONCEPTS = (
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
    {"garden", "marigolds", "greenhouse", "bloom"},
)


def concept_vector(text: str) -> list[float]:
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
    """A working local backend whose cosines the test fixes."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


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


# ── 2. A briefing fetched by mnemos_context counts as shown ──

LAMP = "The lighthouse keeper closes the storm shutters before the fog comes in."
MESSAGE = "The lighthouse keeper asked whether the storm shutters close tonight in the fog"


def _hook(db: Path, prompt: str = MESSAGE, session: str = "session-one", **environ) -> str:
    payload = {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    return cue.prompt_hook(payload, db_path=str(db), environ={"CLAUDE_PID": "999999", **environ}, **SCOPE)


def _seen(session: str, db: Path) -> dict:
    return cue.SeenFile(session, cue.scope_key(str(db), **SCOPE)).read()


def test_a_briefing_fetched_by_mnemos_context_counts_as_shown(tmp_path, home, monkeypatch):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(LAMP, signed_as=MODEL))
    finally:
        rt.close()

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "context-session")
    rt = _runtime(db)
    try:
        packet = rt.context()
    finally:
        rt.close()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    assert "lighthouse keeper closes" in packet, packet
    seen = _seen("context-session", db)
    assert memory in seen["shown"], seen
    assert seen["offered"] == 0, "a briefing showing something is not an offer"
    assert _hook(db, session="context-session") == "", "the cue brought the briefing's line again"
    assert memory in _hook(db, session="another-session"), "another session starts fresh"


def test_mnemos_context_over_the_protocol_counts_as_shown_for_the_hook(tmp_path, home):
    """The real server, in its own process with the session's id, delivers the
    briefing; the prompt hook, in another, leaves that line out."""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory = _memory_id(rt.capture(LAMP, signed_as=MODEL))
    finally:
        rt.close()

    async def run() -> str:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemos.cli", "serve", "--mode", "simple", "--db-path", str(db), *SCOPE_ARGS],
            env={**get_default_environment(), **_env(home, CLAUDE_CODE_SESSION_ID="served-session")},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("mnemos_context", {})
                assert not result.isError
                return "\n".join(block.text for block in result.content if block.type == "text")

    briefing = anyio.run(run)
    assert "lighthouse keeper closes" in briefing, briefing

    def hook(session: str) -> bytes:
        payload = json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit",
                              "prompt": MESSAGE})
        done = subprocess.run(
            [sys.executable, "-m", "mnemos.cli", "hook", "prompt", "--db-path", str(db), *SCOPE_ARGS],
            input=payload.encode(), capture_output=True, timeout=60,
            env=_env(home, CLAUDE_PID="999999"),
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    assert hook("served-session") == b"", "the cue brought what mnemos_context showed"
    assert memory.encode() in hook("another-session")


# ── 4. The scheduled consolidate reports; old reports are retired ──

BEACONS = (
    "The beacon lamp on the north point needs a new mantle before the storm season.",
    "Replace the north point beacon lamp mantle before the storm season starts.",
    "The north point beacon lamp mantle cracked during the last storm season.",
)


def _saved(db: Path, *texts: str) -> list[str]:
    store = EngramStore(str(db))
    try:
        memories = [_engram(text, author_kind="agent") for text in texts]
        for memory in memories:
            store.save_engram(memory)
        return [memory.id for memory in memories]
    finally:
        store.close()


def _reports(db: Path) -> list[tuple[str, int, str]]:
    """The dream journal's reports in the scope: id, active, content."""
    return _read(db, """
        SELECT id, active, content FROM hypomnema_entries
        WHERE agent_id = ? AND person_id = ? AND project_scope = ?
          AND tags_json LIKE '%"dream-journal"%'
        ORDER BY created_at
    """, tuple(SCOPE.values()))


def test_the_scheduled_consolidate_writes_a_report_the_next_briefing_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _saved(db, *BEACONS)
    home = tmp_path / "home"
    home.mkdir()

    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "--db-path", str(db), *SCOPE_ARGS, "consolidate"],
        capture_output=True, text=True, timeout=180, env=_env(home),
    )

    assert done.returncode == 0, done.stderr
    assert "Maintenance report: written" in done.stdout, done.stdout
    [(report_id, active, content)] = _reports(db)
    assert active == 1 and content.startswith("Mnemos connected"), content
    rt = _runtime(db)
    try:
        packet = rt.context()
        data = rt.health()
    finally:
        rt.close()
    assert "### While you were away" in packet and content in packet, packet
    report = data["watchdog"]["checks"]["report"]
    assert report["report_id"] == report_id and report["cycles_untold"] == 0, report
    assert data["dream"]["last_written_at"] is not None


def test_a_cycle_with_nothing_worth_reporting_writes_no_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _saved(db, "The harbour ferry leaves at noon.")

    assert main(["--db-path", str(db), *SCOPE_ARGS, "consolidate"]) == 0

    assert "Maintenance report: none, nothing worth reporting" in capsys.readouterr().out
    assert _reports(db) == []


def _pile(db: Path, count: int) -> list[str]:
    """Reports left in use by the code that wrote a new one each cycle."""
    store = EngramStore(str(db))
    try:
        made = []
        for n in range(count):
            made.append(store.write_hypomnema_entry(
                f"Mnemos connected {n + 2} memories that belong together.",
                **SCOPE, source="synthesized", entry_kind="maintenance_report", authored_by="system",
                author_id="mnemos", domain="situational", tags=["dream-journal"],
                confidence=0.55, salience=0.4,
            ))
        # Oldest first, an hour apart.
        for n, entry in enumerate(made):
            at = _ago(hours=count - n)
            store._get_conn().execute(
                "UPDATE hypomnema_entries SET created_at = ?, last_revised_at = ? WHERE id = ?",
                (at, at, entry),
            )
        # An identity report is a maintenance report of another kind: kept.
        identity = store.write_hypomnema_entry(
            "Identity divergence: the declared and the computed identity differ.", **SCOPE,
            source="synthesized", entry_kind="maintenance_report", authored_by="system",
            author_id="mnemos", domain="identity", tags=["identity-divergence"],
        )
        store._get_conn().commit()
        return [*made, identity]
    finally:
        store.close()


def test_reports_older_than_the_newest_five_are_retired_never_deleted(tmp_path):
    db = tmp_path / "memory.db"
    *pile, identity = _pile(db, 8)
    rows_before = _read(db, "SELECT COUNT(*) FROM hypomnema_entries")[0][0]

    store = EngramStore(str(db))
    try:
        new = dream_journal.write_dream_entry(store, MnemosScope(db_path=str(db), **SCOPE),
                                              "Mnemos moved 1 faded memory into the archive.")
    finally:
        store.close()

    active = {report for report, is_active, _ in _reports(db) if is_active}
    # The newest supersedes the last; with it, the four newest of the pile stay.
    assert active == {new, *pile[3:7]}, (active, pile)
    assert _read(db, "SELECT active FROM hypomnema_entries WHERE id = ?", (identity,)) == [(1,)]
    assert _read(db, "SELECT COUNT(*) FROM hypomnema_entries")[0][0] == rows_before + 1, (
        "a report was deleted"
    )
    for retired in pile[:3]:
        content, revisions = _read(db, "SELECT content, revisions_json FROM hypomnema_entries WHERE id = ?",
                                   (retired,))[0]
        assert content.startswith("Mnemos connected"), "a retired report lost its words"
        trail = json.loads(revisions)[-1]
        assert trail["prior_content"] == content
        assert trail["reason"] == "archived: retired: older than the newest 5 maintenance reports"
    # Restorable: nothing about a retired report is lost but its place in use.
    _write(db, "UPDATE hypomnema_entries SET active = 1 WHERE id = ?", (pile[0],))
    assert pile[0] in {report for report, is_active, _ in _reports(db) if is_active}


def test_a_session_maintenance_retires_the_pile_too(tmp_path, monkeypatch):
    """Both writers report through one rule: a session's maintenance retires
    the old reports as the scheduled job does."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _pile(db, 7)
    _saved(db, *BEACONS)
    rt = _runtime(db)
    try:
        said = rt.maintain()
    finally:
        rt.close()
    assert "Dream journal: updated" in said, said
    assert sum(is_active for _, is_active, _ in _reports(db)) == 5


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


# ── 7. Network waits are bounded ──


class _HangingServer:
    """A TCP server on 127.0.0.1 that takes connections and never answers,
    as a network that hangs does."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.held: list[socket.socket] = []
        self.done = threading.Event()
        threading.Thread(target=self._take, daemon=True).start()

    def _take(self) -> None:
        self.listener.settimeout(0.2)
        while not self.done.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                continue
            self.held.append(conn)

    def close(self) -> None:
        self.done.set()
        for conn in self.held:
            conn.close()
        self.listener.close()


@pytest.fixture
def hanging(monkeypatch):
    """Recall's backend is Gemini, and every request to it goes to a server
    that never answers. The waits are the code's own (the request's timeout
    reaches a real socket); ``waits`` keeps each."""
    server = _HangingServer()
    real = urllib.request.urlopen
    waits: list[float | None] = []

    def rerouted(request, timeout=None, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if "generativelanguage.googleapis.com" in url or "/v1beta/models/" in url:
            waits.append(timeout)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/v1beta/models/hanging",
                data=request.data, headers=dict(request.header_items()), method="POST",
            )
        return real(request, timeout=timeout, **kwargs)

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(urllib.request, "urlopen", rerouted)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    server.waits = waits
    yield server
    server.close()


def _finishes(call, seconds: float = 10.0) -> bool:
    done = threading.Event()

    def run():
        try:
            call()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds)


def test_a_hanging_network_blocks_neither_health_doctor_nor_recall(tmp_path, hanging):
    db = tmp_path / "memory.db"
    _saved(db, "The lighthouse lamp was rewired on Tuesday.")
    said: dict = {}

    def recall_then_health():
        rt = _runtime(db)
        try:
            started = time.monotonic()
            said["recall"] = rt.recall("lighthouse lamp")
            said["recall seconds"] = time.monotonic() - started
            said["health"] = rt.health()
        finally:
            rt.close()

    assert _finishes(recall_then_health), "recall or health waited on the network"
    assert _finishes(lambda: main(["doctor", "--db-path", str(db), *SCOPE_ARGS])), (
        "doctor waited on the network"
    )
    assert "rewired on Tuesday" in said["recall"], "recall stopped finding by words"
    assert said["recall seconds"] < ei.NETWORK_TIMEOUT + 1.5, said["recall seconds"]
    assert hanging.waits and all(0 < wait <= ei.NETWORK_TIMEOUT for wait in hanging.waits), hanging.waits
    waits = said["health"]["watchdog"]["checks"]["network_waits"]
    assert waits["network"] is True and waits["timeouts_here"] >= 1, waits


def test_the_maintenance_link_lookup_is_bounded_and_counted(tmp_path, hanging):
    """Maintenance runs inside a capture. It looked up each of up to 50
    memories by meaning, 30 s each against a network that hung. Now its
    lookups share the write path's budget, the first timeout ends them, and
    the rest are linked by their words."""
    db = tmp_path / "memory.db"
    memories = _saved(db, *BEACONS, "The harbour ferry leaves at noon.", "Garden marigolds bloom in June.")
    said: dict = {}

    def maintain():
        rt = _runtime(db)
        try:
            started = time.monotonic()
            said["maintain"] = rt.maintain()
            said["seconds"] = time.monotonic() - started
            said["health"] = rt.health()
        finally:
            rt.close()

    assert _finishes(maintain, 20), "maintenance waited on the network for every memory"
    assert ei.NETWORK_TIMEOUT == 2.0
    assert said["seconds"] < 3 * ei.NETWORK_TIMEOUT + 2, said["seconds"]
    stats = json.loads(_read(db, "SELECT stats FROM consolidation_log WHERE pass_name = 'cycle'")[0][0])
    discovery = stats["connection_discovery"]
    assert discovery["embedding_timeouts"] == 1, discovery
    assert discovery["embedding_deferred"] == len(memories), discovery
    assert discovery["connections_created"] > 0, "the words did not link what belongs together"
    waits = said["health"]["watchdog"]["checks"]["network_waits"]
    assert waits["link_lookup_timeouts"] == 1 and waits["cycles_with_timeouts"] == 1, waits
    assert "1 link lookup timed out" in waits["seen"], waits["seen"]


class _Answering:
    """urlopen for Gemini's endpoints: answers, fails, or times out, and keeps
    each request's size and wait."""

    def __init__(self, fail: str = "", fail_after: int = 0) -> None:
        self.fail = fail
        self.fail_after = fail_after
        self.requests: list[tuple[int, float | None]] = []

    def __call__(self, request, timeout=None, **kwargs):
        payload = json.loads(request.data)
        texts = [item["content"]["parts"][0]["text"] for item in payload.get("requests") or []] or [
            payload["content"]["parts"][0]["text"]
        ]
        self.requests.append((len(texts), timeout))
        if self.fail and len(self.requests) > self.fail_after:
            if self.fail == "timeout":
                raise TimeoutError("timed out")
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
        import io

        if "requests" in payload:
            body = {"embeddings": [{"values": concept_vector(text)} for text in texts]}
        else:
            body = {"embedding": {"values": concept_vector(texts[0])}}
        return io.BytesIO(json.dumps(body).encode())


def test_every_network_embedding_call_waits_at_most_two_seconds(monkeypatch):
    """One text waited 30 s and a batch 120 s outside the write path."""
    gemini = ei._GeminiEmbedder("test-key-not-real")
    fake = _Answering()
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    assert gemini.embed("the beacon lamp") is not None
    assert len(gemini.batch_embed([f"lamp {n}" for n in range(40)])) == 40
    assert fake.requests[0] == (1, 2.0), fake.requests
    assert [size for size, _ in fake.requests[1:]] == [16, 16, 8], "a request larger than fits in 2 s"
    assert {wait for _, wait in fake.requests} == {2.0}, fake.requests


def test_a_timeout_ends_the_call_and_is_counted(monkeypatch):
    """A network that hangs costs one wait, not one per text: no request after
    a timeout, and no retrying its texts one at a time."""
    gemini = ei._GeminiEmbedder("test-key-not-real")
    fake = _Answering(fail="timeout", fail_after=1)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    got = gemini.batch_embed([f"lamp {n}" for n in range(40)])

    assert [value is not None for value in got] == [True] * 16 + [False] * 24
    assert len(fake.requests) == 2, fake.requests
    assert gemini.timeouts == 1

    # Another failure is retried one text at a time, each waiting at most
    # 2 s, until one of those times out.
    refused = _Answering(fail="error")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert gemini.batch_embed(["a", "b", "c"]) == [None, None, None]
    assert [wait for _, wait in refused.requests] == [2.0, 2.0, 2.0, 2.0]
