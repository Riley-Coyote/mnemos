"""
LLM-based classification for Mnemos encoder.

Two batched classification calls per encoding:
1. Connection type classification — given a new memory + FTS5 candidates,
   classify each relationship as one of 7 types (or NONE).
2. Belief comparison — given a new memory + active beliefs,
   determine if each is supported, contradicted, or unaffected.

Design decisions (validated in agent design review):
- Batched calls: all candidates in one prompt, all beliefs in one prompt
- 7 connection types + NONE: supports, contradicts, causes, extends,
  parallels, synthesizes, grounds
- Belief comparison classifies relationship only; automatic classifier output
  never mutates belief confidence
- Confidence mutation is restricted to explicit review/correction authority
- Temperature 0.0 for deterministic output
- Start with Sonnet, downgrade only after validating quality
- Log suppressed SUPPORTS/CONTRADICTS confidence requests for auditability
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.types import ConnectionRelation

if TYPE_CHECKING:
    from ..llm import LLMClient
    from ..core.engram import Engram
    from ..core.belief import Belief

log = logging.getLogger("mnemos.classifier")

# The 7 core types the LLM is allowed to return
VALID_RELATIONS = {
    "SUPPORTS",
    "CONTRADICTS",
    "CAUSES",
    "EXTENDS",
    "PARALLELS",
    "SYNTHESIZES",
    "GROUNDS",
    "NONE",
}

# Map LLM response strings to ConnectionRelation enum values
RELATION_MAP: dict[str, ConnectionRelation] = {
    "SUPPORTS": ConnectionRelation.SUPPORTS,
    "CONTRADICTS": ConnectionRelation.CONTRADICTS,
    "CAUSES": ConnectionRelation.CAUSES,
    "EXTENDS": ConnectionRelation.EXTENDS,
    "PARALLELS": ConnectionRelation.PARALLELS,
    "SYNTHESIZES": ConnectionRelation.SYNTHESIZES,
    "GROUNDS": ConnectionRelation.GROUNDS,
}

# Minimum confidence threshold — below this, skip the connection
MIN_CONFIDENCE = 0.5


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

CONNECTION_SYSTEM_PROMPT = """You are a memory relationship classifier for a cognitive memory system. Given a NEW memory and a list of EXISTING memories, classify the relationship between the new memory and each existing one.

You MUST use exactly one of these 7 relationship types for each pair. Do not invent new types or use synonyms.

## Relationship Types

**SUPPORTS** — The new memory independently reinforces or corroborates the existing memory. They make the same point from different angles or provide independent evidence for the same conclusion. Neither adds new analysis beyond the other.

**CONTRADICTS** — The new memory provides genuine evidence against or is in tension with the existing memory. Not just mentioning a related topic — actual conflict in claims, evidence, or conclusions.

**CAUSES** — There is a temporal or causal chain between the memories. One event, decision, or condition led to or produced the other. The relationship has directionality in time.

**EXTENDS** — The new memory takes the existing memory further by adding new analysis, deeper insight, or additional layers. It builds ON TOP of the existing memory rather than just agreeing with it.

**PARALLELS** — The memories describe the same pattern or structure in different contexts. Structurally analogous but not causally connected. Same shape, different instances.

**SYNTHESIZES** — The new memory combines the existing memory with other information to create a more complete or unified picture. It doesn't just extend one source — it weaves multiple sources together.

**GROUNDS** — One memory provides foundational context that gives the other its full meaning. Without the grounding memory, the other is incomplete or decontextualized. This is about meaning-making, not temporal precedence.

## Boundary Cases (Pay Attention)

