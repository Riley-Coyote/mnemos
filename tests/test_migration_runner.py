"""Seven verification checks for the SQL-file migration runner (v1 §14 step 0).

Each check from the ratified spec (§Verification 1-7) is one or more tests, and
each carries its MUTATION PROOF: an assertion that the gate goes red when the
protected property is violated (a green that cannot go red is decoration).

The runner never touches ~/.mnemos — every test builds a virgin store in
tmp_path and points a fresh MigrationRunner at it with an isolated migrations
dir and snapshot root.
"""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mnemos.store.migration_runner import (
    GRANDFATHERED_CHECKSUM,
    MigrationError,
    MigrationLintError,
    MigrationRunner,
    classify_statement,
    discover_migration_files,
    lint_migration_sql,
    split_statements,
)
from mnemos.store.sqlite_store import EngramStore


# The frozen Python versions this build owns, plus the v1 baseline.
def _python_versions() -> list[int]:
    from mnemos.store.migrations import list_migrations

    versions = [int(m["version"]) for m in list_migrations()]
    if 1 not in versions:
        versions.append(1)
    return versions


def _fresh_store(tmp_path: Path, name: str = "store.db") -> Path:
    """Build a virgin store whose bootstrap grandfathers v1..SCHEMA_VERSION but
    applies NO shipped SQL-file migrations — so each test owns version space
    >= 11 with its own isolated migrations dir. We point the store's bootstrap
    runner at an empty migrations dir for the duration of construction.
    """
    db = tmp_path / name
    empty_dir = tmp_path / f".empty_migrations_{name}"
    empty_dir.mkdir(parents=True, exist_ok=True)
    import mnemos.store.migration_runner as mr

    original = mr.default_migrations_dir
    mr.default_migrations_dir = lambda: empty_dir
    try:
        EngramStore(db).close()
    finally:
        mr.default_migrations_dir = original
    return db


def _runner(db: Path, migrations_dir: Path, snapshot_root: Path) -> MigrationRunner:
    return MigrationRunner(
        db,
        migrations_dir=migrations_dir,
        snapshot_root=snapshot_root,
        known_python_versions=_python_versions(),
    )


def _write_migration(migrations_dir: Path, filename: str, body: str) -> Path:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / filename
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


ADDITIVE_0011 = """
-- purpose: test additive migration
-- v1 section: test
-- additive-only: yes
CREATE TABLE IF NOT EXISTS runner_probe (
    id TEXT PRIMARY KEY,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_runner_probe_note ON runner_probe(note);
"""


# ── Check (1): virgin-store run applies 0..N and doctor passes ──────────────


def test_check1_virgin_store_applies_and_integrity_ok(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    # The EngramStore bootstrap already grandfathered v1..N; point a runner at
    # our test migrations dir and apply the SQL file.
    runner = _runner(db, migrations, snapshots)
    applied = runner.apply()

    assert [a.version for a in applied] == [11]
    conn = sqlite3.connect(db)
    try:
        # schema_migrations carries grandfathered rows + the applied v11.
        rows = {
            int(r[0]): (r[1], r[2])
            for r in conn.execute(
                "SELECT version, checksum, name FROM schema_migrations"
            ).fetchall()
        }
        for v in _python_versions():
            assert rows[v][0] == GRANDFATHERED_CHECKSUM
        assert rows[11][0] != GRANDFATHERED_CHECKSUM  # real checksum
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runner_probe'"
        ).fetchone()
        # doctor == integrity_check passes
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_check1_mutation_missing_table_after_apply_would_fail(tmp_path):
    """MUTATION: if the migration SQL did not actually create the table, the
    post-apply assertion goes red — proving check (1) is not decoration."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    # A migration that creates a DIFFERENT table than the test expects.
    _write_migration(
        migrations,
        "0011_probe.sql",
        """
        -- additive-only: yes
        CREATE TABLE IF NOT EXISTS other_table (id TEXT PRIMARY KEY);
        """,
    )
    db = _fresh_store(tmp_path)
    _runner(db, migrations, snapshots).apply()
    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='runner_probe'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


# ── Check (2): re-run is a no-op ────────────────────────────────────────────


def test_check2_rerun_is_noop(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)

    first = runner.apply()
    assert [a.version for a in first] == [11]
    second = runner.apply()
    assert second == []  # nothing pending
    plan = runner.plan()
    assert plan.current_version == 11
    assert plan.pending == []


def test_check2_mutation_rerun_that_reapplied_would_double_insert(tmp_path):
    """MUTATION: the idempotency guard is 'skip versions already in
    schema_migrations'. Prove it fires by showing a second apply does NOT add a
    second schema_migrations row for v11 (a re-apply would violate the PRIMARY
    KEY and raise, or duplicate). Here we assert exactly one v11 row after two
    applies — remove the skip and this count becomes an IntegrityError."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)
    runner.apply()
    runner.apply()
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ── Check (3): kill -9 crash coverage ───────────────────────────────────────

