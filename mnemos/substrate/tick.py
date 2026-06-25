from __future__ import annotations
"""
Substrate — consolidation daemon for the Mnemos memory system.

This is the background process that keeps the memory graph alive:
  - Decay: memories naturally fade over time
  - Connection discovery: find new links between memories
  - Belief review: check if beliefs need revision based on recent evidence
  - Event cascade: handlers fire on events produced by consolidation
  - Tier crossing detection: uses Mnemos core classify_belief_change()

Runs via cron every 4 hours. Each tick is a complete cycle.
"""

import sys
import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import SubstrateEvent, EventType
from .config import SubstrateConfig
from .modulators import compute_modulators, ModulatorState

from mnemos.store.sqlite_store import EngramStore
from mnemos.store.embedding_index import EmbeddingIndex

try:
    from mnemos.core.types import classify_belief_change, BeliefChangeKind
except ImportError:
    # Graceful fallback if core.types not available yet
    class BeliefChangeKind:
        CONTRADICTED = "contradicted"
        CONFIRMED = "confirmed"
        STABLE = "stable"

    def classify_belief_change(prev: float, current: float) -> str:
        if current < prev - 0.1:
            return BeliefChangeKind.CONTRADICTED
        elif current > prev + 0.1:
            return BeliefChangeKind.CONFIRMED
        return BeliefChangeKind.STABLE

try:
    from mnemos.llm import create_client
except ImportError:
    create_client = None

# Handler registry: event_type -> handler module
from .handlers import reflection, dreaming, insight, surprise, wandering, initiation

HANDLER_MAP = {
    EventType.BELIEF_CONTRADICTED: reflection,
    EventType.MEMORY_SOFTENED: dreaming,
    EventType.CONNECTION_DISCOVERED: insight,
    EventType.SURPRISE_DETECTED: surprise,
    EventType.SILENCE_EXTENDED: wandering,
    EventType.SALIENCE_ACCUMULATED: initiation,
}

log = logging.getLogger("mnemos.substrate")