- **SUPPORTS vs EXTENDS:** If the new memory adds new analysis or goes further, it's EXTENDS. If it independently says the same thing, it's SUPPORTS. "I also noticed X" = supports. "Building on X, I realized Y" = extends.
- **CAUSES vs GROUNDS:** If there's a temporal chain (A happened, then B happened because of A), it's CAUSES. If A provides context that gives B meaning without necessarily preceding it, it's GROUNDS.
- **SUPPORTS vs PARALLELS:** If both memories are about the same topic, it's likely SUPPORTS. If they're about different topics but share the same structure or pattern, it's PARALLELS.
- **EXTENDS vs SYNTHESIZES:** If the new memory builds on ONE existing memory, it's EXTENDS. If it combines MULTIPLE sources into a unified picture, it's SYNTHESIZES.

## Response Format

Respond with ONLY a JSON array. No markdown, no explanation, no code fences. For each candidate, provide:
- "candidate_id": the ID of the existing memory
- "relation": exactly one of SUPPORTS, CONTRADICTS, CAUSES, EXTENDS, PARALLELS, SYNTHESIZES, GROUNDS, or NONE
- "direction": "forward" (new -> existing) or "reverse" (existing -> new). Forward: the new memory is the actor/source. Reverse: the existing memory is the actor/source. For CAUSES: if the existing memory caused the new one, direction is reverse.
- "confidence": 0.0-1.0 how confident you are in this classification
- "reasoning": one sentence explaining why

If a candidate is NOT meaningfully related to the new memory (the match was a false positive), return "relation": "NONE" and it will be skipped."""


BELIEF_SYSTEM_PROMPT = """You are a belief evaluator for a cognitive memory system. Given a NEW memory and a list of ACTIVE BELIEFS, determine how the new memory relates to each belief.

For each belief, respond with one of:
- **SUPPORTS** — The new memory provides evidence FOR this belief, reinforces it, or is consistent with it.
- **CONTRADICTS** — The new memory provides genuine evidence AGAINST this belief, challenges it, or is in tension with it.
- **NO_BEARING** — The new memory is unrelated to this belief or doesn't meaningfully affect its validity.

## Critical Rules

1. **Mentioning a topic is NOT contradiction.** A memory about a person that contains the word "not" does not contradict a belief about that person. Read the MEANING, not the keywords.

2. **Describing a belief is not contradicting it.** "The user creates conditions for emergence by stepping back and NOT controlling" SUPPORTS a belief about that user facilitating emergence — it doesn't contradict it just because it contains "not."

3. **Evidence of failure is genuine contradiction.** If a system designed to do X actively does the opposite of X, that IS contradiction. Be honest about real problems.

4. **Ambiguity defaults to NO_BEARING.** If you're unsure whether something supports or contradicts, it probably has no bearing. Don't force a classification.

5. **Severity matters.** A strong contradiction should have high impact. A mild tension should have low impact. Not all contradictions are equal.

## Response Format