# A helper script run as a subprocess so SIGKILL kills a real process at a
# controlled point. MNEMOS_CRASH_AT selects the crash site.
_CRASH_SCRIPT = r"""
import os, sys, signal
from pathlib import Path
from mnemos.store.migration_runner import MigrationRunner, snapshot_db
import mnemos.store.migration_runner as mr

db = Path(sys.argv[1])
migrations = Path(sys.argv[2])
snapshots = Path(sys.argv[3])
crash_at = os.environ["MNEMOS_CRASH_AT"]
py_versions = [int(v) for v in os.environ["MNEMOS_PY_VERSIONS"].split(",")]

runner = MigrationRunner(
    db, migrations_dir=migrations, snapshot_root=snapshots,
    known_python_versions=py_versions,
)

# Monkeypatch crash points.
_orig_snapshot = mr.snapshot_db
def crashing_snapshot(src, dest):
    if crash_at == "during-snapshot":
        # write a partial file then die
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"partial")
        os.kill(os.getpid(), signal.SIGKILL)
    result = _orig_snapshot(src, dest)
    if crash_at == "between-snapshot-and-begin":
        os.kill(os.getpid(), signal.SIGKILL)
    return result
mr.snapshot_db = crashing_snapshot

_orig_apply_one = runner._apply_one
import types
def apply_one_midmigration(self, conn, mig):
    # lint + snapshot happen, then we crash mid-transaction
    from mnemos.store.migration_runner import lint_migration_sql, split_statements
    lint_migration_sql(mig.sql)
    self._snapshot_or_resnapshot(conn, mig.version)
    for stmt in split_statements(mig.sql):
        conn.execute(stmt)
    # crash BEFORE commit -> transaction must roll back on next open
    os.kill(os.getpid(), signal.SIGKILL)
if crash_at == "mid-migration":
    runner._apply_one = types.MethodType(apply_one_midmigration, runner)

runner.apply()
print("COMPLETED")
"""


