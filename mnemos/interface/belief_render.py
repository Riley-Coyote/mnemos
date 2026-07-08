"""Deterministic render helpers for operational beliefs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


STATE_NEVER_CHALLENGED = "never-challenged"
STATE_UNDER_CHALLENGE = "under-challenge"
STATE_REVISED_DOWN = "revised-down"


@dataclass(frozen=True)
class BeliefChallengeState:
    state: str
    date: str | None = None

    @property
    def line(self) -> str:
        if self.state == STATE_REVISED_DOWN and self.date:
            return f"challenge: {self.state} ({self.date})"
        return f"challenge: {self.state}"


def belief_challenge_state(belief: Any) -> BeliefChallengeState:
    """Derive launch-minimal challenge state from existing belief JSON only."""

    if bool(getattr(belief, "needs_review", False)) or bool(
        getattr(belief, "confidence_pending_review", False)
    ):
        return BeliefChallengeState(STATE_UNDER_CHALLENGE)

    annulled = _annulled_timestamps(belief)
    latest_down = None
    for revision in getattr(belief, "revision_history", []) or []:
        timestamp = _revision_value(revision, "timestamp")
        if str(timestamp) in annulled:
            continue
        old_conf = _float_or_none(_revision_value(revision, "old_confidence"))
        new_conf = _float_or_none(_revision_value(revision, "new_confidence"))
        if old_conf is None or new_conf is None or old_conf <= new_conf:
            continue
        if latest_down is None or str(timestamp) > str(
            _revision_value(latest_down, "timestamp")
        ):
            latest_down = revision

    if latest_down is not None:
        return BeliefChallengeState(
            STATE_REVISED_DOWN,
            _date_from_timestamp(_revision_value(latest_down, "timestamp")),
        )
    # Launch-minimal render exposes active/non-annulled challenge state only.
    # Annulled false challenges intentionally collapse out of operational belief
    # context until the future critic can write real challenge outcomes.
    return BeliefChallengeState(STATE_NEVER_CHALLENGED)


def format_belief_challenge_line(belief: Any) -> str:
    return belief_challenge_state(belief).line


def belief_render_metadata() -> dict[str, Any]:
    return {
        "tier": "rendered",
        "fitting_eligible": False,
        "citation_role": "belief-render",
    }


def format_belief_summary_line(
    belief: Any,
    *,
    include_revision_count: bool = False,
) -> str:
    pct = int(float(getattr(belief, "confidence", 0.0)) * 100)
    domain = getattr(belief, "domain", "general")
    suffix = ""
    if include_revision_count:
        revisions = len(getattr(belief, "revision_history", []) or [])
        suffix = f", {revisions} revisions"
    return (
        f"- {getattr(belief, 'content', '')} [{domain}, {pct}%{suffix}]\n"
        f"  {format_belief_challenge_line(belief)}"
    )


def _annulled_timestamps(belief: Any) -> set[str]:
    timestamps: set[str] = set()
    for revision in getattr(belief, "revision_history", []) or []:
        annuls = _revision_value(revision, "annuls")
        if isinstance(annuls, list):
            timestamps.update(str(item) for item in annuls)
    return timestamps


def _revision_value(revision: Any, key: str) -> Any:
    if isinstance(revision, dict):
        return revision.get(key)
    if hasattr(revision, key):
        return getattr(revision, key)
    extra = getattr(revision, "_extra_fields", {}) or {}
    return extra.get(key)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_from_timestamp(timestamp: Any) -> str:
    text = str(timestamp or "").strip()
    if not text:
        return "unknown-date"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else "unknown-date"
