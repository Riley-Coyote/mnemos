"""
Core encoding pipeline for Mnemos.

Transforms raw content (text from sessions, reflections, observations) into
fully-formed Engrams with confidence scoring, encoding context, and discovered
connections to related memories.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from ..core.engram import Connection, Engram, EncodingContext, MemorySource
from ..store.fts import distinctive_terms, fts_words, or_query, overlap
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
    SourceType,
    Visibility,
)
from .llm_classifier import (
    classify_connections,
    evaluate_beliefs,
    apply_belief_update,
)

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
_AUTO_SHARE_TAGS = frozenset({
    "task-completion", "decision", "summary", "error", "discovery",
    "deployment", "architecture", "lesson", "distilled",
})

# Tags that force engrams to stay private
_PRIVATE_TAGS = frozenset({
    "internal", "emotional", "working-memory", "reflection", "thinking",
})

# Source types that are internal processing and should stay private
_PRIVATE_SOURCES = frozenset({SourceType.DREAM, SourceType.REFLECTION})


def should_auto_share(engram: Engram) -> bool:
    """Determine whether an engram should be auto-published to the shared pool.

    Auto-share: task completions, decisions, summaries, errors, discoveries,
    lessons, and high-confidence semantic/procedural knowledge.

    Keep private: internal reasoning, emotional state, working memory,
    reflections, and dream-sourced content.
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
        )
    """

    def __init__(
        self,
        store: EngramStore,
        max_connections: int = 5,
        embedding_index: Any | None = None,
        llm_client: LLMClient | None = None,
        shared_pool: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._max_connections = max_connections
        self._embedding_index = embedding_index
        self._llm_client = llm_client
        self._shared_pool = shared_pool
        # The "encoding" section of the config. keyword_overlap is how much of
        # what two memories are about they must share before saving one links it
        # to the other without a model (see _discover_connections).
        config = config if isinstance(config, dict) else {}
        self._keyword_overlap = config.get("keyword_overlap", 0.2)

    def encode(
        self,
        content: str,
        impact: str = "",
        kind: str = EngramKind.EPISODIC,
        tags: list[str] | None = None,
        source: str = SourceType.SESSION,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        emotional_state: dict[str, float] | None = None,
        override_confidence: float | None = None,
        override_confidence_source: str | None = None,
        skip_surprise_detection: bool = False,
        impact_source: str = "",
        discover_connections: bool = True,
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
            person_id: Which person this memory concerns.
            project_scope: Which project boundary owns this memory.
            emotional_state: Current emotional state dict (6 dimensions).
            override_confidence: If set, use this confidence score instead of auto-scoring.
            override_confidence_source: If set, use this confidence source label.
            discover_connections: Link the new memory to existing ones as it is
                saved. False saves it with its own shape only (classification,
                full-text index, vector) and no links; code older than the store
                saves this way, and maintenance's connection discovery links it
                later.

        Returns:
            The fully-formed, persisted Engram with connections attached.
        """
        if not content or not content.strip():
            raise ValueError("Cannot encode empty content")

        tags = tags or []

        # 1. Score confidence
        if override_confidence is not None:
            confidence = override_confidence
            confidence_source = override_confidence_source or ConfidenceSource.MODEL_INFERRED
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
        )

        engram = Engram(
            content=content,
            impact=impact,
            impact_source=impact_source if impact else "",
            kind=kind,
            tags=tags,
            strength=strength,
            stability=stability,
            accessibility=DEFAULT_ACCESSIBILITY,
            encoding_context=encoding_context,
            source=memory_source,
            owner_agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )

        # 5. Discover connections to existing memories
        connections = (
            self._discover_connections(engram, self._store) if discover_connections else []
        )
        for conn in connections:
            engram.add_connection(
                conn.target_id, conn.relation, conn.strength, conn.formed_by
            )

        # 6. SHIFT 3: Surprise detection — check for contradictions
        # Skip for reflections/metacognition (they examine beliefs, not contradict them)
        if skip_surprise_detection:
            surprise = 0.0
        else:
            surprise = self._detect_surprise(engram, self._store)
        if surprise > 0:
            engram.encoding_context.surprise_level = surprise
            # Deep encoding: boost strength and stability proportional to surprise
            engram.strength = min(1.0, engram.strength + 0.15 * surprise)
            engram.stability = min(1.0, engram.stability + 0.10 * surprise)

        # 7. Persist
        self._store.save_engram(engram)

        # 8. Auto-index embedding (if embedding index available)
        if self._embedding_index:
            try:
                self._embedding_index.index_engram(engram.id, engram.content)
            except Exception:
                pass  # Don't fail encoding if embedding fails

        # 9. Auto-publish to shared pool if applicable
        if self._shared_pool and should_auto_share(engram):
            engram.visibility = Visibility.SHARED
            self._store.save_engram(engram)  # update visibility in private DB
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

    # A store with almost nothing in it has no expectations to violate, so
    # novelty there is meaningless — the first memories are not surprising,
    # they are simply first.
    _NOVELTY_MIN_STORE = 5

    @staticmethod
    def _terms(text: str) -> set[str]:
        import re

        return {
            t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 4
        }

    def _structural_novelty(self, engram: Any, store: Any, agent_id: str) -> float:
        """How unlike everything already remembered this is, from 0.0 to 1.0.

        Model-free and belief-free by construction: overlap against the
        nearest existing memories. Being wrong is where understanding
        reorganises, and the cheapest available signal for "this does not
        fit what I already hold" is that nothing already held resembles it.
        """
        mine = self._terms(engram.content)
        if not mine:
            return 0.0

        try:
            if store.count_engrams(agent_id=agent_id) < self._NOVELTY_MIN_STORE:
                return 0.0
            # FTS5 ANDs the terms of a bare query, so passing whole content
            # matches only near-identical text and every memory looks novel.
            # Neighbours are anything sharing *any* salient term.
            query = " OR ".join(sorted(mine)[:12])
            neighbours = store.search_fts(
                query, limit=12, agent_id=engram.owner_agent_id,
                person_id=engram.person_id, project_scope=engram.project_scope,
            )
        except Exception:
            return 0.0

        best = 0.0
        for other in neighbours:
            if other.id == engram.id:
                continue
            theirs = self._terms(other.content)
            if not theirs:
                continue
            # Overlap coefficient rather than Jaccard: "the same thing said
            # at greater length" should read as familiar, and dividing by the
            # union punishes it for the extra words alone.
            overlap = len(mine & theirs) / min(len(mine), len(theirs))
            best = max(best, overlap)

        # No lexical neighbour at all is the strongest signal available, but
        # it is still weaker evidence than a belief being contradicted, so it
        # is deliberately capped below the contradiction range.
        return round(min(0.6, 1.0 - best), 3)

    def _detect_surprise(
        self,
        engram: Engram,
        store: EngramStore,
    ) -> float:
        """Detect if new content contradicts existing beliefs or memories.

        Shift 3: Surprise as encoding trigger. When reality contradicts
        expectations, that's the most important moment to encode deeply.

        Uses LLM-based semantic comparison instead of negation word heuristics.
        The LLM evaluates meaning, not keywords — "The user creates conditions by
        stepping back and NOT controlling" correctly SUPPORTS a belief about
        that user facilitating emergence.

        Belief updates use asymmetric impact:
        - Supports: +impact * 0.07 (beliefs grow from genuine evidence)
        - Contradicts: -impact * 0.04 (harder to erode through noise)
        - Confidence clamped to [0.05, 0.95] — never fully dies, never unquestionable

        Returns surprise level 0.0-1.0. Higher = more surprising.
        Also creates CONTRADICTS connections and fires emotional events.
        """
        surprise = 0.0
        agent_id = engram.owner_agent_id

        # Shift 3 says surprise is the primary encoding trigger, but it was
        # unreachable: surprise needed beliefs, beliefs were only created by
        # the LLM-gated belief-review pass, and that pass never ran without a
        # provider. So on the install Mnemos ships, surprise always returned
        # 0.0 — which is why every call site passed skip_surprise_detection.
        #
        # Novelty breaks the cycle. Something unlike anything already
        # remembered is genuinely surprising, and measuring that needs no
        # beliefs and no model.
        surprise = self._structural_novelty(engram, store, agent_id)

        # 1. Get active beliefs
        beliefs = store.get_beliefs(agent_id, active_only=True)
        if not beliefs:
            return surprise

        # 2. LLM-based belief evaluation (or skip if no client)
        if self._llm_client:
            evaluations = evaluate_beliefs(
                self._llm_client, engram, beliefs,
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
                        engram.add_connection(
                            target_id=supporting_id,
                            relation=ConnectionRelation.CONTRADICTS,
                            strength=0.7,
                            formed_by="encoding",
                        )

                # Apply belief update (with cooldown guard)
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

        # Without a model nothing here weighs the memory against beliefs. The
        # keyword-and-negation check that stood in lowered a belief by 0.05 and
        # linked the memory as contradicting the belief's evidence whenever
        # the two shared any word of four or more characters and the memory
        # held a negation anywhere: "not" also matched "note", and "instead"
        # or "no longer" said nothing about the belief. Whether a memory
        # contradicts what is held is asked of the agent (mnemos_reflect), and
        # a belief changes only by its verdict.

        # 3. Fire emotional event if surprised
        if surprise > 0.1:
            from ..core.emotional_state import EmotionalState
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
        1. FTS5 search for the memory's distinctive words → find candidate engrams
        2. LLM classifier → classify each candidate relationship type
           (supports, contradicts, causes, extends, parallels, synthesizes, grounds)
           Without an LLM client, a candidate that shares enough distinctive
           words becomes CO_ACTIVATED.
        3. Same session → TEMPORAL_AFTER connections (unchanged)
        4. NONE results from LLM → filter out FTS5 false positives

        Returns at most max_connections connections, highest strength first.
        """
        connections: list[Connection] = []

        # 1. FTS search for content similarity — find candidates
        words = fts_words(engram.content)
        if not words:
            return []

        # Search by what the memory is about: its distinctive words (four
        # letters or more, without common ones). The search used its first
        # eight words, which included "for", "with" and "every" and so matched
        # nearly everything. The classifier gets better candidates from this
        # too. A memory made only of common words has nothing to search for;
        # its same-session links below still form.
        mine = distinctive_terms(engram.content)
        about = [w for w in words if w.lower() in mine]
        fts_results = []
        if about:
            try:
                fts_results = store.search_fts(
                    or_query(about[:8]), limit=10, agent_id=engram.owner_agent_id,
                    person_id=engram.person_id, project_scope=engram.project_scope,
                )
            except (ValueError, OSError):
                fts_results = []

        # Filter out self
        fts_candidates = [r for r in fts_results if r.id != engram.id]

        # 2. Classify relationships via LLM (or, without one, keyword overlap)
        if self._llm_client and fts_candidates:
            # LLM-based classification — batched single call
            classifications = classify_connections(
                self._llm_client, engram, fts_candidates,
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
            # Without a model this is keyword overlap, which is correlation.
            # Calling it SUPPORTS asserts that one memory independently
            # reinforces another's conclusion — a claim nothing established.
            # It is also the bulk of the relation-type monoculture that
            # hollows out Shift 4: spreading activation through *typed*
            # connections means little when 96% of edges are one type, and
            # reactive.py weights SUPPORTS at 1.0 against CO_ACTIVATED's 0.6,
            # so a mislabel pulls retrieval toward mere shared vocabulary at
            # full evidence weight.
            #
            # The same fix landed for connection_discovery in #4; this is the
            # other, larger source. formed_by distinguishes these so a later
            # pass can reclassify or strip them.
            #
            # A link also says the two belong together, and shared words only
            # suggest that when there are enough of them. So a candidate must
            # share at least two distinctive words (one is incidental), making
            # up at least keyword_overlap (0.2) of the smaller memory's. When the
            # smaller has ten or fewer, that is simply two shared words, and a
            # new memory is often short: "Rule for visits: every invitation to a
            # resident comes with a real way to decline." shares two of its
            # seven with the rule it restates, 0.29, which maintenance's bar of
            # 0.3 (#71) would drop. Replaying a real store's saves, this cut the
            # links saving made from 775 to 260, and after a simulated week
            # recall found as much as before (see the PR).
            for result in fts_candidates:
                theirs = distinctive_terms(result.content)
                if len(mine & theirs) < 2 or overlap(mine, theirs) < self._keyword_overlap:
                    continue
                tag_overlap = len(set(engram.tags) & set(result.tags))
                base_strength = 0.3
                if tag_overlap >= 2:
                    base_strength = min(0.8, 0.3 + 0.1 * tag_overlap)

                connections.append(
                    Connection(
                        target_id=result.id,
                        relation=ConnectionRelation.CO_ACTIVATED,
                        strength=base_strength,
                        formed_by="encoding_no_llm",
                    )
                )

        # 3. Same-session temporal connections (unchanged — these are correct)
        if engram.encoding_context.session_id:
            session_id = engram.encoding_context.session_id
            active_engrams = store.get_active_engrams(
                agent_id=engram.owner_agent_id, person_id=engram.person_id,
                project_scope=engram.project_scope, limit=50,
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


# ── What the removed no-model check wrote ──
#
# Without a model, encoding once weighed a memory against beliefs by keyword
# and negation (removed; see _detect_surprise). `mnemos repair
# keyword-contradictions` finds what it wrote by its signatures, and only by
# them.
#
# Its revisions lowered the belief by 0.05 (to no less than 0), with a reason
# of this prefix and the memory's first 50 characters. The model path writes
# "Contradicted by new evidence (impact 0.60): <its reasoning>" or "Supported
# by new evidence (impact ...)", so the colon right after "evidence" tells
# them apart.
KEYWORD_CONTRADICTION_REASON = "Contradicted by new evidence: "
KEYWORD_CONTRADICTION_STEP = 0.05
MODEL_BELIEF_REASONS = (
    "Contradicted by new evidence (impact ",
    "Supported by new evidence (impact ",
)
# Its links: CONTRADICTS, formed at encoding, strength 0.7, from the memory to
# the first three memories each belief it held rested on.
KEYWORD_CONTRADICTION_STRENGTH = 0.7
# The revision a repair appends. Revisions of the check before it are undone.
KEYWORD_CONTRADICTIONS_RESTORED = "Restored by mnemos repair keyword-contradictions: "


def _moment(timestamp: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(timestamp or "")
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def find_keyword_contradictions(store: Any, agent_id: str) -> dict[str, Any]:
    """What the removed no-model keyword-and-negation check wrote for an agent.

    Reads only. Beliefs belong to the agent, so every scope's memories count.

    Links. The model path writes links of exactly the check's shape, so shape
    alone decides nothing. A link counts as the check's only when its memory
    shows it was saved without a model: it has a link formed at encoding by
    keyword overlap ('encoding_no_llm', which encoding with a model never
    writes), or it triggered one of the check's revisions. One whose memory
    triggered a model-path revision is the model's. Any other is ambiguous.
    The model's and the ambiguous are only reported. So is every other
    CONTRADICTS link, which lacks the shape: another strength, or not to the
    memory a belief rested on when the link formed.

    Revisions. Only those after the last restoration a repair appended count,
    so a repaired belief is found clean. A revision with the check's reason
    that did not lower the belief by exactly its step is ambiguous and left.
    A retired belief is left as it is: its confidence no longer shapes
    anything, and the agent may have set it on purpose.
    """
    beliefs = store.get_beliefs(agent_id, active_only=False)
    conn = store._get_conn()

    keyword_triggers: set[str] = set()
    model_triggers: set[str] = set()
    for belief in beliefs:
        for revision in belief.revision_history:
            if revision.reason.startswith(KEYWORD_CONTRADICTION_REASON):
                keyword_triggers.add(revision.trigger_engram_id or "")
            elif revision.reason.startswith(MODEL_BELIEF_REASONS):
                model_triggers.add(revision.trigger_engram_id or "")

    rested_on: dict[str, list[Any]] = {}
    for belief in beliefs:
        for engram_id in belief.supporting_engram_ids[:3]:
            rested_on.setdefault(engram_id, []).append(belief)

    no_model = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT c.source_id FROM connections c "
            "JOIN engrams e ON e.id = c.source_id "
            "WHERE c.formed_by = 'encoding_no_llm' AND e.owner_agent_id = ?",
            (agent_id,),
        ).fetchall()
    }

    links: list[dict[str, Any]] = []
    model_links: list[dict[str, Any]] = []
    ambiguous_links: list[dict[str, Any]] = []
    other_links = 0
    for row in conn.execute(
        "SELECT c.source_id, c.target_id, c.strength, c.formed_at FROM connections c "
        "JOIN engrams e ON e.id = c.source_id "
        "WHERE c.relation = 'contradicts' AND c.formed_by = 'encoding' "
        "AND e.owner_agent_id = ? ORDER BY c.formed_at, c.source_id, c.target_id",
        (agent_id,),
    ).fetchall():
        source_id, target_id, strength, formed_at = tuple(row)
        formed = _moment(formed_at)
        held = [
            belief for belief in rested_on.get(target_id, [])
            if formed is not None
            and (_moment(belief.created_at) or formed) <= formed
        ]
        if abs(float(strength) - KEYWORD_CONTRADICTION_STRENGTH) > 1e-6 or not held:
            other_links += 1
            continue
        link = {
            "source_id": source_id,
            "target_id": target_id,
            "formed_at": formed_at,
            "belief_ids": [belief.id for belief in held],
        }
        link["no_model_link"] = source_id in no_model
        link["revision"] = source_id in keyword_triggers
        if source_id in model_triggers:
            model_links.append(link)
        elif link["no_model_link"] or link["revision"]:
            links.append(link)
        else:
            ambiguous_links.append(link)

    restored: list[dict[str, Any]] = []
    retired_revisions = 0
    ambiguous_revisions = 0
    for belief in beliefs:
        history = belief.revision_history
        start = max(
            (
                i + 1 for i, revision in enumerate(history)
                if revision.reason.startswith(KEYWORD_CONTRADICTIONS_RESTORED)
            ),
            default=0,
        )
        found = []
        for revision in history[start:]:
            if not revision.reason.startswith(KEYWORD_CONTRADICTION_REASON):
                continue
            expected = max(0.0, revision.old_confidence - KEYWORD_CONTRADICTION_STEP)
            if abs(revision.new_confidence - expected) > 1e-6:
                ambiguous_revisions += 1
                continue
            found.append(revision)
        if not found:
            continue
        if belief.superseded_by:
            retired_revisions += len(found)
            continue
        lowered = sum(r.old_confidence - r.new_confidence for r in found)
        restored.append({
            "belief_id": belief.id,
            "content": belief.content,
            "revisions": len(found),
            "lowered": lowered,
            "before": belief.confidence,
            "after": round(min(0.99, max(0.0, belief.confidence + lowered)), 6),
        })

    return {
        "links": links,
        "model_links": model_links,
        "ambiguous_links": ambiguous_links,
        "other_links": other_links,
        "beliefs": restored,
        "revisions": sum(item["revisions"] for item in restored),
        "retired_revisions": retired_revisions,
        "ambiguous_revisions": ambiguous_revisions,
    }