def _run_crash(db, migrations, snapshots, crash_at, tmp_path):
    script = tmp_path / "crash_runner.py"
    script.write_text(_CRASH_SCRIPT)
    env = dict(os.environ)
    env["MNEMOS_CRASH_AT"] = crash_at
    env["MNEMOS_PY_VERSIONS"] = ",".join(str(v) for v in _python_versions())
    env["MNEMOS_DISABLE_DOTENV"] = "1"
    proc = subprocess.run(
        [sys.executable, str(script), str(db), str(migrations), str(snapshots)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc


@pytest.mark.parametrize(
    "crash_at",
    ["mid-migration", "during-snapshot", "between-snapshot-and-begin"],
)
def test_check3_kill9_then_reapply_cleanly(tmp_path, crash_at):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)

    proc = _run_crash(db, migrations, snapshots, crash_at, tmp_path)
    # SIGKILL => returncode is -9 (or 137), and never prints COMPLETED.
    assert proc.returncode != 0
    assert "COMPLETED" not in proc.stdout

    # After the crash, the store must open and re-apply cleanly. No
    # partially-applied v11 may exist.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        v11 = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0]
        # mid-migration crashes before commit → no v11 row; snapshot crashes
        # happen before BEGIN → also no v11 row.
        assert v11 == 0
        # The probe table must not be half-present with a committed version row.
        probe = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runner_probe'"
        ).fetchone()
        if crash_at == "mid-migration":
            assert probe is None  # rolled back
    finally:
        conn.close()

    # Re-run applies cleanly.
    applied = _runner(db, migrations, snapshots).apply()
    assert [a.version for a in applied] == [11]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_check3_mutation_resnapshot_fires_when_data_version_moves(tmp_path):
    """MUTATION for the data_version re-snapshot path: if a writer moves
    data_version across every snapshot attempt, the runner must ABORT rather
    than accept a stale snapshot. We force data_version to always look moved and
    assert the named abort."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)

    import mnemos.store.migration_runner as mr

    counter = {"n": 0}

    def always_moving(conn):
        counter["n"] += 1
        return counter["n"]  # every read differs → window never clean

    original = mr._read_data_version
    mr._read_data_version = always_moving
    try:
        with pytest.raises(MigrationError, match="faithful snapshot"):
            runner.apply()
    finally:
        mr._read_data_version = original

    # And a clean data_version (stable) lets it apply — the re-snapshot logic
    # accepts a faithful window.
    applied = _runner(db, migrations, snapshots).apply()
    assert [a.version for a in applied] == [11]


def test_check3_snapshot_acceptance_reads_data_version_under_write_lock(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)

    import mnemos.store.migration_runner as mr

    lock_states = []

    def stable_version(conn):
        lock_states.append(conn.in_transaction)
        return 7

    original = mr._read_data_version
    mr._read_data_version = stable_version
    conn = sqlite3.connect(db)
    try:
        runner._snapshot_or_resnapshot(conn, 11)
        assert lock_states == [False, True]
        assert conn.in_transaction is True
    finally:
        conn.rollback()
        conn.close()
        mr._read_data_version = original


# ── Check (4): edited-history checksum → named abort ────────────────────────


def test_check4_checksum_incident_abort(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    path = _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)
    runner.apply()

    # Edit shipped history: change the file after it was applied.
    path.write_text(
        path.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError, match="checksum mismatch on applied version 11"):
        _runner(db, migrations, snapshots).apply()
    # plan must also surface the incident (it is what David reads first).
    with pytest.raises(MigrationError, match="checksum mismatch"):
        _runner(db, migrations, snapshots).plan()


def test_check4_mutation_unedited_history_passes(tmp_path):
    """MUTATION baseline: an UNEDITED file re-runs without a checksum abort —
    proving the abort keys on the edit, not on re-run itself."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)
    runner.apply()
    # No edit — second apply is a clean no-op, no MigrationError.
    assert _runner(db, migrations, snapshots).apply() == []


# ── Check (5): snapshot restore drill + delta report ────────────────────────


