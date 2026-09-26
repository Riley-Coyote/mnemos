"""Old code must not keep maintaining a memory that newer code has moved on from.

A Claude Code session keeps the Mnemos code it imported when it started, so a
server that has run for days maintains memory by the rules of the day it began.
Once newer code has changed those rules, the old server keeps applying them to
the same store, and every layer reports success.

Each store now remembers the newest maintenance code version that opened it
(``meta.min_code_version``). Code below that runs no maintenance on the store,
but still takes the agent's own writes: refusing those would lose memories.

These tests put the store ahead of the running code the way a newer server's
startup does, by writing the store's minimum directly. They compare against
literal text, so on code without the gate they fail on behaviour rather than
on a missing import.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import anyio
import pytest

from mnemos.cli import main
from mnemos.simple_runtime import MnemosRuntime, format_health_card


OLDER = (
    "This session runs older Mnemos code than the store expects. "
    "Restart the session."
)
# When restarting does not clear it, the installed code is itself older.
FIX = (
    "If it still says this, the store was opened by newer code than is "
    "installed; update Mnemos, or reset with 'mnemos repair min-code-version'."
)
# A store opened by code far newer than this one.
AHEAD = 999
SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), **SCOPE)


def _read(db, sql: str, params: tuple = ()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _count(db, table: str) -> int:
    return _read(db, f"SELECT COUNT(*) FROM {table}")[0]


def _store_minimum(db) -> str | None:
    row = _read(db, "SELECT value FROM meta WHERE key = 'min_code_version'")
    return row[0] if row else None


def _claim_for_newer_code(db, version: int = AHEAD) -> None:
    """What a newer server's startup does to a store: raise its minimum."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(version),),
        )
        conn.commit()
    finally:
        conn.close()


def _seeded_store(tmp_path) -> Path:
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the release notes in docs/releases.")
        runtime.capture("The staging server restarts every Sunday night.")
    finally:
        runtime.close()
    return db


