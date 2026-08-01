"""
SQLite-backed engram storage with FTS5 full-text search.

Replaces Anima's JSON file persistence. Key advantages:
- Scales to 100K+ engrams without loading everything into memory
- FTS5 gives free full-text search with no external dependencies
- WAL mode for concurrent reads without locking
- Atomic transactions prevent corruption
- Still local-first, single file, portable

All tables are created on init. Migrations handle schema evolution.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..file_security import secure_directory, secure_file
from ..core.engram import Connection, Engram, VersionRef
from ..core.belief import Belief
from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity


# Schema version — increment when tables change
SCHEMA_VERSION = 7

VALID_FUNCTIONAL_TYPES = {
    "working",
    "preference",
    "fact",
    "decision",
    "commitment",
    "open_question",
    "correction",
    "profile",
    "project",
}

VALID_SESSION_STATUSES = {"active", "paused", "closed"}

# Upper bound on hypomnema rows considered for ranking. Deliberately far
# above any healthy continuity store: this is a backstop against a
# pathological store, not a relevance filter. See search_hypomnema.
_MAX_HYPOMNEMA_CANDIDATES = 5000

VALID_HYPO_SOURCES = {"observed", "synthesized", "co-formed"}
VALID_HYPO_ENTRY_KINDS = {
    "continuity",
    "handoff",
    "maintenance_report",
}
VALID_HYPO_AUTHORSHIP = {"agent", "system", "coauthored", "unknown"}
VALID_HYPO_DOMAINS = {
    "foundational",
    "identity",
    "recurring",
    "long-arc",
    "topical",
    "situational",
}

# Allowed column names for engrams table — prevents SQL injection via to_dict() keys
_ENGRAM_COLUMNS = frozenset({
    "id", "content", "content_at_encoding", "impact", "impact_source",
    "resolution", "kind", "tags",
    "schema_refs", "strength", "stability", "accessibility", "encoding_context",
    "source", "lineage", "owner_agent_id", "person_id", "project_scope",
    "visibility", "state", "created_at",
    "last_accessed", "access_count", "reconsolidation_count",
})

# Allowed column names for beliefs table
_BELIEF_COLUMNS = frozenset({
    "id", "agent_id", "content", "confidence", "domain", "created_at",
    "last_revised", "last_challenged", "revision_history", "superseded_by",
    "supporting_engram_ids", "source",
})

# Columns a store from before 0.2 may be missing, added on open by
# `_reconcile_columns`. Every definition (and its default) must match the
# corresponding column in SQL_CREATE_TABLES below. Only columns that carry a
# default are listed — a NOT NULL column without one cannot be added to a table
# that already has rows, and those (id, content, created_at, …) are structural
# originals present in any store that has the table.
_RECONCILABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "engrams": [
        ("impact", "impact TEXT NOT NULL DEFAULT ''"),
        ("impact_source", "impact_source TEXT NOT NULL DEFAULT ''"),
        ("resolution", "resolution REAL NOT NULL DEFAULT 1.0"),
        ("kind", "kind TEXT NOT NULL DEFAULT 'episodic'"),
        ("tags", "tags TEXT NOT NULL DEFAULT '[]'"),
        ("schema_refs", "schema_refs TEXT NOT NULL DEFAULT '[]'"),
        ("strength", "strength REAL NOT NULL DEFAULT 0.5"),
        ("stability", "stability REAL NOT NULL DEFAULT 0.1"),
        ("accessibility", "accessibility REAL NOT NULL DEFAULT 0.5"),
        ("encoding_context", "encoding_context TEXT NOT NULL DEFAULT '{}'"),
        ("source", "source TEXT NOT NULL DEFAULT '{}'"),
        ("lineage", "lineage TEXT NOT NULL DEFAULT '{}'"),
        ("owner_agent_id", "owner_agent_id TEXT NOT NULL DEFAULT 'default'"),
        ("person_id", "person_id TEXT"),
        ("project_scope", "project_scope TEXT"),
        ("visibility", "visibility TEXT NOT NULL DEFAULT 'private'"),
        ("state", "state TEXT NOT NULL DEFAULT 'active'"),
        ("access_count", "access_count INTEGER NOT NULL DEFAULT 0"),
        ("reconsolidation_count", "reconsolidation_count INTEGER NOT NULL DEFAULT 0"),
    ],
    "beliefs": [
        ("source", "source TEXT NOT NULL DEFAULT ''"),
    ],
    "consolidation_log": [
        ("agent_id", "agent_id TEXT"),
        ("person_id", "person_id TEXT"),
        ("project_scope", "project_scope TEXT"),
    ],
    "hypomnema_entries": [
        (
            "entry_kind",
            "entry_kind TEXT NOT NULL "
            "CHECK (entry_kind IN ('continuity', 'handoff', 'maintenance_report')) "
            "DEFAULT 'continuity'",
        ),
        (
            "authored_by",
            "authored_by TEXT NOT NULL "
            "CHECK (authored_by IN ('agent', 'system', 'coauthored', 'unknown')) "
            "DEFAULT 'unknown'",
        ),
        ("author_id", "author_id TEXT NOT NULL DEFAULT ''"),
        ("last_surfaced_at", "last_surfaced_at TEXT"),
        ("surface_count", "surface_count INTEGER NOT NULL DEFAULT 0"),
    ],
}


SQL_CREATE_TABLES = """
-- Core engram storage
CREATE TABLE IF NOT EXISTS engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
    impact_source TEXT NOT NULL DEFAULT '',
    resolution REAL NOT NULL DEFAULT 1.0,
    kind TEXT NOT NULL DEFAULT 'episodic',
    tags TEXT NOT NULL DEFAULT '[]',
    schema_refs TEXT NOT NULL DEFAULT '[]',
    strength REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.1,
    accessibility REAL NOT NULL DEFAULT 0.5,
    encoding_context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '{}',
    lineage TEXT NOT NULL DEFAULT '{}',
    owner_agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT,
    project_scope TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0
);

-- Full-text search on engram content
CREATE VIRTUAL TABLE IF NOT EXISTS engrams_fts USING fts5(
    content,
    id UNINDEXED
);

-- Typed connections between engrams
CREATE TABLE IF NOT EXISTS connections (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.5,
    formed_at TEXT NOT NULL,
    formed_by TEXT NOT NULL DEFAULT 'encoding',
    PRIMARY KEY (source_id, target_id, relation)
);

-- Reconsolidation version history
CREATE TABLE IF NOT EXISTS versions (
    engram_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content_snapshot TEXT NOT NULL,
    resolution_at_version REAL NOT NULL,
    changed_at TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT 'reconsolidation',
    PRIMARY KEY (engram_id, version_num)
);

-- Beliefs
CREATE TABLE IF NOT EXISTS beliefs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.3,
    domain TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    last_revised TEXT NOT NULL,
    last_challenged TEXT NOT NULL,
    revision_history TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    supporting_engram_ids TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT ''
);

-- Hypomnema: scoped durable continuity that can revise before promotion
CREATE TABLE IF NOT EXISTS hypomnema_entries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    entry_kind TEXT NOT NULL DEFAULT 'continuity'
        CHECK (entry_kind IN ('continuity', 'handoff', 'maintenance_report')),
    authored_by TEXT NOT NULL DEFAULT 'unknown'
        CHECK (authored_by IN ('agent', 'system', 'coauthored', 'unknown')),
    author_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'observed'
        CHECK (source IN ('observed', 'synthesized', 'co-formed')),
    density REAL NOT NULL DEFAULT 0.5,
    domain TEXT NOT NULL DEFAULT 'topical'
        CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    foundational INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    revisions_json TEXT NOT NULL DEFAULT '[]',
    related_session_id TEXT,
    related_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    graduated_to_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    superseded_by TEXT REFERENCES hypomnema_entries(id),
    created_at TEXT NOT NULL,
    last_revised_at TEXT NOT NULL,
    last_challenged_at TEXT,
    last_surfaced_at TEXT,
    surface_count INTEGER NOT NULL DEFAULT 0
);

-- Functional memory sessions: the active conversational frame
CREATE TABLE IF NOT EXISTS memory_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'mcp',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