def test_check5_snapshot_restore_drill_and_delta(tmp_path):
    from mnemos.store.migration_runner import snapshot_db

    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    db = _fresh_store(tmp_path)

    # Baseline engram via a direct connection — reopening EngramStore would
    # re-run the shipped migrations and record a different v11 checksum than the
    # test's own 0011_probe, colliding with this test's isolated migrations dir.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engrams (id, content, content_at_encoding, created_at, "
        "last_accessed) VALUES ('pre_mig', 'pre-migration engram', "
        "'pre-migration engram', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    pre_count = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
    conn.close()

    # Take a manual snapshot (the drill's rollback primitive), then apply +
    # write a post-migration engram (the delta that restore would drop). The
    # post-migration write goes through a direct connection rather than
    # reopening EngramStore, because reopening would re-run the version-ahead
    # check against the test-injected v11 (correct in production, noise here).
    manual_snapshot = snapshots / "drill" / "before.db"
    snapshot_db(db, manual_snapshot)
    _runner(db, migrations, snapshots).apply()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engrams (id, content, content_at_encoding, created_at, "
        "last_accessed) VALUES ('post_mig', 'post-migration engram', "
        "'post-migration engram', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    post_count = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
    conn.close()
    assert post_count == pre_count + 1

    # Restore drill: move current aside, copy snapshot in, doctor passes.
    moved_aside = tmp_path / "moved-aside.db"
    Path(db).rename(moved_aside)
    snapshot_db(manual_snapshot, Path(db))  # copy snapshot into place
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored_count = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
    finally:
        conn.close()

    # Delta report line: restore dropped the post-migration engram.
    conn = sqlite3.connect(moved_aside)
    aside_count = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
    conn.close()
    delta = aside_count - restored_count
    assert delta == 1  # the post-migration engram lives only in moved-aside
    delta_report = (
        f"restore-delta: moved-aside engrams={aside_count}, "
        f"restored engrams={restored_count}, dropped={delta}"
    )
    assert "dropped=1" in delta_report


def test_check5_mutation_snapshot_is_not_file_copy(tmp_path):
    """MUTATION: snapshot must use the backup API (no WAL/SHM companions), not a
    file copy. Prove the snapshot is a self-contained integrity-checked db and
    that a WAL-mode source does not leave a -wal/-shm beside the snapshot."""
    from mnemos.store.migration_runner import snapshot_db

    db = _fresh_store(tmp_path)
    # Open in WAL mode and leave uncommitted-to-main data in the WAL.
    store = EngramStore(db)
    from mnemos.core.engram import Engram

    store.save_engram(Engram(content="wal-resident row"))
    # do NOT close (WAL not checkpointed to main db file)
    dest = tmp_path / "snapshots" / "backup.db"
    snapshot_db(db, dest)
    store.close()

    assert dest.exists()
    assert not dest.with_name("backup.db-wal").exists()
    assert not dest.with_name("backup.db-shm").exists()
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The WAL-resident row is present in the snapshot (backup API captures
        # it); a naive file copy of the main db would have missed it.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE content = 'wal-resident row'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


# ── Check (6): statement lint gate — THE gate ───────────────────────────────


@pytest.mark.parametrize(
    "banned_sql, match",
    [
        (
            "-- additive-only: yes\nDROP TABLE engrams;",
            "not on the additive-only allowlist",
        ),
        (
            "-- additive-only: yes\nCREATE TRIGGER t AFTER INSERT ON engrams "
            "BEGIN SELECT 1; END;",
            "CREATE TRIGGER",
        ),
        ("-- additive-only: yes\nPRAGMA journal_mode=DELETE;", "PRAGMA"),
        ("-- additive-only: yes\nVACUUM;", "VACUUM"),
        ("-- additive-only: yes\nATTACH DATABASE 'x.db' AS x;", "ATTACH"),
        (
            "-- additive-only: yes\nDELETE FROM engrams WHERE 1=1;",
            "not on the additive-only allowlist",
        ),
        (
            "-- additive-only: yes\nUPDATE engrams SET content='x';",
            "not on the additive-only allowlist",
        ),
        (
            "-- additive-only: yes\nALTER TABLE engrams DROP COLUMN content;",
            "not on the additive-only allowlist",
        ),
        (
            "-- additive-only: yes\nINSERT INTO engrams (id) VALUES ('x');",
            "not on the additive-only allowlist",
        ),
        (
            "-- additive-only: yes\nCREATE TABLE copied AS SELECT * FROM engrams;",
            "CREATE TABLE AS SELECT",
        ),
        (
            "-- additive-only: yes\n"
            "CREATE TABLE copied AS WITH source AS "
            "(SELECT * FROM engrams) SELECT * FROM source;",
            "CREATE TABLE AS SELECT",
        ),
        (
            "-- additive-only: yes\n"
            'CREATE TABLE "copied table" AS SELECT * FROM engrams;',
            "CREATE TABLE AS SELECT",
        ),
        (
            "-- additive-only: yes\n"
            "CREATE TABLE [copied table] AS WITH source AS "
            "(SELECT * FROM engrams) SELECT * FROM source;",
            "CREATE TABLE AS SELECT",
        ),
    ],
)
def test_check6_lint_bans_destructive_under_valid_attestation(banned_sql, match):
    """The migration carries a valid `-- additive-only: yes` line; the lint must
    STILL abort — the attestation is not the gate. This is the mutation proof:
    the gate goes red even when the human marker says green."""
    assert "additive-only: yes" in banned_sql  # attestation present
    with pytest.raises(MigrationLintError, match=match):
        lint_migration_sql(banned_sql)


@pytest.mark.parametrize(
    "statement, match",
    [
        (
            "ALTER TABLE engrams ADD COLUMN required TEXT NOT NULL;",
            "NOT NULL requires a non-NULL constant DEFAULT",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN c TEXT NOT NULL DEFAULT NULL;",
            "NOT NULL requires a non-NULL constant DEFAULT",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN c TEXT UNIQUE;",
            "PRIMARY KEY/UNIQUE",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN c TEXT PRIMARY KEY;",
            "PRIMARY KEY/UNIQUE",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN seen_at TEXT DEFAULT (datetime('now'));",
            "DEFAULT must be a constant literal",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN parent_id TEXT REFERENCES parent(id) DEFAULT 0;",
            "REFERENCES requires a NULL DEFAULT",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN calculated TEXT AS (content || id);",
            "AS/generated columns",
        ),
        (
            "ALTER TABLE engrams ADD COLUMN generated TEXT GENERATED ALWAYS AS (content || id);",
            "generated columns",
        ),
    ],
)
def test_check6_lint_bans_non_additive_add_column_shapes(statement, match):
    with pytest.raises(MigrationLintError, match=match):
        lint_migration_sql("-- additive-only: yes\n" + statement)


def test_check6_lint_passes_additive(tmp_path):
    """Positive: the allowed classes lint green and return their names."""
    classes = lint_migration_sql(ADDITIVE_0011)
    assert classes == ["CREATE TABLE", "CREATE INDEX"]
    assert classify_statement(
        "ALTER TABLE engrams ADD COLUMN foo TEXT"
    ) == "ALTER TABLE ADD COLUMN"
    assert (
        classify_statement("ALTER TABLE engrams ADD COLUMN x TEXT")
        == "ALTER TABLE ADD COLUMN"
    )
    assert (
        classify_statement("ALTER TABLE engrams ADD COLUMN y INTEGER DEFAULT 0")
        == "ALTER TABLE ADD COLUMN"
    )
    assert (
        classify_statement("ALTER TABLE engrams ADD COLUMN z TEXT NOT NULL DEFAULT 'x'")
        == "ALTER TABLE ADD COLUMN"
    )
    assert (
        classify_statement("ALTER TABLE engrams ADD COLUMN ref_id TEXT REFERENCES parent(id)")
        == "ALTER TABLE ADD COLUMN"
    )
    assert classify_statement("CREATE UNIQUE INDEX ux ON t(a)") in (
        "CREATE UNIQUE INDEX",
        "CREATE INDEX",
    )
    assert classify_statement("CREATE VIEW v AS SELECT 1") == "CREATE VIEW"
    assert (
        classify_statement("CREATE TABLE t (note TEXT DEFAULT 'schema_migrations')")
        == "CREATE TABLE"
    )


def test_check6_lint_bans_schema_migrations_tampering_with_statement_named():
    sql = """
    -- additive-only: yes
    INSERT INTO schema_migrations (version, name, checksum, applied_at, snapshot)
    VALUES (99, 'future', 'abc', '2026-01-01T00:00:00+00:00', 'x');
    """
    with pytest.raises(MigrationLintError) as excinfo:
        lint_migration_sql(sql)
    message = str(excinfo.value)
    assert "schema_migrations is runner-owned" in message
    assert "INSERT INTO schema_migrations" in message


@pytest.mark.parametrize(
    "statement",
    [
        'ALTER TABLE "main"."schema_migrations" ADD COLUMN rogue TEXT;',
        "ALTER TABLE [main].[schema_migrations] ADD COLUMN rogue TEXT;",
        "ALTER TABLE 'main'.'schema_migrations' ADD COLUMN rogue TEXT;",
    ],
)
def test_check6_lint_bans_qualified_schema_migrations_references(statement):
    with pytest.raises(MigrationLintError) as excinfo:
        lint_migration_sql("-- additive-only: yes\n" + statement)
    message = str(excinfo.value)
    assert "schema_migrations is runner-owned" in message
    assert "ALTER TABLE" in message


def test_check6_lint_ignores_string_literal_semicolons_and_comments():
    """The splitter must not be fooled by ';' inside string literals or by a
    banned keyword sitting inside a comment."""
    sql = (
        "-- DROP TABLE engrams (this is a comment, must be ignored)\n"
        "CREATE TABLE t (id TEXT, note TEXT DEFAULT 'a; b; c');"
    )
    classes = lint_migration_sql(sql)
    assert classes == ["CREATE TABLE"]
    stmts = split_statements("CREATE TABLE t (x TEXT DEFAULT 'has;semis');")
    assert len(stmts) == 1


def test_check6_apply_preserves_comment_markers_inside_literals(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(
        migrations,
        "0011_literal_markers.sql",
        """
        -- additive-only: yes
        CREATE TABLE literal_comment_probe (
            id TEXT PRIMARY KEY,
            note TEXT DEFAULT '-- literal marker',
            marker TEXT CHECK (marker != '/* blocked marker */')
        );
        """,
    )
    db = _fresh_store(tmp_path)
    applied = _runner(db, migrations, snapshots).apply()
    assert [a.version for a in applied] == [11]

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO literal_comment_probe (id, marker) VALUES ('a', 'ok')"
        )
        note = conn.execute(
            "SELECT note FROM literal_comment_probe WHERE id = 'a'"
        ).fetchone()[0]
        assert note == "-- literal marker"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO literal_comment_probe (id, marker) "
                "VALUES ('b', '/* blocked marker */')"
            )
    finally:
        conn.close()


def test_check6_apply_aborts_on_banned_statement(tmp_path):
    """End-to-end: a migration file with a banned statement aborts the apply and
    leaves the store untouched (no v-row, no side effects)."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(
        migrations,
        "0011_bad.sql",
        """
        -- additive-only: yes
        CREATE TABLE ok_table (id TEXT PRIMARY KEY);
        DROP TABLE engrams;
        """,
    )
    db = _fresh_store(tmp_path)
    with pytest.raises(MigrationLintError, match="not on the additive-only allowlist"):
        _runner(db, migrations, snapshots).apply()
    conn = sqlite3.connect(db)
    try:
        # engrams still exists; ok_table was never created (lint precedes apply).
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engrams'"
        ).fetchone()
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ok_table'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


# ── Check (7): duplicate version files → whole-set refusal ──────────────────


def test_check7_duplicate_version_whole_set_refusal(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_a.sql", "-- additive-only: yes\nCREATE TABLE a (id TEXT);")
    _write_migration(migrations, "0011_b.sql", "-- additive-only: yes\nCREATE TABLE b (id TEXT);")
    _write_migration(migrations, "0012_c.sql", "-- additive-only: yes\nCREATE TABLE c (id TEXT);")
    db = _fresh_store(tmp_path)
    with pytest.raises(MigrationError, match="duplicate migration version 11"):
        _runner(db, migrations, snapshots).apply()
    # Whole-set refusal: even the non-duplicate 0012 must NOT have applied.
    conn = sqlite3.connect(db)
    try:
        for t in ("a", "b", "c"):
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone()
                is None
            )
    finally:
        conn.close()


def test_check7_mutation_unique_versions_apply(tmp_path):
    """MUTATION baseline: distinct versions apply in order — proving the refusal
    keys on the collision, not on having multiple files."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_a.sql", "-- additive-only: yes\nCREATE TABLE ta (id TEXT);")
    _write_migration(migrations, "0012_b.sql", "-- additive-only: yes\nCREATE TABLE tb (id TEXT);")
    db = _fresh_store(tmp_path)
    applied = _runner(db, migrations, snapshots).apply()
    assert [a.version for a in applied] == [11, 12]


