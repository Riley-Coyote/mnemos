"""
External observer: multi-model calibration and reflection.

An external model (different from the agent) periodically reviews
the agent's memory state and provides calibration feedback:
- Are beliefs well-supported by evidence?
- Are there patterns the agent might be missing?
- Are confidence scores well-calibrated?
- Are there blind spots in the memory coverage?

The observer creates OBSERVER-source engrams with its findings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


class Observer:
    """External multi-model observer for memory calibration.

    Uses a different LLM to review the agent's memory state and
    provide calibration feedback.
    """

    def __init__(
        self,
        store: EngramStore,
        observer_llm_client: Any | None = None,
    ) -> None:
        self._store = store
        self._llm = observer_llm_client

    def run_observation(
        self,
        agent_id: str = "default",
        focus_area: str | None = None,
    ) -> dict[str, Any]:
        """Run an observation cycle on the agent's memory.

        Args:
            agent_id: Which agent's memory to observe.
            focus_area: Optional area to focus on (e.g., "beliefs", "gaps").

        Returns:
            Observation report dict.
        """
        unavailable("external observer")
