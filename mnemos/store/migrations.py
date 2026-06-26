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
import time
from pathlib import Path
from typing import Callable


# Migration registry: version -> (description, migration_function)
_MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {}
_PAI_IMPORT_TARGET_TABLES = {"engrams", "beliefs", "hypomnema_entries"}


U3A_PHASE0_DECAY_FINDING = """
U3a Phase 0 finding: Mnemos has two decay paths. The consolidation decay pass
is time-based exponential forgetting, modulated by stability, connection count,
tags, and recency; ordinary unretrieved engrams can decay toward archival
thresholds. The substrate tick also performs a direct SQL time/tick decrement.
`decay_protected` is therefore load-bearing, not merely defensive: both decay
candidate paths must exclude protected engrams before mutating accessibility or
strength.
"""

U3A_U3B_IMPORT_CONTRACT = """
U3a/U3b contract:
- decay_protected guards accessibility, strength, dormancy, and archival
  transitions from forgetting paths. It does not make a row immune to
  content softening when softening_protected is false.
- softening_protected guards content, resolution, version snapshots, and voice
  exemplar use from softening paths. It does not make a row immune to decay
  when decay_protected is false.
- consolidation_authorized=false is a read-only quarantine for consolidation
  mutation paths until the importer or reviewer authorizes the row.
- pai_import_row_map's import key is (job_id, source_path, source_anchor,
  target_table). Re-running the same key repairs or updates the same target;
  it must not silently remap that source to a different target row.
"""


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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        raise RuntimeError(f"Malformed schema_version: {row[0]!r}")
    if version < 0:
        raise RuntimeError(f"Malformed schema_version: {row[0]!r}")
    return version


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
    if target_version is None:
        target_version = max(_MIGRATIONS.keys(), default=get_current_version(conn))

    current = get_current_version(conn)
    if current > target_version:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported {target_version}"
        )
    if current == 0 and not _has_table(conn, "engrams"):
        latest = max(_MIGRATIONS.keys(), default=target_version)
        if target_version < latest:
            raise RuntimeError(
                "Cannot bootstrap an empty database to historical schema "
                f"version {target_version}; latest schema is {latest}"
            )
        from .sqlite_store import SQL_CREATE_TABLES

        conn.executescript(SQL_CREATE_TABLES)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(target_version),),
        )
        conn.commit()
        current = target_version

    applied: list[int] = []

    for version, (_, migrate) in sorted(_MIGRATIONS.items()):
        if version <= current or version > target_version:
            continue
        try:
            conn.execute("BEGIN")
            migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(version),),
            )
            conn.commit()
            applied.append(version)
            current = version
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(f"Migration {version} failed") from exc

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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _row_value(row: sqlite3.Row | tuple, name: str, index: int):
    return row[name] if isinstance(row, sqlite3.Row) else row[index]


def _clean_required(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} is required")
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def apply_u3a_schema_migration(conn: sqlite3.Connection) -> None:
    """Apply the PAI import readiness schema additions idempotently."""
    for column, definition in (
        (
            "voice_exemplar_eligible",
            "INTEGER NOT NULL DEFAULT 1 CHECK (voice_exemplar_eligible IN (0, 1))",
        ),
        (
            "softening_protected",
            "INTEGER NOT NULL DEFAULT 0 CHECK (softening_protected IN (0, 1))",
        ),
        ("original_substrate", "TEXT"),
        ("original_timestamp", "INTEGER"),
        (
            "consolidation_authorized",
            "INTEGER NOT NULL DEFAULT 1 CHECK (consolidation_authorized IN (0, 1))",
        ),
        (
            "decay_protected",
            "INTEGER NOT NULL DEFAULT 0 CHECK (decay_protected IN (0, 1))",
        ),
    ):
        _add_column_if_missing(conn, "engrams", column, definition)

    for column, definition in (
        (
            "tier",
            "TEXT CHECK (tier IS NULL OR tier IN ('foundational', 'operational', 'tactical'))",
        ),
        (
            "needs_review",
            "INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1))",
        ),
        (
            "confidence_pending_review",
            "INTEGER NOT NULL DEFAULT 0 CHECK (confidence_pending_review IN (0, 1))",
        ),
    ):
        _add_column_if_missing(conn, "beliefs", column, definition)

    # Parent plan section 6.3.11 requires imported hypomnema rows to
    # preserve source time independently from created_at.
    _add_column_if_missing(conn, "hypomnema_entries", "original_timestamp", "INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pai_import_row_map (
            job_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_anchor TEXT NOT NULL DEFAULT '',
            target_table TEXT NOT NULL DEFAULT 'engrams'
                CHECK (target_table IN ('engrams', 'beliefs', 'hypomnema_entries')),
            target_id TEXT NOT NULL,
            engram_id TEXT,
            source_hash TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            imported_at INTEGER NOT NULL,
            PRIMARY KEY (job_id, source_path, source_anchor, target_table)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_target "
        "ON pai_import_row_map(target_table, target_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_job "
        "ON pai_import_row_map(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_engrams_decay_eligible "
        "ON engrams(owner_agent_id, state, decay_protected, consolidation_authorized)"
    )