# ── Version-ahead fail-closed extension (spec §1, charter deliverable) ──────


def test_version_ahead_fail_closed_on_schema_migrations(tmp_path):
    """A schema_migrations row ahead of the binary's known-max version must make
    the store refuse to open (extends the meta.schema_version check to the new
    table)."""
    db = _fresh_store(tmp_path)
    # Inject a v99 row the binary does not understand.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at, snapshot)"
        " VALUES (99, 'from_the_future', 'abc', '2026-01-01T00:00:00+00:00', 'x')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="newer than this binary understands"):
        EngramStore(db)


def test_version_ahead_mutation_current_version_opens(tmp_path):
    """MUTATION baseline: a store whose max schema_migrations version equals the
    known-max opens fine — proving the guard keys on 'ahead', not on presence of
    the table."""
    db = _fresh_store(tmp_path)
    # Re-open — the highest grandfathered/applied version is <= known-max.
    store = EngramStore(db)
    store.close()


# ── §6 boundary: runner refuses a nonexistent store ─────────────────────────


def test_runner_refuses_missing_store(tmp_path):
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    missing = tmp_path / "does-not-exist.db"
    runner = MigrationRunner(
        missing,
        migrations_dir=migrations,
        snapshot_root=tmp_path / "snap",
        known_python_versions=_python_versions(),
    )
    with pytest.raises(FileNotFoundError):
        runner.apply()


