"""
Reflection handler — the most dangerous handler.

Triggered by: BELIEF_CONTRADICTED
Effect: Re-examines a belief in light of contradicting evidence.
         May revise confidence up or down.

Guardrails:
  - Cooldown: won't re-examine the same belief within cooldown_hours
  - Max confidence change: capped at max_delta per reflection
  - skip_surprise_detection=True on all outputs to prevent feedback loops
  - Full audit trail via belief.revise()
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from ..events import SubstrateEvent
from ..config import SubstrateConfig
from ..modulators import ModulatorState
from ..llm import load_prompt

log = logging.getLogger("mnemos.substrate.reflection")


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Examine a contradicted belief and potentially revise confidence."""
    produced_events: list[SubstrateEvent] = []

    belief_id = event.payload.get("belief_id")
    trigger_engram_id = event.payload.get("trigger_engram_id")

    if not belief_id:
        log.warning("Reflection event missing belief_id")
        return produced_events

    # Get the belief
    beliefs = store.get_beliefs(agent_id=config.agent_id)
    belief = None
    for b in beliefs:
        if b.id == belief_id:
            belief = b
            break

    if belief is None:
        log.warning(f"Belief {belief_id} not found")
        return produced_events

    # ── Cooldown check ──
    now = datetime.now(timezone.utc)
    last_revised = datetime.fromisoformat(belief.last_revised)
    if last_revised.tzinfo is None:
        last_revised = last_revised.replace(tzinfo=timezone.utc)
    cooldown = timedelta(hours=config.reflection_cooldown_hours)
    if now - last_revised < cooldown:
        log.info(f"Belief {belief_id} in cooldown (last revised {belief.last_revised})")
        return produced_events

    # Get the triggering engram for context
    trigger_content = ""
    trigger_impact = ""
    if trigger_engram_id:
        engram = store.get_engram(trigger_engram_id, read_visibility="operational_context")
        if (
            engram
            and engram.owner_agent_id == config.agent_id
            and engram.consolidation_authorized
        ):
            trigger_content = engram.content
            trigger_impact = engram.impact or ""

    # ── Load and format prompt ──
    prompt_template = load_prompt("reflection.md")
    if prompt_template:
        prompt = prompt_template.format(
            belief_content=belief.content,
            belief_confidence=f"{belief.confidence:.2f}",
            trigger_content=trigger_content or "(no specific trigger — consolidation review)",
            trigger_impact=trigger_impact or "(no impact recorded)",
        )
    else:
        # Inline fallback if prompt file not found
        prompt = f"""You are examining one of your beliefs because new evidence has challenged it.

The Belief: {belief.content}
Current confidence: {belief.confidence:.2f}

Triggering Evidence: {trigger_content or '(no specific trigger — consolidation review)'}
Impact: {trigger_impact or '(no impact recorded)'}

Should this evidence change your confidence? Respond with JSON:
{{"new_confidence": <float 0.0-0.99>, "reasoning": "<why>", "should_revise": <true/false>}}
"""

    # ── LLM call ──
    agent_name = config.agent_name
    temperature = modulators.temperature
    try:
        response_text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent critically examining one of your beliefs against new evidence. Respond only with valid JSON.",
            user=prompt,
            temperature=temperature,
        )

        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        result = json.loads(response_text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error(f"Failed to parse reflection response: {e}")
        return produced_events

    # ── Apply guardrails ──
    should_revise = result.get("should_revise", False)
    if not should_revise:
        log.info(f"Reflection on belief {belief_id}: no revision needed. Reason: {result.get('reasoning', '')}")
        return produced_events

    new_confidence = result.get("new_confidence", belief.confidence)
    delta = new_confidence - belief.confidence

    # Cap the change
    if abs(delta) > config.max_confidence_change:
        capped_delta = config.max_confidence_change if delta > 0 else -config.max_confidence_change
        new_confidence = belief.confidence + capped_delta
        log.info(f"Capped confidence change from {delta:.3f} to {capped_delta:.3f}")

    # Clamp
    new_confidence = max(0.01, min(0.99, new_confidence))

    # ── Revise ──
    reasoning = result.get("reasoning", "reflection handler")
    belief.revise(
        new_confidence=new_confidence,
        reason=reasoning,
        trigger_engram_id=trigger_engram_id,
    )
    store.save_belief(belief)

    log.info(
        f"Revised belief {belief_id}: {belief.confidence - delta:.2f} -> {new_confidence:.2f} "
        f"({'+' if delta > 0 else ''}{delta:.3f}). Reason: {reasoning}"
    )

    return produced_events