@register_migration(4, "U3a PAI import schema readiness")
def migrate_v4_u3a(conn: sqlite3.Connection) -> None:
    apply_u3a_schema_migration(conn)


U3B_HARDENING_CONTRACT = """
U3b hardening v5 (additive, no U3a semantic changes):

pai_import_row_map gains:
- content_at_last_import: literal content the importer last wrote. _classify_row
  uses this to detect operator hand-edits and refuse silent REPAIR/UPDATE
  clobber. Without it, the row-map only tracks source_hash — the importer
  cannot tell "target still has what I wrote" from "operator changed the target."
- tombstone_at: epoch seconds when the mapped target was DELETED. Populated by
  AFTER DELETE triggers on engrams/beliefs/hypomnema_entries. _classify_row
  refuses REPAIR on tombstoned targets — hard-DELETE-then-reimport must be an
  explicit operator decision, not silent resurrection.
- agent_id / project_scope / source_kind / original_timestamp: source metadata
  the U3c watcher needs to reconcile SOUL/ edits against row-map without
  re-joining target tables every cycle. source_kind specifically prevents the
  `_infer_source_kind_for_target` fallback to identity_kernel from corrupting
  stale-row profile reconstruction.

beliefs gains:
- original_substrate / original_timestamp: provenance fields engrams + hypomnema
  already carry. Without them, a belief computed under claude-opus-4-6 cannot
  be down-weighted when substrate shifts to claude-opus-4-8.

pai_import_events: append-only audit table. One row per row-touched per apply.
After job B touches a target job A inserted, pai_import_row_map is overwritten
in place; without events, the operator cannot reconstruct "what did job X do"
post-mortem. versions.change_reason is per-engram and carries no job link.

Triggers: AFTER DELETE on engrams/beliefs/hypomnema_entries set
pai_import_row_map.tombstone_at for any matching mapped row. DB-level
enforcement — fires regardless of WHO deleted the target.
"""