def test_plan_refuses_missing_store_without_creating_file(tmp_path):
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    missing = tmp_path / "missing-plan.db"
    runner = MigrationRunner(
        missing,
        migrations_dir=migrations,
        snapshot_root=tmp_path / "snap",
        known_python_versions=_python_versions(),
    )

    with pytest.raises(FileNotFoundError, match="migration plan requires"):
        runner.plan()
    assert not missing.exists()


def test_plan_is_read_only_and_reports_legacy_meta_version(tmp_path):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '10')"
        )
        conn.commit()
    finally:
        conn.close()

    plan = _runner(legacy, migrations, snapshots).plan()
    assert plan.current_version == 10
    assert [p.version for p in plan.pending] == [11]

    conn = sqlite3.connect(legacy)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("method_name", ["plan", "apply"])
def test_meta_schema_version_ahead_refuses_before_migration_state_changes(
    tmp_path, method_name
):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    _write_migration(migrations, "0011_probe.sql", ADDITIVE_0011)
    future = tmp_path / "future-python.db"
    future_version = max(_python_versions()) + 1
    conn = sqlite3.connect(future)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(future_version),),
        )
        conn.commit()
    finally:
        conn.close()

    runner = _runner(future, migrations, snapshots)
    with pytest.raises(MigrationError, match="meta.schema_version .* newer"):
        getattr(runner, method_name)()

    conn = sqlite3.connect(future)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("method_name", ["plan", "apply"])
