"""
Memory inspector: observability and debugging tools for Mnemos.

Provides introspection into the memory system's state — individual engrams,
aggregate statistics, and connection graph visualization. Essential for
debugging, transparency, and building trust in the memory system.

Used by:
- CLI (mnemos inspect, mnemos stats)
- MCP server (expose as tools for the agent to inspect its own memory)
- Dashboard UI (if built)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

logger = logging.getLogger(__name__)


class MemoryInspector:
    """Observability tools for inspecting memory system state.

    Provides methods to examine individual engrams (with full history),
    get aggregate statistics, and explore the connection graph.

    Usage:
        inspector = MemoryInspector(store=store)
        details = inspector.inspect_engram("engram_abc123")
        stats = inspector.get_stats("nova")
        graph = inspector.get_connections_graph("engram_abc123", depth=2)
    """

    def __init__(self, store: EngramStore) -> None:
        self._store = store

    def inspect_engram(self, engram_id: str) -> dict[str, Any]:
        """Get full details of an engram including version history and connections.

        Returns a comprehensive view of a single engram:
        - All fields (content, resolution, S0/strength, stability, accessibility, etc.)
        - Full encoding context
        - Source and confidence information
        - Version history (reconsolidation snapshots)
        - All outgoing connections with target summaries
        - Lineage information (parents, supersedes)

        Args:
            engram_id: The ID of the engram to inspect.

        Returns:
            Dict containing all engram details. Returns empty dict with
            error key if engram not found.
        """
        logger.warning("MemoryInspector.inspect_engram() not yet implemented")
        return {}

    def get_stats(self, agent_id: str = "default") -> dict[str, Any]:
        """Get aggregate statistics for an agent's memory system.

        Returns:
        {
            "engram_counts": {"active": N, "dormant": N, "archived": N, ...},
            "total_connections": N,
            "active_beliefs": N,
            "reconsolidation_events": N,
            "accessibility_distribution": {"avg": F, "min": F, "max": F},
            "s0_distribution": {"avg": F, "min": F, "max": F},
            "kind_distribution": {"episodic": N, "semantic": N, ...},
            "oldest_memory": ISO timestamp,
            "newest_memory": ISO timestamp,
            "last_consolidation": ISO timestamp or None,
        }

        Args:
            agent_id: Which agent's stats to retrieve.

        Returns:
            Dict of aggregate statistics.
        """
        logger.warning("MemoryInspector.get_stats() not yet implemented")
        return {}

    def get_connections_graph(
        self,
        engram_id: str,
        depth: int = 2,
    ) -> dict[str, Any]:
        """Get the connection graph around an engram up to a given depth.

        Returns a graph structure suitable for visualization:
        {
            "root": engram_id,
            "nodes": [
                {"id": str, "content_preview": str, "kind": str, "s0": float},
                ...
            ],
            "edges": [
                {"source": str, "target": str, "relation": str, "strength": float},
                ...
            ],
        }

        Args:
            engram_id: The center engram to build the graph from.
            depth: How many hops to traverse (1 = direct connections only).

        Returns:
            Graph dict with nodes and edges lists.
        """
        logger.warning("MemoryInspector.get_connections_graph() not yet implemented")
        return {"root": engram_id, "nodes": [], "edges": []}
