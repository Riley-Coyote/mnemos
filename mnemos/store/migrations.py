"""
Schema migration management for Mnemos SQLite store.

Handles schema evolution by tracking the current schema version and
applying migration functions in order. Each migration is a function
that takes a SQLite connection and applies the schema changes.

Migration strategy:
- Schema version is tracked in the `meta` table
- Migrations are ordered by version number
- Each migration is applied in a transaction
- Forward-only (no rollback support — backup before migrating)
"""

from __future__ import annotations

import sqlite3
from typing import Callable


# Migration registry: version -> (description, migration_function)
_MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {}


def register_migration(
    version: int,
    description: str,
) -> Callable:
    """Decorator to register a schema migration function.

    Usage:
        @register_migration(2, "Add embedding column to engrams")
        def migrate_v2(conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE engrams ADD COLUMN embedding BLOB")

    Args:
        version: The schema version this migration upgrades TO.
        description: Human-readable description of the migration.

    Returns:
        Decorator function.
    """
    def decorator(func: Callable[[sqlite3.Connection], None]) -> Callable:
        _MIGRATIONS[version] = (description, func)
        return func
    return decorator


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the meta table.

    Args:
        conn: SQLite connection.

    Returns:
        Current schema version number. Returns 0 if meta table doesn't exist.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid schema_version: {row[0]!r}") from exc


def run_migrations(conn: sqlite3.Connection, target_version: int | None = None) -> list[int]:
    """Apply all pending migrations up to target_version.

    Args:
        conn: SQLite connection (with autocommit off).
        target_version: Version to migrate to. If None, migrates to latest.

    Returns:
        List of version numbers that were applied.

    Raises:
        RuntimeError: If a migration fails (transaction is rolled back).
    """
    current = get_current_version(conn)
    latest = max(_MIGRATIONS, default=current)
    target = latest if target_version is None else target_version
    if target < current:
        raise ValueError(
            f"Schema downgrade is not supported ({current} -> {target})"
        )
    pending = [
        version for version in sorted(_MIGRATIONS)
        if current < version <= target
    ]
    applied: list[int] = []
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()
    for version in pending:
        description, migration = _MIGRATIONS[version]
        try:
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(version),),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(
                f"Migration {version} failed ({description}): {exc}"
            ) from exc
        applied.append(version)
    return applied


def list_migrations() -> list[dict[str, str | int]]:
    """List all registered migrations.

    Returns:
        List of {"version": int, "description": str} dicts.
    """
    return [
        {"version": v, "description": desc}
        for v, (desc, _) in sorted(_MIGRATIONS.items())
    ]
