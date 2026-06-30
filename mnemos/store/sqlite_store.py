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
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.engram import Connection, Engram, VersionRef
from ..core.belief import Belief
from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity
from .migrations import (
    apply_u3a_schema_migration,
    apply_u3b_hardening_schema_migration,
    get_current_version,
    run_migrations,
)


# Schema version — increment when tables change
SCHEMA_VERSION = 6

READ_VISIBILITY_OPERATIONAL = "operational_context"
READ_VISIBILITY_REVIEW = "review_only"
READ_VISIBILITY_AUDIT = "audit_only"
VALID_READ_VISIBILITIES = {
    READ_VISIBILITY_OPERATIONAL,
    READ_VISIBILITY_REVIEW,
    READ_VISIBILITY_AUDIT,
}

VALID_PROPOSAL_AUTHORITIES = {
    "user_stated",
    "agent_generated",
    "agent_observed",
    "imported",
    "system_policy",
    "operator_review",
}
VALID_PROPOSAL_KINDS = {
    "belief",
    "engram",
    "hypomnema",
    "functional_memory",
    "identity",
    "modulation",
    "correction",
    "promotion",
}
VALID_PROPOSAL_TARGET_SURFACES = {
    "engrams",
    "beliefs",
    "hypomnema_entries",
    "functional_memories",
    "dynamic_modulations",
    "identity_profile",
    "runtime_context",
}
VALID_PROPOSAL_BLAST_RADII = {"low", "medium", "high", "identity", "foundational"}
VALID_PROPOSAL_STATUSES = {
    "pending_review",
    "approved",
    "rejected",
    "applied",
    "superseded",
}

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

VALID_HYPO_SOURCES = {"observed", "synthesized", "co-formed"}
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
    "id", "content", "content_at_encoding", "impact", "resolution", "kind", "tags",
    "schema_refs", "strength", "stability", "accessibility", "encoding_context",
    "source", "lineage", "owner_agent_id", "visibility", "state", "created_at",
    "last_accessed", "access_count", "reconsolidation_count",
    "voice_exemplar_eligible", "softening_protected", "original_substrate",
    "original_timestamp", "consolidation_authorized", "decay_protected",
    "read_visibility",
})

# Allowed column names for beliefs table
_BELIEF_COLUMNS = frozenset({
    "id", "agent_id", "content", "confidence", "domain", "created_at",
    "last_revised", "last_challenged", "revision_history", "superseded_by",
    "supporting_engram_ids", "tier", "needs_review", "confidence_pending_review",
    "read_visibility",
})

