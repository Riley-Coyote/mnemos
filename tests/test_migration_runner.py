from __future__ import annotations

import sqlite3

import pytest

from mnemos.store import migrations


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(migrations, "_MIGRATIONS", {})


def test_migrations_are_ordered_and_versioned_atomically():
    conn = sqlite3.connect(":memory:")

    @migrations.register_migration(1, "create example")
    def first(db):
        db.execute("CREATE TABLE example (value TEXT)")

    @migrations.register_migration(2, "seed example")
    def second(db):
        db.execute("INSERT INTO example VALUES ('ready')")

    assert migrations.run_migrations(conn) == [1, 2]
    assert migrations.get_current_version(conn) == 2
    assert conn.execute("SELECT value FROM example").fetchone()[0] == "ready"
    assert migrations.run_migrations(conn) == []


def test_failed_migration_rolls_back_and_does_not_advance_version():
    conn = sqlite3.connect(":memory:")

    @migrations.register_migration(1, "fails")
    def broken(db):
        db.execute("CREATE TABLE should_rollback (value TEXT)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="Migration 1 failed"):
        migrations.run_migrations(conn)
    assert migrations.get_current_version(conn) == 0
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
    ).fetchone() is None
