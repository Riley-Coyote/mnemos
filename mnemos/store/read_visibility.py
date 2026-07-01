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
HYPO_HIGH_BLAST_DOMAINS = {"identity", "foundational"}
HYPO_REVIEW_CANDIDATE_SQL = (
    "active = 1 "
    "AND graduated_to_engram_id IS NULL "
    "AND ("
    "(confidence >= ? "
    "AND salience >= ? "
    "AND (revision_count >= 1 OR foundational = 1)) "
    "OR foundational = 1 "
    "OR domain IN ('identity', 'foundational')"
    ")"
)


def is_hypomnema_promotion_candidate(
    *,
    active: Any = True,
    graduated_to_engram_id: Any = None,
    confidence: Any = 0,
    salience: Any = 0,
    revision_count: Any = 0,
    foundational: Any = False,
    domain: Any = "",
) -> bool:
    """Return whether a hypomnema row needs explicit review before operational use."""
    return (
        bool(active)
        and graduated_to_engram_id is None
        and (
            (
                float(confidence or 0) >= HYPO_PROMOTION_MIN_CONFIDENCE
                and float(salience or 0) >= HYPO_PROMOTION_MIN_SALIENCE
                and (int(revision_count or 0) >= 1 or bool(foundational))
            )
            or bool(foundational)
            or str(domain or "").strip() in HYPO_HIGH_BLAST_DOMAINS
        )
    )


def classify_hypomnema_read_visibility(
    *,
    confidence: Any = 0,
    salience: Any = 0,
    foundational: Any = False,
    revision_count: Any = 0,
    domain: Any = "",
) -> str:
    """Classify new hypomnema writes before they can enter operational reads."""
    if is_hypomnema_promotion_candidate(
        confidence=confidence,
        salience=salience,
        foundational=foundational,
        revision_count=revision_count,
        domain=domain,
    ):
        return READ_VISIBILITY_REVIEW
    if bool(foundational) or str(domain or "").strip() in HYPO_HIGH_BLAST_DOMAINS:
        return READ_VISIBILITY_REVIEW
    return READ_VISIBILITY_OPERATIONAL
