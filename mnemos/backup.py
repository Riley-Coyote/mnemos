"""Verified, private SQLite backup and restore operations."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .file_security import secure_directory, secure_file


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def check_database(path: str | Path) -> dict[str, Any]:
    """Open a database read-only and run SQLite's complete integrity check."""
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise RuntimeError("SQLite integrity check failed: " + "; ".join(rows[:10]))
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        engrams = 0
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engrams'"
        ).fetchone():
            engrams = conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
        schema_version = None
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            schema_version = row[0] if row else None
        return {
            "path": str(db_path),
            "integrity": "ok",
            "size_bytes": db_path.stat().st_size,
            "tables": tables,
            "engrams": engrams,
            "schema_version": schema_version,
        }
    finally:
        conn.close()


def create_backup(
    source: str | Path,
    destination: str | Path | None = None,
    *,
    source_connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create an online SQLite backup, then verify it before returning."""
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Database not found: {source_path}")
    check_database(source_path)

    if destination is None:
        backup_dir = source_path.parent / "backups"
        destination_path = backup_dir / f"{source_path.stem}-{_stamp()}.db"
    else:
        destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists():
        raise FileExistsError(f"Backup already exists: {destination_path}")

    secure_directory(destination_path.parent)
    temp_path = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.tmp"
    )
    destination_conn = sqlite3.connect(str(temp_path))
    opened_source = source_connection is None
    source_conn = source_connection or sqlite3.connect(str(source_path))
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        if opened_source:
            source_conn.close()

    secure_file(temp_path)
    try:
        check_database(temp_path)
        os.replace(temp_path, destination_path)
        secure_file(destination_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return check_database(destination_path)


def restore_backup(
    backup: str | Path,
    destination: str | Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Atomically restore a verified backup, preserving the current database."""
    backup_path = Path(backup).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    check_database(backup_path)
    if destination_path.exists() and not replace:
        raise FileExistsError(
            f"Destination exists: {destination_path}. Pass --force to replace it."
        )

    safety_backup = None
    if destination_path.exists():
        safety_backup = create_backup(destination_path)

    secure_directory(destination_path.parent)
    temp_path = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.restore.tmp"
    )
    source_conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(str(temp_path))
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        source_conn.close()
        destination_conn.close()
    secure_file(temp_path)
    try:
        check_database(temp_path)
        os.replace(temp_path, destination_path)
        secure_file(destination_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    result = check_database(destination_path)
    result["safety_backup"] = safety_backup["path"] if safety_backup else None
    return result
