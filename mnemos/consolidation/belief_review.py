"""
Belief review pass: examine recent memories against active beliefs.

Phase 2 upgrade:
- Uses LLM classifier for semantic belief evaluation
- Explicit review-queue confidence mutation remains allowed here
- Confidence bounds [0.05, 0.95]: beliefs never fully die or become unquestionable
- Skips substrate-generated engrams to prevent feedback loops
- Logs only meaningful changes (not NO_BEARING evaluations)

During consolidation, belief review catches memories that were encoded
without surprise detection (e.g., skip_surprise_detection=True for
substrate reflections), or memories that gain new relevance to beliefs
as the graph grows.
"""

from __future__ import annotations

from ..core.types import DEFAULT_AGENT_ID

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from ..encoding.llm_classifier import BeliefEvaluation, evaluate_beliefs

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

log = logging.getLogger("mnemos.consolidation.beliefs")

REVIEW_SUPPORT_MULTIPLIER = 0.07
REVIEW_CONTRADICT_MULTIPLIER = 0.04
REVIEW_CONFIDENCE_FLOOR = 0.05
REVIEW_CONFIDENCE_CEILING = 0.95


def format_belief_review_summary(stats: dict[str, Any]) -> str:
    return (
        f"{stats.get('beliefs_reviewed', 0)} reviewed, "
        f"{stats.get('beliefs_strengthened', 0)} strengthened, "
        f"{stats.get('beliefs_weakened', 0)} weakened, "
        f"{stats.get('beliefs_left_pending', 0)} left pending"
    )


def run_belief_review(
    store: EngramStore,
    config: dict[str, Any] | None = None,
    llm_client: Any | None = None,
    agent_id: str = DEFAULT_AGENT_ID,
) -> dict[str, Any]:
    """Review recent memories against active beliefs.

    Evaluates recent memories against active beliefs using the same LLM
    classifier as the encoder. Automatic evidence for non-pending beliefs is
    logged without confidence mutation; only beliefs already queued with
    ``needs_review`` or ``confidence_pending_review`` can be resolved here.

    Args:
        store: Engram store.
        config: Consolidation config.
        llm_client: LLM client for semantic evaluation. Without this,
            the pass is a no-op (old heuristic removed).
        agent_id: Agent whose beliefs to review.

    Returns:
        Statistics dict.
    """
    config = config or {}
    max_memories = config.get("belief_review_max_memories", 30)
    review_window_hours = config.get("belief_review_window_hours", 12)

    stats = {
        "memories_reviewed": 0,
        "beliefs_reviewed": 0,
        "beliefs_resolved": 0,
        "beliefs_left_pending": 0,
        "beliefs_strengthened": 0,
        "beliefs_weakened": 0,
        "beliefs_unchanged": 0,
        "skipped_substrate": 0,
    }

    if not llm_client:
        log.info("No LLM client — belief review skipped (heuristic removed)")
        return stats

    # Get active beliefs
    beliefs = store.get_beliefs(
        agent_id,
        active_only=True,
        include_pending_review=True,
    )
    if not beliefs:
        log.info("No active beliefs to review")
        return stats

    # Get recent memories (last N hours, exclude substrate-generated)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=review_window_hours)
    recent = store.get_recent_engrams(
        agent_id=agent_id,
        since=cutoff,
        limit=max_memories,
        require_consolidation_authorized=True,
    )

    for engram in recent:
        # Skip substrate-generated engrams — prevent feedback loop
        source = getattr(engram, "source_type", None) or getattr(engram, "source", None)
        if source and str(source).lower() in (
            "substrate",
            "reflection",
            "consolidation",
        ):
            stats["skipped_substrate"] += 1
            continue

        beliefs_to_evaluate = beliefs
        if getattr(engram.encoding_context, "surprise_level", 0) > 0:
            beliefs_to_evaluate = [
                belief
                for belief in beliefs
                if belief.needs_review or belief.confidence_pending_review
            ]
            if not beliefs_to_evaluate:
                continue

        stats["memories_reviewed"] += 1

        # Evaluate against beliefs via LLM
        evaluations = evaluate_beliefs(
            llm_client,
            engram,
            beliefs_to_evaluate,
            include_no_bearing=True,
        )

        belief_map = {b.id: b for b in beliefs_to_evaluate}
        for evaluation in belief_map.values():
            stats["beliefs_unchanged"] += 1

        for eval_result in evaluations:
            belief = belief_map.get(eval_result.belief_id)
            if not belief:
                continue

            # Check cooldown
            cooldown_ok = True
            try:
                last_rev = datetime.fromisoformat(belief.last_revised)
                if last_rev.tzinfo is None:
                    last_rev = last_rev.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_rev) < timedelta(hours=6):
                    cooldown_ok = False
            except (ValueError, TypeError, AttributeError):
                pass

            if cooldown_ok:
                old_conf = belief.confidence
                pending_review = belief.needs_review or belief.confidence_pending_review
                if pending_review and eval_result.relation in {
                    "SUPPORTS",
                    "CONTRADICTS",
                }:
                    stats["beliefs_reviewed"] += 1
                    _resolve_pending_review_belief(
                        belief,
                        eval_result,
                        engram.id,
                        store,
                    )
                    stats["beliefs_resolved"] += 1
                elif pending_review and eval_result.relation == "NO_BEARING":
                    stats["beliefs_reviewed"] += 1
                    stats["beliefs_left_pending"] += 1
                    log.info(
                        "Left pending belief %s in review state; NO_BEARING "
                        "automatic evidence is not explicit review authority",
                        belief.id,
                    )
                elif eval_result.relation in {"SUPPORTS", "CONTRADICTS"}:
                    log.info(
                        "Suppressed automatic belief-review confidence revision "
                        "for non-pending belief %s; confidence changes require "
                        "explicit queued review authority",
                        belief.id,
                    )
                new_conf = belief.confidence

                if new_conf > old_conf:
                    stats["beliefs_strengthened"] += 1
                    stats["beliefs_unchanged"] -= 1
                elif new_conf < old_conf:
                    stats["beliefs_weakened"] += 1
                    stats["beliefs_unchanged"] -= 1
    return stats


