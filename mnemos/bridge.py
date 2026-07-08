"""
Mnemos Bridge — direct Python API for memory operations.

Thin wrapper around Mnemos store/encoder/retriever so any agent
has a single interface to memory without going through MCP.

Usage:
    from mnemos.bridge import MnemosBridge

    bridge = MnemosBridge(agent_id="myagent")
    bridge.remember("User prefers dark mode", impact="Design starts from void")
    results = bridge.recall("dark mode preferences")
    print(bridge.status())
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .interface.belief_render import format_belief_summary_line

log = logging.getLogger("mnemos.bridge")

_bridges: dict[str, "MnemosBridge"] = {}


class MnemosBridge:
    """Direct Python interface to Mnemos memory operations."""

    def __init__(
        self,
        agent_id: str | None = None,
        db_path: str | None = None,
    ) -> None:
        self.agent_id = agent_id or os.environ.get("MNEMOS_AGENT_ID", "default")
        self.db_path = db_path or os.environ.get(
            "MNEMOS_DB_PATH",
            str(Path.home() / ".mnemos" / "memory.db"),
        )

        self._store = None
        self._encoder = None
        self._retriever = None
        self._llm_client = None
        self._embedding_index = None
        self._shared_pool = None
        self._initialized = False

    def _ensure_init(self) -> None:
        """Lazy-initialize store, encoder, retriever."""
        if self._initialized:
            return

        from .store.sqlite_store import EngramStore
        from .store.embedding_index import EmbeddingIndex
        from .encoding.encoder import Encoder
        from .retrieval.reactive import ReactiveRetriever
        from .llm import create_client

        self._store = EngramStore(self.db_path)
        self._embedding_index = EmbeddingIndex(db_path=self.db_path)
        self._llm_client = create_client()

        try:
            from .multiagent.shared_pool import SharedPool

            self._shared_pool = SharedPool()
        except Exception:
            self._shared_pool = None

        self._encoder = Encoder(
            self._store,
            embedding_index=self._embedding_index,
            llm_client=self._llm_client,
            shared_pool=self._shared_pool,
        )
        self._retriever = ReactiveRetriever(
            self._store,
            embedding_index=self._embedding_index,
            shared_store=self._shared_pool._store if self._shared_pool else None,
        )

        self._initialized = True
        log.info("Bridge initialized (agent=%s, db=%s)", self.agent_id, self.db_path)

    def remember(
        self,
        content: str,
        impact: str = "",
        kind: str = "semantic",
        tags: str = "",
        skip_surprise_detection: bool = False,
    ) -> str:
        """Encode a memory."""
        self._ensure_init()
        from .core.types import SourceAuthority, SourceType

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        engram = self._encoder.encode(
            content=content,
            impact=impact,
            kind=kind,
            tags=tag_list,
            source=SourceType.SESSION,
            agent_id=self.agent_id,
            skip_surprise_detection=skip_surprise_detection,
            # Programmatic library surface (agent channel): observed (F1).
            source_authority=SourceAuthority.OBSERVED,
        )

        return (
            f"Remembered: {engram.id}\n"
            f"  Confidence: {engram.source.confidence}\n"
            f"  Connections: {len(engram.connections)} discovered\n"
            f"  Tags: {', '.join(engram.tags) or '(none)'}"
        )

    def recall(self, query: str, max_results: int = 10) -> str:
        """Retrieve relevant memories."""
        self._ensure_init()
        emotional_state = self._store.get_latest_emotional_state(self.agent_id)
        results = self._retriever.retrieve(
            cue=query,
            agent_id=self.agent_id,
            max_results=max_results,
            emotional_state=emotional_state,
        )

        if not results:
            return "No relevant memories found."

        lines = []
        for r in results:
            display = r.engram.impact if r.engram.impact else r.engram.content
            if len(display) > 150:
                display = display[:147] + "..."
            pct = int(r.engram.source.confidence * 100)
            lines.append(
                f"[{r.score:.2f}] {display}\n"
                f"       id={r.engram.id[:25]}... kind={r.engram.kind} confidence={pct}%"
            )
        return f"Found {len(results)} memories:\n\n" + "\n\n".join(lines)

    def status(self) -> str:
        """Get memory system status."""
        self._ensure_init()
        stats = self._store.get_stats(self.agent_id)

        lines = [
            f"Mnemos Status (agent: {self.agent_id})",
            f"  Active engrams: {stats.get('engrams_active', 0)}",
            f"  Dormant: {stats.get('engrams_dormant', 0)}",
            f"  Archived: {stats.get('archived', 0)}",
            f"  Connections: {stats.get('connections', 0)}",
            f"  Active beliefs: {stats.get('beliefs_active', 0)}",
            f"  Reconsolidations: {stats.get('reconsolidation_events', 0)}",
        ]
        if "accessibility_avg" in stats:
            lines.append(f"  Avg accessibility: {stats['accessibility_avg']:.3f}")
        return "\n".join(lines)

    def beliefs(self) -> str:
        """List current beliefs."""
        self._ensure_init()
        belief_list = self._store.get_beliefs(
            agent_id=self.agent_id,
            domain=None,
            active_only=True,
        )
        if not belief_list:
            return "No active beliefs found."

        lines = []
        for b in belief_list:
            lines.append(format_belief_summary_line(b, include_revision_count=True))
        return f"{len(belief_list)} active beliefs:\n\n" + "\n".join(lines)

    def consolidate(self, deep: bool = False) -> str:
        """Run a consolidation cycle."""
        self._ensure_init()
        from .consolidation.daemon import ConsolidationDaemon

        daemon = ConsolidationDaemon(
            store=self._store,
            config={},
            llm_client=self._llm_client,
            embedding_index=self._embedding_index,
        )
        stats = daemon.run_cycle(deep=deep, agent_id=self.agent_id)

        lines = [f"Consolidation complete ({stats.get('cycle_type', 'unknown')})"]
        if "decay" in stats:
            d = stats["decay"]
            lines.append(f"  Decay: {d.get('engrams_decayed', 0)} decayed")
        if "connection_discovery" in stats:
            cd = stats["connection_discovery"]
            lines.append(f"  Connections: {cd.get('connections_created', 0)} new")
        if "belief_review" in stats:
            from .consolidation.belief_review import format_belief_review_summary

            br = stats["belief_review"]
            lines.append(f"  Beliefs: {format_belief_review_summary(br)}")
        return "\n".join(lines)
