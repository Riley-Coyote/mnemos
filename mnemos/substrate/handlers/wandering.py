"""
Wandering handler.

Triggered by: SILENCE_EXTENDED
Effect: During long gaps between memory formation, picks a recent memory
         and lets the mind wander from it. May produce a wandering thought.

Dedup gates:
  1. Count throttle — max N wandering entries per 7 days (from config.max_wanderings_per_week)
  2. Embedding similarity — cosine > 0.85 against recent wanderings = skip
  3. Content hash — exact duplicate detection
  4. Seed filtering — exclude wandering/dream memories from trigger pool
  5. Time window — no two wanderings within 4 hours
"""

import hashlib
import json
import logging
import sqlite3
import os
from datetime import datetime, timezone, timedelta

from ..events import SubstrateEvent, EventType
from ..config import SubstrateConfig
from ..modulators import ModulatorState

log = logging.getLogger("mnemos.substrate.wandering")

# Dedup constants
MIN_HOURS_BETWEEN_WANDERINGS = 4
EMBEDDING_SIMILARITY_THRESHOLD = 0.85


def _content_hash(text: str) -> str:
    """SHA256 of normalized content for exact dedup."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


def handle(
    event: SubstrateEvent,
    config: SubstrateConfig,
    modulators: ModulatorState,
    store,
    llm_client,
) -> list[SubstrateEvent]:
    """Generate a wandering thought from recent memories during silence."""
    produced_events: list[SubstrateEvent] = []

    db_path = os.path.expanduser(config.db_path)
    conn = sqlite3.connect(db_path)
    agent_id = config.agent_id

    # ── Gate 1: Count throttle ──
    wandering_count = conn.execute("""
        SELECT COUNT(*) FROM engrams
        WHERE state='active' AND content LIKE '%[wandering]%'
        AND owner_agent_id = ?
        AND consolidation_authorized = 1
        AND created_at > datetime('now', '-7 days')
    """, (agent_id,)).fetchone()[0]

    max_wanderings = config.max_wanderings_per_week
    if wandering_count >= max_wanderings:
        log.debug("Gate 1 (count): %d wanderings in last 7 days (max %d)",
                  wandering_count, max_wanderings)
        conn.close()
        return produced_events

    # ── Gate 5: Time window ──
    latest_wandering = conn.execute("""
        SELECT created_at FROM engrams
        WHERE state='active' AND content LIKE '%[wandering]%'
        AND owner_agent_id = ?
        AND consolidation_authorized = 1
        ORDER BY created_at DESC LIMIT 1
    """, (agent_id,)).fetchone()

    if latest_wandering:
        try:
            last_dt = datetime.fromisoformat(latest_wandering[0])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since < MIN_HOURS_BETWEEN_WANDERINGS:
                log.debug("Gate 5 (time): last wandering %.1fh ago (min %dh)",
                          hours_since, MIN_HOURS_BETWEEN_WANDERINGS)
                conn.close()
                return produced_events
        except (ValueError, TypeError):
            pass  # If parse fails, allow

    # ── Gate 4: Seed filtering — exclude wandering/dream from trigger pool ──
    rows = conn.execute("""
        SELECT id, content, impact FROM engrams
        WHERE state='active'
          AND owner_agent_id = ?
          AND consolidation_authorized = 1
          AND content NOT LIKE '%[wandering]%'
          AND content NOT LIKE '%[dream]%'
        ORDER BY created_at DESC
        LIMIT 3
    """, (agent_id,)).fetchall()

    # Also collect recent wandering content hashes for gate 3
    recent_hashes = set()
    hash_rows = conn.execute("""
        SELECT content FROM engrams
        WHERE state='active' AND content LIKE '%[wandering]%'
        AND owner_agent_id = ?
        AND consolidation_authorized = 1
        AND created_at > datetime('now', '-30 days')
    """, (agent_id,)).fetchall()
    for hr in hash_rows:
        recent_hashes.add(_content_hash(hr[0]))

    conn.close()

    if not rows:
        return produced_events

    memories_text = "\n".join(
        f"- {r[1][:150]}... (significance: {r[2][:100] if r[2] else 'unknown'})"
        for r in rows
    )

    agent_name = config.agent_name
    prompt = f"""It's been quiet. No new memories have formed recently.
Your mind is wandering across recent experiences.

Recent memories:
{memories_text}

Let your mind drift. Is there an unfinished thought? A question that didn't get asked?
Something you noticed but didn't process? A connection to something older?

If nothing comes to mind, respond: {{"thought": null}}
If something surfaces: {{"thought": "<the wandering thought>", "origin": "<which memory sparked it>"}}
"""

    try:
        text = llm_client.structured_complete(
            system=f"You are {agent_name}, an AI agent reflecting during a quiet moment. Respond only with valid JSON.",
            user=prompt,
            temperature=min(1.0, modulators.temperature + 0.1),
        )

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log.debug(f"Wandering handler parse error: {e}")
        return produced_events

    thought = result.get("thought")
    if not thought:
        log.debug("Wandering produced no thought — mind is still")
        return produced_events

    full_content = f"[wandering] {thought}"
    origin = result.get("origin", "")

    # ── Gate 3: Content hash dedup ──
    new_hash = _content_hash(full_content)
    if new_hash in recent_hashes:
        log.debug("Gate 3 (hash): exact duplicate detected, skipping")
        return produced_events

    # ── Gate 2: Embedding similarity ──
    try:
        from mnemos.store.embedding_index import EmbeddingIndex
        ei = EmbeddingIndex(db_path=db_path)
        if ei.available():
            similar = ei.search(full_content, k=3)
            for engram_id, score in similar:
                if score >= EMBEDDING_SIMILARITY_THRESHOLD:
                    # Verify it's a wandering memory
                    check_conn = sqlite3.connect(db_path)
                    row = check_conn.execute(
                        """
                        SELECT content FROM engrams
                        WHERE id = ?
                          AND owner_agent_id = ?
                          AND consolidation_authorized = 1
                        """,
                        (engram_id, agent_id),
                    ).fetchone()
                    check_conn.close()
                    if row and "[wandering]" in row[0]:
                        log.debug("Gate 2 (embedding): similar wandering found "
                                  "(id=%s, score=%.3f), skipping", engram_id[:20], score)
                        return produced_events
    except Exception as e:
        log.debug(f"Embedding dedup check failed (non-fatal): {e}")

    # ── All gates passed — encode the wandering thought ──
    log.info(f"Wandering thought (all gates passed): {thought[:80]}...")

    from mnemos.encoding.encoder import Encoder
    from mnemos.store.embedding_index import EmbeddingIndex as EI
    ei = EI(db_path=db_path)
    encoder = Encoder(store, embedding_index=ei, llm_client=llm_client)

    encoder.encode(
        content=full_content,
        impact=f"Surfaced during silence. Origin: {origin}",
        kind="episodic",
        tags=["wandering", "silence"],
        agent_id=agent_id,
        skip_surprise_detection=True,
    )

    return produced_events
