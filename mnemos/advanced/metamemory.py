"""
Metamemory: knowing what you know (and what you don't).

Metamemory tracks the agent's awareness of its own memory state:
- What topics it has strong knowledge about
- Where it has gaps or low confidence
- How accurate its memories have been historically
- What it should be uncertain about

This enables responses like "I'm not sure about that — I have some
memories but they're low confidence" rather than confabulating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..experimental import unavailable


@dataclass
class MetamemoryState:
    """The agent's awareness of its own memory capabilities."""

    agent_id: str = "default"

    known_domains: dict[str, float] = field(default_factory=dict)
    """Domain -> confidence coverage score (0-1)."""

    known_gaps: list[str] = field(default_factory=list)
    """Topics where the agent knows it lacks information."""

    accuracy_history: dict[str, float] = field(default_factory=dict)
    """Domain -> historical accuracy (fraction of verified memories)."""

    last_updated: str = ""

    def confidence_for_domain(self, domain: str) -> float:
        """Get the agent's metamemory confidence for a domain.

        Args:
            domain: The topic domain to query.

        Returns:
            Confidence level (0.0 = no knowledge, 1.0 = expert coverage).
        """
        unavailable("metamemory confidence")

    def has_gap(self, topic: str) -> bool:
        """Check if the agent has a known knowledge gap.

        Args:
            topic: The topic to check.

        Returns:
            True if this topic is in the known gaps list.
        """
        unavailable("metamemory gap detection")

    def to_dict(self) -> dict[str, Any]:
        """Serialize metamemory state to dict."""
        unavailable("metamemory serialization")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MetamemoryState:
        """Deserialize metamemory state from dict."""
        unavailable("metamemory deserialization")
