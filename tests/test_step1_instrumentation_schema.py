"""Step 1 additive instrumentation schema tests."""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.store.migration_runner import MigrationLintError, lint_migration_sql
from mnemos.store.sqlite_store import EngramStore


def test_step1_schema_applies_on_virgin_store(tmp_path):
    db = tmp_path / "step1.db"
    store = EngramStore(db)
    store.close()

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "runtime_receipts" in tables
        assert "retrieval_events" in tables
        assert "retrieval_citations" in tables
        assert "drift_eval_runs" in tables
        assert "drift_eval_observations" in tables

        engram_cols = {
            row[1]: row for row in conn.execute("PRAGMA table_info(engrams)").fetchall()
        }
        assert "origin_stamp" in engram_cols
        assert engram_cols["origin_stamp"][3] == 0  # nullable legacy rows
        assert engram_cols["origin_stamp"][4] is None
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 12"
        ).fetchone()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_step1_migration_is_additive_only():
    sql = (
        "mnemos/store/migrations/0012_step1_instrumentation.sql"
    )
    with open(sql, encoding="utf-8") as handle:
        classes = lint_migration_sql(handle.read())

    assert "CREATE TABLE" in classes
    assert "ALTER TABLE ADD COLUMN" in classes
    assert "CREATE INDEX" in classes


def test_step1_migration_lint_rejects_dml():
    with pytest.raises(MigrationLintError):
        lint_migration_sql(
            """
            -- additive-only: yes
            INSERT INTO runtime_receipts (receipt_id) VALUES ('bad');
            """
        )