def apply_u3b_hardening_schema_migration(conn: sqlite3.Connection) -> None:
    """Apply the U3b hardening schema additions idempotently.

    Strictly additive over U3a (v4). Existing rows get NULL for new columns;
    upsert_pai_import_row + the importer populate them on the next touch.
    """
    for column, definition in (
        ("content_at_last_import", "TEXT"),
        ("tombstone_at", "INTEGER"),
        ("agent_id", "TEXT"),
        ("project_scope", "TEXT"),
        ("source_kind", "TEXT"),
        ("original_timestamp", "INTEGER"),
    ):
        _add_column_if_missing(conn, "pai_import_row_map", column, definition)

    for column, definition in (
        ("original_substrate", "TEXT"),
        ("original_timestamp", "INTEGER"),
    ):
        _add_column_if_missing(conn, "beliefs", column, definition)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pai_import_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_anchor TEXT NOT NULL DEFAULT '',
            target_table TEXT NOT NULL
                CHECK (target_table IN ('engrams', 'beliefs', 'hypomnema_entries')),
            target_id TEXT NOT NULL,
            action TEXT NOT NULL,
            source_hash_before TEXT,
            source_hash_after TEXT,
            event_at INTEGER NOT NULL,
            change_reason TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pai_import_events_job "
        "ON pai_import_events(job_id, event_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pai_import_events_target "
        "ON pai_import_events(target_table, target_id)"
    )

    # Tombstone triggers — DB-level, fire on any DELETE regardless of caller.
    for table in ("engrams", "beliefs", "hypomnema_entries"):
        trigger_name = f"pai_import_{table}_tombstone"
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_name}
            AFTER DELETE ON {table}
            FOR EACH ROW
            BEGIN
                UPDATE pai_import_row_map
                SET tombstone_at = CAST(strftime('%s', 'now') AS INTEGER)
                WHERE target_table = '{table}' AND target_id = OLD.id
                  AND tombstone_at IS NULL;
            END
            """
        )


@register_migration(5, "U3b hardening: row-map extensions + pai_import_events + tombstone triggers")
def migrate_v5_u3b_hardening(conn: sqlite3.Connection) -> None:
    apply_u3b_hardening_schema_migration(conn)


def insert_pai_import_event(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    source_path: str,
    target_table: str,
    target_id: str,
    action: str,
    source_anchor: str = "",
    source_hash_before: str | None = None,
    source_hash_after: str | None = None,
    change_reason: str | None = None,
    timestamp: int | None = None,
) -> None:
    """Append a row to pai_import_events. Caller owns the transaction."""
    if target_table not in _PAI_IMPORT_TARGET_TABLES:
        raise ValueError(f"Unsupported target_table: {target_table}")
    now = int(timestamp if timestamp is not None else time.time())
    conn.execute(
        """
        INSERT INTO pai_import_events (
            job_id, source_path, source_anchor, target_table, target_id,
            action, source_hash_before, source_hash_after, event_at, change_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            source_path,
            source_anchor,
            target_table,
            target_id,
            action,
            source_hash_before,
            source_hash_after,
            now,
            change_reason,
        ),
    )