def test_sql_migration_version_collision_with_python_history_refuses(
    tmp_path, method_name
):
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    python_max = max(_python_versions())
    _write_migration(
        migrations,
        f"{python_max:04d}_collision.sql",
        "-- additive-only: yes\nCREATE TABLE python_overlap (id TEXT PRIMARY KEY);",
    )
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)

    with pytest.raises(MigrationError, match="collides with frozen Python"):
        getattr(runner, method_name)()

    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='python_overlap'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


# ── §3 receipts: bootstrap rule + drain after journal exists ────────────────


def test_receipts_bootstrap_then_drain(tmp_path):
    """The journal-creating migration (0011) leaves its receipt queued (the
    schema_migrations row is the receipt of record); the NEXT migration (0012)
    drains the queue into migration_receipts."""
    migrations = tmp_path / "migrations"
    snapshots = tmp_path / "snap"
    # 0011 creates the receipts journal (same shape as the shipped one).
    _write_migration(
        migrations,
        "0011_journal.sql",
        """
        -- additive-only: yes
        CREATE TABLE IF NOT EXISTS migration_receipts (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """,
    )
    db = _fresh_store(tmp_path)
    runner = _runner(db, migrations, snapshots)
    runner.apply()

    conn = sqlite3.connect(db)
    try:
        # After 0011: journal exists, v11's receipt drained into it (the drain
        # runs at the end of _apply_one, and the table now exists).
        v11_receipts = conn.execute(
            "SELECT COUNT(*) FROM migration_receipts WHERE version = 11"
        ).fetchone()[0]
    finally:
        conn.close()
    # v11 receipt is drained (journal existed by drain time within the same
    # apply call).
    assert v11_receipts == 1

    # Add 0012; applying it drains its receipt too.
    _write_migration(
        migrations,
        "0012_more.sql",
        "-- additive-only: yes\nCREATE TABLE more_t (id TEXT PRIMARY KEY);",
    )
    _runner(db, migrations, snapshots).apply()
    conn = sqlite3.connect(db)
    try:
        v12_receipts = conn.execute(
            "SELECT COUNT(*) FROM migration_receipts WHERE version = 12"
        ).fetchone()[0]
    finally:
        conn.close()
    assert v12_receipts == 1


