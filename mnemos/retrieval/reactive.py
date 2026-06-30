"""
Core retrieval for Mnemos — resonance-based, not search-based.

Shift 4: Instead of a weighted scoring formula, retrieval works through
spreading activation in the connection graph. FTS finds seed nodes,
activation propagates through connections weighted by relation type,
and what lights up after N hops is what's relevant.

The graph structure IS the relevance model. No formula needed.

Pipeline:
1. Read-visibility filter → FTS search seed nodes
2. Spreading activation through visible connection graph (3 hops)
3. Emotional bias applied multiplicatively
4. Threshold → return activated engrams
5. Reconsolidation on all returned visible engrams
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.engram import Engram
from ..core.emotional_state import EmotionalState
from ..core.types import ConnectionRelation
from .reconsolidation import reconsolidate

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


@dataclass
class RetrievalResult:
    """A scored retrieval result wrapping an engram."""

    engram: Engram
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    retrieval_path: str = "fts"


# Activation weights by connection relation type
_RELATION_WEIGHTS: dict[str, float] = {
    ConnectionRelation.SUPPORTS: 1.0,
    ConnectionRelation.ELABORATES: 1.0,
    ConnectionRelation.CAUSES: 0.9,
    ConnectionRelation.DISTILLED_INTO: 0.9,
    ConnectionRelation.PART_OF: 0.9,
    ConnectionRelation.INSTANCE_OF: 0.9,
    ConnectionRelation.ANALOGOUS_TO: 0.8,
    ConnectionRelation.TEMPORAL_BEFORE: 0.4,
    ConnectionRelation.TEMPORAL_AFTER: 0.4,
    ConnectionRelation.CONTRADICTS: 0.5,  # Still propagate — contradictions are relevant
    ConnectionRelation.INTERFERES_WITH: 0.3,
    ConnectionRelation.CO_ACTIVATED: 0.6,  # Correlation, weaker than evidence relations
}


class ReactiveRetriever:
    """Resonance-based memory retrieval.

    Instead of scoring candidates with a weighted formula, retrieval
    works through spreading activation in the connection graph. FTS
    finds visible seed nodes, activation spreads through typed connections
    whose targets are visible to the requested read surface, and what lights up
    is what's relevant.

    Usage:
        retriever = ReactiveRetriever(store)
        results = retriever.retrieve("What does the user think about dark mode?")
    """

    def __init__(
        self,
        store: EngramStore,
        embedding_index: Any | None = None,
        shared_store: Any | None = None,
        activation_depth: int = 3,
        activation_decay: float = 0.5,
        activation_threshold: float = 0.1,
        reconsolidation_enabled: bool = True,
        confidence_floor: float = 0.3,
    ) -> None:
        self._store = store
        self._embedding_index = embedding_index
        self._shared_store = shared_store
        self._depth = activation_depth
        self._decay = activation_decay
        self._threshold = activation_threshold
        self._reconsolidation_enabled = reconsolidation_enabled
        self._confidence_floor = confidence_floor

    def retrieve(
        self,
        cue: str,
        agent_id: str = "default",
        max_results: int = 10,
        emotional_state: EmotionalState | None = None,
        read_visibility: str | None = "operational_context",
    ) -> list[RetrievalResult]:
        """Retrieve memories via resonance through the visible graph.

        Pipeline:
        1. FTS search with read visibility → seed nodes
        2. Spreading activation through visible targets
        3. Emotional bias (multiplicative boost for congruent tags)
        4. Filter by threshold + confidence floor
        5. Reconsolidate returned engrams

        ``read_visibility`` defaults to ``operational_context``. Pass
        ``review_only`` only for explicit review surfaces; pass ``None`` only
        for audit/admin scans that intentionally include all visibilities.

        Returns:
            List of RetrievalResult sorted by activation level (descending).
        """
        if not cue or not cue.strip():
            return []

        # 1. SEED: Find entry points via FTS + embeddings
        seeds: dict[str, Engram] = {}

        # FTS seeds (keyword matching)
        fts_query = _to_fts_query(cue)
        fts_results = self._store.search_fts(
            fts_query,
            limit=30,
            read_visibility=read_visibility,
        )
        for engram in fts_results:
            if engram.owner_agent_id == agent_id:
                seeds[engram.id] = engram

        # Shared DB seeds (cross-agent shared memories)
        if self._shared_store:
            try:
                shared_fts = self._shared_store.search_fts(
                    fts_query,
                    limit=20,
                    read_visibility=read_visibility,
                )
                for engram in shared_fts:
                    if engram.visibility in ("shared", "public") and engram.id not in seeds:
                        seeds[engram.id] = engram
            except Exception:
                pass  # Shared store is optional

        # Embedding seeds (meaning matching — finds what FTS misses)
        if self._embedding_index and hasattr(self._embedding_index, 'search'):
            try:
                embedding_hits = self._embedding_index.search(
                    cue, k=20, exclude_ids=set(seeds.keys())
                )
                for eid, similarity in embedding_hits:
                    if similarity > 0.3 and eid not in seeds:  # Threshold for relevance
                        engram = self._store.get_engram(
                            eid,
                            read_visibility=read_visibility,
                        )
                        if engram and engram.state == "active" and engram.owner_agent_id == agent_id:
                            seeds[eid] = engram
            except Exception:
                pass  # Embeddings are optional — FTS still works

        if not seeds:
            return []

        # 2. PROPAGATE: Spreading activation through connection graph
        activation: dict[str, float] = {}
        visible_engrams: dict[str, Engram] = dict(seeds)
        hidden_engram_ids: set[str] = set()

        def visible_engram(eid: str) -> Engram | None:
            if eid in visible_engrams:
                return visible_engrams[eid]
            if eid in hidden_engram_ids:
                return None
            engram = self._load_visible_engram(eid, agent_id, read_visibility)
            if engram is None:
                hidden_engram_ids.add(eid)
                return None
            visible_engrams[eid] = engram
            return engram

        # Seeds start at activation 1.0
        for seed_id in seeds:
            activation[seed_id] = 1.0

        # Spread through connections
        for hop in range(1, self._depth + 1):
            hop_decay = self._decay ** hop
            new_activation: dict[str, float] = defaultdict(float)

            for engram_id, current_act in list(activation.items()):
                if current_act < self._threshold:
                    continue
                if visible_engram(engram_id) is None:
                    continue

                connections = self._store.get_connections(engram_id)
                # Cross-DB connections: also check shared store
                if self._shared_store:
                    try:
                        connections = connections + self._shared_store.get_connections(engram_id)
                    except Exception:
                        pass
                for conn in connections:
                    # Weight by relation type
                    relation_weight = _RELATION_WEIGHTS.get(conn.relation, 0.5)
                    propagated = current_act * hop_decay * conn.strength * relation_weight

                    if propagated > self._threshold * 0.5:
                        target = visible_engram(conn.target_id)
                        if target is None:
                            continue
                        new_activation[conn.target_id] += propagated

            # Merge new activations (additive — multiple paths reinforce)
            for eid, act in new_activation.items():
                activation[eid] = activation.get(eid, 0.0) + act

        # 3. EMOTIONAL BIAS: multiplicative boost for congruent engrams
        if emotional_state:
            bias = emotional_state.get_retrieval_bias()
            if bias:
                for eid in list(activation.keys()):
                    engram = visible_engram(eid)
                    if engram and engram.tags:
                        overlap = sum(bias.get(tag, 0.0) for tag in engram.tags)
                        if overlap > 0:
                            activation[eid] *= (1.0 + min(0.5, overlap))

        # 4. FILTER + LOAD: threshold, confidence floor, build results
        results: list[RetrievalResult] = []
        for eid, act_level in activation.items():
            if act_level < self._threshold:
                continue

            engram = visible_engram(eid)
            if not engram:
                continue

            if engram.source.confidence < self._confidence_floor:
                continue

            path = "fts" if eid in seeds else "resonance"
            results.append(
                RetrievalResult(
                    engram=engram,
                    score=round(act_level, 4),
                    score_breakdown={
                        "activation": round(act_level, 4),
                        "is_seed": eid in seeds,
                    },
                    retrieval_path=path,
                )
            )

        # Sort by activation level
        results.sort(key=lambda r: r.score, reverse=True)
        top_results = results[:max_results]

        # 5. RECONSOLIDATE returned engrams
        if self._reconsolidation_enabled and top_results:
            co_retrieved_ids = [r.engram.id for r in top_results]
            for result in top_results:
                # Reconsolidate in the engram's home store
                target_store = self._store
                if (
                    result.engram.owner_agent_id != agent_id
                    and self._shared_store
                ):
                    target_store = self._shared_store
                result.engram = reconsolidate(
                    engram=result.engram,
                    current_context=cue,
                    co_retrieved_ids=[
                        eid for eid in co_retrieved_ids if eid != result.engram.id
                    ],
                    store=target_store,
                )

        return top_results

    def _load_visible_engram(
        self,
        eid: str,
        agent_id: str,
        read_visibility: str | None,
    ) -> Engram | None:
        engram = self._store.get_engram(eid, read_visibility=read_visibility)
        if engram and self._can_retrieve(engram, agent_id):
            return engram
        if self._shared_store:
            try:
                engram = self._shared_store.get_engram(
                    eid,
                    read_visibility=read_visibility,
                )
            except Exception:
                engram = None
            if engram and self._can_retrieve(engram, agent_id):
                return engram
        return None

    @staticmethod
    def _can_retrieve(engram: Engram, agent_id: str) -> bool:
        if engram.state != "active":
            return False
        return engram.owner_agent_id == agent_id or engram.visibility != "private"


def _to_fts_query(cue: str) -> str:
    """Convert a natural language cue to an FTS5 OR query.

    Words are quoted for FTS5 safety (prevents operators like hyphens
    from causing errors).
    """
    words = [w for w in cue.split() if len(w) > 2 and w.isalnum()]
    if not words:
        clean = "".join(c for c in cue if c.isalnum() or c == " ").strip()
        return f'"{clean}"' if clean else '""'
    return " OR ".join(f'"{w}"' for w in words)
