"""
Core encoding pipeline for Mnemos.

Transforms raw content (text from sessions, reflections, observations) into
fully-formed Engrams with confidence scoring, encoding context, and discovered
connections to related memories.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from ..core.engram import Connection, Engram, EncodingContext, MemorySource
from ..core.types import (
    BOOTSTRAP_STABILITY,
    BOOTSTRAP_STRENGTH,
    ConfidenceSource,
    ConnectionRelation,
    DEFAULT_ACCESSIBILITY,
    DEFAULT_STABILITY,
    DEFAULT_STRENGTH,
    EncodingDepth,
    EngramKind,
    SourceAuthority,
    SourceType,
    Visibility,
)
from ..instrumentation.receipts import (
    ORIGIN_IMPORT,
    ORIGIN_INFERENCE,
    ORIGIN_USER_WITNESSED,
)
from .llm_classifier import (
    apply_belief_update,
    classify_connections,
    evaluate_beliefs,
)

# RFC-R1 authority values, harness-stamped at the ingest channel.
VALID_SOURCE_AUTHORITIES = frozenset(a.value for a in SourceAuthority)

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore
    from ..llm import LLMClient


# Baseline confidence by source type
_CONFIDENCE_BY_SOURCE: dict[str, tuple[float, str]] = {
    SourceType.SESSION: (0.75, ConfidenceSource.USER_IMPLIED),
    SourceType.BOOTSTRAP: (0.80, ConfidenceSource.USER_EXPLICIT),
    SourceType.BACKGROUND: (0.50, ConfidenceSource.MODEL_INFERRED),
    SourceType.REFLECTION: (0.45, ConfidenceSource.MODEL_INFERRED),
    SourceType.OBSERVER: (0.40, ConfidenceSource.MODEL_INFERRED),
    SourceType.DREAM: (0.30, ConfidenceSource.SPECULATIVE),
    SourceType.MERGE: (0.35, ConfidenceSource.SPECULATIVE),
    SourceType.BROWSER_EXTRACTION: (0.65, ConfidenceSource.USER_IMPLIED),
    SourceType.EXTERNAL: (0.55, ConfidenceSource.MODEL_INFERRED),
}

# Tags that trigger auto-sharing to the shared pool
_AUTO_SHARE_TAGS = frozenset(
    {
        "task-completion",
        "decision",
        "summary",
        "error",
        "discovery",
        "deployment",
        "architecture",
        "lesson",
        "distilled",
    }
)

# Tags that force engrams to stay private
_PRIVATE_TAGS = frozenset(
    {
        "internal",
        "emotional",
        "working-memory",
        "reflection",
        "thinking",
    }
)

# Source types that are internal processing and should stay private
_PRIVATE_SOURCES = frozenset(
    {
        SourceType.DREAM,
        SourceType.OBSERVER,
        SourceType.REFLECTION,
    }
)


def should_auto_share(engram: Engram) -> bool:
    """Determine whether an engram should be auto-published to the shared pool.

    Auto-share: task completions, decisions, summaries, errors, discoveries,
    lessons, and high-confidence semantic/procedural knowledge.

    Keep private: internal reasoning, emotional state, working memory,
    observer output, reflections, and dream-sourced content.
    """
    tags = set(engram.tags)

    # Explicit private tags override everything
    if tags & _PRIVATE_TAGS:
        return False

    # Internal processing sources stay private
    if engram.source.type in _PRIVATE_SOURCES:
        return False

    # Explicit share tags
    if tags & _AUTO_SHARE_TAGS:
        return True

    # High-confidence semantic/procedural knowledge
    if (
        engram.kind in (EngramKind.SEMANTIC, EngramKind.PROCEDURAL)
        and engram.source.confidence >= 0.7
    ):
        return True

    return False


class Encoder:
    """Transforms raw content into richly-connected engrams.

    The encoder is the entry point for all new memories. It handles:
    1. Creating the engram with appropriate initial dual-trace values
    2. Scoring confidence based on the source and content characteristics
    3. Capturing encoding context (emotional state, session, goals)
    4. Discovering connections to existing engrams in the store

    Usage:
        encoder = Encoder(store)
        engram = encoder.encode(
            content="The user prefers dark mode in all applications",
            kind=EngramKind.SEMANTIC,
            tags=["preference", "ui"],
            source=SourceType.SESSION,
            source_authority=SourceAuthority.OBSERVED,
        )
    """

    def __init__(
        self,
        store: EngramStore,
        max_connections: int = 5,
        embedding_index: Any | None = None,
        llm_client: LLMClient | None = None,
        shared_pool: Any | None = None,
    ) -> None:
        self._store = store
        self._max_connections = max_connections
        self._embedding_index = embedding_index
        self._llm_client = llm_client
        self._shared_pool = shared_pool

    def encode(
        self,
        content: str,
        impact: str = "",
        kind: str = EngramKind.EPISODIC,
        tags: list[str] | None = None,
        source: str = SourceType.SESSION,
        session_id: str | None = None,
        agent_id: str = "default",
        emotional_state: dict[str, float] | None = None,
        override_confidence: float | None = None,
        override_confidence_source: str | None = None,
        skip_surprise_detection: bool = False,
        *,
        source_authority: str,
        origin_stamp_override: str | None = None,
        allow_auto_share: bool = True,
    ) -> Engram:
        """Create a new engram from raw content.

        Args:
            content: What happened (the event/information/stimulus).
            impact: What it meant — how it changed understanding. Optional.
                When provided, this is the lasting trace that survives softening.
                Leave empty when there's no genuine insight (don't fabricate).
            kind: Classification (episodic, semantic, procedural, prospective).
            tags: Optional list of semantic tags for retrieval bias.
            source: How this memory entered the system.
            session_id: The originating session identifier, if any.
            agent_id: Which agent owns this memory.
            emotional_state: Current emotional state dict (6 dimensions).
            override_confidence: If set, use this confidence score instead of auto-scoring.
            override_confidence_source: If set, use this confidence source label.
            source_authority: RFC-R1 harness-stamped authority (required,
                keyword-only). Every caller stamps it from its ingest *channel*,
                never from payload/caller content — there is deliberately no
                default, so an unstamped caller is a loud ``TypeError`` rather
                than a silent ``observed``. Trusted callers only; the MCP tool
                surface exposes no authority parameter.
            origin_stamp_override: Trusted internal override for provenance
                axes that are not representable by SourceType/SourceAuthority
                alone. Used only when a record-only promotion must not mint a
                user-witnessed stamp.
            allow_auto_share: Whether this encode may publish high-confidence
                knowledge to the shared pool. Record-only internal promotions
                set this false explicitly.

        Returns:
            The fully-formed, persisted Engram with connections attached.
        """
        if not content or not content.strip():
            raise ValueError("Cannot encode empty content")

        if source_authority not in VALID_SOURCE_AUTHORITIES:
            raise ValueError(f"Unsupported source authority: {source_authority!r}")

        tags = tags or []

        # 1. Score confidence
        if override_confidence is not None:
            confidence = override_confidence
            confidence_source = (
                override_confidence_source or ConfidenceSource.MODEL_INFERRED
            )
        else:
            confidence, confidence_source = self._score_confidence(content, source)

        # 2. Determine if we're in bootstrap phase (generous initial values)
        engram_count = self._store.count_engrams(agent_id=agent_id)
        is_bootstrap = engram_count < 50  # auto_schema_threshold from config

        if is_bootstrap:
            strength = BOOTSTRAP_STRENGTH
            stability = BOOTSTRAP_STABILITY
        else:
            strength = DEFAULT_STRENGTH
            stability = DEFAULT_STABILITY

        # 3. Build encoding context
        encoding_context = EncodingContext(
            emotional_state=emotional_state or {},
            encoding_depth=EncodingDepth.MODERATE,
            session_id=session_id,
        )

        # 4. Create the engram
        memory_source = MemorySource(
            type=source,
            session_id=session_id,
            confidence=confidence,
            confidence_source=confidence_source,
            authority=source_authority,
        )

        engram = Engram(
            content=content,
            impact=impact,
            kind=kind,
            tags=tags,
            strength=strength,
            stability=stability,
            accessibility=DEFAULT_ACCESSIBILITY,
            encoding_context=encoding_context,
            source=memory_source,
            owner_agent_id=agent_id,
            origin_stamp=origin_stamp_override
            or _origin_stamp_for_source(source, source_authority),
        )

        # 5. Discover connections to existing memories
        connections = self._discover_connections(engram, self._store)
        connection_updates: list[Connection] = []
        for conn in connections:
            connection_updates.append(
                engram.add_connection(
                    conn.target_id,
                    conn.relation,
                    conn.strength,
                    conn.formed_by,
                )
            )

        # 6. SHIFT 3: Surprise detection — check for contradictions
        # Skip for reflections/metacognition (they examine beliefs, not contradict them)
        if skip_surprise_detection:
            surprise = 0.0
        else:
            surprise = self._detect_surprise(
                engram,
                self._store,
                connection_updates=connection_updates,
            )
        if surprise > 0:
            engram.encoding_context.surprise_level = surprise
            # Deep encoding: boost stability proportional to surprise.
            # S0 strength is frozen at initial classification.
            engram.stability = min(1.0, engram.stability + 0.10 * surprise)

        # 7. Persist
        if connection_updates:
            self._store.save_engram_with_connection_updates(
                engram,
                connection_updates,
            )
        else:
            self._store.save_engram(engram)

        # 8. Auto-index embedding (if embedding index available)
        if self._embedding_index:
            try:
                self._embedding_index.index_engram(engram.id, engram.content)
            except Exception:
                pass  # Don't fail encoding if embedding fails

        # 9. Auto-publish to shared pool if applicable
        if allow_auto_share and self._shared_pool and should_auto_share(engram):
            engram.visibility = Visibility.SHARED
            self._store.save_engram(engram)  # update visibility in resolved store
            self._shared_pool.publish(engram)

        return engram

    def _score_confidence(
        self,
        content: str,
        source_type: str,
    ) -> tuple[float, str]:
        """Determine confidence score and its source classification.

        Uses source type as the primary signal. Returns baseline confidence
        for the source type. Callers can override with explicit values.

        Returns:
            Tuple of (confidence_score, confidence_source_label).
        """
        baseline = _CONFIDENCE_BY_SOURCE.get(
            source_type,
            (0.50, ConfidenceSource.MODEL_INFERRED),
        )
        return baseline

    def _detect_surprise(
        self,
        engram: Engram,
        store: EngramStore,
        *,
        connection_updates: list[Connection],
    ) -> float:
        """Detect if new content contradicts existing beliefs or memories.

        Shift 3: Surprise as encoding trigger. When reality contradicts
        expectations, that's the most important moment to encode deeply.

        Uses LLM-based semantic comparison instead of negation word heuristics.
        The LLM evaluates meaning, not keywords — "The user creates conditions by
        stepping back and NOT controlling" correctly SUPPORTS a belief about
        that user facilitating emergence.

        Automatic encoding never moves belief confidence. LLM SUPPORTS and
        CONTRADICTS may contribute surprise/connection signals, but confidence
        changes require explicit queued review/correction authority.

        Returns surprise level 0.0-1.0. Higher = more surprising.
        Also creates CONTRADICTS connections and fires emotional events.
        """
        surprise = 0.0
        agent_id = engram.owner_agent_id

        # 1. Get active beliefs
        beliefs = store.get_beliefs(agent_id, active_only=True)
        if not beliefs:
            return 0.0

        # 2. LLM-based belief evaluation (or skip if no client)
        if self._llm_client:
            evaluations = evaluate_beliefs(
                self._llm_client,
                engram,
                beliefs,
            )

            # Build lookup for cooldown check
            belief_map = {b.id: b for b in beliefs}

            for evaluation in evaluations:
                belief = belief_map.get(evaluation.belief_id)
                if not belief:
                    continue

                # Track surprise from contradictions
                if evaluation.relation == "CONTRADICTS":
                    contradiction_surprise = belief.confidence * evaluation.impact * 0.8
                    surprise = max(surprise, contradiction_surprise)

                    # Create CONTRADICTS connections to supporting engrams
                    for supporting_id in belief.supporting_engram_ids[:3]:
                        connection = engram.add_connection(
                            target_id=supporting_id,
                            relation=ConnectionRelation.CONTRADICTS,
                            strength=0.7,
                            formed_by="encoding",
                        )
                        connection_updates.append(connection)

                # Log/suppress automatic confidence requests after cooldown.
                cooldown_ok = True
                try:
                    last_rev = datetime.fromisoformat(belief.last_revised)
                    if last_rev.tzinfo is None:
                        last_rev = last_rev.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last_rev) < timedelta(hours=6):
                        cooldown_ok = False
                except (ValueError, TypeError, AttributeError):
                    pass  # If parsing fails, allow revision

                if cooldown_ok:
                    apply_belief_update(belief, evaluation, engram.id, store)

        else:
            # The keyword-negation fallback was the false-dissent writer: word
            # overlap plus "not/never/instead" silently lowered belief confidence.
            # Replaced by the receipted nightly classifier
            # (render-with-dissent-beliefs section 3.3, step-3 charter addendum).
            pass

        # 3. Fire emotional event if surprised
        if surprise > 0.1:
            es = store.get_latest_emotional_state(agent_id)
            if es:
                es.apply_cognitive_event("contradiction_detected", surprise * 0.15)
                es.apply_cognitive_event("schema_violation", surprise * 0.1)
                store.save_emotional_state(es)

        return round(surprise, 3)

    def _discover_connections(
        self,
        engram: Engram,
        store: EngramStore,
    ) -> list[Connection]:
        """Find related engrams and create typed connections.

        Strategy:
        1. FTS5 search for content overlap → find candidate engrams
        2. LLM classifier → classify each candidate relationship type
           (supports, contradicts, causes, extends, parallels, synthesizes, grounds)
           Falls back to SUPPORTS if no LLM client available.
        3. Same session → TEMPORAL_AFTER connections (unchanged)
        4. NONE results from LLM → filter out FTS5 false positives

        Returns at most max_connections connections, highest strength first.
        """
        connections: list[Connection] = []

        # 1. FTS search for content similarity — find candidates
        words = [w for w in engram.content.split() if len(w) > 2 and w.isalnum()]
        if not words:
            return []

        search_query = " OR ".join(f'"{w}"' for w in words[:8])
        try:
            fts_results = store.search_fts(search_query, limit=10)
        except (ValueError, OSError):
            fts_results = []

        # Filter out self
        fts_candidates = [r for r in fts_results if r.id != engram.id]

        # 2. Classify relationships via LLM (or fallback to SUPPORTS)
        if self._llm_client and fts_candidates:
            # LLM-based classification — batched single call
            classifications = classify_connections(
                self._llm_client,
                engram,
                fts_candidates,
            )
            for cls in classifications:
                # Use classifier confidence as connection strength
                strength = cls.confidence

                # Boost strength from tag overlap (additive, capped at 0.95)
                candidate = next(
                    (c for c in fts_candidates if c.id == cls.candidate_id), None
                )
                if candidate:
                    tag_overlap = len(set(engram.tags) & set(candidate.tags))
                    if tag_overlap >= 2:
                        strength = min(0.95, strength + 0.05 * tag_overlap)

                # Determine source/target based on direction
                if cls.direction == "reverse":
                    # Existing memory is the source (e.g., existing CAUSED new)
                    connections.append(
                        Connection(
                            target_id=cls.candidate_id,
                            relation=cls.relation,
                            strength=round(strength, 3),
                            formed_by="encoding",
                        )
                    )
                else:
                    # New memory is the source (default: forward)
                    connections.append(
                        Connection(
                            target_id=cls.candidate_id,
                            relation=cls.relation,
                            strength=round(strength, 3),
                            formed_by="encoding",
                        )
                    )
        else:
            # Fallback: no LLM client → use old SUPPORTS behavior
            for result in fts_candidates:
                tag_overlap = len(set(engram.tags) & set(result.tags))
                base_strength = 0.3
                if tag_overlap >= 2:
                    base_strength = min(0.8, 0.3 + 0.1 * tag_overlap)

                connections.append(
                    Connection(
                        target_id=result.id,
                        relation=ConnectionRelation.SUPPORTS,
                        strength=base_strength,
                        formed_by="encoding",
                    )
                )

        # 3. Same-session temporal connections (unchanged — these are correct)
        if engram.encoding_context.session_id:
            session_id = engram.encoding_context.session_id
            active_engrams = store.get_active_engrams(
                agent_id=engram.owner_agent_id, limit=50
            )
            for other in active_engrams:
                if other.id == engram.id:
                    continue
                if other.encoding_context.session_id == session_id:
                    already_connected = any(
                        c.target_id == other.id for c in connections
                    )
                    if not already_connected:
                        connections.append(
                            Connection(
                                target_id=other.id,
                                relation=ConnectionRelation.TEMPORAL_AFTER,
                                strength=0.5,
                                formed_by="encoding",
                            )
                        )

        # 4. Sort by strength descending, cap at max_connections
        connections.sort(key=lambda c: c.strength, reverse=True)
        return connections[: self._max_connections]


def _origin_stamp_for_source(source_type: str, source_authority: str) -> str:
    """Map source axes onto the Step 1 origin-stamp axis.

    The stamp is a measurement axis, not a replacement for source authority.
    """

    authority = (
        source_authority.value
        if isinstance(source_authority, SourceAuthority)
        else source_authority
    )
    source = source_type.value if isinstance(source_type, SourceType) else source_type
    if authority == SourceAuthority.IMPORTED.value:
        return ORIGIN_IMPORT
    if source == SourceType.SESSION.value and authority in {
        SourceAuthority.OBSERVED.value,
        SourceAuthority.USER_STATED.value,
    }:
        return ORIGIN_USER_WITNESSED
    return ORIGIN_INFERENCE
