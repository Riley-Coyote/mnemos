"""
Intention sweep: periodic check for triggered intentions.

Runs during consolidation or at session start to check all active
intentions against the current context. Triggered intentions are
surfaced to the agent's working memory or prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..experimental import unavailable
from .intention import Intention

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def sweep_intentions(
    store: EngramStore,
    context: dict[str, Any],
    agent_id: str = "default",
) -> list[Intention]:
    """Check all active intentions against current context.

    Args:
        store: The engram store for loading intentions.
        context: Current context (keywords, time, emotional state, etc.).
        agent_id: Which agent's intentions to check.

    Returns:
        List of triggered intentions that should be surfaced.
    """
    unavailable("intention sweep")