def _settled_sha256(path: Path) -> str:
    """Hash the store with everything written so far folded into the file.

    Writes can sit in the write-ahead log with the file itself untouched,
    whether a check made them or a connection opened earlier did.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*args, home):
    """The Mnemos CLI in a genuinely separate process (as in the continuity assay)."""
    return subprocess.run(
        [sys.executable, "-m", "mnemos.cli", *args],
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


# ── An older server: the agent's writes land, maintenance does not run ──


def test_an_older_server_takes_the_agents_writes_but_runs_no_maintenance(tmp_path):
    db = _seeded_store(tmp_path)
    _claim_for_newer_code(db)
    cycles = _count(db, "consolidation_log")
    asks = _count(db, "reflection_queue")
    memories = _count(db, "engrams")

    runtime = _runtime(db)
    try:
        packet = runtime.context()
        # No impact: on code without the gate, maintenance after this capture
        # queues a question asking what it changed.
        captured = runtime.capture("The release moved to Friday.")
        corrected = runtime.correct(
            "The release moved to Monday.", query="release moved Friday"
        )
        explicit = runtime.maintain()
        deep = runtime.maintain(deep=True)
    finally:
        runtime.close()

    for said in (packet, captured, corrected, explicit, deep):
        assert OLDER in said, f"older code did not say why it skipped maintenance:\n{said}"
    for said in (explicit, deep):
        assert "Cycle: skipped" in said
        assert "Passes: none" in said

    assert _count(db, "consolidation_log") == cycles, "a maintenance cycle ran on older code"
    assert _count(db, "reflection_queue") == asks, "older code generated questions"
    assert _store_minimum(db) == str(AHEAD), "older code lowered the store's minimum"

    # The agent's own writes all landed: the capture, and the correction.
    assert "Captured continuity." in captured
    assert _count(db, "engrams") == memories + 2
    reader = _runtime(db)
    try:
        assert "The release moved to Monday." in reader.recall("release moved Monday")
        assert "release notes in docs/releases" in reader.recall("release notes docs")
    finally:
        reader.close()


def test_a_running_server_stops_maintaining_once_newer_code_opens_the_store(tmp_path):
    """The case this exists for, over the real protocol.

    The server starts and maintains normally. Then a newer session starts
    and its server raises the store's minimum. The server already running
    must notice on its next maintenance, not only at its own startup, while
    its captures keep landing and another process can read them back.
    """
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = tmp_path / "served.db"
    token = f"ferry-{uuid.uuid4().hex[:10]}"

    def text(result) -> str:
        return "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )

    async def session_run() -> dict:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemos.cli", "serve", "--mode", "simple", "--db-path", str(db), *SCOPE_ARGS],
        )
        seen: dict = {}
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                first = text(await session.call_tool(
                    "mnemos_capture", {"content": "Riley keeps the release notes in docs/releases."}
                ))
                assert "Captured continuity." in first
                seen["before"] = text(await session.call_tool("mnemos_maintain", {}))

                _claim_for_newer_code(db)  # a newer session's server has started
                seen["cycles"] = _count(db, "consolidation_log")

                seen["maintain"] = text(await session.call_tool("mnemos_maintain", {}))
                seen["capture"] = text(await session.call_tool(
                    "mnemos_capture", {"content": f"The ferry timetable is kept as {token}."}
                ))
                health = await session.call_tool("mnemos_health", {})
                seen["health_text"] = text(health)
                seen["health_code"] = (health.structuredContent or {}).get("code")
                seen["cycles_after"] = _count(db, "consolidation_log")
        return seen

    seen = anyio.run(session_run)

    assert "Cycle: skipped" not in seen["before"], "premise: current code maintains"
    assert "Cycle: skipped" in seen["maintain"]
    assert OLDER in seen["maintain"]
    assert OLDER in seen["capture"]
    assert "Captured continuity." in seen["capture"]
    assert seen["cycles_after"] == seen["cycles"], "the running server kept maintaining"
    assert f"ATTENTION — {OLDER}" in seen["health_text"]
    assert seen["health_code"]["store_minimum"] == AHEAD
    assert seen["health_code"]["older_than_store"] is True

    # Another process, after the server has gone, finds what it captured.
    reader = _runtime(db)
    try:
        assert token in reader.recall("ferry timetable")
    finally:
        reader.close()


def test_an_older_process_capture_is_read_back_by_another_process(tmp_path):
    """Default scope, separate processes: an older writer, a later reader."""
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    # Maintenance that rides on a capture waits five idle minutes by default;
    # without the wait, code with no gate maintains on every capture here.
    (home / ".mnemos" / "config.json").write_text(
        json.dumps({"consolidation": {"min_idle_minutes": 0}})
    )
    first = _run("remember", "Riley starts the day with a walk.", home=home)
    assert first.returncode == 0, first.stderr
    stores = [path for path in (home / ".mnemos").glob("*.db") if path.name != "audit.db"]
    assert len(stores) == 1, stores
    db = stores[0]
    _claim_for_newer_code(db)
    cycles = _count(db, "consolidation_log")

    token = f"harbour-{uuid.uuid4().hex[:12]}"
    wrote = _run("remember", f"The spare key is kept at {token}.", home=home)
    assert wrote.returncode == 0, wrote.stderr
    assert "Captured continuity." in wrote.stdout
    assert OLDER in wrote.stdout
    assert _count(db, "consolidation_log") == cycles, "the older process ran maintenance"

    read = _run("hook", "session-start", home=home)
    assert read.returncode == 0, read.stderr
    packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]
    assert token in packet, f"a capture from older code did not come back:\n{packet}"


# ── Raising the minimum ──


def test_a_newer_server_raises_the_minimum_and_an_older_one_never_lowers_it(
    tmp_path, monkeypatch
):
    db = tmp_path / "memory.db"

    def start(version: int, *, fresh: bool = False) -> str | None:
        monkeypatch.setattr(
            "mnemos.simple_runtime.MAINTENANCE_CODE_VERSION", version, raising=False
        )
        runtime = _runtime(db)
        try:
            if fresh:
                runtime.capture("The first memory in this store.")
            else:
                runtime.health()  # the first use of a server is its startup
        finally:
            runtime.close()
        return _store_minimum(db)

    assert start(3, fresh=True) == "3", "the first code to open a store records itself"
    assert start(5) == "5", "newer code raises the minimum at startup"
    assert start(4) == "5", "older code lowered the minimum"
    assert start(5) == "5"


# ── Health ──


def test_health_names_an_older_session_and_the_fix(tmp_path):
    db = _seeded_store(tmp_path)

    runtime = _runtime(db)
    try:
        current = runtime.health()
    finally:
        runtime.close()
    current_card = format_health_card(current)
    assert "ATTENTION — This session runs older" not in current_card

    _claim_for_newer_code(db)
    runtime = _runtime(db)
    try:
        older = runtime.health()
    finally:
        runtime.close()
    older_card = format_health_card(older)

    assert f"ATTENTION — {OLDER} {FIX}" in older_card
    assert f"(the store needs {AHEAD} or newer)" in older_card
    assert older["code"]["store_minimum"] == AHEAD
    assert older["code"]["older_than_store"] is True

    running = current["code"]["running"]
    assert current["code"] == {
        "running": running, "store_minimum": running, "older_than_store": False,
    }
    assert f"Code:          version {running} (the store needs {running} or newer)" in current_card


# ── Doctor ──


def test_doctor_leaves_the_store_byte_identical(tmp_path, capsys):
    """Doctor used to print ``runtime.context()``, which runs maintenance and
    uses up the showings of pending questions, and every plain open migrates
    the schema and rewrites the file. A check that changes what it checks
    reports on itself."""
    source = _seeded_store(tmp_path)
    runtime = _runtime(source)
    try:
        runtime.handoff("Left off wiring the release checklist.")
    finally:
        runtime.close()

    store = tmp_path / "copy.db"
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(str(store))
    try:
        reader.backup(copy)
    finally:
        copy.close()
        reader.close()
    before = _settled_sha256(store)

    assert main(["doctor", "--db-path", str(store), *SCOPE_ARGS]) == 0
    out = capsys.readouterr().out

    assert _settled_sha256(store) == before, "doctor changed the store it was checking"
    assert "DB exists:    yes" in out
    assert "Continuity:" in out


class _Vector(list):
    def tolist(self):
        return list(self)


class _Model:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector([1.0, 0.0, 0.5])
        return [_Vector([1.0, 0.0, 0.5]) for _ in texts]


def _turn_semantic_recall_on(monkeypatch) -> None:
    """A working local embedding backend, without torch."""
    from mnemos.store import embedding_index as ei

    class Embedder(ei._LocalEmbedder):
        def _get_model(self):
            if self._model is None:
                self._model = _Model()
            return self._model

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", Embedder)


def _has_vector_table(db) -> bool:
    return _read(
        db, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
    ) is not None


@pytest.mark.parametrize("vectors_stored", [False, True], ids=["no-vectors-yet", "vectors-stored"])
def test_doctor_stays_read_only_with_semantic_recall_on(
    tmp_path, capsys, monkeypatch, vectors_stored
):
    """With a working backend, opening the embedding index creates its table.
    On a store written without one, a doctor that opened the index that way
    would add a table to the store it was only looking at."""
    if vectors_stored:
        _turn_semantic_recall_on(monkeypatch)
    db = _seeded_store(tmp_path)
    _turn_semantic_recall_on(monkeypatch)
    assert _has_vector_table(db) is vectors_stored, "premise"
    before = _settled_sha256(db)

    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    out = capsys.readouterr().out

    assert _settled_sha256(db) == before, "doctor changed the store it was checking"
    assert _has_vector_table(db) is vectors_stored
    # And it still reads the vectors that are there, rather than reporting none.
    searchable = 2 if vectors_stored else 0
    assert f"{searchable} of 2 active memories searchable by meaning" in out


def test_doctor_names_an_older_code_version(tmp_path, capsys):
    db = _seeded_store(tmp_path)
    _claim_for_newer_code(db)

    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    out = capsys.readouterr().out

    assert f"(the store needs {AHEAD} or newer)" in out
    assert f"ATTENTION:  {OLDER} {FIX}" in out
    assert _store_minimum(db) == str(AHEAD)


def test_a_read_only_store_refuses_writes_and_never_creates_one(tmp_path):
    from mnemos.store.sqlite_store import ReadOnlyEngramStore

    missing = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        ReadOnlyEngramStore(missing)
    assert not missing.exists()

    db = _seeded_store(tmp_path)
    store = ReadOnlyEngramStore(db)
    try:
        assert store.min_code_version() is not None
        with pytest.raises(sqlite3.OperationalError):
            store.set_meta("written_by_a_reader", "yes")
    finally:
        store.close()


# ── The way back: a human resets the minimum ──
#
# Opening a store only ever raises its minimum. If code newer than what is
# installed raised it, nothing installed maintains the store again and
# restarting does not help. The reset is a dry run unless --write, makes a
# verified backup first, and never goes below 1.


def _reset(db, *extra) -> int:
    return main(["repair", "min-code-version", "--db-path", str(db), *SCOPE_ARGS, *extra])


def _backups(db) -> list[Path]:
    return sorted((Path(db).parent / "backups").glob("*.pre-repair-min-code-version-*.db"))


def test_the_reset_is_a_dry_run_unless_written(tmp_path, capsys):
    db = _seeded_store(tmp_path)
    _claim_for_newer_code(db)
    before = _settled_sha256(db)

    assert _reset(db) == 0
    shown = capsys.readouterr().out
    assert "This code:      version" in shown
    assert f"Store minimum:  {AHEAD}" in shown
    assert "does not maintain it" in shown
    assert "Dry run: nothing changed" in shown

    assert _reset(db, "--set", "1") == 0
    planned = capsys.readouterr().out
    assert f"Would set the minimum from {AHEAD} to 1." in planned
    assert "Dry run: nothing changed" in planned

    assert _settled_sha256(db) == before, "a dry run changed the store"
    assert _backups(db) == []

    missing = tmp_path / "typo" / "memory.db"
    assert main(["repair", "min-code-version", "--db-path", str(missing), *SCOPE_ARGS]) == 0
    assert "nothing to repair" in capsys.readouterr().out
    assert not missing.exists(), "the reset created a store just by looking"


def test_the_reset_lowers_or_raises_the_minimum_after_a_verified_backup(tmp_path, capsys):
    from mnemos.backup import check_database

    db = _seeded_store(tmp_path)
    _claim_for_newer_code(db)

    assert _reset(db, "--set", "1", "--write") == 0
    out = capsys.readouterr().out
    assert f"Set the minimum from {AHEAD} to 1." in out
    assert _store_minimum(db) == "1"
    [backup] = _backups(db)
    assert check_database(backup)["integrity"] == "ok"
    assert _store_minimum(backup) == str(AHEAD), "the backup must be the state before"

    # The code installed here maintains the store again.
    runtime = _runtime(db)
    try:
        assert "Cycle: skipped" not in runtime.maintain()
    finally:
        runtime.close()

    # And it raises as well as lowers, with a backup each time.
    assert _reset(db, "--set", str(AHEAD), "--write") == 0
    capsys.readouterr()
    assert _store_minimum(db) == str(AHEAD)
    assert len(_backups(db)) == 2
    runtime = _runtime(db)
    try:
        assert OLDER in runtime.maintain()
    finally:
        runtime.close()


def test_the_reset_refuses_below_one_and_needs_a_version_to_write(tmp_path, capsys):
    db = _seeded_store(tmp_path)
    _claim_for_newer_code(db)
    before = _settled_sha256(db)

    assert _reset(db, "--set", "0", "--write") == 1
    assert "Refused" in capsys.readouterr().out
    assert _reset(db, "--set", "-3", "--write") == 1
    assert _reset(db, "--write") == 1
    assert "--set N" in capsys.readouterr().out

    assert _settled_sha256(db) == before, "a refused reset changed the store"
    assert _store_minimum(db) == str(AHEAD)
    assert _backups(db) == []


# ── The consolidate command ──


def test_the_consolidate_command_runs_no_passes_on_older_code(tmp_path, capsys):
    db = _seeded_store(tmp_path)
    consolidate = [
        "--db-path", str(db), "--agent-id", "nova", "--person-id", "riley",
        "--project-scope", "demo", "consolidate",
    ]
    cycles = _count(db, "consolidation_log")
    assert main(consolidate) == 0
    assert "Passes: connection_discovery" in capsys.readouterr().out
    assert _count(db, "consolidation_log") == cycles + 1, "premise: current code consolidates"

    _claim_for_newer_code(db)
    cycles = _count(db, "consolidation_log")
    assert main(consolidate) == 0
    out = capsys.readouterr().out

    assert _count(db, "consolidation_log") == cycles, "older code ran a consolidation cycle"
    assert "Consolidation skipped: no passes ran." in out
    assert f"{OLDER} {FIX}" in out


# ── A capture on older code never moves a belief ──


def test_older_code_captures_without_weighing_beliefs(tmp_path):
    """Capture also weighs what arrives as evidence for or against beliefs.

    Without a model that is a keyword-and-negation check: a capture sharing a
    word with a belief and containing "not" lowers it and records a
    contradiction. Newer code may replace those rules (the next package
    removes that check), and an older server must never go on applying them,
    so on older code a capture lands without the step.
    """
    from datetime import datetime, timedelta, timezone

    from mnemos.core.belief import Belief

    db = tmp_path / "memory.db"

    def runtime() -> MnemosRuntime:
        return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)

    seed = runtime()
    try:
        seed.capture("Riley reviews every deploy checklist before shipping.")
        [anchor] = seed._store.get_active_engrams(agent_id="nova", limit=1)
        belief = Belief(
            agent_id="nova",
            content="Riley reviews every deploy checklist",
            confidence=0.6,
            source="agent",
            supporting_engram_ids=[anchor.id],
            # Past the six-hour cooldown between revisions.
            last_revised=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )
        seed._store.save_belief(belief)
    finally:
        seed.close()

    _claim_for_newer_code(db)
    contradictions = _read(
        db, "SELECT COUNT(*) FROM connections WHERE relation = 'contradicts'"
    )[0]
    memories = _count(db, "engrams")

    older = runtime()
    try:
        captured = older.capture("Riley did not review the deploy checklist this week.")
    finally:
        older.close()

    assert "Captured continuity." in captured
    assert _count(db, "engrams") == memories + 1
    confidence = _read(db, "SELECT confidence FROM beliefs WHERE id = ?", (belief.id,))[0]
    assert confidence == pytest.approx(0.6), "older code lowered a belief"
    assert _read(
        db, "SELECT COUNT(*) FROM connections WHERE relation = 'contradicts'"
    )[0] == contradictions, "older code recorded a contradiction"