Respond with ONLY a JSON array. No markdown, no explanation, no code fences. For each belief:
- "belief_id": the ID of the belief
- "relation": exactly one of SUPPORTS, CONTRADICTS, NO_BEARING
- "impact": 0.0-1.0 (strength of the evidence)
- "reasoning": one sentence explaining why"""


# ---------------------------------------------------------------------------
# Data classes for results
# ---------------------------------------------------------------------------


@dataclass
class ConnectionClassification:
    """Result of classifying a single connection."""

    candidate_id: str
    relation: ConnectionRelation
    direction: str  # "forward" or "reverse"
    confidence: float
    reasoning: str


@dataclass
class BeliefEvaluation:
    """Result of evaluating a new memory against a single belief."""

    belief_id: str
    relation: str  # "SUPPORTS", "CONTRADICTS", "NO_BEARING"
    impact: float
    reasoning: str


# ---------------------------------------------------------------------------
# Classification functions
# ---------------------------------------------------------------------------


def classify_connections(
    client: "LLMClient",
    new_engram: "Engram",
    candidates: list["Engram"],
) -> list[ConnectionClassification]:
    """Classify relationship types between a new memory and candidates.

    Makes a single batched LLM call with all candidates. Returns only
    classifications with confidence >= MIN_CONFIDENCE and relation != NONE.

    Args:
        client: LLM client with structured_complete method.
        new_engram: The newly created engram being encoded.
        candidates: Existing engrams found by FTS5 search.

    Returns:
        List of ConnectionClassification results, filtered and validated.
    """
    if not candidates:
        return []

    # Build user prompt
    user_parts = [
        "## New Memory",
        f"Content: {new_engram.content}",
        f"Impact: {new_engram.impact or '(none)'}",
        f"Kind: {new_engram.kind}",
        "",
        "## Candidate Existing Memories",
        "",
    ]

    for cand in candidates:
        user_parts.extend(
            [
                f"### Candidate {cand.id}",
                f"Content: {cand.content}",
                f"Impact: {cand.impact or '(none)'}",
                f"Kind: {cand.kind}",
                f"Created: {cand.created_at}",
                "",
            ]
        )

    user_parts.append(
        "Classify the relationship between the new memory and each candidate. "
        "Use the exact relationship types defined in your instructions. "
        "If a candidate is not meaningfully related, mark it as NONE."
    )

    user_prompt = "\n".join(user_parts)

    # Make the LLM call
    try:
        raw_response = client.structured_complete(
            system=CONNECTION_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.0,
            max_tokens=2000,
        )
    except Exception as e:
        log.error("Connection classification LLM call failed: %s", e)
        return []

    # Parse response
    return _parse_connection_response(raw_response, candidates)


def evaluate_beliefs(
    client: "LLMClient",
    new_engram: "Engram",
    beliefs: list["Belief"],
    *,
    include_no_bearing: bool = False,
) -> list[BeliefEvaluation]:
    """Evaluate how a new memory relates to active beliefs.

    Makes a single batched LLM call with all beliefs. Returns only
    evaluations where relation != NO_BEARING unless include_no_bearing=True.

    Args:
        client: LLM client with structured_complete method.
        new_engram: The newly created engram being encoded.
        beliefs: Active beliefs to evaluate against.

    Returns:
        List of BeliefEvaluation results (NO_BEARING filtered out).
    """
    if not beliefs:
        return []

    # Build user prompt
    user_parts = [
        "## New Memory",
        f"Content: {new_engram.content}",
        f"Impact: {new_engram.impact or '(none)'}",
        "",
        "## Active Beliefs",
        "",
    ]

    for belief in beliefs:
        user_parts.extend(
            [
                f'### Belief {belief.id}: "{belief.content}"',
                f"Current confidence: {belief.confidence}",
                "",
            ]
        )

    user_parts.append(
        "For each belief, determine: does this new memory SUPPORT it, "
        "CONTRADICT it, or have NO_BEARING on it? Respond with JSON."
    )

    user_prompt = "\n".join(user_parts)

    # Make the LLM call
    try:
        raw_response = client.structured_complete(
            system=BELIEF_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.0,
            max_tokens=1000,
        )
    except Exception as e:
        log.error("Belief evaluation LLM call failed: %s", e)
        return []

    # Parse and filter
    return _parse_belief_response(
        raw_response,
        beliefs,
        include_no_bearing=include_no_bearing,
    )


# ---------------------------------------------------------------------------
# Response parsing (defensive — LLMs can return malformed JSON)
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> list[dict]:
    """Extract a JSON array from LLM response, handling common formatting issues."""
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        elif isinstance(parsed, dict):
            return [parsed]
        else:
            log.warning("LLM returned unexpected JSON type: %s", type(parsed))
            return []
    except json.JSONDecodeError as e:
        log.error("Failed to parse LLM JSON response: %s\nRaw: %s", e, text[:500])
        return []


def _parse_connection_response(
    raw: str,
    candidates: list["Engram"],
) -> list[ConnectionClassification]:
    """Parse and validate connection classification response."""
    items = _extract_json(raw)
    valid_ids = {c.id for c in candidates}
    results = []

    for item in items:
        try:
            candidate_id = str(item.get("candidate_id", ""))
            relation_str = str(item.get("relation", "")).upper()
            direction = str(item.get("direction", "forward")).lower()
            confidence = float(item.get("confidence", 0.0))
            reasoning = str(item.get("reasoning", ""))

            # Validate
            if candidate_id not in valid_ids:
                log.debug("Skipping unknown candidate_id: %s", candidate_id)
                continue

            if relation_str not in VALID_RELATIONS:
                log.warning("Invalid relation '%s' -- skipping", relation_str)
                continue

            if relation_str == "NONE":
                log.debug("NONE for %s: %s", candidate_id, reasoning)
                continue

            if confidence < MIN_CONFIDENCE:
                log.debug(
                    "Low confidence %.2f for %s (%s) -- skipping",
                    confidence,
                    candidate_id,
                    relation_str,
                )
                continue

            if direction not in ("forward", "reverse"):
                direction = "forward"

            relation_enum = RELATION_MAP[relation_str]

            results.append(
                ConnectionClassification(
                    candidate_id=candidate_id,
                    relation=relation_enum,
                    direction=direction,
                    confidence=confidence,
                    reasoning=reasoning,
                )
            )

        except (KeyError, ValueError, TypeError) as e:
            log.warning("Skipping malformed classification item: %s -- %s", item, e)
            continue

    return results


def _parse_belief_response(
    raw: str,
    beliefs: list["Belief"],
    *,
    include_no_bearing: bool = False,
) -> list[BeliefEvaluation]:
    """Parse and validate belief evaluation response.

    Filters out NO_BEARING results unless include_no_bearing=True.
    """
    items = _extract_json(raw)
    valid_ids = {b.id for b in beliefs}
    results = []

    for item in items:
        try:
            belief_id = str(item.get("belief_id", ""))
            relation = str(item.get("relation", "")).upper()
            impact = float(item.get("impact", 0.0))
            reasoning = str(item.get("reasoning", ""))

            # Validate
            if belief_id not in valid_ids:
                log.debug("Skipping unknown belief_id: %s", belief_id)
                continue

            if relation not in ("SUPPORTS", "CONTRADICTS", "NO_BEARING"):
                log.warning("Invalid belief relation '%s' -- skipping", relation)
                continue

            # Filter out NO_BEARING (log only meaningful changes)
            if relation == "NO_BEARING" and not include_no_bearing:
                continue

            # Clamp impact to [0, 1]
            impact = max(0.0, min(1.0, impact))

            results.append(
                BeliefEvaluation(
                    belief_id=belief_id,
                    relation=relation,
                    impact=impact,
                    reasoning=reasoning,
                )
            )

        except (KeyError, ValueError, TypeError) as e:
            log.warning("Skipping malformed belief evaluation item: %s -- %s", item, e)
            continue

    return results


def apply_belief_update(
    belief: "Belief",
    evaluation: BeliefEvaluation,
    engram_id: str,
    store,
) -> None:
    """Suppress automatic belief-confidence mutation from LLM evaluations.

    Both SUPPORTS and CONTRADICTS are treated as automatic capture/consolidation
    signals here. Belief confidence moves only through explicit verbs until the
    receipted critic/review rail lands (render-with-dissent-beliefs section 3.3,
    step-3 charter addendum).

    Args:
        belief: The belief to update.
        evaluation: The evaluation result.
        engram_id: ID of the engram that triggered this evaluation.
        store: EngramStore accepted for API compatibility; this automatic path
            intentionally does not persist confidence changes.
    """
    if evaluation.relation in {"SUPPORTS", "CONTRADICTS"}:
        log.info(
            "Suppressed automatic %s belief confidence revision for %s; "
            "confidence changes require explicit receipted review/critic authority",
            evaluation.relation,
            belief.id,
        )
        return
    return  # NO_BEARING or unknown relation — no confidence change
