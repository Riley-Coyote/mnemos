"""Read-visibility policy shared by store and migrations."""

from __future__ import annotations

from typing import Any

READ_VISIBILITY_OPERATIONAL = "operational_context"
READ_VISIBILITY_REVIEW = "review_only"
READ_VISIBILITY_AUDIT = "audit_only"
VALID_READ_VISIBILITIES = {
    READ_VISIBILITY_OPERATIONAL,
    READ_VISIBILITY_REVIEW,
    READ_VISIBILITY_AUDIT,
}

HYPO_PROMOTION_MIN_CONFIDENCE = 0.82
HYPO_PROMOTION_MIN_SALIENCE = 0.65


def is_hypomnema_promotion_candidate(
    *,
    active: Any = True,
    graduated_to_engram_id: Any = None,
    confidence: Any = 0,
    salience: Any = 0,
    revision_count: Any = 0,
    foundational: Any = False,
) -> bool:
    """Return whether a hypomnema row is stable enough for promotion review."""
    return (
        bool(active)
        and graduated_to_engram_id is None
        and float(confidence or 0) >= HYPO_PROMOTION_MIN_CONFIDENCE
        and float(salience or 0) >= HYPO_PROMOTION_MIN_SALIENCE
        and (int(revision_count or 0) >= 1 or bool(foundational))
    )


def classify_hypomnema_read_visibility(
    *,
    confidence: Any = 0,
    salience: Any = 0,
    foundational: Any = False,
    revision_count: Any = 0,
) -> str:
    """Classify new hypomnema writes before they can enter operational reads."""
    if is_hypomnema_promotion_candidate(
        confidence=confidence,
        salience=salience,
        foundational=foundational,
        revision_count=revision_count,
    ):
        return READ_VISIBILITY_REVIEW
    return READ_VISIBILITY_OPERATIONAL