SQL_CREATE_TABLES = """
-- Core engram storage
CREATE TABLE IF NOT EXISTS engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
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
    visibility TEXT NOT NULL DEFAULT 'private',
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0,
    voice_exemplar_eligible INTEGER NOT NULL DEFAULT 1
        CHECK (voice_exemplar_eligible IN (0, 1)),
    softening_protected INTEGER NOT NULL DEFAULT 0
        CHECK (softening_protected IN (0, 1)),
    original_substrate TEXT,
    original_timestamp INTEGER,
    consolidation_authorized INTEGER NOT NULL DEFAULT 1
        CHECK (consolidation_authorized IN (0, 1)),
    decay_protected INTEGER NOT NULL DEFAULT 0
        CHECK (decay_protected IN (0, 1))
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
    tier TEXT CHECK (tier IS NULL OR tier IN ('foundational', 'operational', 'tactical')),
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    confidence_pending_review INTEGER NOT NULL DEFAULT 0
        CHECK (confidence_pending_review IN (0, 1)),
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only'))
);

-- Hypomnema: scoped durable continuity that can revise before promotion
CREATE TABLE IF NOT EXISTS hypomnema_entries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'observed'
        CHECK (source IN ('observed', 'synthesized', 'co-formed')),
    density REAL NOT NULL DEFAULT 0.5,
    domain TEXT NOT NULL DEFAULT 'topical'
        CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    foundational INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    revisions_json TEXT NOT NULL DEFAULT '[]',
    original_timestamp INTEGER,
    related_session_id TEXT,
    related_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    graduated_to_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    superseded_by TEXT REFERENCES hypomnema_entries(id),
    created_at TEXT NOT NULL,
    last_revised_at TEXT NOT NULL,
    last_challenged_at TEXT
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
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
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
    pass_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}'
);

-- PAI import source-to-row map for idempotent importer re-runs and repair
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
);

-- Afferent Membrane proposal ledger: generated candidates and durable transitions
CREATE TABLE IF NOT EXISTS proposal_ledger (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    source_authority TEXT NOT NULL,
    kind TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'general',
    target_surface TEXT NOT NULL
        CHECK (target_surface IN (
            'engrams', 'beliefs', 'hypomnema_entries',
            'functional_memories', 'dynamic_modulations',
            'identity_profile', 'runtime_context'
        )),
    transition TEXT NOT NULL,
    blast_radius TEXT NOT NULL DEFAULT 'medium'
        CHECK (blast_radius IN ('low', 'medium', 'high', 'identity', 'foundational')),
    read_visibility TEXT NOT NULL DEFAULT 'review_only'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review', 'approved', 'rejected', 'applied', 'superseded')),
    reason TEXT NOT NULL DEFAULT '',
    gate_version TEXT NOT NULL DEFAULT 'affmem-v1',
    target_id TEXT,
    provenance_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    decided_at INTEGER,
    applied_at INTEGER
);

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
CREATE INDEX IF NOT EXISTS idx_engrams_last_accessed ON engrams(last_accessed);
CREATE INDEX IF NOT EXISTS idx_connections_source ON connections(source_id);
CREATE INDEX IF NOT EXISTS idx_connections_target ON connections(target_id);
CREATE INDEX IF NOT EXISTS idx_beliefs_domain ON beliefs(agent_id, domain);
CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_target
    ON pai_import_row_map(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_job
    ON pai_import_row_map(job_id);
CREATE INDEX IF NOT EXISTS idx_hypomnema_scope_revised
    ON hypomnema_entries(agent_id, person_id, project_scope, last_revised_at DESC)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_hypomnema_promotion
    ON hypomnema_entries(agent_id, project_scope, created_at)
    WHERE active = 1 AND graduated_to_engram_id IS NULL;
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
CREATE INDEX IF NOT EXISTS idx_proposal_ledger_status_scope
    ON proposal_ledger(agent_id, person_id, project_scope, status, created_at);
CREATE INDEX IF NOT EXISTS idx_proposal_ledger_visibility
    ON proposal_ledger(read_visibility, status);
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


_READ_VISIBILITY_DEFAULT = object()


def _normalize_read_visibility(
    value: str | None,
    *,
    allow_all: bool = True,
    default: str = READ_VISIBILITY_OPERATIONAL,
) -> str | None:
    if value is None and allow_all:
        return None
    read_visibility = (value if value is not None else default).strip()
    if not read_visibility:
        read_visibility = default
    if read_visibility not in VALID_READ_VISIBILITIES:
        raise ValueError(f"Unsupported read_visibility: {read_visibility}")
    return read_visibility


def _normalize_read_visibility_values(
    value: str | Sequence[str] | None,
    *,
    allow_all: bool = True,
    default: str = READ_VISIBILITY_OPERATIONAL,
) -> tuple[str, ...] | None:
    if value is None:
        return None if allow_all else (default,)
    if isinstance(value, str):
        return (_normalize_read_visibility(value, allow_all=False, default=default),)
    values = tuple(
        _normalize_read_visibility(item, allow_all=False, default=default)
        for item in value
    )
    if not values:
        raise ValueError("read_visibility must include at least one visibility")
    return values


def _append_read_visibility_filter(
    sql: str,
    params: list[Any],
    column: str,
    values: tuple[str, ...] | None,
) -> str:
    if values is None:
        return sql
    placeholders = ", ".join("?" for _ in values)
    params.extend(values)
    return f"{sql} AND {column} IN ({placeholders})"


def _apply_read_visibility_filter(
    sql: str,
    params: list[Any],
    column: str,
    read_visibility: str | None,
) -> str:
    normalized = _normalize_read_visibility(read_visibility)
    if normalized is None:
        return sql
    params.append(normalized)
    return f"{sql} AND {column} = ?"


def _clean_choice(value: str, allowed: set[str], label: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in allowed:
        raise ValueError(f"Unsupported {label}: {cleaned}")
    return cleaned


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

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        """Open a Mnemos store.

        Args:
            db_path: SQLite file path. Created if missing in read-write mode.
            read_only: When True, open via `file:...?mode=ro` URI and skip
                schema bootstrap entirely. Required for preview/inspection
                paths that must not mutate the database — without this, the
                default constructor runs `executescript`, `ALTER TABLE`,
                migrations, and a `meta` write on every instantiation, which
                silently upgrades older-schema DBs and writes 2+ bytes even on
                already-current DBs. Caller must guarantee the DB exists.
        """
        self.db_path = Path(db_path).expanduser()
        self._read_only = read_only
        self._conn: sqlite3.Connection | None = None
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"read_only EngramStore requires existing db: {self.db_path}"
                )
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with schema. Never called in read_only mode."""
        if self._read_only:
            raise RuntimeError("_init_db must not be called on a read-only store")
        conn = self._get_conn()
        current_version = get_current_version(conn)
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported "
                f"{SCHEMA_VERSION}"
            )
        conn.executescript(SQL_CREATE_TABLES)
        # Migrate: add impact column if missing (v0.1 → v0.2)
        try:
            conn.execute("ALTER TABLE engrams ADD COLUMN impact TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()
        run_migrations(conn, target_version=SCHEMA_VERSION)
        apply_u3a_schema_migration(conn)
        apply_u3b_hardening_schema_migration(conn)
        # Set schema version
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create SQLite connection.

        Read-only mode opens via SQLite URI form (`file:...?mode=ro`), which
        makes the connection reject INSERT/UPDATE/DELETE/DDL at the SQLite
        layer. WAL pragma is incompatible with read-only mode (would attempt
        to create WAL file); we skip the write-side pragmas there.
        """
        if self._conn is None:
            if self._read_only:
                uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
                self._conn = sqlite3.connect(
                    uri,
                    uri=True,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                # foreign_keys is harmless on read-only; PRAGMA query is allowed
                self._conn.execute("PRAGMA foreign_keys=ON")
            else:
                self._conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Engram CRUD ──

    def save_engram(self, engram: Engram) -> None:
        """Insert or update an engram.

        All operations (engram table, FTS index, connections, versions) are
        wrapped in a single transaction for atomicity.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._save_engram_no_commit(conn, engram)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _save_engram_no_commit(self, conn: sqlite3.Connection, engram: Engram) -> None:
        data = engram.to_dict()

        # Validate column names to prevent SQL injection
        safe_data = {k: v for k, v in data.items() if k in _ENGRAM_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in safe_data if k != "id")

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

    def get_engram(
        self,
        engram_id: str,
        *,
        read_visibility: str | None = None,
    ) -> Engram | None:
        """Load an engram by ID, including connections and versions."""
        conn = self._get_conn()
        query = "SELECT * FROM engrams WHERE id = ?"
        params: list[Any] = [engram_id]
        normalized = _normalize_read_visibility(read_visibility)
        if normalized is not None:
            query += " AND read_visibility = ?"
            params.append(normalized)
        row = conn.execute(
            query, params
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
        include_decay_protected: bool = True,
        require_consolidation_authorized: bool = False,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[Engram]:
        """Get all active engrams for an agent, sorted by accessibility.

        Args:
            agent_id: Which agent's engrams to return. If None, returns all
                agents' active engrams (useful for shared DB consolidation).
            load_connections: If True, load connections for each engram.
                Set to False for bulk operations where connections aren't needed
                (e.g., decay pass only needs accessibility/strength fields).
            include_decay_protected: If False, exclude engrams that the
                decay pass must not mutate.
            require_consolidation_authorized: If True, exclude read-only
                imported engrams from consolidation mutation candidates.
            read_visibility: Defaults to operational-context rows. Pass None
                only for explicit review/audit callers that need all rows.
        """
        conn = self._get_conn()
        predicates = ["state = 'active'"]
        params: list[Any] = []
        if agent_id is not None:
            predicates.append("owner_agent_id = ?")
            params.append(agent_id)
        if not include_decay_protected:
            predicates.append("decay_protected = 0")
        if require_consolidation_authorized:
            predicates.append("consolidation_authorized = 1")
        normalized_visibility = _normalize_read_visibility(read_visibility)
        if normalized_visibility is not None:
            predicates.append("read_visibility = ?")
            params.append(normalized_visibility)
        where = " AND ".join(predicates)
        params.append(limit)

        if agent_id is None:
            rows = conn.execute(
                f"SELECT * FROM engrams WHERE {where} "
                "ORDER BY accessibility DESC LIMIT ?",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM engrams WHERE {where} "
                "ORDER BY accessibility DESC LIMIT ?",
                params,
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
        self,
        query: str,
        limit: int = 50,
        *,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[Engram]:
        """Search engrams using FTS5 full-text search."""
        conn = self._get_conn()
        normalized_visibility = _normalize_read_visibility(read_visibility)
        predicates = ["engrams_fts MATCH ?", "e.state = 'active'"]
        params: list[Any] = [query]
        if normalized_visibility is not None:
            predicates.append("e.read_visibility = ?")
            params.append(normalized_visibility)
        params.append(limit)
        rows = conn.execute(
            "SELECT e.* FROM engrams e "
            "JOIN engrams_fts f ON e.id = f.id "
            f"WHERE {' AND '.join(predicates)} "
            "ORDER BY rank LIMIT ?",
            params,
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
        conn = self._get_conn()
        conn.execute(
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
        conn.commit()

    def remove_connection(self, source_id: str, target_id: str) -> None:
        """Remove a connection between two engrams."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM connections WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        )
        conn.commit()

    def get_recent_engrams(
        self,
        agent_id: str | None = None,
        since: "datetime | None" = None,
        limit: int = 50,
        require_consolidation_authorized: bool = False,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list:
        """Get recently created engrams, optionally filtered by agent and time.

        Args:
            agent_id: Filter by agent ID (optional).
            since: Only return engrams created after this datetime (optional).
            limit: Maximum number to return.
            require_consolidation_authorized: If True, exclude read-only
                imported engrams from consolidation review inputs.
            read_visibility: Defaults to operational-context rows. Pass None
                for explicit review/audit scans.

        Returns:
            List of Engram objects, most recent first.
        """
        query = "SELECT * FROM engrams WHERE state = 'active'"
        params: list = []
        normalized_visibility = _normalize_read_visibility(read_visibility)

        if agent_id:
            query += " AND owner_agent_id = ?"
            params.append(agent_id)

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        if require_consolidation_authorized:
            query += " AND consolidation_authorized = 1"

        if normalized_visibility is not None:
            query += " AND read_visibility = ?"
            params.append(normalized_visibility)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        rows = conn.execute(query, params).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]


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
        self._save_belief_no_commit(conn, belief)
        conn.commit()

    def _save_belief_no_commit(self, conn: sqlite3.Connection, belief: Belief) -> None:
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

    def get_beliefs(
        self,
        agent_id: str = "default",
        domain: str | None = None,
        active_only: bool = True,
        include_pending_review: bool = False,
        read_visibility: str | Sequence[str] | None | object = _READ_VISIBILITY_DEFAULT,
    ) -> list[Belief]:
        """Get beliefs, excluding pending-confidence rows unless opted in.

        Imported or changed PAI beliefs use ``confidence_pending_review`` to
        keep stale confidence out of normal consumers. Belief review passes
        ``include_pending_review=True`` so it can resolve those rows.
        """
        conn = self._get_conn()
        query = "SELECT * FROM beliefs WHERE agent_id = ?"
        params: list[Any] = [agent_id]
        if read_visibility is _READ_VISIBILITY_DEFAULT:
            visibility_values = (
                (READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW)
                if include_pending_review
                else (READ_VISIBILITY_OPERATIONAL,)
            )
        else:
            visibility_values = _normalize_read_visibility_values(read_visibility)  # type: ignore[arg-type]

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if active_only:
            query += " AND superseded_by IS NULL"

        if not include_pending_review:
            query += " AND confidence_pending_review = 0"

        query = _append_read_visibility_filter(
            query,
            params,
            "read_visibility",
            visibility_values,
        )

        query += " ORDER BY confidence DESC"
        rows = conn.execute(query, params).fetchall()
        return [Belief.from_dict(dict(r)) for r in rows]

    # ── Proposal Ledger ──

    def write_proposal(
        self,
        *,
        source_authority: str,
        kind: str,
        target_surface: str,
        transition: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        domain: str = "general",
        blast_radius: str = "medium",
        read_visibility: str = READ_VISIBILITY_REVIEW,
        status: str = "pending_review",
        reason: str = "",
        gate_version: str = "affmem-v1",
        target_id: str | None = None,
        provenance_ids: list[str] | tuple[str, ...] | None = None,
        payload: dict[str, Any] | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a proposed durable transition for explicit review."""
        source_authority = _clean_choice(
            source_authority,
            VALID_PROPOSAL_AUTHORITIES,
            "source authority",
        )
        kind = _clean_choice(kind, VALID_PROPOSAL_KINDS, "proposal kind")
        target_surface = _clean_choice(
            target_surface,
            VALID_PROPOSAL_TARGET_SURFACES,
            "target surface",
        )
        blast_radius = _clean_choice(
            blast_radius,
            VALID_PROPOSAL_BLAST_RADII,
            "blast radius",
        )
        if status not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Unsupported proposal status: {status}")
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=READ_VISIBILITY_REVIEW,
        )
        if not transition.strip():
            raise ValueError("Proposal transition cannot be empty")

        now = int(datetime.now(timezone.utc).timestamp())
        pid = (proposal_id or "").strip() or _new_id()
        provenance = [str(item).strip() for item in (provenance_ids or []) if str(item).strip()]
        payload_json = _encode_json(payload or {})
        decided_at = now if status in {"approved", "rejected", "superseded"} else None
        applied_at = now if status == "applied" else None
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO proposal_ledger (
                id, agent_id, person_id, project_scope, source_authority, kind,
                domain, target_surface, transition, blast_radius, read_visibility,
                status, reason, gate_version, target_id, provenance_ids_json,
                payload_json, created_at, updated_at, decided_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                source_authority = excluded.source_authority,
                kind = excluded.kind,
                domain = excluded.domain,
                target_surface = excluded.target_surface,
                transition = excluded.transition,
                blast_radius = excluded.blast_radius,
                read_visibility = excluded.read_visibility,
                status = excluded.status,
                reason = excluded.reason,
                gate_version = excluded.gate_version,
                target_id = excluded.target_id,
                provenance_ids_json = excluded.provenance_ids_json,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                decided_at = CASE
                    WHEN excluded.status IN ('approved', 'rejected', 'superseded')
                    THEN excluded.updated_at
                    ELSE proposal_ledger.decided_at
                END,
                applied_at = CASE
                    WHEN excluded.status = 'applied'
                    THEN excluded.updated_at
                    ELSE proposal_ledger.applied_at
                END
            """,
            (
                pid,
                agent_id,
                person_id,
                project_scope,
                source_authority,
                kind,
                domain.strip() or "general",
                target_surface,
                transition.strip(),
                blast_radius,
                normalized_visibility,
                status,
                reason.strip(),
                gate_version.strip() or "affmem-v1",
                (target_id or "").strip() or None,
                _encode_json(provenance),
                payload_json,
                now,
                now,
                decided_at,
                applied_at,
            ),
        )
        conn.commit()
        proposal = self.get_proposal(pid)
        if proposal is None:
            raise RuntimeError(f"Failed to write proposal: {pid}")
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Load one proposal ledger row by ID."""
        row = self._get_conn().execute(
            "SELECT * FROM proposal_ledger WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return self._hydrate_proposal_row(dict(row))

    def list_proposals(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        status: str | None = None,
        read_visibility: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List proposal ledger rows for an explicit review surface."""
        if status is not None and status not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Unsupported proposal status: {status}")
        normalized_visibility = _normalize_read_visibility(read_visibility)
        sql = (
            "SELECT * FROM proposal_ledger "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if normalized_visibility is not None:
            sql += " AND read_visibility = ?"
            params.append(normalized_visibility)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = self._get_conn().execute(sql, params).fetchall()
        return [self._hydrate_proposal_row(dict(row)) for row in rows]

    @staticmethod
    def _hydrate_proposal_row(row: dict[str, Any]) -> dict[str, Any]:
        row["provenance_ids"] = _decode_json(row.pop("provenance_ids_json", "[]"), [])
        row["payload"] = _decode_json(row.pop("payload_json", "{}"), {})
        return row

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
        read_visibility: str | None = None,
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
        existing = self.get_functional_memory(fid, include_deleted=True)
        default_visibility = (
            existing["read_visibility"]
            if existing is not None and read_visibility is None
            else (
                READ_VISIBILITY_REVIEW
                if needs_confirmation
                else READ_VISIBILITY_OPERATIONAL
            )
        )
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=default_visibility,
        )
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
                source, read_visibility, metadata_json, created_at, updated_at, expires_at,
                is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
                read_visibility = excluded.read_visibility,
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
                normalized_visibility,
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
        exclude_needs_confirmation: bool = False,
        include_deleted: bool = False,
        limit: int = 12,
        read_visibility: str | Sequence[str] | None | object = _READ_VISIBILITY_DEFAULT,
    ) -> list[dict[str, Any]]:
        """Search functional memories for the current scope/session."""
        if memory_type and memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")
        if needs_confirmation_only and exclude_needs_confirmation:
            raise ValueError(
                "needs_confirmation_only and exclude_needs_confirmation conflict"
            )

        sql = (
            "SELECT * FROM functional_memories "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if read_visibility is _READ_VISIBILITY_DEFAULT:
            visibility_values = (
                (READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW)
                if needs_confirmation_only
                else (READ_VISIBILITY_OPERATIONAL,)
            )
        else:
            visibility_values = _normalize_read_visibility_values(read_visibility)  # type: ignore[arg-type]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if needs_confirmation_only:
            sql += " AND needs_confirmation = 1"
        if exclude_needs_confirmation:
            sql += " AND needs_confirmation = 0"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
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
                density=0.72,
                domain="situational",
                tags=["session-close", "functional-memory", project_scope],
                confidence=confidence,
                salience=salience,
                related_session_id=session_id,
            )

        now = _utc_now()
        conn = self._get_conn()
        promoted_memory_ids = [m["id"] for m in memories]
        if hypomnema_id and promoted_memory_ids:
            placeholders = ", ".join("?" for _ in promoted_memory_ids)
            conn.execute(
                f"""
                UPDATE functional_memories
                SET is_deleted = 1,
                    promoted_to_hypomnema_id = ?,
                    updated_at = ?
                WHERE session_id = ? AND is_deleted = 0
                  AND id IN ({placeholders})
                """,
                (hypomnema_id, now, session_id, *promoted_memory_ids),
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
        read_visibility: str | Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Count active functional memory and session state."""
        functional_where = ["agent_id = ?"]
        session_where = ["agent_id = ?"]
        functional_params: list[Any] = [agent_id]
        session_params: list[Any] = [agent_id]
        if person_id is not None:
            functional_where.append("person_id = ?")
            session_where.append("person_id = ?")
            functional_params.append(person_id)
            session_params.append(person_id)
        if project_scope is not None:
            functional_where.append("project_scope = ?")
            session_where.append("project_scope = ?")
            functional_params.append(project_scope)
            session_params.append(project_scope)
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            functional_where.append(f"read_visibility IN ({placeholders})")
            functional_params.extend(visibility_values)
        functional_where_sql = " AND ".join(functional_where)
        session_where_sql = " AND ".join(session_where)
        conn = self._get_conn()
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN is_deleted = 0 AND pinned = 1 THEN 1 ELSE 0 END) AS pinned,
              SUM(CASE WHEN is_deleted = 0 AND needs_confirmation = 1 THEN 1 ELSE 0 END) AS needs_confirmation
            FROM functional_memories
            WHERE {functional_where_sql}
            """,
            functional_params,
        ).fetchone()
        session_row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
            FROM memory_sessions
            WHERE {session_where_sql}
            """,
            session_params,
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
        entry_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        read_visibility: str | None = None,
        original_timestamp: int | None = None,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        """Write a scoped hypomnema continuity entry.

        Hypomnema is durable, relationship-scoped continuity that can be
        revised before it graduates into shared Mnemos engrams.
        """
        conn = self._get_conn()
        entry_id = self._write_hypomnema_entry_no_commit(
            conn,
            content,
            entry_id=entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            source=source,
            density=density,
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            read_visibility=read_visibility,
            original_timestamp=original_timestamp,
            related_session_id=related_session_id,
            related_engram_id=related_engram_id,
        )
        conn.commit()
        return entry_id

    def _write_hypomnema_entry_no_commit(
        self,
        conn: sqlite3.Connection,
        content: str,
        *,
        entry_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        read_visibility: str | None = None,
        original_timestamp: int | None = None,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        if source not in VALID_HYPO_SOURCES:
            raise ValueError(f"Unsupported hypomnema source: {source}")
        if domain not in VALID_HYPO_DOMAINS:
            raise ValueError(f"Unsupported hypomnema domain: {domain}")
        if not content.strip():
            raise ValueError("Hypomnema content cannot be empty")

        now = _utc_now()
        entry_id = (entry_id or "").strip() or _new_id()
        existing = conn.execute(
            """
            SELECT agent_id, person_id, project_scope, content, revision_count,
                   revisions_json, read_visibility
            FROM hypomnema_entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        if existing is not None and (
            existing["agent_id"] != agent_id
            or existing["person_id"] != person_id
            or existing["project_scope"] != project_scope
        ):
            raise ValueError(
                "Hypomnema entry ID already exists outside the requested scope"
            )
        default_visibility = (
            existing["read_visibility"]
            if existing is not None and read_visibility is None
            else READ_VISIBILITY_OPERATIONAL
        )
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=default_visibility,
        )
        revision_count = 0
        revisions_json = "[]"
        if existing is not None:
            revision_count = int(existing["revision_count"] or 0)
            revisions = _decode_json(existing["revisions_json"], [])
            if existing["content"] != content.strip():
                revisions.append(
                    {
                        "content": existing["content"],
                        "revised_at": now,
                        "reason": "pai_import_update",
                    }
                )
                revision_count += 1
            revisions_json = _encode_json(revisions)
        conn.execute(
            """
            INSERT INTO hypomnema_entries(
                id, agent_id, person_id, project_scope, content, source,
                density, domain, read_visibility, tags_json, confidence, salience,
                active, foundational, revision_count, revisions_json,
                original_timestamp, related_session_id, related_engram_id,
                created_at, last_revised_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                source = excluded.source,
                density = excluded.density,
                domain = excluded.domain,
                read_visibility = excluded.read_visibility,
                tags_json = excluded.tags_json,
                confidence = excluded.confidence,
                salience = excluded.salience,
                active = hypomnema_entries.active,
                foundational = excluded.foundational,
                revision_count = excluded.revision_count,
                revisions_json = excluded.revisions_json,
                original_timestamp = excluded.original_timestamp,
                related_session_id = excluded.related_session_id,
                related_engram_id = excluded.related_engram_id,
                last_revised_at = excluded.last_revised_at
            """,
            (
                entry_id,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                source,
                _clamp(density),
                domain,
                normalized_visibility,
                _encode_json(_split_tags(tags)),
                _clamp(confidence),
                _clamp(salience),
                int(foundational),
                revision_count,
                revisions_json,
                original_timestamp,
                related_session_id,
                related_engram_id,
                now,
                now,
            ),
        )
        return entry_id

    def get_hypomnema_entry(
        self,
        entry_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = False,
        read_visibility: str | None = None,
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
        query = _apply_read_visibility_filter(
            query,
            params,
            "read_visibility",
            read_visibility,
        )
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._hydrate_hypomnema_row(dict(row))

    def search_hypomnema(
        self,
        query: str = "",
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 8,
        include_inactive: bool = False,
        exclude_promotion_candidates: bool = False,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[dict[str, Any]]:
        """Search scoped hypomnema entries by text, confidence, and salience."""
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        normalized_visibility = _normalize_read_visibility(read_visibility)
        if not include_inactive:
            sql += " AND active = 1"
        if normalized_visibility is not None:
            sql += " AND read_visibility = ?"
            params.append(normalized_visibility)
        if exclude_promotion_candidates:
            sql += (
                " AND NOT (active = 1 "
                "AND graduated_to_engram_id IS NULL "
                "AND confidence >= 0.82 "
                "AND salience >= 0.65 "
                "AND (revision_count >= 1 OR foundational = 1))"
            )
        sql += " ORDER BY foundational DESC, last_revised_at DESC LIMIT 100"
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
            source="synthesized",
            density=row["density"],
            domain=row["domain"],
            tags=row["tags"],
            confidence=row["confidence"],
            salience=row["salience"],
            foundational=row["foundational"],
            read_visibility=row["read_visibility"],
            original_timestamp=row["original_timestamp"],
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
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[dict[str, Any]]:
        """List stable hypomnema entries ready to become Mnemos engrams."""
        conn = self._get_conn()
        visibility_values = _normalize_read_visibility_values(read_visibility)
        sql = """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND active = 1
              AND graduated_to_engram_id IS NULL
              AND confidence >= 0.82
              AND salience >= 0.65
              AND (revision_count >= 1 OR foundational = 1)
        """
        params: list[Any] = [agent_id, person_id, project_scope]
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
        sql += (
            " ORDER BY foundational DESC, confidence DESC, salience DESC, created_at ASC"
            " LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def get_hypomnema_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
        read_visibility: str | Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Count hypomnema entries for a scope."""
        conn = self._get_conn()
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            where.append(f"read_visibility IN ({placeholders})")
            params.extend(visibility_values)
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

    def save_identity(self, identity: AgentIdentity) -> None:
        """Save agent identity."""
        conn = self._get_conn()
        data = identity.to_dict()
        data["agent_id"] = identity.memory_profile.agent_id
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "agent_id")

        conn.execute(
            f"INSERT INTO agent_identity ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
            list(data.values()),
        )
        conn.commit()

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
    ) -> None:
        """Log a consolidation pass."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_log "
            "(id, pass_name, started_at, completed_at, stats) "
            "VALUES (?, ?, ?, ?, ?)",
            (log_id, pass_name, started_at, completed_at, json.dumps(stats or {})),
        )
        conn.commit()

    def get_consolidation_runs(
        self, pass_name: str, limit: int = 5
    ) -> list[dict]:
        """Most recent consolidation_log rows for a pass, newest first.

        The stats column is JSON-decoded. The table has no agent_id
        column; passes that need agent scoping carry it inside stats.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM consolidation_log WHERE pass_name = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (pass_name, limit),
        ).fetchall()
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
        self,
        agent_id: str = "default",
        include_pending_review: bool = False,
    ) -> dict:
        """Get summary statistics for an agent's memory."""
        conn = self._get_conn()
        stats = {}

        # Engram counts by state
        for state in ("active", "consolidating", "dormant", "archived"):
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams "
                "WHERE owner_agent_id = ? AND state = ?",
                (agent_id, state),
            ).fetchone()
            stats[f"engrams_{state}"] = row[0] if row else 0

        # Connection count
        row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        stats["connections"] = row[0] if row else 0

        # Belief count
        belief_query = (
            "SELECT COUNT(*) FROM beliefs "
            "WHERE agent_id = ? AND superseded_by IS NULL"
        )
        if not include_pending_review:
            belief_query += " AND confidence_pending_review = 0"
        row = conn.execute(belief_query, (agent_id,)).fetchone()
        stats["beliefs_active"] = row[0] if row else 0

        # Version count (reconsolidation events)
        row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        stats["reconsolidation_events"] = row[0] if row else 0

        # Archive count
        row = conn.execute("SELECT COUNT(*) FROM archive").fetchone()
        stats["archived"] = row[0] if row else 0

        # Hypomnema counts use the default person/project scope for status.
        stats.update(self.get_hypomnema_stats(agent_id=agent_id))

        # Functional memory counts cover active working context and review load.
        stats.update(self.get_functional_stats(agent_id=agent_id))

        # Accessibility distribution
        rows = conn.execute(
            "SELECT "
            "AVG(accessibility) as avg_acc, "
            "MIN(accessibility) as min_acc, "
            "MAX(accessibility) as max_acc "
            "FROM engrams WHERE owner_agent_id = ? AND state = 'active'",
            (agent_id,),
        ).fetchone()
        if rows and rows["avg_acc"] is not None:
            stats["accessibility_avg"] = round(rows["avg_acc"], 3)
            stats["accessibility_min"] = round(rows["min_acc"], 3)
            stats["accessibility_max"] = round(rows["max_acc"], 3)

        return stats
