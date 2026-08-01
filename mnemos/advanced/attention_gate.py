"""
Attention gate: determines encoding depth based on salience signals.

Not everything deserves deep encoding. The attention gate evaluates
incoming content against multiple salience signals to determine how
deeply it should be encoded:

Signals:
- Novelty: how different from existing memories
- Emotional salience: emotional state amplification
- Schema relevance: matches an active schema
- Goal relevance: relates to current agent goals
- WM load: cognitive load affects available encoding resources
- User emphasis: explicit markers of importance

Output: EncodingDepth (shallow | moderate | deep | elaborative)
"""

from __future__ import annotations

from typing import Any

from ..core.types import EncodingDepth
from ..experimental import unavailable


def gate_attention(
    content: str,
    tags: list[str],
    emotional_state: dict[str, float] | None = None,
    wm_load: float = 0.5,
    active_schemas: list[str] | None = None,
    agent_goals: list[str] | None = None,
) -> str:
    """Determine encoding depth for incoming content.

    Args:
        content: The content to evaluate.
        tags: Tags extracted from the content.
        emotional_state: Current emotional state dimensions.
        wm_load: Current working memory load (0-1+).
        active_schemas: Currently active schema IDs.
        agent_goals: Currently active agent goal descriptions.

    Returns:
        EncodingDepth value (shallow, moderate, deep, or elaborative).
    """
    unavailable("attention-gated encoding")
