"""
Interference modeling: similar memories competing for retrieval.

When multiple similar memories exist, they can interfere with each other:
- Proactive interference: old memories block retrieval of new ones
- Retroactive interference: new memories make old ones harder to access

Interference reduces accessibility of competing memories and can trigger
interference resolution during consolidation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..core.engram import Engram
    from ..store.sqlite_store import EngramStore


def calculate_interference(
    target: Engram,
    competitors: list[Engram],
) -> float:
    """Calculate interference level for a target engram.

    Args:
        target: The engram being retrieved.
        competitors: Similar engrams that may interfere.

    Returns:
        Interference level (0.0 = no interference, 1.0 = maximum).
    """
    unavailable("memory interference calculation")


def apply_interference(
    store: EngramStore,
    target_id: str,
    competitor_ids: list[str],
    interference_level: float,
) -> None:
    """Apply interference effects to competing engrams.

    Args:
        store: The engram store.
        target_id: The successfully retrieved engram.
        competitor_ids: Engrams that competed with the target.
        interference_level: The calculated interference level.
    """
    unavailable("memory interference application")
