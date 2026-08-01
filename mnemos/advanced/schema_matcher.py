"""
Schema matcher: match incoming content against active schemas.

When new content arrives, the schema matcher checks it against all
active schemas to find the best match. A matching schema triggers
deeper encoding with slot-filling.
"""

from __future__ import annotations

from typing import Any

from ..experimental import unavailable
from .schema import CognitiveSchema


def match_schemas(
    content: str,
    tags: list[str],
    active_schemas: list[CognitiveSchema],
    threshold: float = 0.3,
) -> list[tuple[CognitiveSchema, float]]:
    """Find schemas that match incoming content.

    Args:
        content: The content to match against schemas.
        tags: Content tags for matching.
        active_schemas: List of schemas to check.
        threshold: Minimum match score to include.

    Returns:
        List of (schema, match_score) tuples sorted by descending score.
    """
    unavailable("schema matching")
