"""
Dreaming: creative connection discovery through free association.

Dream consolidation uses LLM-mediated free association to find
non-obvious connections between memories. Unlike regular connection
discovery (which uses embedding similarity), dreaming explores
metaphorical, analogical, and creative relationships.

Dream-generated connections have source='dream' and often produce
ANALOGOUS_TO connections that bridge different domains.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def run_dream_cycle(
    store: EngramStore,
    llm_client: Any,
    agent_id: str = "default",
    seed_count: int = 3,
) -> dict[str, Any]:
    """Run a dream cycle for creative connection discovery.

    Process:
    1. Select random seed engrams from different domains
    2. Ask the LLM to free-associate between them
    3. Create ANALOGOUS_TO connections for valid associations
    4. Generate dream-source engrams for novel insights

    Args:
        store: The engram store for reading and creating memories.
        llm_client: LLM client for free association generation.
        agent_id: Which agent is dreaming.
        seed_count: Number of random seed engrams to select.

    Returns:
        Statistics dict:
        {
            "seeds_selected": int,
            "associations_generated": int,
            "connections_created": int,
            "dream_engrams_created": int,
        }
    """
    unavailable("advanced dreaming")
