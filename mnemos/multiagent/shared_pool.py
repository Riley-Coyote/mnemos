"""
Shared memory pool: memories visible to multiple agents.

Manages the shared namespace where agents can publish memories for
other agents to access. Respects visibility controls:
- PRIVATE: only the owning agent can see it
- SHARED: all agents in the same instance can see it
- PUBLIC: available for federation across instances

Shared reads also enforce Mnemos read visibility: only
``operational_context`` rows participate in ordinary shared-pool reads and
conflict resolution.

The shared pool handles conflict resolution when multiple agents
create memories about the same topic with different content.

Architecture:
    SharedPool owns a dedicated EngramStore pointing at a shared SQLite
    database (default: ~/.mnemos/shared.db). Each agent's resolved store
    remains for fast scoped access; the shared DB is the "workspace memory"
    that all agents can see.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.types import ConnectionRelation, Visibility

if TYPE_CHECKING:
    from ..core.engram import Engram


# SQL for supplemental tables in shared.db (relationship tracking)
_SHARED_EXTRA_TABLES = """
CREATE TABLE IF NOT EXISTS agent_relationships (
    agent_a_id TEXT NOT NULL,
    agent_b_id TEXT NOT NULL,
    trust_score REAL NOT NULL DEFAULT 0.5,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    common_topics TEXT NOT NULL DEFAULT '[]',
    last_interaction TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_a_id, agent_b_id)
);

CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_a_id TEXT NOT NULL,
    agent_b_id TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    interaction_type TEXT NOT NULL DEFAULT 'memory_share',
    timestamp TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SharedPool:
    """Manages shared memories across multiple agents.

    SharedPool owns its own EngramStore backed by a shared SQLite database.
    All standard EngramStore infrastructure (schema, FTS5, connections, WAL)
    is reused — no new storage layer needed.

    Usage:
        pool = SharedPool()  # defaults to ~/.mnemos/shared.db
        pool.publish(engram, visibility="shared")
        shared_memories = pool.get_shared(agent_id="nova", limit=50)
    """

    def __init__(self, shared_db_path: str = "~/.mnemos/shared.db") -> None:
        from ..store.sqlite_store import EngramStore

        self._store = EngramStore(shared_db_path)
        self._init_shared_tables()

    def _init_shared_tables(self) -> None:
        """Create supplemental tables for relationship tracking."""
        conn = self._store._get_conn()
        conn.executescript(_SHARED_EXTRA_TABLES)

    def publish(self, engram: Engram, visibility: str = Visibility.SHARED) -> None:
        """Publish a memory to the shared pool.

        Copies the engram into shared.db with the specified visibility.
        The original engram in the agent's resolved store is not modified here -
        callers should update that copy's visibility separately if needed.

        Args:
            engram: The engram to share.
            visibility: Visibility level ("shared" or "public").
        """
        engram.visibility = visibility
        self._store.save_engram(engram)

    def get_shared(
        self,
        agent_id: str = "default",
        limit: int = 50,
        query: str | None = None,
        kind: str | None = None,
    ) -> list[Engram]:
        """Get operational shared memories visible to an agent.

        Returns engrams from all agents that have been published to the
        shared pool and are operational-context rows. The caller can check owner_agent_id to see who
        created each memory.

        Args:
            agent_id: The requesting agent (currently unused for filtering,
                but included for future per-agent visibility controls).
            limit: Maximum number of memories to return.
            query: Optional FTS query to filter results.
            kind: Optional engram kind filter (episodic, semantic, etc.).

        Returns:
            List of shared engrams, newest first.
        """
        from ..core.engram import Engram

        if query:
            results = self._store.search_fts(query, limit=limit)
            # Safety filter — shared.db should only contain shared/public
            # engrams, but guard against any private ones that slipped in
            results = [
                e
                for e in results
                if e.visibility in (Visibility.SHARED, Visibility.PUBLIC)
            ]
            if kind:
                results = [e for e in results if e.kind == kind]
            return results

        # No query — return recent shared engrams via direct SQL
        conn = self._store._get_conn()
        sql = (
            "SELECT * FROM engrams WHERE state = 'active' "
            "AND visibility IN ('shared', 'public') "
            "AND read_visibility = 'operational_context'"
        )
        params: list[Any] = []

        if kind:
            sql += " AND kind = ?"
            params.append(kind)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]

    def get_agent_memories(
        self,
        agent_id: str,
        limit: int = 20,
    ) -> list[Engram]:
        """Get operational shared engrams from a specific agent.

        Args:
            agent_id: The agent whose shared memories to retrieve.
            limit: Maximum number to return.

        Returns:
            List of that agent's shared engrams, newest first.
        """
        from ..core.engram import Engram

        conn = self._store._get_conn()
        rows = conn.execute(
            "SELECT * FROM engrams WHERE owner_agent_id = ? "
            "AND state = 'active' "
            "AND read_visibility = 'operational_context' "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]

    def resolve_conflict(
        self,
        engram_a_id: str,
        engram_b_id: str,
    ) -> dict[str, Any]:
        """Resolve a conflict between two shared memories.

        Compares confidence, S0/strength, and recency to determine a winner.
        Creates a CONTRADICTS connection between the two engrams.

        Args:
            engram_a_id: First conflicting engram.
            engram_b_id: Second conflicting engram.

        Returns:
            Resolution result dict with winner_id, loser_id, resolution method.
        """
        from ..core.engram import Connection

        a = self._store.get_engram(engram_a_id, read_visibility="operational_context")
        b = self._store.get_engram(engram_b_id, read_visibility="operational_context")

        if a is None or b is None:
            missing = engram_a_id if a is None else engram_b_id
            return {"error": "not_found", "missing_id": missing}

        # Determine winner: confidence > S0/strength > recency
        if a.source.confidence != b.source.confidence:
            winner, loser = (
                (a, b) if a.source.confidence > b.source.confidence else (b, a)
            )
            method = "higher_confidence"
        elif a.strength != b.strength:
            winner, loser = (a, b) if a.strength > b.strength else (b, a)
            method = "stronger"
        else:
            winner, loser = (a, b) if a.created_at >= b.created_at else (b, a)
            method = "newer"

        # Create CONTRADICTS connection from loser → winner
        contradiction = Connection(
            target_id=winner.id,
            relation=ConnectionRelation.CONTRADICTS,
            strength=0.8,
            formed_at=_now_iso(),
            formed_by="conflict_resolution",
        )
        loser.add_connection(
            contradiction.target_id,
            contradiction.relation,
            contradiction.strength,
            contradiction.formed_by,
        )
        self._store.save_engram(loser)

        return {
            "winner_id": winner.id,
            "loser_id": loser.id,
            "resolution": method,
            "winner_confidence": winner.source.confidence,
            "loser_confidence": loser.source.confidence,
            "contradiction_connection_added": True,
        }

    def close(self) -> None:
        """Close the shared database connection."""
        self._store.close()