class Substrate:
    """The consolidation daemon for an agent's memory."""

    def __init__(self, config: SubstrateConfig | None = None):
        self.config = config or SubstrateConfig()
        self.db_path = os.path.expanduser(self.config.db_path)

        # Initialize Mnemos components
        self.store = EngramStore(self.db_path)
        self.embedding_index = EmbeddingIndex(db_path=self.db_path)

        if create_client is not None:
            self.llm_client = create_client()
        else:
            self.llm_client = None

        # Snapshot belief states for tier crossing detection
        self._belief_snapshot: dict[str, float] = {}

        log.info(f"Substrate initialized (agent={self.config.agent_id}, db={self.db_path})")

    def tick(self) -> dict:
        """Run one complete substrate cycle.

        Returns a summary dict of what happened.
        """
        tick_start = datetime.now(timezone.utc)
        summary = {
            "tick_start": tick_start.isoformat(),
            "events_produced": 0,
            "events_handled": 0,
            "engrams_decayed": 0,
            "connections_discovered": 0,
            "beliefs_reviewed": 0,
            "handler_outputs": [],
        }

        # ── Phase 1: Snapshot beliefs (for tier crossing detection) ──
        self._snapshot_beliefs()

        # ── Phase 2: Consolidation (decay, connections, belief review) ──
        events = self._consolidate(summary)
        summary["events_produced"] = len(events)

        # ── Phase 3: Temporal events (silence detection) ──
        temporal_events = self._check_temporal(summary)
        events.extend(temporal_events)

        # ── Phase 4: Belief tier crossing detection ──
        crossing_events = self._check_belief_crossings()
        events.extend(crossing_events)

        # ── Phase 5: Compute modulators ──
        modulators = compute_modulators(
            self.db_path,
            recent_window_hours=self.config.recent_window_hours,
        )
        log.info(
            f"Modulators: arousal={modulators.arousal:.2f} openness={modulators.openness:.2f} "
            f"resolution={modulators.resolution:.2f} selection={modulators.selection_threshold:.2f} "
            f"temp={modulators.temperature:.2f}"
        )

        # ── Phase 6: Event cascade (handlers) ──
        engrams_produced = 0
        for event in events:
            if engrams_produced >= self.config.max_engrams_per_tick:
                log.info(f"Hit max engrams per tick ({self.config.max_engrams_per_tick}), stopping cascade")
                break

            handler = HANDLER_MAP.get(event.event_type)
            if handler is None:
                # BELIEF_CONFIRMED events are logged but not handled
                if event.event_type == EventType.BELIEF_CONFIRMED:
                    log.info(f"Belief confirmed: {event.payload.get('belief_id', 'unknown')}")
                continue

            try:
                new_events = handler.handle(
                    event=event,
                    config=self.config,
                    modulators=modulators,
                    store=self.store,
                    llm_client=self.llm_client,
                )
                summary["events_handled"] += 1
                summary["handler_outputs"].append({
                    "handler": handler.__name__.split(".")[-1],
                    "event": event.event_type.value,
                    "produced": len(new_events),
                })
                # Don't cascade further — depth 1 is enough for now
            except Exception as e:
                log.error(f"Handler {handler.__name__} failed on {event}: {e}", exc_info=True)

        # ── Phase 6.5: Introspection self-audit (opt-in, off by default) ──
        if self.config.introspection_enabled:
            try:
                from .introspection_pass import run_introspection_pass
                summary["introspection"] = run_introspection_pass(
                    self.config, self.store, self.llm_client
                )
            except Exception as e:
                log.error(f"Introspection pass failed: {e}", exc_info=True)
                summary["introspection_error"] = str(e)

        # ── Phase 7: Log tick summary ──
        tick_end = datetime.now(timezone.utc)
        summary["tick_duration_seconds"] = (tick_end - tick_start).total_seconds()
        summary["modulators"] = {
            "arousal": modulators.arousal,
            "openness": modulators.openness,
            "resolution": modulators.resolution,
            "selection_threshold": modulators.selection_threshold,
            "temperature": modulators.temperature,
        }

        self._log_tick(summary)
        return summary

    def _snapshot_beliefs(self):
        """Take a snapshot of belief confidences for tier crossing detection."""
        beliefs = self.store.get_beliefs(agent_id=self.config.agent_id)
        self._belief_snapshot = {b.id: b.confidence for b in beliefs}

    def _consolidate(self, summary: dict) -> list[SubstrateEvent]:
        """Run consolidation: decay, connection discovery, belief review."""
        events: list[SubstrateEvent] = []

        # ── Decay ──
        conn = sqlite3.connect(self.db_path)
        decayed = conn.execute("""
            UPDATE engrams
            SET accessibility = MAX(0.05, accessibility - ?),
                strength = MAX(0.05, strength - ? * 0.5)
            WHERE state = 'active'
              AND owner_agent_id = ?
              AND accessibility > 0.1
              AND decay_protected = 0
              AND consolidation_authorized = 1
        """, (self.config.decay_rate, self.config.decay_rate, self.config.agent_id))
        decay_count = decayed.rowcount
        conn.commit()

        # Find memories that dropped below vividness threshold (softened)
        softened = conn.execute("""
            SELECT id FROM engrams
            WHERE state = 'active'
              AND owner_agent_id = ?
              AND (accessibility * strength) < 0.15
              AND (accessibility * strength) > 0.01
              AND softening_protected = 0
              AND consolidation_authorized = 1
            ORDER BY RANDOM()
            LIMIT 3
        """, (self.config.agent_id,)).fetchall()
        conn.close()

        for row in softened:
            events.append(SubstrateEvent(
                event_type=EventType.MEMORY_SOFTENED,
                payload={"engram_id": row[0]},
                source="decay",
            ))

        summary["engrams_decayed"] = decay_count
        log.info(f"Decay pass: {decay_count} engrams decayed, {len(softened)} softened")

        # ── Connection Discovery ──
        conn = sqlite3.connect(self.db_path)
        recent = conn.execute("""
            SELECT id FROM engrams
            WHERE state = 'active'
            ORDER BY created_at DESC
            LIMIT ?
        """, (self.config.connection_discovery_limit,)).fetchall()
        conn.close()

        new_connections = 0
        for row in recent[:5]:
            engram = self.store.get_engram(row[0])
            if not engram:
                continue
            try:
                similar = self.embedding_index.search(engram.content, limit=3)
                for match in similar:
                    if match["id"] != row[0]:
                        existing = sqlite3.connect(self.db_path)
                        exists = existing.execute(
                            "SELECT COUNT(*) FROM connections WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)",
                            (row[0], match["id"], match["id"], row[0])
                        ).fetchone()[0]
                        existing.close()

                        if exists == 0 and match.get("score", 0) > 0.7:
                            events.append(SubstrateEvent(
                                event_type=EventType.CONNECTION_DISCOVERED,
                                payload={
                                    "from_engram_id": row[0],
                                    "to_engram_id": match["id"],
                                    "connection_type": "parallels",
                                    "similarity": match.get("score", 0),
                                },
                                source="connection_discovery",
                            ))
                            new_connections += 1
            except Exception as e:
                log.debug(f"Connection discovery failed for {row[0]}: {e}")

        summary["connections_discovered"] = new_connections
        log.info(f"Connection discovery: {new_connections} potential new connections")

        # ── Belief Review ──
        beliefs = self.store.get_beliefs(agent_id=self.config.agent_id)
        reviewed = 0
        for belief in beliefs[:self.config.belief_review_limit]:
            try:
                results = self.embedding_index.search(belief.content, limit=3)
                for match in results:
                    if match.get("score", 0) > 0.6:
                        engram = self.store.get_engram(match["id"])
                        if engram and engram.impact:
                            reviewed += 1
            except Exception:
                pass

        summary["beliefs_reviewed"] = reviewed

        return events

    def _check_temporal(self, summary: dict) -> list[SubstrateEvent]:
        """Check for temporal events like extended silence."""
        events: list[SubstrateEvent] = []

        conn = sqlite3.connect(self.db_path)
        last_memory = conn.execute("""
            SELECT created_at FROM engrams
            WHERE state = 'active'
            ORDER BY created_at DESC
            LIMIT 1
        """).fetchone()
        conn.close()

        if last_memory:
            last_time = datetime.fromisoformat(last_memory[0])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            silence_hours = (now - last_time).total_seconds() / 3600

            if silence_hours > self.config.silence_threshold_hours:
                events.append(SubstrateEvent(
                    event_type=EventType.SILENCE_EXTENDED,
                    payload={"silence_hours": silence_hours},
                    source="temporal",
                ))
                log.info(f"Extended silence detected: {silence_hours:.1f}h since last memory")

        return events

    def _check_belief_crossings(self) -> list[SubstrateEvent]:
        """Detect belief tier crossings since the last snapshot."""
        events: list[SubstrateEvent] = []
        beliefs = self.store.get_beliefs(agent_id=self.config.agent_id)

        for belief in beliefs:
            prev = self._belief_snapshot.get(belief.id)
            if prev is None:
                continue

            kind = classify_belief_change(prev, belief.confidence)

            if kind == BeliefChangeKind.CONTRADICTED:
                events.append(SubstrateEvent(
                    event_type=EventType.BELIEF_CONTRADICTED,
                    payload={
                        "belief_id": belief.id,
                        "previous_confidence": prev,
                        "current_confidence": belief.confidence,
                    },
                    source="tier_crossing",
                ))
                log.info(
                    f"Belief CONTRADICTED: {belief.content[:50]}... "
                    f"({prev:.2f} -> {belief.confidence:.2f})"
                )
            elif kind == BeliefChangeKind.CONFIRMED:
                events.append(SubstrateEvent(
                    event_type=EventType.BELIEF_CONFIRMED,
                    payload={
                        "belief_id": belief.id,
                        "previous_confidence": prev,
                        "current_confidence": belief.confidence,
                    },
                    source="tier_crossing",
                ))
                log.info(
                    f"Belief CONFIRMED: {belief.content[:50]}... "
                    f"({prev:.2f} -> {belief.confidence:.2f})"
                )

        return events

    def _log_tick(self, summary: dict):
        """Write tick summary to log file."""
        log_dir = os.path.expanduser(self.config.log_dir)
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, "substrate.jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(summary) + "\n")

        log.info(
            f"Tick complete in {summary['tick_duration_seconds']:.1f}s: "
            f"{summary['events_produced']} events, {summary['events_handled']} handled, "
            f"{summary['engrams_decayed']} decayed"
        )


# ── CLI entry point ──
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = SubstrateConfig.from_env()
    substrate = Substrate(config)

    if "--dry-run" in sys.argv:
        print("Dry run — initializing without tick")
        print(f"Store: {substrate.store.count_engrams(agent_id=config.agent_id)} engrams")
        beliefs = substrate.store.get_beliefs(agent_id=config.agent_id)
        print(f"Beliefs: {len(beliefs)}")
        mods = compute_modulators(substrate.db_path)
        print(f"Modulators: arousal={mods.arousal} openness={mods.openness} resolution={mods.resolution}")
    else:
        summary = substrate.tick()
        print(json.dumps(summary, indent=2))