def _resolve_pending_review_belief(
    belief: Any,
    evaluation: BeliefEvaluation,
    engram_id: str,
    store: EngramStore,
) -> None:
    """Resolve one explicit pending-review belief and persist the review outcome."""

    if not (belief.needs_review or belief.confidence_pending_review):
        raise ValueError(
            f"belief {belief.id} is not pending review; refusing confidence mutation"
        )

    if evaluation.relation == "SUPPORTS":
        delta = evaluation.impact * REVIEW_SUPPORT_MULTIPLIER
        new_confidence = belief.confidence + delta
        reason = (
            "Explicit belief review support "
            f"(impact {evaluation.impact:.2f}): {evaluation.reasoning}"
        )
    elif evaluation.relation == "CONTRADICTS":
        delta = evaluation.impact * REVIEW_CONTRADICT_MULTIPLIER
        new_confidence = belief.confidence - delta
        reason = (
            "Explicit belief review contradiction "
            f"(impact {evaluation.impact:.2f}): {evaluation.reasoning}"
        )
    else:
        raise ValueError(
            f"belief {belief.id} review relation {evaluation.relation!r} "
            "does not carry confidence authority"
        )

    new_confidence = max(
        REVIEW_CONFIDENCE_FLOOR,
        min(REVIEW_CONFIDENCE_CEILING, new_confidence),
    )
    old_confidence = belief.confidence
    if abs(new_confidence - belief.confidence) > 0.001:
        belief.revise(new_confidence, reason, trigger_engram_id=engram_id)
        log.info(
            "Explicit belief review updated '%s': %.3f -> %.3f (%s)",
            belief.id,
            old_confidence,
            new_confidence,
            evaluation.relation,
        )

    belief.needs_review = False
    belief.confidence_pending_review = False
    belief.read_visibility = "operational_context"
    belief.challenge()
    store.save_reviewed_belief(belief)
