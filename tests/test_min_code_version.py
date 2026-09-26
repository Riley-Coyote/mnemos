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


# ── Older code records the agent's words and applies no rules ──
#
# This code becomes the older code once the next packages land, and every
# session left open keeps running it for days. What it does by rule, rather
# than by the agent's words, it must not do to a store newer code has opened.


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _memory_state(db) -> dict[str, list[tuple]]:
    """What a return can change: counts, weights, history and links."""
    return {
        "engrams": _all(
            db,
            "SELECT id, access_count, last_accessed, reconsolidation_count, "
            "strength, stability, accessibility FROM engrams ORDER BY id",
        ),
        "versions": _all(db, "SELECT COUNT(*) FROM versions"),
        "connections": _all(
            db,
            "SELECT source_id, target_id, relation, strength FROM connections "
            "ORDER BY source_id, target_id, relation",
        ),
    }


def _belief_state(db) -> list[tuple]:
    return _all(
        db,
        "SELECT id, content, confidence, superseded_by, revision_history "
        "FROM beliefs ORDER BY id",
    )


def _memory_ids(db) -> dict[str, str]:
    return {content: engram_id for engram_id, content in _all(db, "SELECT id, content FROM engrams")}


def test_older_recall_returns_memories_but_reconsolidates_none(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the ferry timetable in the kitchen drawer.")
        runtime.capture("The ferry leaves the harbour at seven on weekdays.")
    finally:
        runtime.close()
    _claim_for_newer_code(db)
    before = _memory_state(db)

    runtime = _runtime(db)
    try:
        recalled = runtime.recall("ferry timetable")
        packet = runtime.context("ferry timetable")
    finally:
        runtime.close()

    assert "Durable memories:" in recalled
    assert "ferry timetable in the kitchen drawer" in recalled
    assert "The ferry leaves the harbour" in recalled, "premise: two memories come back together"
    assert "Relevant memories:" in packet
    assert _memory_state(db) == before, (
        "older code changed what it returned: access counts, strength, "
        "version rows or co-activation links"
    )


def _asks(db) -> list[tuple]:
    return _all(
        db,
        "SELECT id, kind, target_id, prompt, surfaced_count, answered_at, answer "
        "FROM reflection_queue ORDER BY id",
    )


def test_older_reflect_leaves_belief_and_contradiction_questions_open(tmp_path):
    """Stale code cannot take the verdict current code reads from these
    answers, so answering there would spend the question. The words are kept
    as a signed note naming the question, and the question waits."""
    from mnemos.core.belief import Belief

    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        # With an impact, so maintenance asks nothing about these memories.
        for content in (
            "Riley plans every trip around the ferry timetable.",
            "Riley said the old ferry route was the prettiest.",
            "Riley now avoids the ferry in winter.",
            "Riley takes the ferry every winter weekend.",
        ):
            runtime.capture(content, impact="Trips bend to the ferry.")
        held = Belief(
            agent_id="nova", content="Riley loves the ferry", confidence=0.6,
            source="agent",
        )
        runtime._store.save_belief(held)
    finally:
        runtime.close()
    ids = _memory_ids(db)
    formation = ids["Riley plans every trip around the ferry timetable."]
    reaffirmation = ids["Riley said the old ferry route was the prettiest."]
    contradiction = ids["Riley now avoids the ferry in winter."]
    other = ids["Riley takes the ferry every winter weekend."]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM reflection_queue")
        conn.commit()
    finally:
        conn.close()
    store = _runtime(db)
    try:
        store._ensure_init()
        for kind, target, prompt in (
            ("belief", formation, "Is there a belief here? [theme:ferry]"),
            ("belief", reaffirmation, f'You hold "Riley loves the ferry". Still true? [belief:{held.id}]'),
            ("contradiction", contradiction, f"Do these contradict? [ref:{other}]"),
        ):
            store._store.enqueue_reflection(
                kind, target, prompt, agent_id="nova", person_id="riley", project_scope="demo",
            )
    finally:
        store.close()

    _claim_for_newer_code(db)
    beliefs, memories, asks = _belief_state(db), _memory_state(db), _asks(db)
    answers = {
        formation: "Yes. Riley builds trips around the ferry.",
        reaffirmation: "No, not any more.",
        contradiction: "Yes, they contradict each other.",
    }

    runtime = _runtime(db)
    try:
        runtime.introduce("claude-opus-5-5")
        said = {target: runtime.reflect(target, answer) for target, answer in answers.items()}
    finally:
        runtime.close()

    assert _asks(db) == asks, "older code answered a question or spent a showing"
    assert _belief_state(db) == beliefs, "older code formed, revised or retired a belief"
    assert _memory_state(db) == memories, "older code linked or weakened a memory"
    notes = _all(
        db,
        "SELECT content, entry_kind, authored_by, author_model, active "
        "FROM hypomnema_entries WHERE content LIKE '%open question%'",
    )
    for ask_id, _kind, target, prompt, *_ in asks:
        answer = answers[target]
        assert "the question stays open for a current session" in said[target]
        assert said[target].splitlines()[-1] == OLDER
        kept = [note for note in notes if note[0].startswith(answer)]
        assert len(kept) == 1, f"the answer to {ask_id} was not kept exactly once"
        content, entry_kind, authored_by, author_model, active = kept[0]
        assert ask_id in content and prompt in content, "the note does not name its question"
        assert (entry_kind, authored_by, author_model, active) == (
            "continuity", "agent", "claude-opus-5-5", 1,
        )


def test_older_reflect_lands_a_lesson_answer_on_its_memory_and_files_no_lesson(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley missed the last ferry and walked home in the rain.")
    finally:
        runtime.close()
    [(memory,)] = _all(db, "SELECT id FROM engrams")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM reflection_queue")
        conn.commit()
    finally:
        conn.close()
    store = _runtime(db)
    try:
        store._ensure_init()
        store._store.enqueue_reflection(
            "lesson", memory, "This memory is fading. What did it teach?",
            agent_id="nova", person_id="riley", project_scope="demo",
        )
    finally:
        store.close()
    _claim_for_newer_code(db)
    memories = _count(db, "engrams")

    lesson = "Check the last ferry before staying out late."
    runtime = _runtime(db)
    try:
        said = runtime.reflect(memory, lesson)
    finally:
        runtime.close()

    # The agent's words land on the memory they are about.
    assert _read(db, "SELECT impact, impact_source FROM engrams WHERE id = ?", (memory,)) == (
        lesson, "agent",
    )
    # Filing a lesson matches it against the lessons already held, by rule.
    assert _count(db, "engrams") == memories, "older code filed a lesson"
    assert _all(db, "SELECT 1 FROM connections WHERE relation = 'distilled_into'") == []
    assert "Filing it as a lesson waits for current Mnemos." in said


def test_older_correct_never_moves_a_belief(tmp_path):
    from mnemos.core.belief import Belief

    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture(
            "Riley prefers dark roast coffee in the morning.",
            impact="Morning coffee is dark.",
        )
        belief = Belief(
            agent_id="nova", content="Riley prefers dark roast coffee",
            confidence=0.6, source="agent",
        )
        runtime._store.save_belief(belief)
    finally:
        runtime.close()
    _claim_for_newer_code(db)
    beliefs = _belief_state(db)

    runtime = _runtime(db)
    try:
        updated = runtime.correct(
            "Riley prefers light roast coffee now.", query="Riley prefers dark roast coffee",
        )
        forgotten = runtime.correct("", query="Riley prefers dark roast coffee", action="forget")
    finally:
        runtime.close()

    assert _belief_state(db) == beliefs, "older code moved a belief"
    # The correction still lands on the memory it names.
    assert "Updated closest continuity note" in updated
    notes = [content for (content,) in _all(db, "SELECT content FROM hypomnema_entries")]
    assert "Riley prefers light roast coffee now." in notes
    assert OLDER in forgotten


def test_the_notice_ends_each_tool_result_only_when_older(tmp_path):
    db = tmp_path / "memory.db"
    tools = ("capture", "recall", "context", "reflect", "correct", "handoff", "introduce")

    def run(tag: str) -> dict[str, str]:
        runtime = _runtime(db)
        try:
            results = {"capture": runtime.capture(f"Riley waters the {tag} ferns on Sundays.")}
            [(memory,)] = _all(
                db, "SELECT id FROM engrams WHERE content = ?",
                (f"Riley waters the {tag} ferns on Sundays.",),
            )
            runtime._store.enqueue_reflection(
                "impact", memory, "What did this change?",
                agent_id="nova", person_id="riley", project_scope="demo",
            )
            results["recall"] = runtime.recall(f"{tag} ferns")
            results["context"] = runtime.context(f"{tag} ferns")
            results["reflect"] = runtime.reflect(memory, f"The {tag} ferns need a steady hand.")
            results["correct"] = runtime.correct(
                f"Riley waters the {tag} ferns on Saturdays.", query=f"{tag} ferns Sundays",
            )
            results["handoff"] = runtime.handoff(f"Left off repotting the {tag} ferns.")
            results["introduce"] = runtime.introduce("claude-opus-5-5")
            results["maintain"] = runtime.maintain()
            results["health"] = format_health_card(runtime.health())
        finally:
            runtime.close()
        return results

    current = run("maidenhair")
    for tool in (*tools, "maintain", "health"):
        assert OLDER not in current[tool], f"{tool} carried the notice on current code"

    _claim_for_newer_code(db)
    older = run("staghorn")
    for tool in tools:
        assert older[tool].splitlines()[-1] == OLDER, f"{tool} did not end with the notice"
        assert older[tool].count(OLDER) == 1, f"{tool} said it more than once"
    # These say it in their own words already, once.
    assert older["maintain"].count(OLDER) == 1
    assert older["health"].count(OLDER) == 1


def _dump(db) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_the_reset_backs_up_the_store_exactly_as_found(tmp_path, capsys):
    db = _seeded_store(tmp_path)
    # A store last opened by code from before the minimum existed holds no
    # minimum at all, and opening it for writing records one.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM meta WHERE key = 'min_code_version'")
        conn.commit()
    finally:
        conn.close()
    as_found = _dump(db)

    assert _reset(db, "--set", "5", "--write") == 0
    capsys.readouterr()

    [backup] = _backups(db)
    assert _dump(backup) == as_found, "the backup is not the store as the human found it"
    assert _store_minimum(backup) is None
    assert _store_minimum(db) == "5"


# ── Questions wait, captures stand alone, handoffs stay, versions only rise ──


def test_older_context_shows_no_questions_and_spends_no_showings(tmp_path):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture(
            "Riley plans every trip around the ferry timetable.",
            impact="Trips bend to the ferry.",
        )
        [(memory,)] = _all(db, "SELECT id FROM engrams")
        runtime._store._get_conn().execute("DELETE FROM reflection_queue")
        runtime._store._get_conn().commit()
        runtime._store.enqueue_reflection(
            "belief", memory, "Is there a belief here? [theme:ferry]",
            agent_id="nova", person_id="riley", project_scope="demo",
        )
        shown = runtime.context()
    finally:
        runtime.close()
    assert "Is there a belief here?" in shown, "premise: current code shows the question"

    _claim_for_newer_code(db)
    asks = _asks(db)
    runtime = _runtime(db)
    try:
        packet = runtime.context()
    finally:
        runtime.close()

    assert "Something of yours is waiting on you" not in packet
    assert "Is there a belief here?" not in packet, "older code presented a question"
    assert _asks(db) == asks, "older code spent a showing"


def test_older_capture_saves_its_own_shape_without_links(tmp_path, monkeypatch):
    """No links at save time on older code; the capture keeps everything that
    is its own. Maintenance under current code links it later."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the ferry timetable pinned beside the kitchen door.")
    finally:
        runtime.close()
    _claim_for_newer_code(db)

    text = "The ferry timetable pinned beside the kitchen door changed for winter."
    _turn_semantic_recall_on(monkeypatch)  # so the capture's vector can be checked
    runtime = _runtime(db)
    try:
        captured = runtime.capture(text)
    finally:
        runtime.close()
    assert "Captured continuity." in captured
    [(capture, kind, tags)] = _all(db, "SELECT id, kind, tags FROM engrams WHERE content = ?", (text,))

    def links() -> list[tuple]:
        return _all(
            db,
            "SELECT source_id, target_id, relation, formed_by FROM connections "
            "WHERE source_id = ? OR target_id = ?",
            (capture, capture),
        )

    assert links() == [], "older code linked the capture to other memories"
    # Its own shape is all there: classification, full-text index, vector.
    assert kind and json.loads(tags)
    assert (capture,) in _all(db, "SELECT id FROM engrams_fts WHERE engrams_fts MATCH 'winter'")
    assert _read(db, "SELECT COUNT(*) FROM embeddings WHERE engram_id = ?", (capture,))[0] == 1

    # Current code's maintenance finds the links later, by the words the two
    # memories share (semantic recall off again, so no vector does it).
    monkeypatch.undo()
    monkeypatch.setattr("mnemos.simple_runtime.MAINTENANCE_CODE_VERSION", AHEAD, raising=False)
    runtime = _runtime(db)
    try:
        maintained = runtime.maintain()
    finally:
        runtime.close()
    assert "connection_discovery" in maintained
    assert any(formed_by.startswith("consolidation") for *_, formed_by in links()), (
        "current code's connection discovery did not link the capture"
    )


def test_older_correct_writes_no_placeholder_meaning(tmp_path):
    """With no meaning given and none to carry over, a correction's
    replacement got a server-written placeholder where its meaning goes.
    Older code leaves it empty: the agent's words, or nothing."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley parks the bike behind the library.")
        runtime.capture("Riley stores the kayak in the garage.")
    finally:
        runtime.close()
    ids = _memory_ids(db)
    _claim_for_newer_code(db)

    runtime = _runtime(db)
    try:
        runtime.correct(
            "Riley parks the bike beside the library now.",
            target_id=ids["Riley parks the bike behind the library."],
        )
        runtime.correct("Riley stores the kayak at the marina now.", query="kayak garage")
    finally:
        runtime.close()

    for replacement in (
        "Riley parks the bike beside the library now.",
        "Riley stores the kayak at the marina now.",
    ):
        assert _all(
            db, "SELECT impact, impact_source FROM engrams WHERE content = ?", (replacement,),
        ) == [("", "")], f"older code wrote a placeholder meaning for {replacement!r}"


def test_older_handoff_retires_no_other_sessions_note(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        for n in range(1, 9):
            runtime._store.write_handoff(
                f"Session {n} left off here.", agent_id="nova", person_id="riley",
                project_scope="demo", author_session=f"session-{n}",
            )
    finally:
        runtime.close()

    def active() -> list[str]:
        return [
            session for (session,) in _all(
                db,
                "SELECT author_session FROM hypomnema_entries "
                "WHERE entry_kind = 'handoff' AND active = 1 ORDER BY created_at",
            )
        ]

    assert len(active()) == 8, "premise: eight sessions' notes, the most kept"
    _claim_for_newer_code(db)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-9")
    runtime = _runtime(db)
    try:
        said = runtime.handoff("Session 9 left off here.")
    finally:
        runtime.close()

    assert "Session handoff saved exactly as written." in said
    assert active() == [f"session-{n}" for n in range(1, 10)], (
        "older code retired another session's note"
    )

    # Current code retires them the next time it writes one.
    monkeypatch.setattr("mnemos.simple_runtime.MAINTENANCE_CODE_VERSION", AHEAD, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-10")
    runtime = _runtime(db)
    try:
        runtime.handoff("Session 10 left off here.")
    finally:
        runtime.close()
    assert active() == [f"session-{n}" for n in range(3, 11)]


def test_schema_version_only_ever_rises(tmp_path):
    from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

    db = _seeded_store(tmp_path)

    def stamp(value: int | None = None) -> str:
        if value is not None:
            conn = sqlite3.connect(str(db))
            try:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(value),)
                )
                conn.commit()
            finally:
                conn.close()
        return _read(db, "SELECT value FROM meta WHERE key = 'schema_version'")[0]

    # Newer code stamped the store; this code opens it, as a store and as a server.
    stamp(SCHEMA_VERSION + 1)
    EngramStore(str(db)).close()
    assert stamp() == str(SCHEMA_VERSION + 1), "older code lowered the schema version"
    runtime = _runtime(db)
    try:
        runtime.health()
    finally:
        runtime.close()
    assert stamp() == str(SCHEMA_VERSION + 1)

    # A store stamped lower is still raised to this code's version.
    stamp(SCHEMA_VERSION - 1)
    EngramStore(str(db)).close()
    assert stamp() == str(SCHEMA_VERSION)