def upsert_pai_import_row(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    source_path: str,
    target_id: str | None = None,
    engram_id: str | None = None,
    source_anchor: str = "",
    target_table: str = "engrams",
    source_hash: str = "",
    timestamp: int | None = None,
    ensure_schema: bool = True,
    content_at_last_import: str | None = None,
    agent_id: str | None = None,
    project_scope: str | None = None,
    source_kind: str | None = None,
    original_timestamp: int | None = None,
) -> dict[str, int | bool]:
    """Idempotently record a PAI import source-to-row mapping.

    Re-running the same job/source/hash is a no-op. Changed source content
    advances updated_at AND content_at_last_import while preserving the mapped
    target identifier. A source key that already maps to one target cannot
    silently drift to another target; use a new source anchor or job boundary
    for that.

    U3b hardening fields (all optional, written on first INSERT and refreshed
    on UPDATE where they represent fresh state):
    - content_at_last_import: literal content the importer last wrote.
      Refreshed on every UPDATE because it tracks importer-side state.
    - agent_id / project_scope / source_kind / original_timestamp: source
      metadata. Written on INSERT; preserved on UPDATE unless caller
      explicitly overrides (defensive — these should be stable per source).
    """
    job_id = _clean_required(job_id, "job_id")
    source_path = _clean_required(source_path, "source_path")
    source_anchor = _clean_optional(source_anchor) or ""
    target_table = _clean_required(target_table, "target_table")
    if target_table not in _PAI_IMPORT_TARGET_TABLES:
        raise ValueError(f"Unsupported target_table: {target_table}")
    source_hash = _clean_optional(source_hash) or ""
    target_id = _clean_optional(target_id)
    engram_id = _clean_optional(engram_id)
    if target_table == "engrams":
        target_id = target_id or engram_id
        if not target_id:
            raise ValueError("target_id or engram_id is required")
        if engram_id is None:
            engram_id = target_id
    elif not target_id:
        raise ValueError(f"target_id is required for {target_table}")

    if ensure_schema:
        apply_u3a_schema_migration(conn)
        apply_u3b_hardening_schema_migration(conn)
    now = int(timestamp if timestamp is not None else time.time())
    existing = conn.execute(
        """
        SELECT target_id, engram_id, source_hash, created_at, updated_at,
               content_at_last_import, agent_id, project_scope, source_kind,
               original_timestamp
        FROM pai_import_row_map
        WHERE job_id = ? AND source_path = ? AND source_anchor = ? AND target_table = ?
        """,
        (job_id, source_path, source_anchor, target_table),
    ).fetchone()

    if existing is not None:
        existing_target_id = _row_value(existing, "target_id", 0)
        existing_engram_id = _row_value(existing, "engram_id", 1)
        existing_source_hash = _row_value(existing, "source_hash", 2)
        existing_created_at = _row_value(existing, "created_at", 3)
        existing_updated_at = _row_value(existing, "updated_at", 4)
        existing_content = _row_value(existing, "content_at_last_import", 5)
        existing_agent_id = _row_value(existing, "agent_id", 6)
        existing_project_scope = _row_value(existing, "project_scope", 7)
        existing_source_kind = _row_value(existing, "source_kind", 8)
        existing_original_ts = _row_value(existing, "original_timestamp", 9)
        if existing_target_id != target_id:
            raise ValueError(
                "PAI import source already maps to target "
                f"{existing_target_id!r}; refusing to remap to {target_id!r}"
            )
        effective_engram_id = engram_id
        if target_table != "engrams" and effective_engram_id is None:
            effective_engram_id = existing_engram_id or None
        same_engram = (existing_engram_id or None) == effective_engram_id
        same_hash = existing_source_hash == source_hash
        same_content = (
            content_at_last_import is None
            or existing_content == content_at_last_import
        )
        if same_engram and same_hash and same_content:
            return {
                "inserted": False,
                "updated": False,
                "created_at": int(existing_created_at),
                "updated_at": int(existing_updated_at),
            }
        new_content = (
            content_at_last_import
            if content_at_last_import is not None
            else existing_content
        )
        # Source metadata is write-once unless caller overrides.
        new_agent_id = agent_id if agent_id is not None else existing_agent_id
        new_project_scope = (
            project_scope if project_scope is not None else existing_project_scope
        )
        new_source_kind = (
            source_kind if source_kind is not None else existing_source_kind
        )
        new_original_ts = (
            original_timestamp
            if original_timestamp is not None
            else existing_original_ts
        )
        conn.execute(
            """
            UPDATE pai_import_row_map
            SET target_id = ?,
                engram_id = ?,
                source_hash = ?,
                updated_at = ?,
                content_at_last_import = ?,
                agent_id = ?,
                project_scope = ?,
                source_kind = ?,
                original_timestamp = ?
            WHERE job_id = ? AND source_path = ? AND source_anchor = ? AND target_table = ?
            """,
            (
                existing_target_id,
                effective_engram_id,
                source_hash,
                now,
                new_content,
                new_agent_id,
                new_project_scope,
                new_source_kind,
                new_original_ts,
                job_id,
                source_path,
                source_anchor,
                target_table,
            ),
        )
        return {
            "inserted": False,
            "updated": True,
            "created_at": int(existing_created_at),
            "updated_at": now,
        }

    conn.execute(
        """
        INSERT INTO pai_import_row_map (
            job_id, source_path, source_anchor, target_table, target_id,
            engram_id, source_hash, created_at, updated_at, imported_at,
            content_at_last_import, agent_id, project_scope, source_kind,
            original_timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            source_path,
            source_anchor,
            target_table,
            target_id,
            engram_id,
            source_hash,
            now,
            now,
            now,
            content_at_last_import,
            agent_id,
            project_scope,
            source_kind,
            original_timestamp,
        ),
    )
    return {"inserted": True, "updated": False, "created_at": now, "updated_at": now}


def backup_sqlite_db(source_db: str | Path, dest_db: str | Path) -> Path:
    """Create an atomic SQLite backup without copying WAL/SHM companions."""
    source = Path(source_db).expanduser()
    dest = Path(dest_db).expanduser()
    if not source.exists():
        raise FileNotFoundError(source)
    if source.resolve() == dest.resolve():
        raise ValueError("SQLite backup destination must differ from source")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp")
    if tmp.exists():
        tmp.unlink()

    source_conn = sqlite3.connect(str(source))
    dest_conn = sqlite3.connect(str(tmp))
    committed = False
    try:
        source_conn.backup(dest_conn)
        result = dest_conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {result}")
        dest_conn.commit()
        committed = True
    finally:
        dest_conn.close()
        source_conn.close()
        if not committed and tmp.exists():
            tmp.unlink()

    tmp.replace(dest)
    return dest
