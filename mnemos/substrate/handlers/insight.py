"""
Insight handler.

Triggered by: CONNECTION_DISCOVERED
Effect: Reflects on what a new connection between memories means.
         May produce a new "insight" engram if the connection reveals something non-obvious.
"""

import json
import logging
import os

from ..events import SubstrateEvent
from ..config import SubstrateConfig
from ..modulators import ModulatorState

log = logging.getLogger("mnemos.substrate.insight")


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Reflect on a newly discovered connection."""
    produced_events: list[SubstrateEvent] = []

    from_id = event.payload.get("from_engram_id")
    to_id = event.payload.get("to_engram_id")
    connection_type = event.payload.get("connection_type", "unknown")

    if not from_id or not to_id:
        return produced_events

    from_engram = store.get_engram(from_id, read_visibility="operational_context")
    to_engram = store.get_engram(to_id, read_visibility="operational_context")

    if not from_engram or not to_engram:
        return produced_events
    if (
        from_engram.owner_agent_id != config.agent_id
        or to_engram.owner_agent_id != config.agent_id
        or not from_engram.consolidation_authorized
        or not to_engram.consolidation_authorized
    ):
        return produced_events

    agent_name = config.agent_name
    prompt = f"""Two of your memories just became connected.

Memory A: {from_engram.content}
Memory B: {to_engram.content}
Connection type: {connection_type}

What does this connection reveal? Is there an insight here — something you
didn't notice before that becomes visible now that these memories are linked?

If the connection is obvious or trivial, respond: {{"insight": null}}
If there's a genuine insight: {{"insight": "<the insight>", "significance": "<why it matters>"}}
"""

    try:
        text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent reflecting on newly discovered connections between memories. Respond only with valid JSON.",
            user=prompt,
            temperature=modulators.temperature,
        )

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log.debug(f"Insight handler parse error: {e}")
        return produced_events

    insight_content = result.get("insight")
    if not insight_content:
        log.debug(f"Connection {from_id}<->{to_id} yielded no insight")
        return produced_events

    significance = result.get("significance", "")
    log.info(f"Insight: {insight_content[:80]}...")

    # Encode the insight
    from mnemos.encoding.encoder import Encoder
    from mnemos.store.embedding_index import EmbeddingIndex
    from mnemos.core.types import SourceAuthority

    ei = EmbeddingIndex(db_path=os.path.expanduser(config.db_path))
    encoder = Encoder(store, embedding_index=ei, llm_client=llm_client)

    encoder.encode(
        content=f"[insight] {insight_content}",
        impact=significance,
        kind="semantic",
        tags=["insight", "connection"],
        agent_id=config.agent_id,
        skip_surprise_detection=True,
        # Autonomous substrate producer: generated (F1) — model-synthesized
        # content must not wear observed authority.
        source_authority=SourceAuthority.GENERATED,
    )

    return produced_events