-- Functional memory: current working context before it becomes continuity
CREATE TABLE IF NOT EXISTS functional_memories (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES memory_sessions(id) ON DELETE SET NULL,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'working'
        CHECK (memory_type IN (
            'working', 'preference', 'fact', 'decision', 'commitment',
            'open_question', 'correction', 'profile', 'project'
        )),
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    needs_confirmation INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'agent_observed',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    promoted_to_hypomnema_id TEXT REFERENCES hypomnema_entries(id) ON DELETE SET NULL
);

-- Emotional state history
CREATE TABLE IF NOT EXISTS emotional_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL DEFAULT 'default',
    curiosity REAL NOT NULL,
    restlessness REAL NOT NULL,
    warmth REAL NOT NULL,
    clarity REAL NOT NULL,
    creative_flow REAL NOT NULL,
    isolation REAL NOT NULL,
    timestamp TEXT NOT NULL
);

-- Agent identity
CREATE TABLE IF NOT EXISTS agent_identity (
    agent_id TEXT PRIMARY KEY,
    kernel_id TEXT NOT NULL,
    invariants TEXT NOT NULL DEFAULT '{}',
    evolution_rules TEXT NOT NULL DEFAULT '{}',
    epoch_state TEXT NOT NULL DEFAULT '{}',
    epoch_history TEXT NOT NULL DEFAULT '[]',
    memory_profile TEXT NOT NULL DEFAULT '{}'
);

-- Archived engrams (cold storage)
CREATE TABLE IF NOT EXISTS archive (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    kind TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    archived_at TEXT NOT NULL,
    archive_reason TEXT NOT NULL DEFAULT 'low_accessibility',
    final_accessibility REAL NOT NULL DEFAULT 0.0
);

-- Consolidation audit log
CREATE TABLE IF NOT EXISTS consolidation_log (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    person_id TEXT,
    project_scope TEXT,
    pass_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}'
);

