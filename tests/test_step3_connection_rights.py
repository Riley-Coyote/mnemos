"""Step 3 S1 additive connection-rights schema tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mnemos.store.migration_runner as migration_runner_module
from mnemos.store.migration_runner import (
    MigrationLintError,
    MigrationRunner,
    default_migrations_dir,
    lint_migration_sql,
    split_statements,
)
from mnemos.store.sqlite_store import EngramStore


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "mnemos/store/migrations/0014_step3_connection_rights.sql"
)
LEGACY_COLUMNS = (
    "source_id",
    "target_id",
    "relation",
    "strength",
    "formed_at",
    "formed_by",
)
RIGHTS_COLUMNS = {
    "valid_at": "TEXT",
    "invalid_at": "TEXT",
    "confidence": "REAL",
    "runner_up_label": "TEXT",
    "runner_up_confidence": "REAL",
    "classifier_version": "TEXT",
}
EXPECTED_RIGHTS_STATEMENTS = (
    "ALTER TABLE connections ADD COLUMN valid_at TEXT",
    "ALTER TABLE connections ADD COLUMN invalid_at TEXT",
    "ALTER TABLE connections ADD COLUMN confidence REAL",
    "ALTER TABLE connections ADD COLUMN runner_up_label TEXT",
    "ALTER TABLE connections ADD COLUMN runner_up_confidence REAL",
    "ALTER TABLE connections ADD COLUMN classifier_version TEXT",
)
LEGACY_ROW = (
    "source-1",
    "target-1",
    "supports",
    0.75,
    "2026-07-09T21:00:00+00:00",
    "encoding",
)


def _python_versions() -> list[int]:
    from mnemos.store.migrations import list_migrations

    versions = [int(migration["version"]) for migration in list_migrations()]
    if 1 not in versions:
        versions.append(1)
    return versions


def _runner(db: Path, snapshot_root: Path) -> MigrationRunner:
    return MigrationRunner(
        db,
        migrations_dir=default_migrations_dir(),
        snapshot_root=snapshot_root,
        known_python_versions=_python_versions(),
    )


def _bootstrap_v13_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a populated v13 store without letting EngramStore auto-apply v14."""
    db = tmp_path / "pre-v14.db"
    empty_migrations = tmp_path / "empty-migrations"
    empty_migrations.mkdir()

    with monkeypatch.context() as patch:
        patch.setattr(
            migration_runner_module,
            "default_migrations_dir",
            lambda: empty_migrations,
        )
        EngramStore(db).close()

    applied = _runner(db, tmp_path / "v13-snapshots").apply(target_version=13)
    assert [migration.version for migration in applied] == [11, 12, 13]

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO connections "
            "(source_id, target_id, relation, strength, formed_at, formed_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            LEGACY_ROW,
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 14"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    return db


def _assert_rights_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
        for row in conn.execute("PRAGMA table_info(connections)").fetchall()
    }
    assert set(RIGHTS_COLUMNS).issubset(columns)
    for name, declared_type in RIGHTS_COLUMNS.items():
        assert columns[name] == {
            "type": declared_type,
            "notnull": 0,
            "default": None,
        }


def _assert_sql_contract(sql: str) -> None:
    assert lint_migration_sql(sql) == ["ALTER TABLE ADD COLUMN"] * 6
    statements = tuple(
        " ".join(statement.split()) for statement in split_statements(sql)
    )
    assert statements == EXPECTED_RIGHTS_STATEMENTS


def test_v14_applies_on_virgin_store_with_nullable_rights_columns(tmp_path):
    db = tmp_path / "virgin-v14.db"
    EngramStore(db).close()

    conn = sqlite3.connect(db)
    try:
        _assert_rights_columns(conn)
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 14"
        ).fetchone()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_v14_preserves_legacy_connection_and_reapply_is_noop(tmp_path, monkeypatch):
    db = _bootstrap_v13_store(tmp_path, monkeypatch)
    runner = _runner(db, tmp_path / "v14-snapshots")

    applied = runner.apply(target_version=14)
    assert [migration.version for migration in applied] == [14]

    conn = sqlite3.connect(db)
    try:
        _assert_rights_columns(conn)
        selected = ", ".join((*LEGACY_COLUMNS, *RIGHTS_COLUMNS))
        row = conn.execute(f"SELECT {selected} FROM connections").fetchone()
        assert row[: len(LEGACY_COLUMNS)] == LEGACY_ROW
        assert row[len(LEGACY_COLUMNS) :] == (None,) * len(RIGHTS_COLUMNS)
        assert conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 1
    finally:
        conn.close()

    assert runner.apply(target_version=14) == []
    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 14"
            ).fetchone()[0]
            == 1
        )
        selected = ", ".join((*LEGACY_COLUMNS, *RIGHTS_COLUMNS))
        assert conn.execute(f"SELECT {selected} FROM connections").fetchone() == (
            *LEGACY_ROW,
            *((None,) * len(RIGHTS_COLUMNS)),
        )
    finally:
        conn.close()


def test_v14_sql_is_exactly_six_nullable_additive_columns():
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "-- additive-only: yes" in sql
    _assert_sql_contract(sql)

    defaulted = sql.replace(
        "ADD COLUMN confidence REAL;",
        "ADD COLUMN confidence REAL DEFAULT 0.5;",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_sql_contract(defaulted)

    required = sql.replace(
        "ADD COLUMN confidence REAL;",
        "ADD COLUMN confidence REAL NOT NULL;",
        1,
    )
    with pytest.raises(MigrationLintError, match="NOT NULL"):
        _assert_sql_contract(required)

    constrained = sql.replace(
        "ADD COLUMN confidence REAL;",
        "ADD COLUMN confidence REAL CHECK (confidence BETWEEN 0 AND 1);",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_sql_contract(constrained)

    with pytest.raises(MigrationLintError, match="allowlist"):
        _assert_sql_contract(sql + "\nUPDATE connections SET confidence = 0.5;\n")