# ── §6: the CLI refuses a path argument (one-store config only) ─────────────


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_cli_migrate_refuses_db_path_argument(verb, capsys):
    """Spec §6: the runner runs only against the canonical DB resolved through
    the one-store config; it refuses a path argument. MUTATION PROOF: passing
    --db-path to either migrate subcommand is an argparse error (exit 2),
    rejected before any store is touched."""
    from mnemos.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["migrate", verb, "--db-path", "/tmp/x.db"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--db-path" in err  # argparse names the unrecognized argument


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_cli_migrate_refuses_global_db_path_argument(verb, tmp_path, capsys):
    from mnemos.cli import main

    shadow = tmp_path / "shadow.db"
    rc = main(["--db-path", str(shadow), "migrate", verb])
    err = capsys.readouterr().err
    assert rc == 1
    assert "does not accept --db-path" in err
    assert not shadow.exists()


def test_cli_migrate_plan_resolves_via_one_store_config(tmp_path, monkeypatch, capsys):
    """Positive baseline for the §6 guard: the migrate verbs DO work through the
    one-store config surface (MNEMOS_DB_PATH), proving the refusal above removes
    the path flag, not the verb."""
    from mnemos.cli import main

    db = tmp_path / "canonical.db"
    EngramStore(db).close()  # bootstrap; shipped SQL migrations applied
    monkeypatch.setenv("MNEMOS_DB_PATH", str(db))
    rc = main(["migrate", "plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No pending migrations." in out


def test_cli_migrate_plan_resolves_via_store_db_path_config_env(
    tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main

    db = tmp_path / "configured.db"
    EngramStore(db).close()
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(db))

    rc = main(["migrate", "plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No pending migrations." in out


def test_cli_migrate_aborts_on_malformed_config_before_default_store(
    tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main

    home = tmp_path / "home"
    config_dir = home / ".mnemos"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    rc = main(["migrate", "plan"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not load one-store config" in err
    assert not (config_dir / "memory.db").exists()


@pytest.mark.parametrize("store_value", [[], "not-an-object", None])
def test_cli_migrate_aborts_on_malformed_store_config_shape(
    tmp_path, monkeypatch, capsys, store_value
):
    from mnemos.cli import main

    home = tmp_path / "home"
    config_dir = home / ".mnemos"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"store": store_value}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    rc = main(["migrate", "plan"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not load one-store config" in err
    assert "store section must be an object" in err
    assert not (config_dir / "memory.db").exists()


@pytest.mark.parametrize("db_path_value", [[], False, None, ""])
def test_cli_migrate_aborts_on_malformed_store_db_path_before_default_store(
    tmp_path, monkeypatch, capsys, db_path_value
):
    from mnemos.cli import main

    home = tmp_path / "home"
    config_dir = home / ".mnemos"
    config_dir.mkdir(parents=True)
    EngramStore(config_dir / "memory.db").close()
    (config_dir / "config.json").write_text(
        json.dumps({"store": {"db_path": db_path_value}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    rc = main(["migrate", "plan"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not load one-store config" in err
    assert "store.db_path must be a non-empty string path" in err


def test_cli_migrate_aborts_on_top_level_non_object_config(
    tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main

    home = tmp_path / "home"
    config_dir = home / ".mnemos"
    config_dir.mkdir(parents=True)
    EngramStore(config_dir / "memory.db").close()
    (config_dir / "config.json").write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)

    rc = main(["migrate", "plan"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not load one-store config" in err
    assert "top-level config must be an object" in err


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_cli_migrate_missing_store_returns_named_abort(
    verb, tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main

    missing = tmp_path / "missing.db"
    monkeypatch.setenv("MNEMOS_DB_PATH", str(missing))
    rc = main(["migrate", verb])
    err = capsys.readouterr().err
    assert rc == 1
    assert "migration aborted:" in err
    assert "requires an existing store" in err
    assert "Traceback" not in err


# ── Shipped default migration lints clean (the real 0011 in the tree) ───────


def test_shipped_migrations_all_lint_additive():
    from mnemos.store.migration_runner import default_migrations_dir

    files = discover_migration_files(default_migrations_dir())
    assert files, "expected at least the shipped 0011 receipts-journal migration"
    for mig in files:
        # Must lint clean AND carry the attestation line.
        lint_migration_sql(mig.sql)
        assert mig.has_attestation(), f"{mig.path.name} missing additive-only line"
