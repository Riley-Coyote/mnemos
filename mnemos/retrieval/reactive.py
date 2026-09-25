"""
Core retrieval for Mnemos — resonance-based, not search-based.

Shift 4: Instead of a weighted scoring formula, retrieval works through
spreading activation in the connection graph. FTS finds seed nodes,
activation propagates through connections weighted by relation type,
and what lights up after N hops is what's relevant.

The graph structure IS the relevance model. No formula needed.

Pipeline:
1. FTS search → seed nodes
2. Spreading activation through connection graph (3 hops)
3. Emotional bias applied multiplicatively
4. Threshold → return activated engrams
5. Reconsolidation on all returned engrams
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.engram import Engram
from ..core.emotional_state import EmotionalState
from ..core.types import ConnectionRelation
from .reconsolidation import reconsolidate
from ..store.fts import fts_words, or_query

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

log = logging.getLogger(__name__)
_EMBEDDING_SEED_FAILURE_LOGGED = False


def _log_seed_failure_once(exc: Exception) -> None:
    global _EMBEDDING_SEED_FAILURE_LOGGED
    if _EMBEDDING_SEED_FAILURE_LOGGED:
        return
    _EMBEDDING_SEED_FAILURE_LOGGED = True
    log.warning(
        "Embedding seeding failed; recall continues on keywords: %s: %s",
        type(exc).__name__, exc,
    )


@dataclass
class RetrievalResult:
    """A scored retrieval result wrapping an engram.

    ``retrieval_path`` says how the engram was reached: "fts" for a keyword
    seed, "embedding" for a seed found by meaning alone, "resonance" for one
    reached through connections.
    """

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
    finds seed nodes, activation spreads through typed connections,
    and what lights up is what's relevant.

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
        person_id: str = "user",
        project_scope: str = "global",
        max_results: int = 10,
        emotional_state: EmotionalState | None = None,
    ) -> list[RetrievalResult]:
        """Retrieve memories via resonance — spreading activation through the graph.

        Pipeline:
        1. FTS search → seed nodes (entry points into the graph)
        2. Spreading activation (3 hops, decay per hop, weighted by relation)
        3. Emotional bias (multiplicative boost for congruent tags)
        4. Filter by threshold + confidence floor
        5. Reconsolidate returned engrams

        Returns:
            List of RetrievalResult sorted by activation level (descending).
        """
        if not cue or not cue.strip():
            return []

        # 1. SEED: Find entry points via FTS + embeddings
        seeds: dict[str, Engram] = {}
        # Each seed starts as bright as it matched the cue. Every word of the cue
        # is a way in, so a memory that shares only a common word with it ("the",
        # "what") is found too; it starts faint, and the best match starts at 1.0.
        # Starting them all at 1.0 let common words light most of the graph and
        # let the most-linked memories answer almost every cue.
        seed_activation: dict[str, float] = {}

        # FTS seeds (keyword matching), with FTS5's bm25 rank for each
        fts_query = _to_fts_query(cue)
        fts_results = self._store.search_fts_ranked(
            fts_query, limit=30, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope,
        )
        _add_ranked_seeds(
            seeds, seed_activation,
            [(e, rank) for e, rank in fts_results if e.owner_agent_id == agent_id],
        )

        # Shared DB seeds (cross-agent shared memories), ranked within their own search
        if self._shared_store:
            try:
                if hasattr(self._shared_store, "search_fts_ranked"):
                    shared_fts = self._shared_store.search_fts_ranked(fts_query, limit=20)
                else:
                    shared_fts = [(e, -1.0) for e in self._shared_store.search_fts(fts_query, limit=20)]
                _add_ranked_seeds(
                    seeds, seed_activation,
                    [(e, rank) for e, rank in shared_fts if e.visibility in ("shared", "public")],
                )
            except Exception:
                pass  # Shared store is optional

        # Embedding seeds (meaning matching — finds what FTS misses). Kept
        # apart so results can say which seeds came from meaning alone:
        # labelled "fts" like the rest, nothing could show whether
        # embeddings contributed anything at all.
        embedding_similarity: dict[str, float] = {}
        if self._embedding_index and hasattr(self._embedding_index, 'search'):
            try:
                embedding_hits = self._embedding_index.search(
                    cue, k=20, exclude_ids=set(seeds.keys())
                )
                for eid, similarity in embedding_hits:
                    if similarity > 0.3 and eid not in seeds:  # Threshold for relevance
                        engram = self._store.get_engram_in_scope(
                            eid, agent_id=agent_id, person_id=person_id,
                            project_scope=project_scope,
                        )
                        if engram and engram.state == "active":
                            seeds[eid] = engram
                            embedding_similarity[eid] = similarity
                            # a meaning match starts as bright as it matched, too
                            seed_activation[eid] = min(1.0, similarity)
            except Exception as exc:
                # Embeddings are optional — FTS still works — but a failure
                # here is a bug, not a missing backend (the index reports
                # those itself), so say it once instead of hiding it.
                _log_seed_failure_once(exc)

        if not seeds:
            return []

        # 2. PROPAGATE: Spreading activation through connection graph
        activation: dict[str, float] = {}

        # Seeds start as bright as they matched
        for seed_id in seeds:
            activation[seed_id] = seed_activation.get(seed_id, 1.0)

        # Spread through connections
        for hop in range(1, self._depth + 1):
            hop_decay = self._decay ** hop
            new_activation: dict[str, float] = defaultdict(float)

            for engram_id, current_act in list(activation.items()):
                if current_act < self._threshold:
                    continue

                connections = self._store.get_connections(engram_id)
                # Cross-DB connections: also check shared store
                if self._shared_store:
                    try:
                        connections = connections + self._shared_store.get_connections(engram_id)
                    except Exception:
                        pass
                for conn in connections:
                    if not self._store.engram_visible_in_scope(
                        conn.target_id, agent_id=agent_id, person_id=person_id,
                        project_scope=project_scope,
                    ):
                        continue
                    # Weight by relation type
                    relation_weight = _RELATION_WEIGHTS.get(conn.relation, 0.5)
                    propagated = current_act * hop_decay * conn.strength * relation_weight

                    if propagated > self._threshold * 0.5:
                        new_activation[conn.target_id] += propagated

            # Merge new activations (additive — multiple paths reinforce)
            for eid, act in new_activation.items():
                activation[eid] = activation.get(eid, 0.0) + act

        # 3. EMOTIONAL BIAS: multiplicative boost for congruent engrams
        if emotional_state:
            bias = emotional_state.get_retrieval_bias()
            if bias:
                for eid in list(activation.keys()):
                    engram = seeds.get(eid) or self._store.get_engram_in_scope(
                        eid, agent_id=agent_id, person_id=person_id,
                        project_scope=project_scope,
                    )
                    if engram and engram.tags:
                        overlap = sum(bias.get(tag, 0.0) for tag in engram.tags)
                        if overlap > 0:
                            activation[eid] *= (1.0 + min(0.5, overlap))

        # 4. FILTER + LOAD: threshold, confidence floor, build results
        results: list[RetrievalResult] = []
        for eid, act_level in activation.items():
            if act_level < self._threshold:
                continue

            engram = seeds.get(eid)
            if not engram:
                engram = self._store.get_engram_in_scope(
                    eid, agent_id=agent_id, person_id=person_id,
                    project_scope=project_scope,
                )
            # Cross-DB: check shared store if not found in private
            if not engram and self._shared_store:
                engram = self._shared_store.get_engram(eid)

            if not engram or engram.state != "active":
                continue
            # Allow own engrams + shared/public from other agents
            if engram.owner_agent_id != agent_id and engram.visibility == "private":
                continue

            if engram.source.confidence < self._confidence_floor:
                continue

            if eid in embedding_similarity:
                path = "embedding"
            else:
                path = "fts" if eid in seeds else "resonance"
            breakdown = {
                "activation": round(act_level, 4),
                "is_seed": eid in seeds,
            }
            if eid in embedding_similarity:
                breakdown["similarity"] = embedding_similarity[eid]
            results.append(
                RetrievalResult(
                    engram=engram,
                    score=round(act_level, 4),
                    score_breakdown=breakdown,
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


def _add_ranked_seeds(
    seeds: dict[str, Engram],
    seed_activation: dict[str, float],
    ranked: list[tuple[Engram, float]],
) -> None:
    """Add one search's results as seeds, each starting at its bm25 rank relative
    to that search's best (FTS5 ranks are negative; lower is a better match).
    An engram already seeded keeps its first, stronger start."""
    best = None
    for engram, rank in ranked:
        if engram.id in seeds:
            continue
        if best is None:
            best = rank
        seeds[engram.id] = engram
        seed_activation[engram.id] = rank / best if best < 0 else 1.0


def _to_fts_query(cue: str) -> str:
    """Convert a natural language cue to an FTS5 OR query.

    Words are quoted for FTS5 safety (prevents operators like hyphens
    from causing errors).
    """
    words = fts_words(cue)
    if not words:
        clean = "".join(c for c in cue if c.isalnum() or c == " ").strip()
        return f'"{clean}"' if clean else '""'
    return or_query(words)
