"""
Spreading activation: activation propagation through connection graph.

When an engram is activated (retrieved or attended to), activation
spreads through its connections with decay at each hop. This models
associative memory — activating one memory partially activates
related memories.

Activation spread follows connection strength and decays exponentially
with graph distance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def spread_activation(
    store: EngramStore,
    source_id: str,
    initial_activation: float = 1.0,
    decay_factor: float = 0.5,
    max_depth: int = 3,
    threshold: float = 0.05,
) -> dict[str, float]:
    """Spread activation from a source engram through the connection graph.

    Args:
        store: The engram store for connection traversal.
        source_id: The engram to activate.
        initial_activation: Starting activation level.
        decay_factor: Multiplier applied at each hop (0-1).
        max_depth: Maximum hops to spread.
        threshold: Minimum activation to continue spreading.

    Returns:
        Dict of {engram_id: activation_level} for all activated engrams.
    """
    unavailable("experimental spreading activation")
