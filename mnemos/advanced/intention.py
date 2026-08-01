"""
Prospective memory: future-directed intentions with triggers.

Intentions are memories about things the agent needs to do in the future.
Each intention has trigger conditions that, when met, surface the intention
to the agent's attention.

Example:
    "When the user mentions the project deadline, remind them about
     the code review they requested."

Intentions are stored as engrams with kind=PROSPECTIVE and linked
to an Intention object that defines trigger conditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..experimental import unavailable


@dataclass
class TriggerCondition:
    """A condition that activates a prospective memory."""

    type: str = "keyword"
    """keyword | temporal | context | emotional"""

    value: str = ""
    """The trigger value (keyword, ISO timestamp, context pattern, emotion threshold)."""

    met: bool = False
    """Whether this condition has been met."""


@dataclass
class Intention:
    """A future-directed memory with trigger conditions.

    When all trigger conditions are met, the intention surfaces
    to the agent's working memory.
    """

    id: str = ""
    engram_id: str = ""
    """The prospective engram this intention is linked to."""

    description: str = ""
    action: str = ""
    triggers: list[TriggerCondition] = field(default_factory=list)
    priority: float = 0.5
    created_at: str = ""
    fulfilled_at: str | None = None
    expired: bool = False

    def check_triggers(self, context: dict[str, Any]) -> bool:
        """Check if all trigger conditions are met.

        Args:
            context: Current context dict with keys matching trigger types.

        Returns:
            True if all triggers are satisfied.
        """
        unavailable("prospective intention triggers")

    def fulfill(self) -> None:
        """Mark this intention as fulfilled."""
        unavailable("prospective intention fulfillment")

    def to_dict(self) -> dict[str, Any]:
        """Serialize intention to dict."""
        unavailable("prospective intention serialization")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Intention:
        """Deserialize intention from dict."""
        unavailable("prospective intention deserialization")
