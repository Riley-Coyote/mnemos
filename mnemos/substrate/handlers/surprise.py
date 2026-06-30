"""
Surprise handler.

Triggered by: SURPRISE_DETECTED
Effect: Processes what was surprising and what it means.
         This is the ONLY handler that does NOT set skip_surprise_detection=True,
         because surprise is the original signal — it can't cause a loop.
"""

import json
import logging
import os

from ..events import SubstrateEvent
from ..config import SubstrateConfig
from ..modulators import ModulatorState

log = logging.getLogger("mnemos.substrate.surprise")


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Process a surprise event — what changed and what does it mean."""
    produced_events: list[SubstrateEvent] = []

    engram_id = event.payload.get("engram_id")
    surprise_score = event.payload.get("surprise_score", 0)

    if not engram_id:
        return produced_events

    engram = store.get_engram(engram_id, read_visibility="operational_context")
    if not engram:
        return produced_events
    if (
        engram.owner_agent_id != config.agent_id
        or not engram.consolidation_authorized
    ):
        return produced_events

    agent_name = config.agent_name
    prompt = f"""Something surprised you during memory formation.

The memory: {engram.content}
Its impact: {engram.impact or '(none)'}
Surprise score: {surprise_score:.2f}

What made this surprising? What expectation was violated? What does this change
about how you understand the world?

Respond with:
{{"reflection": "<what the surprise means>", "expectation_violated": "<what you expected instead>"}}
"""

    try:
        text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent processing something that surprised you. Respond only with valid JSON.",
            user=prompt,
            temperature=modulators.temperature,
        )

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log.debug(f"Surprise handler parse error: {e}")
        return produced_events

    reflection = result.get("reflection", "")
    if not reflection:
        return produced_events

    log.info(f"Surprise processed: {reflection[:80]}...")

    # Encode the surprise reflection (this one does NOT skip surprise detection)
    from mnemos.encoding.encoder import Encoder
    from mnemos.store.embedding_index import EmbeddingIndex
    ei = EmbeddingIndex(db_path=os.path.expanduser(config.db_path))
    encoder = Encoder(store, embedding_index=ei, llm_client=llm_client)

    expectation = result.get("expectation_violated", "")
    encoder.encode(
        content=f"[surprise] {reflection}",
        impact=f"Expectation violated: {expectation}. {reflection}",
        kind="emotional",
        tags=["surprise", "reflection"],
        agent_id=config.agent_id,
        skip_surprise_detection=False,  # Surprise CAN chain — it's the original signal
    )

    return produced_events
