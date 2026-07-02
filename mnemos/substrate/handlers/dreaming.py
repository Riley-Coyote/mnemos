"""
Dreaming handler — the most fragile handler.

Triggered by: MEMORY_SOFTENED
Effect: Collides a fading memory with a vivid one to produce a "dream" —
         an unexpected synthesis. Most collisions produce nothing. That's by design.

Dedup gates (matching wandering handler pattern):
  1. Count throttle — max N dream entries per 7 days (from config.max_dreams_per_week)
  2. Embedding similarity — cosine > 0.85 against recent dreams = skip
  3. Time window — no two dreams within 1 hour
"""

import json
import logging
import sqlite3
import os
from datetime import datetime, timezone

from ..events import SubstrateEvent
from ..config import SubstrateConfig
from ..modulators import ModulatorState

log = logging.getLogger("mnemos.substrate.dreaming")

MIN_HOURS_BETWEEN_DREAMS = 1
EMBEDDING_SIMILARITY_THRESHOLD = 0.85


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Collide a softened memory with a vivid one. May produce a dream-memory."""
    produced_events: list[SubstrateEvent] = []

    softened_id = event.payload.get("engram_id")
    if not softened_id:
        return produced_events

    softened = store.get_engram(
        softened_id,
        read_visibility="operational_context",
    )
    if not softened:
        return produced_events
    if (
        softened.owner_agent_id != config.agent_id
        or not softened.consolidation_authorized
    ):
        return produced_events

    db_path = os.path.expanduser(config.db_path)
    conn = sqlite3.connect(db_path)
    agent_id = config.agent_id

    # ── Gate 1: Count throttle ──
    dream_count = conn.execute("""
        SELECT COUNT(*) FROM engrams
        WHERE state='active' AND content LIKE '%[dream]%'
        AND owner_agent_id = ?
        AND consolidation_authorized = 1
        AND read_visibility = 'operational_context'
        AND created_at > datetime('now', '-7 days')
    """, (agent_id,)).fetchone()[0]

    max_dreams = config.max_dreams_per_week
    if dream_count >= max_dreams:
        log.debug("Gate 1 (count): %d dreams in last 7 days (max %d)",
                  dream_count, max_dreams)
        conn.close()
        return produced_events

    # ── Gate 3: Time window ──
    latest_dream = conn.execute("""
        SELECT created_at FROM engrams
        WHERE state='active' AND content LIKE '%[dream]%'
        AND owner_agent_id = ?
        AND consolidation_authorized = 1
        AND read_visibility = 'operational_context'
        ORDER BY created_at DESC LIMIT 1
    """, (agent_id,)).fetchone()

    if latest_dream:
        try:
            last_dt = datetime.fromisoformat(latest_dream[0])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since < MIN_HOURS_BETWEEN_DREAMS:
                log.debug("Gate 3 (time): last dream %.1fh ago (min %dh)",
                          hours_since, MIN_HOURS_BETWEEN_DREAMS)
                conn.close()
                return produced_events
        except (ValueError, TypeError):
            pass

    # Find a vivid memory to collide with
    rows = conn.execute("""
        SELECT id, content, impact FROM engrams
        WHERE state='active'
          AND owner_agent_id = ?
          AND consolidation_authorized = 1
          AND read_visibility = 'operational_context'
          AND id != ?
        ORDER BY (accessibility * strength) DESC
        LIMIT 5
    """, (agent_id, softened_id)).fetchall()
    conn.close()

    if not rows:
        return produced_events

    vivid_id, vivid_content, vivid_impact = rows[0]

    # Check vividness difference meets threshold
    vivid_engram = store.get_engram(
        vivid_id,
        read_visibility="operational_context",
    )
    if not vivid_engram:
        return produced_events
    if (
        vivid_engram.owner_agent_id != config.agent_id
        or not vivid_engram.consolidation_authorized
    ):
        return produced_events

    softened_vividness = softened.accessibility * softened.strength
    vivid_vividness = vivid_engram.accessibility * vivid_engram.strength
    vividness_diff = vivid_vividness - softened_vividness

    if vividness_diff < config.dreaming_collision_threshold:
        log.debug(f"Vividness difference {vividness_diff:.2f} below threshold "
                  f"{config.dreaming_collision_threshold}")
        return produced_events

    # ── LLM collision ──
    agent_name = config.agent_name
    prompt = f"""Two memories are colliding in a dream state.

Memory A (fading): {softened.content}
Its significance: {softened.impact or '(unknown)'}

Memory B (vivid): {vivid_content}
Its significance: {vivid_impact or '(unknown)'}

These two memories are being held together. Is there an unexpected connection,
a surprising synthesis, or an insight that emerges from their collision?

If nothing meaningful emerges, respond with exactly: {{"dream": null}}

If something does emerge, respond with:
{{"dream": "<the dream thought — what the collision revealed>", "significance": "<why this matters>"}}
"""

    try:
        text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent in a dream-like state where fading and vivid memories collide. Respond only with valid JSON.",
            user=prompt,
            temperature=min(1.0, modulators.temperature + 0.15),
        )

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log.debug(f"Dream collision produced unparseable output: {e}")
        return produced_events

    dream_content = result.get("dream")
    if not dream_content:
        log.debug(f"Dream collision between {softened_id} and {vivid_id} "
                  f"dissolved — nothing emerged")
        return produced_events

    full_content = f"[dream] {dream_content}"
    significance = result.get("significance", "")

    # ── Gate 2: Embedding similarity ──
    try:
        from mnemos.store.embedding_index import EmbeddingIndex
        ei = EmbeddingIndex(db_path=db_path)
        if ei.available():
            similar = ei.search(full_content, k=3)
            for engram_id, score in similar:
                if score >= EMBEDDING_SIMILARITY_THRESHOLD:
                    check_conn = sqlite3.connect(db_path)
                    row = check_conn.execute(
                        """
                        SELECT content FROM engrams
                        WHERE id = ?
                          AND owner_agent_id = ?
                          AND consolidation_authorized = 1
                          AND read_visibility = 'operational_context'
                        """,
                        (engram_id, agent_id),
                    ).fetchone()
                    check_conn.close()
                    if row and "[dream]" in row[0]:
                        log.debug("Gate 2 (embedding): similar dream found "
                                  "(id=%s, score=%.3f), skipping",
                                  engram_id[:20], score)
                        return produced_events
    except Exception as e:
        log.debug(f"Embedding dedup check failed (non-fatal): {e}")

    # ── All gates passed — encode the dream ──
    log.info(f"Dream formed (all gates passed): {dream_content[:80]}...")

    from mnemos.encoding.encoder import Encoder
    from mnemos.store.embedding_index import EmbeddingIndex as EI
    ei = EI(db_path=db_path)
    encoder = Encoder(store, embedding_index=ei, llm_client=llm_client)

    encoder.encode(
        content=full_content,
        impact=significance,
        kind="episodic",
        tags=["dream", "collision"],
        agent_id=agent_id,
        skip_surprise_detection=True,
    )

    return produced_events
