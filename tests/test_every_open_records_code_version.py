"""Every surface that writes through a store tells older code to stand down.

A store records the newest maintenance code version that has opened it
(``meta.min_code_version``), and code below that applies no rules to it. Only
the simple runtime recorded it, when it started. The session-start hook,
`mnemos search`, the prompt builder, the bridge, the advanced server and the
shared pool open their own stores and reinforce memories by current rules, so
a store they wrote could stay marked for the previous version, and a server
still running that version never learned to stand down.

Now every writable open of a store records the version, after its migrations,
and only ever raises it. A read-only open records nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION, OLDER_CODE_MESSAGE
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
CURRENT = str(MAINTENANCE_CODE_VERSION)
PREVIOUS = MAINTENANCE_CODE_VERSION - 1
FERRY = "Riley keeps the ferry timetable in the kitchen drawer."
CUE = "ferry timetable"


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _store_minimum(db) -> str | None:
    rows = _all(db, "SELECT value FROM meta WHERE key = 'min_code_version'")
    return rows[0][0] if rows else None


def _accesses(db) -> list[tuple]:
    return _all(db, "SELECT access_count FROM engrams ORDER BY id")


def _mark_for(db, version: int) -> None:
    """What a server of ``version`` leaves in a store it opened."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(version),),
        )
        conn.commit()
    finally:
        conn.close()


def _settled_sha256(path: Path) -> str:
    """Hash the store with everything written so far folded into the file."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _running(monkeypatch, version: int) -> None:
    """Run this process as Mnemos code of ``version``: every module that
    compares the version or records it."""
    for module in (
        "mnemos.code_version",
        "mnemos.simple_runtime",
        "mnemos.store.sqlite_store",
        "mnemos.retrieval.reactive",
    ):
        monkeypatch.setattr(f"{module}.MAINTENANCE_CODE_VERSION", version, raising=False)


def _memory_store(tmp_path) -> Path:
    """A store holding one memory, marked by the previous version."""
    db = tmp_path / "memory.db"
    runtime = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    try:
        runtime.capture(FERRY)
    finally:
        runtime.close()
    _mark_for(db, PREVIOUS)
    return db


def test_a_bare_open_records_the_code_version(tmp_path):
    fresh = tmp_path / "fresh.db"
    EngramStore(str(fresh)).close()
    assert _store_minimum(fresh) == CURRENT, "opening a new store recorded no code version"

    db = _memory_store(tmp_path)
    EngramStore(str(db)).close()
    assert _store_minimum(db) == CURRENT, (
        "opening a store for writing left it marked for older code"
    )

    # Only ever raised.
    _mark_for(db, 999)
    EngramStore(str(db)).close()
    assert _store_minimum(db) == "999"


def test_mnemos_search_records_the_code_version(tmp_path, capsys):
    db = _memory_store(tmp_path)
    assert _accesses(db) == [(0,)]

    assert main(["--db-path", str(db), *SCOPE_ARGS, "search", CUE]) == 0
    out = capsys.readouterr().out

    assert "ferry timetable" in out, "premise: search found the memory"
    assert _accesses(db) == [(1,)], "premise: search reinforced it by current rules"
    assert _store_minimum(db) == CURRENT, (
        "search wrote by current rules into a store still marked for older code"
    )


def test_the_session_start_hook_records_the_code_version(tmp_path, capsys):
    db = _memory_store(tmp_path)

    assert main(["hook", "session-start", "--db-path", str(db), *SCOPE_ARGS]) == 0
    packet = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]

    assert "ferry timetable" in packet, "premise: the hook built the packet"
    assert _store_minimum(db) == CURRENT


def test_the_shared_pool_records_the_code_version(tmp_path):
    from mnemos.multiagent.shared_pool import SharedPool

    db = tmp_path / "shared.db"
    SharedPool(str(db)).close()
    assert _store_minimum(db) == CURRENT, "the shared pool's store recorded no code version"

    _mark_for(db, PREVIOUS)
    SharedPool(str(db)).close()
    assert _store_minimum(db) == CURRENT


def test_an_older_server_stands_down_once_current_code_opens_the_store(
    tmp_path, monkeypatch
):
    db = tmp_path / "memory.db"
    _running(monkeypatch, PREVIOUS)
    older = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    try:
        older.capture(FERRY)
        assert _store_minimum(db) == str(PREVIOUS), "premise: the older server marked the store"

        # Current code opens the store for writing, as the hook, `mnemos
        # search` or a newer server does, while the older server runs on.
        _running(monkeypatch, MAINTENANCE_CODE_VERSION)
        EngramStore(str(db)).close()
        _running(monkeypatch, PREVIOUS)

        recalled = older.recall(CUE)
        maintained = older.maintain()
    finally:
        older.close()

    assert _accesses(db) == [(0,)], "the older server reinforced a memory by its rules"
    assert FERRY in recalled, "older code still returns what it finds"
    assert recalled.endswith(OLDER_CODE_MESSAGE), "the older server was not told it is older"
    assert "Cycle: skipped" in maintained


def test_a_busy_store_still_opens_and_the_next_open_records_it(tmp_path, monkeypatch):
    db = _memory_store(tmp_path)

    def locked(self, version):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as busy:
        busy.setattr(EngramStore, "raise_min_code_version", locked)
        store = EngramStore(str(db))
        try:
            assert store.get_meta("schema_version") is not None, "the store did not open"
        finally:
            store.close()
    assert _store_minimum(db) == str(PREVIOUS)

    EngramStore(str(db)).close()
    assert _store_minimum(db) == CURRENT, "the next open did not record the version"


def test_a_read_only_open_and_the_doctor_record_nothing(tmp_path):
    """A guard: a writable open would now mark the store, which R01's
    byte-identical doctor test cannot see on a store already marked current.

    No ``capsys`` here: doctor imports the MCP stdio client, which keeps
    whatever ``sys.stderr`` is at import as its default error log, and a
    ``capsys`` stream has no file descriptor for the stdio tests after it."""
    db = _memory_store(tmp_path)
    before = _settled_sha256(db)

    store = ReadOnlyEngramStore(db)
    try:
        assert store.min_code_version() == PREVIOUS
    finally:
        store.close()
    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0

    assert _store_minimum(db) == str(PREVIOUS), "a read-only check marked the store"
    assert _settled_sha256(db) == before, "a read-only check changed the store"