-- Reflection queue: work the agent does on its own memory.
--
-- Mnemos never calls a model. Consolidation that needs judgement — what a
-- fading memory taught, whether a pattern is really a belief — is proposed
-- here by maintenance and performed by the agent itself, in its own turn and
-- its own words, through mnemos_reflect. The server proposes; it never
-- invents the answer.
CREATE TABLE IF NOT EXISTS reflection_queue (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    kind TEXT NOT NULL
        CHECK (kind IN ('impact', 'lesson', 'belief', 'contradiction')),
    target_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    -- How many times this has been shown. An agent that has declined to
    -- answer three times is answering; stop asking.
    surfaced_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    answered_at TEXT,
    answer TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_unique
    ON reflection_queue(agent_id, person_id, project_scope, kind, target_id);
CREATE INDEX IF NOT EXISTS idx_reflection_pending
    ON reflection_queue(agent_id, person_id, project_scope, surfaced_count, created_at)
    WHERE answered_at IS NULL;

-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_engrams_state ON engrams(state);
CREATE INDEX IF NOT EXISTS idx_engrams_accessibility ON engrams(accessibility DESC);
CREATE INDEX IF NOT EXISTS idx_engrams_kind ON engrams(kind);
CREATE INDEX IF NOT EXISTS idx_engrams_owner ON engrams(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_engrams_scope
    ON engrams(owner_agent_id, person_id, project_scope, state);
CREATE INDEX IF NOT EXISTS idx_engrams_last_accessed ON engrams(last_accessed);
CREATE INDEX IF NOT EXISTS idx_connections_source ON connections(source_id);
CREATE INDEX IF NOT EXISTS idx_connections_target ON connections(target_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_scope
    ON consolidation_log(agent_id, person_id, project_scope, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_beliefs_domain ON beliefs(agent_id, domain);
CREATE INDEX IF NOT EXISTS idx_hypomnema_scope_revised
    ON hypomnema_entries(agent_id, person_id, project_scope, last_revised_at DESC)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_hypomnema_promotion
    ON hypomnema_entries(agent_id, project_scope, created_at)
    WHERE active = 1 AND graduated_to_engram_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_hypomnema_one_active_handoff
    ON hypomnema_entries(agent_id, person_id, project_scope)
    WHERE active = 1 AND entry_kind = 'handoff';
CREATE INDEX IF NOT EXISTS idx_memory_sessions_scope
    ON memory_sessions(agent_id, person_id, project_scope, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_functional_scope
    ON functional_memories(agent_id, person_id, project_scope, updated_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_functional_session
    ON functional_memories(session_id, updated_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_functional_review
    ON functional_memories(agent_id, person_id, project_scope, updated_at DESC)
    WHERE is_deleted = 0 AND needs_confirmation = 1;
CREATE INDEX IF NOT EXISTS idx_emotional_history_agent ON emotional_state_history(agent_id, timestamp);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _split_tags(tags: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    return [str(tag).strip() for tag in tags if str(tag).strip()]


def _tokenize(text: str) -> set[str]:
    clean = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {token for token in clean.split() if len(token) > 2}


def _lexical_score(query: str, text: str) -> float:
    query_terms = _tokenize(query)
    if not query_terms:
        return 0.0
    text_terms = _tokenize(text)
    if not text_terms:
        return 0.0
    return len(query_terms & text_terms) / max(1, len(query_terms))


class EngramStore:
    """SQLite-backed storage for Mnemos engrams, beliefs, and identity.

    NOT thread-safe. Each thread should use its own EngramStore instance,
    or callers must synchronize access externally. SQLite WAL mode allows
    concurrent reads from separate connections, but writes must be serialized.

    Usage:
        store = EngramStore("~/.mnemos/memory.db")
        store.save_engram(engram)
        results = store.search_fts("debugging python")
        engram = store.get_engram("engram_abc123")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        private_root = Path.home() / ".mnemos"
        try:
            owned_directory = self.db_path.parent == private_root or self.db_path.parent.is_relative_to(private_root)
        except AttributeError:  # Python 3.8 compatibility for downstream imports
            owned_directory = str(self.db_path.parent).startswith(str(private_root))
        secure_directory(self.db_path.parent, force=owned_directory)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _secure_sqlite_files(self) -> None:
        """Keep the database and transient WAL files private."""

        for suffix in ("", "-wal", "-shm"):
            secure_file(f"{self.db_path}{suffix}")

    def _init_db(self) -> None:
        """Initialize database with schema.

        Reconciliation runs **before** the schema script, not after. The script
        creates indexes on engram columns (`state`, `accessibility`, …); on a
        store written by an earlier Mnemos whose `engrams` table lacks those
        columns, `CREATE INDEX` raises at open time — so a bare `ALTER` after
        `executescript` never even runs. Adding the missing columns first lets
        the rest of the script (which is all `IF NOT EXISTS`) apply cleanly.
        """
        conn = self._get_conn()
        self._backup_before_migration(conn)
        self._reconcile_columns(conn)
        conn.executescript(SQL_CREATE_TABLES)
        self._classify_legacy_hypomnema(conn)
        self._backfill_engram_scopes(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()
        integrity = [
            row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"]:
            details = "; ".join(integrity[:10]) or "no result"
            raise RuntimeError(
                f"SQLite integrity check failed after migration: {details}"
            )

    def _backup_before_migration(self, conn: sqlite3.Connection) -> None:
        """Create a verified recovery point before altering an older schema."""
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('engrams', 'hypomnema_entries') LIMIT 1"
        ).fetchone()
        if not existing:
            return

        version_row = None
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        try:
            current_version = int(version_row[0]) if version_row else 0
        except (TypeError, ValueError):
            current_version = 0
        if current_version >= SCHEMA_VERSION:
            return
        from ..backup import create_backup

        backup_dir = self.db_path.parent / "backups"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = backup_dir / f"{self.db_path.stem}.pre-v{SCHEMA_VERSION}-{stamp}.db"
        create_backup(self.db_path, destination, source_connection=conn)

    @staticmethod
    def _classify_legacy_hypomnema(conn: sqlite3.Connection) -> None:
        """Classify legacy rows without pretending ambiguous prose was agent-written."""

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(hypomnema_entries)")
        }
        if not {"entry_kind", "authored_by", "author_id"}.issubset(columns):
            return
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET entry_kind = 'maintenance_report',
                authored_by = 'system',
                author_id = 'mnemos'
            WHERE authored_by = 'unknown'
              AND tags_json LIKE '%"dream-journal"%'
            """
        )
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET authored_by = 'coauthored'
            WHERE authored_by = 'unknown' AND source = 'co-formed'
            """
        )

    @staticmethod
    def _backfill_engram_scopes(conn: sqlite3.Connection) -> None:
        """Backfill only legacy engrams with one unambiguous continuity scope.

        Ambiguous or unlinked legacy rows remain unscoped and are therefore
        quarantined from normal scoped reads. Guessing would risk disclosing a
        memory to the wrong person or project.
        """
        rows = conn.execute(
            """
            SELECT e.id, MIN(h.agent_id) AS agent_id,
                   MIN(h.person_id) AS person_id,
                   MIN(h.project_scope) AS project_scope,
                   COUNT(DISTINCT h.agent_id || char(31) || h.person_id || char(31) || h.project_scope) AS scopes
            FROM engrams e
            JOIN hypomnema_entries h
              ON h.related_engram_id = e.id OR h.graduated_to_engram_id = e.id
            WHERE e.person_id IS NULL OR e.person_id = ''
               OR e.project_scope IS NULL OR e.project_scope = ''
            GROUP BY e.id
            HAVING scopes = 1
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE engrams
                   SET owner_agent_id = ?, person_id = ?, project_scope = ?
                   WHERE id = ?""",
                (row["agent_id"], row["person_id"], row["project_scope"], row["id"]),
            )

    @staticmethod
    def _reconcile_columns(conn: sqlite3.Connection) -> None:
        """Add any expected column an older table is missing.

        `CREATE TABLE IF NOT EXISTS` never alters a table that already exists,
        so a database from before a column was added is not upgraded by the
        schema script alone — only `impact`/`impact_source` had one-off `ALTER`
        backfills, and a store missing any of the other 0.2 columns raised
        `OperationalError` in ordinary use. This adds every missing column with
        its schema default. It is idempotent (present columns are skipped), and
        it skips a table that does not exist yet (a fresh database — the schema
        script will create it in full).

        Every default here matches ``SQL_CREATE_TABLES``. Columns with no
        default (the structural originals: id, content, created_at, …) are not
        listed: they exist in any store old enough to have the table at all,
        and SQLite cannot ADD a NOT NULL column without a default to a
        populated table anyway.
        """
        for table, columns in _RECONCILABLE_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table absent — the schema script will create it whole
            for name, add_ddl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {add_ddl}")
                    # Some SQLite builds leave numeric defaults virtual after
                    # ALTER TABLE and then report those legacy rows as NULL
                    # during integrity_check. The column is brand new, so
                    # materialize its declared default for every old row.
                    _, marker, default_sql = add_ddl.partition(" DEFAULT ")
                    if marker:
                        conn.execute(
                            f"UPDATE {table} SET {name} = {default_sql}"
                        )

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create SQLite connection with WAL mode."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.set_trace_callback(
                lambda statement: self._secure_sqlite_files()
                if statement.lstrip().upper().startswith(("COMMIT", "END"))
                else None
            )
            self._secure_sqlite_files()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
        self._secure_sqlite_files()

    # ── Engram CRUD ──

    def save_engram(self, engram: Engram) -> None:
        """Insert or update an engram.

        All operations (engram table, FTS index, connections, versions) are
        wrapped in a single transaction for atomicity.
        """
        conn = self._get_conn()
        data = engram.to_dict()

        # Validate column names to prevent SQL injection
        safe_data = {k: v for k, v in data.items() if k in _ENGRAM_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in safe_data if k != "id")

        try:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                f"INSERT INTO engrams ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                list(safe_data.values()),
            )

            # Update FTS index (atomic with engram)
            conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram.id,))
            conn.execute(
                "INSERT INTO engrams_fts (id, content) VALUES (?, ?)",
                (engram.id, engram.content),
            )

            # Save connections
            for conn_obj in engram.connections:
                self._save_connection_no_commit(conn, engram.id, conn_obj)

            # Save versions
            for version in engram.versions:
                self._save_version_no_commit(conn, engram.id, version)

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def engram_visible_in_scope(
        self,
        engram_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> bool:
        """Whether an engram belongs to one exact person/project scope."""
        row = self._get_conn().execute(
            """SELECT 1 FROM engrams
               WHERE id = ? AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?""",
            (engram_id, agent_id, person_id, project_scope),
        ).fetchone()
        return row is not None

    def get_engram_in_scope(
        self, engram_id: str, *, agent_id: str, person_id: str, project_scope: str
    ) -> Engram | None:
        """Load an engram only when its complete scope matches."""
        if not self.engram_visible_in_scope(
            engram_id, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope,
        ):
            return None
        return self.get_engram(engram_id)

    def get_engram(self, engram_id: str) -> Engram | None:
        """Load an engram by ID, including connections and versions."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM engrams WHERE id = ?", (engram_id,)
        ).fetchone()
        if row is None:
            return None

        engram = Engram.from_dict(dict(row))

        # Load connections
        engram.connections = self.get_connections(engram_id)

        # Load versions
        engram.versions = self._get_versions(engram_id)

        return engram

    def get_active_engrams(
        self,
        agent_id: str | None = "default",
        limit: int = 1000,
        load_connections: bool = True,
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> list[Engram]:
        """Get all active engrams for an agent, sorted by accessibility.

        Args:
            agent_id: Which agent's engrams to return. If None, returns all
                agents' active engrams (useful for shared DB consolidation).
            load_connections: If True, load connections for each engram.
                Set to False for bulk operations where connections aren't needed
                (e.g., decay pass only needs accessibility/strength fields).
        """
        conn = self._get_conn()
        if agent_id is None:
            rows = conn.execute(
                "SELECT * FROM engrams WHERE state = 'active' "
                "ORDER BY accessibility DESC LIMIT ?",
                (limit,),
            ).fetchall()
        elif person_id is not None and project_scope is not None:
            rows = conn.execute(
                "SELECT * FROM engrams WHERE state = 'active' "
                "AND owner_agent_id = ? AND person_id = ? AND project_scope = ? "
                "ORDER BY accessibility DESC LIMIT ?",
                (agent_id, person_id, project_scope, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM engrams WHERE state = 'active' "
                "AND owner_agent_id = ? ORDER BY accessibility DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        engrams = [Engram.from_dict(dict(r)) for r in rows]
        if load_connections:
            for engram in engrams:
                engram.connections = self.get_connections(engram.id)
                engram.versions = self._get_versions(engram.id)
        return engrams

    def delete_engram(self, engram_id: str) -> None:
        """Remove an engram (use archive_engram for soft delete)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM engrams WHERE id = ?", (engram_id,))
        conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram_id,))
        conn.execute(
            "DELETE FROM connections WHERE source_id = ? OR target_id = ?",
            (engram_id, engram_id),
        )
        conn.execute("DELETE FROM versions WHERE engram_id = ?", (engram_id,))
        conn.commit()

    def count_engrams(self, agent_id: str | None = "default", state: str = "active") -> int:
        """Count engrams for an agent in a given state.

        Args:
            agent_id: Agent to count for. If None, counts all agents.
            state: Engram state to filter by.
        """
        conn = self._get_conn()
        if agent_id is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE state = ?",
                (state,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?",
                (agent_id, state),
            ).fetchone()
        return row[0] if row else 0

    # ── Full-Text Search ──

    def search_fts(
        self, query: str, limit: int = 50, *, agent_id: str | None = None,
        person_id: str | None = None, project_scope: str | None = None,
    ) -> list[Engram]:
        """Search engrams using FTS5 full-text search."""
        conn = self._get_conn()
        scope_sql = ""
        params: list[Any] = [query]
        if agent_id is not None and person_id is not None and project_scope is not None:
            scope_sql = " AND e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?"
            params.extend([agent_id, person_id, project_scope])
        params.append(limit)
        rows = conn.execute(
            "SELECT e.* FROM engrams e JOIN engrams_fts f ON e.id = f.id "
            "WHERE engrams_fts MATCH ? AND e.state = 'active'" + scope_sql +
            " ORDER BY rank LIMIT ?", params,
        ).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]

    # ── Connections ──

    def save_connection(self, source_id: str, conn_obj: Connection) -> None:
        """Save a typed connection (with auto-commit)."""
        conn = self._get_conn()
        self._save_connection_no_commit(conn, source_id, conn_obj)
        conn.commit()

    def _save_connection_no_commit(
        self, conn: sqlite3.Connection, source_id: str, conn_obj: Connection
    ) -> None:
        """Save a typed connection without committing (for use in transactions)."""
        conn.execute(
            "INSERT OR REPLACE INTO connections "
            "(source_id, target_id, relation, strength, formed_at, formed_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_id,
                conn_obj.target_id,
                conn_obj.relation,
                conn_obj.strength,
                conn_obj.formed_at,
                conn_obj.formed_by,
            ),
        )

    def get_connections(self, engram_id: str) -> list[Connection]:
        """Get all connections FROM an engram."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM connections WHERE source_id = ?", (engram_id,)
        ).fetchall()
        return [
            Connection(
                target_id=r["target_id"],
                relation=r["relation"],
                strength=r["strength"],
                formed_at=r["formed_at"],
                formed_by=r["formed_by"],
            )
            for r in rows
        ]

    def update_connection(self, source_id: str, connection) -> None:
        """Update an existing connection's relation, strength, or formed_by."""
        self._conn.execute(
            """UPDATE connections
               SET relation = ?, strength = ?, formed_by = ?
               WHERE source_id = ? AND target_id = ?""",
            (
                connection.relation.value if hasattr(connection.relation, 'value') else str(connection.relation),
                connection.strength,
                connection.formed_by,
                source_id,
                connection.target_id,
            ),
        )
        self._conn.commit()

    def remove_connection(self, source_id: str, target_id: str) -> None:
        """Remove a connection between two engrams."""
        self._conn.execute(
            "DELETE FROM connections WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        )
        self._conn.commit()

    def get_recent_engrams(
        self,
        agent_id: str | None = None,
        since: "datetime | None" = None,
        limit: int = 50,
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> list:
        """Get recently created engrams, optionally filtered by agent and time.

        Args:
            agent_id: Filter by agent ID (optional).
            since: Only return engrams created after this datetime (optional).
            limit: Maximum number to return.

        Returns:
            List of Engram objects, most recent first.
        """
        query = "SELECT * FROM engrams WHERE state = 'active'"
        params: list = []

        if agent_id:
            query += " AND owner_agent_id = ?"
            params.append(agent_id)

        if person_id is not None and project_scope is not None:
            query += " AND person_id = ? AND project_scope = ?"
            params.extend([person_id, project_scope])

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_engram(dict(r)) for r in rows]


    def get_connected_engram_ids(
        self,
        engram_id: str,
        max_depth: int = 2,
    ) -> set[str]:
        """Get IDs of engrams connected within max_depth hops."""
        visited: set[str] = set()
        frontier = {engram_id}

        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT target_id FROM connections WHERE source_id = ? "
                    "UNION SELECT source_id FROM connections WHERE target_id = ?",
                    (eid, eid),
                ).fetchall()
                next_frontier.update(r[0] for r in rows)
            frontier = next_frontier - visited

        visited.discard(engram_id)
        return visited

    # ── Versions ──

    def _save_version(self, engram_id: str, version: VersionRef) -> None:
        """Save a version snapshot (with auto-commit)."""
        conn = self._get_conn()
        self._save_version_no_commit(conn, engram_id, version)
        conn.commit()

    def _save_version_no_commit(
        self, conn: sqlite3.Connection, engram_id: str, version: VersionRef
    ) -> None:
        """Save a version snapshot without committing (for use in transactions)."""
        conn.execute(
            "INSERT OR REPLACE INTO versions "
            "(engram_id, version_num, content_snapshot, resolution_at_version, "
            "changed_at, change_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                engram_id,
                version.version_num,
                version.content_snapshot,
                version.resolution_at_version,
                version.changed_at,
                version.change_reason,
            ),
        )

    def _get_versions(self, engram_id: str) -> list[VersionRef]:
        """Get version history for an engram."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM versions WHERE engram_id = ? ORDER BY version_num",
            (engram_id,),
        ).fetchall()
        return [VersionRef.from_dict(dict(r)) for r in rows]

    # ── Archive ──

    def archive_engram(self, engram: Engram, reason: str = "low_accessibility") -> None:
        """Move engram to cold storage."""
        conn = self._get_conn()
        from datetime import datetime, timezone

        conn.execute(
            "INSERT OR REPLACE INTO archive "
            "(id, content, content_at_encoding, kind, tags, "
            "archived_at, archive_reason, final_accessibility) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                engram.id,
                engram.content,
                engram.content_at_encoding,
                engram.kind,
                json.dumps(engram.tags),
                datetime.now(timezone.utc).isoformat(),
                reason,
                engram.accessibility,
            ),
        )
        # Remove from active tables
        conn.execute("UPDATE engrams SET state = 'archived' WHERE id = ?", (engram.id,))
        conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram.id,))
        # An unanswered reflection request about an archived memory is a dead
        # end, and pre-fix rows carry a frozen copy of its text. Every path
        # that forgets something ends here, so this is where the request goes.
        conn.execute(
            "DELETE FROM reflection_queue WHERE target_id = ? AND answered_at IS NULL",
            (engram.id,),
        )
        conn.commit()

    def search_archive(self, query: str, limit: int = 20) -> list[dict]:
        """Search archived engrams by content (for resharpen)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM archive WHERE content LIKE ? OR content_at_encoding LIKE ? "
            "LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Beliefs ──

    def save_belief(self, belief: Belief) -> None:
        """Insert or update a belief."""
        conn = self._get_conn()
        data = belief.to_dict()

        # Validate column names
        safe_data = {k: v for k, v in data.items() if k in _BELIEF_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in safe_data if k != "id")

        conn.execute(
            f"INSERT INTO beliefs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(safe_data.values()),
        )
        conn.commit()

    def get_beliefs(
        self,
        agent_id: str = "default",
        domain: str | None = None,
        active_only: bool = True,
    ) -> list[Belief]:
        """Get beliefs for an agent, optionally filtered by domain."""
        conn = self._get_conn()
        query = "SELECT * FROM beliefs WHERE agent_id = ?"
        params: list[Any] = [agent_id]

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if active_only:
            query += " AND superseded_by IS NULL"

        query += " ORDER BY confidence DESC"
        rows = conn.execute(query, params).fetchall()
        return [Belief.from_dict(dict(r)) for r in rows]

    def get_belief(self, belief_id: str) -> Belief | None:
        """Load a single belief by id, or None."""
        row = self._get_conn().execute(
            "SELECT * FROM beliefs WHERE id = ?", (belief_id,)
        ).fetchone()
        return Belief.from_dict(dict(row)) if row else None

    def revise_belief(
        self, belief_id: str, new_confidence: float, reason: str,
        *, trigger_engram_id: str | None = None,
    ) -> bool:
        """Lower (or raise) a belief's confidence with an audit trail.

        The one deliberate way to erode a belief's confidence from the
        agent-facing side — the graph's stability ratchet otherwise only runs
        upward. Wraps ``Belief.revise`` (which clamps and records the change)
        and persists. Returns False if the belief is gone.
        """
        belief = self.get_belief(belief_id)
        if belief is None:
            return False
        belief.revise(new_confidence, reason, trigger_engram_id=trigger_engram_id)
        self.save_belief(belief)
        return True

    def supersede_belief(self, belief_id: str, *, reason: str = "") -> bool:
        """Retire a belief. It stops appearing in ``get_beliefs(active_only)``.

        Activates the built-but-never-called ``superseded_by`` plumbing: the
        row is kept for provenance but hidden from every read path. Records the
        retirement in the revision history so the reason survives.
        """
        belief = self.get_belief(belief_id)
        if belief is None:
            return False
        if reason:
            belief.revise(belief.confidence, f"superseded: {reason}")
        # A sentinel that reads as "retired" without pointing at a replacement.
        belief.superseded_by = belief.superseded_by or "retired"
        self.save_belief(belief)
        return True

    # ── Functional Memory ──

    def start_memory_session(
        self,
        *,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        title: str = "",
        source: str = "mcp",
    ) -> dict[str, Any]:
        """Start or reopen a functional memory session."""
        now = _utc_now()
        sid = (session_id or "").strip() or _new_id()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory_sessions(
                id, agent_id, person_id, project_scope, title, source,
                status, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                title = CASE
                    WHEN excluded.title != '' THEN excluded.title
                    ELSE memory_sessions.title
                END,
                source = excluded.source,
                status = 'active',
                updated_at = excluded.updated_at,
                closed_at = NULL
            """,
            (
                sid,
                agent_id,
                person_id,
                project_scope,
                title.strip(),
                source.strip() or "mcp",
                now,
                now,
            ),
        )
        conn.commit()
        session = self.get_memory_session(sid)
        if session is None:
            raise RuntimeError(f"Failed to start memory session: {sid}")
        return session

    def get_memory_session(self, session_id: str) -> dict[str, Any] | None:
        """Load a functional memory session by ID."""
        row = self._get_conn().execute(
            "SELECT * FROM memory_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def close_memory_session(
        self,
        session_id: str,
        *,
        status: str = "closed",
    ) -> dict[str, Any] | None:
        """Mark a functional memory session closed or paused."""
        if status not in VALID_SESSION_STATUSES:
            raise ValueError(f"Unsupported session status: {status}")
        now = _utc_now()
        closed_at = now if status == "closed" else None
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE memory_sessions
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (status, now, closed_at, session_id),
        )
        conn.commit()
        return self.get_memory_session(session_id)

    def write_functional_memory(
        self,
        content: str,
        *,
        memory_id: str | None = None,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        memory_type: str = "working",
        confidence: float = 0.65,
        salience: float = 0.5,
        needs_confirmation: bool = False,
        pinned: bool = False,
        source: str = "agent_observed",
        metadata: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Write or update a functional memory entry.

        Functional memory is the live, revisable working layer. It is useful
        for current task state, open questions, corrections, commitments, and
        preferences that have not yet earned hypomnema or engram status.
        """
        if memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")
        if not content.strip():
            raise ValueError("Functional memory content cannot be empty")

        now = _utc_now()
        fid = (memory_id or "").strip() or _new_id()
        session = (session_id or "").strip() or None
        conn = self._get_conn()
        if session and self.get_memory_session(session) is None:
            self.start_memory_session(
                session_id=session,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                title="Recovered session",
                source=source,
            )

        conn.execute(
            """
            INSERT INTO functional_memories(
                id, session_id, agent_id, person_id, project_scope, content,
                memory_type, confidence, salience, needs_confirmation, pinned,
                source, metadata_json, created_at, updated_at, expires_at,
                is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                content = excluded.content,
                memory_type = excluded.memory_type,
                confidence = excluded.confidence,
                salience = excluded.salience,
                needs_confirmation = excluded.needs_confirmation,
                pinned = excluded.pinned,
                source = excluded.source,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                is_deleted = 0
            """,
            (
                fid,
                session,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                memory_type,
                _clamp(confidence),
                _clamp(salience),
                int(needs_confirmation),
                int(pinned),
                source.strip() or "agent_observed",
                _encode_json(metadata or {}),
                now,
                now,
                expires_at,
            ),
        )
        if session:
            conn.execute(
                "UPDATE memory_sessions SET updated_at = ? WHERE id = ?",
                (now, session),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM functional_memories WHERE id = ?",
            (fid,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to write functional memory: {fid}")
        return self._hydrate_functional_row(dict(row))

    def get_functional_memory(
        self,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Load a functional memory by ID."""
        sql = "SELECT * FROM functional_memories WHERE id = ?"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        row = self._get_conn().execute(sql, (memory_id,)).fetchone()
        if row is None:
            return None
        return self._hydrate_functional_row(dict(row))

    def load_functional_memories(
        self,
        query: str = "",
        *,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        memory_type: str | None = None,
        needs_confirmation_only: bool = False,
        include_deleted: bool = False,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Search functional memories for the current scope/session."""
        if memory_type and memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")

        sql = (
            "SELECT * FROM functional_memories "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if needs_confirmation_only:
            sql += " AND needs_confirmation = 1"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT 200"

        rows = self._get_conn().execute(sql, params).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._hydrate_functional_row(dict(row))
            if query:
                score = (
                    _lexical_score(query, item["content"]) * 0.5
                    + float(item["confidence"]) * 0.2
                    + float(item["salience"]) * 0.25
                    + (0.05 if item["pinned"] else 0.0)
                )
            else:
                score = (
                    float(item["confidence"]) * 0.35
                    + float(item["salience"]) * 0.45
                    + (0.15 if item["pinned"] else 0.0)
                    + (0.05 if item["needs_confirmation"] else 0.0)
                )
            item["score"] = round(score, 4)
            scored.append(item)

        scored.sort(
            key=lambda item: (item["score"], item["pinned"], item["updated_at"]),
            reverse=True,
        )
        return scored[: max(1, limit)]

    def close_session_to_hypomnema(
        self,
        session_id: str,
        *,
        synthesis: str = "",
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any]:
        """Close a session and compress active functional memories into hypomnema."""
        session = self.get_memory_session(session_id)
        if session is None:
            raise KeyError(f"Functional memory session not found: {session_id}")

        memories = self.load_functional_memories(
            "",
            session_id=session_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            limit=50,
        )
        if synthesis.strip():
            content = synthesis.strip()
        else:
            chosen = memories[:8]
            details = "; ".join(
                f"{m['memory_type']}: {m['content']}" for m in chosen
            )
            title = session.get("title") or session_id
            content = (
                f"Session continuity from {title}: {details}"
                if details
                else f"Session {title} closed without durable functional memories."
            )

        confidence = (
            sum(float(m["confidence"]) for m in memories) / len(memories)
            if memories
            else 0.55
        )
        salience = max((float(m["salience"]) for m in memories), default=0.45)
        hypomnema_id = None
        if memories or synthesis.strip():
            hypomnema_id = self.write_hypomnema_entry(
                content,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                source="synthesized",
                authored_by="agent" if synthesis.strip() else "system",
                author_id=agent_id if synthesis.strip() else "mnemos",
                density=0.72,
                domain="situational",
                tags=["session-close", "functional-memory", project_scope],
                confidence=confidence,
                salience=salience,
                related_session_id=session_id,
            )

        now = _utc_now()
        conn = self._get_conn()
        if hypomnema_id:
            conn.execute(
                """
                UPDATE functional_memories
                SET is_deleted = 1,
                    promoted_to_hypomnema_id = ?,
                    updated_at = ?
                WHERE session_id = ? AND is_deleted = 0
                """,
                (hypomnema_id, now, session_id),
            )
        conn.execute(
            """
            UPDATE memory_sessions
            SET status = 'closed', updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (now, now, session_id),
        )
        conn.commit()
        return {
            "session": self.get_memory_session(session_id),
            "hypomnema_id": hypomnema_id,
            "functional_memories": len(memories),
            "content": content,
        }

    def get_functional_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict[str, int]:
        """Count active functional memory and session state."""
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        where_sql = " AND ".join(where)
        conn = self._get_conn()
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN is_deleted = 0 AND pinned = 1 THEN 1 ELSE 0 END) AS pinned,
              SUM(CASE WHEN is_deleted = 0 AND needs_confirmation = 1 THEN 1 ELSE 0 END) AS needs_confirmation
            FROM functional_memories
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        session_row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
            FROM memory_sessions
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        return {
            "functional_total": int(row["total"] or 0),
            "functional_active": int(row["active"] or 0),
            "functional_pinned": int(row["pinned"] or 0),
            "functional_needs_confirmation": int(row["needs_confirmation"] or 0),
            "functional_sessions_active": int(session_row["active"] or 0),
            "functional_sessions_closed": int(session_row["closed"] or 0),
        }

    @staticmethod
    def _hydrate_functional_row(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = _decode_json(row.pop("metadata_json", "{}"), {})
        row["needs_confirmation"] = bool(row["needs_confirmation"])
        row["pinned"] = bool(row["pinned"])
        row["is_deleted"] = bool(row["is_deleted"])
        return row

    # ── Hypomnema ──

    def write_hypomnema_entry(
        self,
        content: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        entry_kind: str = "continuity",
        authored_by: str | None = None,
        author_id: str = "",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        """Write a scoped hypomnema continuity entry.

        Hypomnema is durable, relationship-scoped continuity that can be
        revised before it graduates into shared Mnemos engrams.
        """
        if source not in VALID_HYPO_SOURCES:
            raise ValueError(f"Unsupported hypomnema source: {source}")
        if authored_by is None:
            authored_by = "coauthored" if source == "co-formed" else "unknown"
        if entry_kind not in VALID_HYPO_ENTRY_KINDS:
            raise ValueError(f"Unsupported hypomnema entry kind: {entry_kind}")
        if authored_by not in VALID_HYPO_AUTHORSHIP:
            raise ValueError(f"Unsupported hypomnema authorship: {authored_by}")
        if domain not in VALID_HYPO_DOMAINS:
            raise ValueError(f"Unsupported hypomnema domain: {domain}")
        if not content.strip():
            raise ValueError("Hypomnema content cannot be empty")

        now = _utc_now()
        entry_id = _new_id()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO hypomnema_entries(
                id, agent_id, person_id, project_scope, content,
                entry_kind, authored_by, author_id, source,
                density, domain, tags_json, confidence, salience,
                active, foundational, revision_count, revisions_json,
                related_session_id, related_engram_id, created_at, last_revised_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, '[]', ?, ?, ?, ?)
            """,
            (
                entry_id,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                entry_kind,
                authored_by,
                author_id.strip(),
                source,
                _clamp(density),
                domain,
                _encode_json(_split_tags(tags)),
                _clamp(confidence),
                _clamp(salience),
                int(foundational),
                related_session_id,
                related_engram_id,
                now,
                now,
            ),
        )
        conn.commit()
        return entry_id

    def write_handoff(
        self,
        text: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        author_id: str = "",
    ) -> str:
        """Atomically replace the active handoff while preserving exact prose."""

        if not text.strip():
            raise ValueError("Handoff text cannot be empty")

        conn = self._get_conn()
        new_id = _new_id()
        now = _utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                """
                SELECT * FROM hypomnema_entries
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND entry_kind = 'handoff' AND active = 1
                LIMIT 1
                """,
                (agent_id, person_id, project_scope),
            ).fetchone()
            if prior is not None:
                revisions = _decode_json(prior["revisions_json"], [])
                revisions.append({
                    "at": now,
                    "prior_content": prior["content"],
                    "reason": "superseded: newer agent-written session handoff",
                })
                conn.execute(
                    """
                    UPDATE hypomnema_entries
                    SET active = 0,
                        revision_count = revision_count + 1,
                        revisions_json = ?, last_revised_at = ?
                    WHERE id = ?
                    """,
                    (_encode_json(revisions), now, prior["id"]),
                )

            conn.execute(
                """
                INSERT INTO hypomnema_entries(
                    id, agent_id, person_id, project_scope, content,
                    entry_kind, authored_by, author_id, source,
                    density, domain, tags_json, confidence, salience,
                    active, foundational, revision_count, revisions_json,
                    created_at, last_revised_at, surface_count
                ) VALUES (?, ?, ?, ?, ?, 'handoff', 'agent', ?, 'observed',
                          0.9, 'situational', ?, 1.0, 1.0,
                          1, 0, 0, '[]', ?, ?, 0)
                """,
                (
                    new_id,
                    agent_id,
                    person_id,
                    project_scope,
                    text,
                    (author_id or agent_id).strip(),
                    _encode_json(["session-handoff", "continuity"]),
                    now,
                    now,
                ),
            )
            if prior is not None:
                conn.execute(
                    "UPDATE hypomnema_entries SET superseded_by = ? WHERE id = ?",
                    (new_id, prior["id"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return new_id

    def get_latest_handoff(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = True,
    ) -> dict[str, Any] | None:
        """Return the newest handoff in this exact scope."""

        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND entry_kind = 'handoff'"
        )
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self._get_conn().execute(
            sql, (agent_id, person_id, project_scope)
        ).fetchone()
        return self._hydrate_hypomnema_row(dict(row)) if row else None

    def mark_handoff_surfaced(
        self,
        handoff_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> bool:
        """Record a real delivery of the currently active scoped handoff."""

        cursor = self._get_conn().execute(
            """
            UPDATE hypomnema_entries
            SET last_surfaced_at = ?, surface_count = surface_count + 1
            WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
              AND entry_kind = 'handoff' AND active = 1
            """,
            (_utc_now(), handoff_id, agent_id, person_id, project_scope),
        )
        self._get_conn().commit()
        return cursor.rowcount == 1

    def get_hypomnema_entry(
        self,
        entry_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = False,
    ) -> dict[str, Any] | None:
        """Load a hypomnema entry by scoped ID."""
        conn = self._get_conn()
        query = (
            "SELECT * FROM hypomnema_entries "
            "WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [entry_id, agent_id, person_id, project_scope]
        if active_only:
            query += " AND active = 1"
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._hydrate_hypomnema_row(dict(row))

    def get_hypomnema_entry_for_engram(
        self,
        engram_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any] | None:
        """The active continuity note a memory was captured as, if any.

        Capture writes both an engram and a scoped hypomnema note, linked by
        ``related_engram_id``. Anything that has learned something *about* an
        engram needs this to reach the layer the session packet is built from;
        writing only to the engram puts it somewhere the automatic path does
        not read.

        Returns None for engrams encoded outside the simple capture path,
        which legitimately have no note.
        """
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE related_engram_id = ?
              AND agent_id = ? AND person_id = ? AND project_scope = ?
              AND active = 1
            ORDER BY last_revised_at DESC
            LIMIT 1
            """,
            (engram_id, agent_id, person_id, project_scope),
        ).fetchone()
        return self._hydrate_hypomnema_row(dict(row)) if row else None

    def archive_hypomnema_for_engram(
        self,
        engram_id: str,
        *,
        reason: str,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> int:
        """Deactivate every scoped continuity note linked to an engram."""

        rows = self._get_conn().execute(
            """
            SELECT id FROM hypomnema_entries
            WHERE active = 1
              AND agent_id = ? AND person_id = ? AND project_scope = ?
              AND (related_engram_id = ? OR graduated_to_engram_id = ?)
            """,
            (agent_id, person_id, project_scope, engram_id, engram_id),
        ).fetchall()
        for row in rows:
            self.archive_hypomnema_entry(
                row["id"],
                reason=reason,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
            )
        return len(rows)

    def search_hypomnema(
        self,
        query: str = "",
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 8,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Search scoped hypomnema entries by text, confidence, and salience."""
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if not include_inactive:
            sql += " AND active = 1"
        # Every note in scope is scored. A cap applied *before* scoring is a
        # silent amnesia: at 200 notes the old `LIMIT 100` made half of an
        # agent's continuity unreachable no matter how relevant it was, and
        # the packet still returned its full eight entries and looked
        # healthy. Continuity is a small, curated layer by design — scoring
        # a few thousand rows in Python costs milliseconds, and a store that
        # has grown past the ceiling below has a different problem than
        # ranking.
        sql += (
            " ORDER BY foundational DESC, last_revised_at DESC"
            f" LIMIT {_MAX_HYPOMNEMA_CANDIDATES}"
        )
        rows = conn.execute(sql, params).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._hydrate_hypomnema_row(dict(row))
            score = (
                _lexical_score(query, item["content"]) * 0.55
                + float(item["confidence"]) * 0.2
                + float(item["salience"]) * 0.2
                + (0.05 if item["foundational"] else 0.0)
            )
            if not query:
                score = (
                    float(item["confidence"]) * 0.4
                    + float(item["salience"]) * 0.4
                    + (0.1 if item["foundational"] else 0.0)
                )
            item["score"] = round(score, 4)
            scored.append(item)

        scored.sort(
            key=lambda item: (item["score"], item["last_revised_at"]),
            reverse=True,
        )
        return scored[: max(1, limit)]

    def get_hypomnema_entries_by_tag(
        self,
        tag: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Scoped hypomnema entries carrying an exact tag, newest first."""
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND tags_json LIKE ?"
        )
        # Quote-delimited match keeps the tag token-exact inside the JSON
        # array (so "dream" never matches "dream-journal").
        params: list[Any] = [agent_id, person_id, project_scope, f'%"{tag}"%']
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY last_revised_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = conn.execute(sql, params).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def revise_hypomnema_entry(
        self,
        entry_id: str,
        new_content: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        confidence: float | None = None,
        salience: float | None = None,
    ) -> str:
        """Revise an existing hypomnema entry while preserving the old version."""
        if not new_content.strip():
            raise ValueError("Revised hypomnema content cannot be empty")
        if not reason.strip():
            raise ValueError("Revision reason cannot be empty")

        now = _utc_now()
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
            """,
            (entry_id, agent_id, person_id, project_scope),
        ).fetchone()
        if row is None:
            raise KeyError(f"Hypomnema entry not found for scope: {entry_id}")

        revisions = _decode_json(row["revisions_json"], [])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": reason.strip(),
            }
        )
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET content = ?,
                confidence = ?,
                salience = ?,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (
                new_content.strip(),
                _clamp(confidence if confidence is not None else row["confidence"]),
                _clamp(salience if salience is not None else row["salience"]),
                _encode_json(revisions),
                now,
                entry_id,
            ),
        )
        conn.commit()
        return entry_id

    def supersede_hypomnema_entry(
        self,
        entry_id: str,
        new_content: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> str:
        """Replace an active hypomnema entry with a new entry and audit link."""
        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        new_id = self.write_hypomnema_entry(
            new_content,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            source=row["source"],
            entry_kind=row["entry_kind"],
            authored_by=row["authored_by"],
            author_id=row["author_id"],
            density=row["density"],
            domain=row["domain"],
            tags=row["tags"],
            confidence=row["confidence"],
            salience=row["salience"],
            foundational=row["foundational"],
            related_session_id=row["related_session_id"],
            related_engram_id=row["related_engram_id"],
        )

        now = _utc_now()
        revisions = list(row["revisions"])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": f"superseded: {reason.strip()}",
            }
        )
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET active = 0,
                superseded_by = ?,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (new_id, _encode_json(revisions), now, entry_id),
        )
        conn.commit()
        return new_id

    def archive_hypomnema_entry(
        self,
        entry_id: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> str:
        """Deactivate a scoped hypomnema entry while preserving its revision trail."""
        if not reason.strip():
            raise ValueError("Archive reason cannot be empty")

        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        now = _utc_now()
        revisions = list(row["revisions"])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": f"archived: {reason.strip()}",
            }
        )
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET active = 0,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (_encode_json(revisions), now, entry_id),
        )
        conn.commit()
        return entry_id

    def mark_hypomnema_promoted(self, entry_id: str, engram_id: str) -> None:
        """Record that a hypomnema entry graduated into a Mnemos engram."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE hypomnema_entries SET graduated_to_engram_id = ? WHERE id = ?",
            (engram_id, entry_id),
        )
        conn.commit()

    def get_hypomnema_promotion_candidates(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List stable hypomnema entries ready to become Mnemos engrams."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND active = 1
              AND entry_kind = 'continuity'
              AND graduated_to_engram_id IS NULL
              AND confidence >= 0.82
              AND salience >= 0.65
              AND (revision_count >= 1 OR foundational = 1)
            ORDER BY foundational DESC, confidence DESC, salience DESC, created_at ASC
            LIMIT ?
            """,
            (agent_id, person_id, project_scope, limit),
        ).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def get_hypomnema_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict[str, int]:
        """Count hypomnema entries for a scope."""
        conn = self._get_conn()
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        where_sql = " AND ".join(where)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN foundational = 1 AND active = 1 THEN 1 ELSE 0 END) AS foundational,
              SUM(CASE WHEN graduated_to_engram_id IS NOT NULL THEN 1 ELSE 0 END) AS promoted
            FROM hypomnema_entries
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        candidate_query = (
            "SELECT COUNT(*) FROM hypomnema_entries "
            f"WHERE {where_sql} "
            "AND active = 1 "
            "AND entry_kind = 'continuity' "
            "AND graduated_to_engram_id IS NULL "
            "AND confidence >= 0.82 "
            "AND salience >= 0.65 "
            "AND (revision_count >= 1 OR foundational = 1)"
        )
        candidate_row = conn.execute(candidate_query, params).fetchone()
        candidates = int(candidate_row[0] or 0)
        return {
            "hypomnema_total": int(row["total"] or 0),
            "hypomnema_active": int(row["active"] or 0),
            "hypomnema_foundational": int(row["foundational"] or 0),
            "hypomnema_promoted": int(row["promoted"] or 0),
            "hypomnema_promotion_candidates": candidates,
        }

    @staticmethod
    def _hydrate_hypomnema_row(row: dict[str, Any]) -> dict[str, Any]:
        row["tags"] = _decode_json(row.pop("tags_json", "[]"), [])
        row["revisions"] = _decode_json(row.pop("revisions_json", "[]"), [])
        row["active"] = bool(row["active"])
        row["foundational"] = bool(row["foundational"])
        return row

    # ── Emotional State ──

    def save_emotional_state(
        self, state: EmotionalState, agent_id: str = "default"
    ) -> None:
        """Save an emotional state snapshot to history."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO emotional_state_history "
            "(agent_id, curiosity, restlessness, warmth, clarity, "
            "creative_flow, isolation, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                state.curiosity,
                state.restlessness,
                state.warmth,
                state.clarity,
                state.creative_flow,
                state.isolation,
                state.timestamp,
            ),
        )
        conn.commit()

    def get_latest_emotional_state(
        self, agent_id: str = "default"
    ) -> EmotionalState | None:
        """Get the most recent emotional state for an agent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM emotional_state_history "
            "WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return EmotionalState.from_dict(dict(row))

    # ── Identity ──

    # ── Reflection queue ──────────────────────────────────────────────
    #
    # Work the agent does on its own memory. Maintenance proposes; the agent
    # answers through mnemos_reflect. Nothing here ever writes an answer on
    # the agent's behalf.

    MAX_SURFACINGS = 3

    def enqueue_reflection(
        self,
        kind: str,
        target_id: str,
        prompt: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        expires_in_days: int = 30,
    ) -> str | None:
        """Propose a reflection. Returns None if this was already asked.

        Asking twice about the same memory is nagging, so the unique index
        makes a repeat enqueue a no-op rather than a duplicate.

        No excerpt is stored. The queue holds a ``target_id`` and nothing else
        quotable, so ``pending_reflections`` resolves the text live and a
        forgotten memory has no second copy here to leak from.
        """
        if kind not in {"impact", "lesson", "belief", "contradiction"}:
            raise ValueError(f"Unsupported reflection kind: {kind}")

        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=expires_in_days)).isoformat()
        entry_id = _new_id()
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO reflection_queue(
                    id, agent_id, person_id, project_scope, kind, target_id,
                    prompt, excerpt, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (entry_id, agent_id, person_id, project_scope, kind, target_id,
                 prompt, now.isoformat(), expires),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Already asked. The failed INSERT leaves an open transaction, so
            # it must be rolled back — otherwise the next write on this
            # connection dies with "cannot start a transaction within a
            # transaction", far from the cause.
            conn.rollback()
            return None
        return entry_id

    def pending_reflections(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Unanswered reflections worth showing, oldest and least-shown first.

        ``excerpt`` is resolved from the engram **at read time**, and the join
        drops any request whose subject is gone or archived. Both properties
        are load-bearing:

        * The queue used to carry a frozen ``content[:160]`` snapshot, so a
          memory the human had asked Mnemos to forget was read back to the
          agent until the surfacing quota ran out. Clearing by quota is not
          deletion.
        * A request pointing at an archived engram is a dead end — ``reflect()``
          answers "the memory is no longer there" — so surfacing one spends the
          agent's turn to reach nothing.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            """
            SELECT q.*, e.content AS live_content
            FROM reflection_queue q
            JOIN engrams e ON e.id = q.target_id
            WHERE q.agent_id = ? AND q.person_id = ? AND q.project_scope = ?
              AND q.answered_at IS NULL
              AND q.surfaced_count < ?
              AND (q.expires_at IS NULL OR q.expires_at > ?)
              AND e.state != 'archived'
            ORDER BY q.surfaced_count ASC, q.created_at ASC
            LIMIT ?
            """,
            (agent_id, person_id, project_scope, self.MAX_SURFACINGS, now, limit),
        ).fetchall()

        items = []
        for row in rows:
            item = dict(row)
            item["excerpt"] = " ".join((item.pop("live_content") or "").split())[:160]
            items.append(item)
        return items

    def purge_stale_reflections(self) -> int:
        """Drop unanswered requests whose subject is archived or gone.

        ``pending_reflections`` already refuses to surface these, so this is
        about the copy on disk rather than the one on screen. Stores written
        before excerpts were removed still hold a frozen ``content[:160]`` of
        memories the human may since have asked Mnemos to forget, and no
        deletion path reached it. Existing stores do not otherwise self-heal.

        Returns the number of rows removed.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            DELETE FROM reflection_queue
            WHERE answered_at IS NULL
              AND target_id NOT IN (SELECT id FROM engrams WHERE state != 'archived')
            """
        )
        conn.commit()
        return cur.rowcount or 0

    def mark_reflections_surfaced(self, ids: list[str]) -> None:
        """Record that these were shown, so an ignored item eventually stops."""
        if not ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE reflection_queue SET surfaced_count = surfaced_count + 1 WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()

    def answer_reflection(
        self,
        target_id: str,
        answer: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any] | None:
        """Record the agent's answer. Returns the item, or None if not pending."""
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM reflection_queue
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND target_id = ? AND answered_at IS NULL
            ORDER BY created_at ASC LIMIT 1
            """,
            (agent_id, person_id, project_scope, target_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reflection_queue SET answered_at = ?, answer = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), answer, row["id"]),
        )
        conn.commit()
        return dict(row)

    def reflection_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, int]:
        conn = self._get_conn()
        scope = (agent_id, person_id, project_scope)
        where = "agent_id = ? AND person_id = ? AND project_scope = ?"
        pending = conn.execute(
            f"SELECT COUNT(*) FROM reflection_queue WHERE {where} AND answered_at IS NULL",
            scope,
        ).fetchone()[0]
        answered = conn.execute(
            f"SELECT COUNT(*) FROM reflection_queue WHERE {where} AND answered_at IS NOT NULL",
            scope,
        ).fetchone()[0]
        return {"pending": pending, "answered": answered}

    def save_identity(self, identity: AgentIdentity) -> None:
        """Save identity while preserving its append-only kernel and history."""
        conn = self._get_conn()
        agent_id = identity.memory_profile.agent_id
        existing = self.get_identity(agent_id)
        if existing is not None:
            if identity.kernel_id != existing.kernel_id:
                raise ValueError("Identity kernel_id is immutable")
            self._validate_identity_invariants(existing.invariants, identity.invariants)
            old_history = [epoch.to_dict() for epoch in existing.epoch_history]
            new_history = [epoch.to_dict() for epoch in identity.epoch_history]
            if new_history[:len(old_history)] != old_history:
                raise ValueError("Identity epoch history is append-only")
            if identity.epoch_state.epoch_number < existing.epoch_state.epoch_number:
                raise ValueError("Identity epoch number cannot move backward")
        data = identity.to_dict()
        data["agent_id"] = agent_id
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "agent_id")

        conn.execute(
            f"INSERT INTO agent_identity ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
            list(data.values()),
        )
        conn.commit()

    @classmethod
    def _validate_identity_invariants(cls, old: Any, new: Any, path: str = "invariants") -> None:
        """Reject removal or rewriting of any existing invariant value."""
        if isinstance(old, dict):
            if not isinstance(new, dict):
                raise ValueError(f"Identity {path} cannot change type")
            for key, value in old.items():
                if key not in new:
                    raise ValueError(f"Identity {path}.{key} cannot be removed")
                cls._validate_identity_invariants(value, new[key], f"{path}.{key}")
            return
        if isinstance(old, list):
            if not isinstance(new, list) or new[:len(old)] != old:
                raise ValueError(f"Identity {path} is append-only")
            return
        if new != old:
            raise ValueError(f"Identity {path} is immutable")

    def get_identity(self, agent_id: str = "default") -> AgentIdentity | None:
        """Load agent identity."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM agent_identity WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        return AgentIdentity.from_dict(dict(row))

    # ── Meta ──

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a meta value. Returns default when the key is absent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta value."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()

    # ── Consolidation Log ──

    def log_consolidation(
        self,
        log_id: str,
        pass_name: str,
        started_at: str,
        completed_at: str | None = None,
        stats: dict | None = None,
        agent_id: str | None = None,
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> None:
        """Log a consolidation pass."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_log "
            "(id, agent_id, person_id, project_scope, pass_name, started_at, completed_at, stats) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (log_id, agent_id, person_id, project_scope, pass_name, started_at,
             completed_at, json.dumps(stats or {})),
        )
        conn.commit()

    def get_consolidation_runs(
        self, pass_name: str, limit: int = 5, *, agent_id: str | None = None,
        person_id: str | None = None, project_scope: str | None = None,
    ) -> list[dict]:
        """Most recent consolidation_log rows for a pass, newest first.

        The stats column is JSON-decoded. The table has no agent_id
        column; passes that need agent scoping carry it inside stats.
        """
        conn = self._get_conn()
        query = "SELECT * FROM consolidation_log WHERE pass_name = ?"
        params: list[Any] = [pass_name]
        if agent_id is not None and person_id is not None and project_scope is not None:
            query += " AND agent_id = ? AND person_id = ? AND project_scope = ?"
            params.extend([agent_id, person_id, project_scope])
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["stats"] = json.loads(item.get("stats") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["stats"] = {}
            out.append(item)
        return out

    # ── Stats ──

    def get_stats(
        self, agent_id: str = "default", *, person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict:
        """Get summary statistics for an agent's memory."""
        conn = self._get_conn()
        stats = {}

        # Engram counts by state
        for state in ("active", "consolidating", "dormant", "archived"):
            if person_id is not None and project_scope is not None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? "
                    "AND person_id = ? AND project_scope = ? AND state = ?",
                    (agent_id, person_id, project_scope, state),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?",
                    (agent_id, state),
                ).fetchone()
            stats[f"engrams_{state}"] = row[0] if row else 0

        # Connection count
        if person_id is not None and project_scope is not None:
            row = conn.execute(
                """SELECT COUNT(*) FROM connections c JOIN engrams e ON e.id = c.source_id
                   WHERE e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?""",
                (agent_id, person_id, project_scope),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        stats["connections"] = row[0] if row else 0

        # Belief count
        row = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE agent_id = ? AND superseded_by IS NULL",
            (agent_id,),
        ).fetchone()
        stats["beliefs_active"] = row[0] if row else 0

        # Version count (reconsolidation events)
        if person_id is not None and project_scope is not None:
            row = conn.execute(
                """SELECT COUNT(*) FROM versions v JOIN engrams e ON e.id = v.engram_id
                   WHERE e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?""",
                (agent_id, person_id, project_scope),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        stats["reconsolidation_events"] = row[0] if row else 0

        # Archive count
        stats["archived"] = stats["engrams_archived"]

        # Hypomnema counts use the default person/project scope for status.
        stats.update(self.get_hypomnema_stats(
            agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        ))

        # Functional memory counts cover active working context and review load.
        stats.update(self.get_functional_stats(
            agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        ))

        # Accessibility distribution
        scope_sql = ""
        params: list[Any] = [agent_id]
        if person_id is not None and project_scope is not None:
            scope_sql = " AND person_id = ? AND project_scope = ?"
            params.extend([person_id, project_scope])
        rows = conn.execute(
            "SELECT AVG(accessibility) as avg_acc, MIN(accessibility) as min_acc, "
            "MAX(accessibility) as max_acc FROM engrams "
            "WHERE owner_agent_id = ? AND state = 'active'" + scope_sql,
            params,
        ).fetchone()
        if rows and rows["avg_acc"] is not None:
            stats["accessibility_avg"] = round(rows["avg_acc"], 3)
            stats["accessibility_min"] = round(rows["min_acc"], 3)
            stats["accessibility_max"] = round(rows["max_acc"], 3)

        return stats
