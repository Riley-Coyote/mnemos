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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.engram import Connection, Engram, VersionRef
from ..core.belief import Belief
from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity


# Schema version — increment when tables change
SCHEMA_VERSION = 2

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
})

# Allowed column names for beliefs table
_BELIEF_COLUMNS = frozenset({
    "id", "agent_id", "content", "confidence", "domain", "created_at",
    "last_revised", "last_challenged", "revision_history", "superseded_by",
    "supporting_engram_ids",
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
    supporting_engram_ids TEXT NOT NULL DEFAULT '[]'
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
    last_challenged_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_hypomnema_scope_revised
    ON hypomnema_entries(agent_id, person_id, project_scope, last_revised_at DESC)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_hypomnema_promotion
    ON hypomnema_entries(agent_id, project_scope, created_at)
    WHERE active = 1 AND graduated_to_engram_id IS NULL;
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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with schema."""
        conn = self._get_conn()
        conn.executescript(SQL_CREATE_TABLES)
        # Migrate: add impact column if missing (v0.1 → v0.2)
        try:
            conn.execute("ALTER TABLE engrams ADD COLUMN impact TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        # Set schema version
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()

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

    def search_fts(self, query: str, limit: int = 50) -> list[Engram]:
        """Search engrams using FTS5 full-text search."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT e.* FROM engrams e "
            "JOIN engrams_fts f ON e.id = f.id "
            "WHERE engrams_fts MATCH ? AND e.state = 'active' "
            "ORDER BY rank LIMIT ?",
            (query, limit),
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

    # ── Hypomnema ──

    def write_hypomnema_entry(
        self,
        content: str,
        *,
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
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        """Write a scoped hypomnema continuity entry.

        Hypomnema is durable, relationship-scoped continuity that can be
        revised before it graduates into shared Mnemos engrams.
        """
        if source not in VALID_HYPO_SOURCES:
            raise ValueError(f"Unsupported hypomnema source: {source}")
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
                id, agent_id, person_id, project_scope, content, source,
                density, domain, tags_json, confidence, salience,
                active, foundational, revision_count, revisions_json,
                related_session_id, related_engram_id, created_at, last_revised_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, '[]', ?, ?, ?, ?)
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

    # ── Stats ──

    def get_stats(self, agent_id: str = "default") -> dict:
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
        row = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE agent_id = ? AND superseded_by IS NULL",
            (agent_id,),
        ).fetchone()
        stats["beliefs_active"] = row[0] if row else 0

        # Version count (reconsolidation events)
        row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        stats["reconsolidation_events"] = row[0] if row else 0

        # Archive count
        row = conn.execute("SELECT COUNT(*) FROM archive").fetchone()
        stats["archived"] = row[0] if row else 0

        # Hypomnema counts use the default person/project scope for status.
        stats.update(self.get_hypomnema_stats(agent_id=agent_id))

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
