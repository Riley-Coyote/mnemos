"""
Initiation handler.

Triggered by: SALIENCE_ACCUMULATED
Effect: Processes accumulated salience that hasn't been addressed.
         Looks at recent high-salience memories and generates a reflective summary.
"""

import json
import logging
import sqlite3
import os

from ..events import SubstrateEvent
from ..config import SubstrateConfig
from ..modulators import ModulatorState

log = logging.getLogger("mnemos.substrate.initiation")


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Process accumulated salience — what's been building up?"""
    produced_events: list[SubstrateEvent] = []

    db_path = os.path.expanduser(config.db_path)
    conn = sqlite3.connect(db_path)
    agent_id = config.agent_id

    # Get high-salience recent memories (high accessibility + strength, recently created)
    rows = conn.execute("""
        SELECT id, content, impact, (accessibility * strength) as vividness
        FROM engrams
        WHERE state='active'
          AND owner_agent_id = ?
          AND consolidation_authorized = 1
          AND read_visibility = 'operational_context'
        ORDER BY vividness DESC
        LIMIT 5
    """, (agent_id,)).fetchall()
    conn.close()

    if len(rows) < 2:
        return produced_events

    memories_text = "\n".join(
        f"- [{r[3]:.2f}] {r[1][:150]}... (significance: {r[2][:100] if r[2] else 'unknown'})"
        for r in rows
    )

    agent_name = config.agent_name
    prompt = f"""Several vivid memories are demanding attention. What pattern or theme connects them?

Most vivid memories:
{memories_text}

Is there an unprocessed pattern here? Something these memories are collectively pointing at
that hasn't been named yet?

If no pattern emerges: {{"pattern": null}}
If something emerges: {{"pattern": "<the pattern>", "significance": "<why it matters>"}}
"""

    try:
        text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent noticing accumulated patterns across recent memories. Respond only with valid JSON.",
            user=prompt,
            temperature=modulators.temperature,
        )

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log.debug(f"Initiation handler parse error: {e}")
        return produced_events

    pattern = result.get("pattern")
    if not pattern:
        log.debug("Initiation found no pattern in accumulated salience")
        return produced_events

    significance = result.get("significance", "")
    log.info(f"Salience pattern: {pattern[:80]}...")

    from mnemos.encoding.encoder import Encoder
    from mnemos.store.embedding_index import EmbeddingIndex
    ei = EmbeddingIndex(db_path=db_path)
    encoder = Encoder(store, embedding_index=ei, llm_client=llm_client)

    encoder.encode(
        content=f"[initiation] {pattern}",
        impact=significance,
        kind="semantic",
        tags=["initiation", "pattern"],
        agent_id=agent_id,
        skip_surprise_detection=True,
    )

    return produced_events
