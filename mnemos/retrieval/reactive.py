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
5. Reconsolidation of what is returned, once per session per memory. A
   caller that filters further retrieves without it and reinforces what it
   finally shows (``ReactiveRetriever.reinforce``).

Quiet memories. A dormant memory is found only by the cue itself: it is seeded
when it matches, at half the activation an active memory would start with, and
a returned one wakes (``EngramStore.record_return``). Dormant and archived
memories take no part in resonance: they pass no activation on, and none
reaches them through a connection. Decay used to be the only way out of the
active set and recall the only way back, and recall never looked, so a memory
that went dormant stayed there, with every check green.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..code_version import MAINTENANCE_CODE_VERSION
from ..core.engram import Engram
from ..core.emotional_state import EmotionalState
from ..core.types import ConnectionRelation
from .reconsolidation import reconsolidate
from ..store.fts import or_query, search_words

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

log = logging.getLogger(__name__)
_EMBEDDING_SEED_FAILURE_LOGGED = False

# A dormant memory that matches the cue starts at this share of the activation
# its match would give an active one, so an equal active match comes first.
DORMANT_SEED_SHARE = 0.5
# How many dormant memories one cue may seed, apart from the active seeds, so
# a store where most memories have gone quiet cannot crowd the active ones out.
DORMANT_SEED_LIMIT = 10
# Memories that pass no activation on and receive none through a connection.
_QUIET_STATES = frozenset({"dormant", "archived"})


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
        *,
        reconsolidate_results: bool = True,
    ) -> list[RetrievalResult]:
        """Retrieve memories via resonance — spreading activation through the graph.

        Pipeline:
        1. FTS search → seed nodes (entry points into the graph)
        2. Spreading activation (3 hops, decay per hop, weighted by relation)
        3. Emotional bias (multiplicative boost for congruent tags)
        4. Filter by threshold + confidence floor
        5. Reconsolidate the engrams returned (see ``reinforce``), unless
           ``reconsolidate_results`` is False: then the results come back and
           nothing is written (no access count, strength or co-activation
           link). A caller that filters the results further retrieves this way
           and reinforces only what it shows. Code older than the store
           retrieves this way too, because how a return changes a memory is a
           rule newer code may have replaced.

        Returns:
            List of RetrievalResult sorted by activation level (descending).
        """
        if not cue or not cue.strip():
            return []

        # 1. SEED: Find entry points via FTS + embeddings
        seeds: dict[str, Engram] = {}
        # Each seed starts as bright as it matched the cue: the best match at 1.0,
        # the rest in proportion. Starting them all at 1.0 let common words light
        # most of the graph and let the most-linked memories answer almost every
        # cue. The cue's common words are not searched at all (_to_fts_query): in
        # a small store bm25 barely discounts them, and a short memory holding
        # "for" was the best match for "getting ready for her reading".
        seed_activation: dict[str, float] = {}

        # FTS seeds (keyword matching), with FTS5's bm25 rank for each: the
        # active memories that match, then up to DORMANT_SEED_LIMIT dormant
        # ones. Both searches run the same query over the same index, so their
        # ranks are placed on one scale before a dormant seed's is halved.
        fts_query = _to_fts_query(cue)
        fts_results = self._store.search_fts_ranked(
            fts_query, limit=30, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope,
        )
        fts_results += self._store.search_fts_ranked(
            fts_query, limit=DORMANT_SEED_LIMIT, agent_id=agent_id,
            person_id=person_id, project_scope=project_scope, state="dormant",
        )
        fts_results.sort(key=lambda ranked: ranked[1])
        _add_ranked_seeds(
            seeds, seed_activation,
            [(e, rank) for e, rank in fts_results if e.owner_agent_id == agent_id],
        )
        for eid, engram in seeds.items():
            if engram.state == "dormant":
                seed_activation[eid] *= DORMANT_SEED_SHARE

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
                        if engram and engram.state in ("active", "dormant"):
                            seeds[eid] = engram
                            embedding_similarity[eid] = similarity
                            # a meaning match starts as bright as it matched,
                            # too, and a dormant one at half that
                            seed_activation[eid] = min(1.0, similarity) * (
                                DORMANT_SEED_SHARE if engram.state == "dormant" else 1.0
                            )
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

        # A quiet memory (dormant or archived) passes nothing on: a dormant
        # seed counts for its own match alone.
        quiet = {eid for eid, engram in seeds.items() if engram.state in _QUIET_STATES}

        # Spread through connections
        for hop in range(1, self._depth + 1):
            hop_decay = self._decay ** hop
            new_activation: dict[str, float] = defaultdict(float)

            for engram_id, current_act in list(activation.items()):
                if current_act < self._threshold or engram_id in quiet:
                    continue

                connections = self._store.get_connections(engram_id)
                # Cross-DB connections: also check shared store
                if self._shared_store:
                    try:
                        connections = connections + self._shared_store.get_connections(engram_id)
                    except Exception:
                        pass
                for conn in connections:
                    state = self._store.engram_state_in_scope(
                        conn.target_id, agent_id=agent_id, person_id=person_id,
                        project_scope=project_scope,
                    )
                    if state is None:
                        continue
                    # Nor does anything reach a quiet memory through a link:
                    # only the cue brings a dormant one back, and nothing
                    # carries on through it.
                    if state in _QUIET_STATES:
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

            # An active memory, or a dormant one the cue itself matched.
            if not engram or not (
                engram.state == "active"
                or (engram.state == "dormant" and eid in seeds)
            ):
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

        # 5. RECONSOLIDATE what this call returns
        if reconsolidate_results:
            self.reinforce(top_results, cue, agent_id=agent_id)

        return top_results

    def reinforce(
        self,
        results: list[RetrievalResult],
        cue: str,
        *,
        agent_id: str = "default",
        session: str | None = None,
    ) -> None:
        """Reconsolidate exactly ``results``: the memories shown to the reader.

        Retrieval strengthens a memory because it came back, so only what a
        caller actually returns, after all of its own filtering, may be
        reinforced; results it drops were never seen. Memories returned
        together are linked as co-activated with each other, and with nothing
        that was dropped.

        Each memory is reinforced at most once per ``session`` (None reads
        ``CLAUDE_CODE_SESSION_ID``; an empty string means this process). A
        second return in the same session changes nothing. Nothing is written
        to a store that newer Mnemos code has opened: how a return changes a
        memory is a rule that newer code may have replaced.
        """
        if not self._reconsolidation_enabled or not results:
            return
        returned_ids = [r.engram.id for r in results]
        older: dict[int, bool] = {}
        for result in results:
            # Reconsolidate in the engram's home store
            target_store = self._store
            if (
                result.engram.owner_agent_id != agent_id
                and self._shared_store
            ):
                target_store = self._shared_store
            if id(target_store) not in older:
                older[id(target_store)] = _code_older_than(target_store)
            if older[id(target_store)]:
                continue
            result.engram = reconsolidate(
                engram=result.engram,
                current_context=cue,
                co_retrieved_ids=[
                    eid for eid in returned_ids if eid != result.engram.id
                ],
                store=target_store,
                session=session,
            )


def _code_older_than(store: Any) -> bool:
    """Whether code newer than this has opened ``store`` (see code_version)."""
    minimum = store.min_code_version()
    return minimum is not None and minimum > MAINTENANCE_CODE_VERSION


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
    """Convert a natural language cue to an FTS5 OR query of the words that
    mean something in it.

    Every word used to be ORed in, so "getting ready for her reading" made
    every memory holding "for" or "her" a seed, and those came back above the
    ones about the reading. A cue made only of common words keeps them.

    Words are quoted for FTS5 safety (prevents operators like hyphens
    from causing errors).
    """
    words = search_words(cue)
    if not words:
        clean = "".join(c for c in cue if c.isalnum() or c == " ").strip()
        return f'"{clean}"' if clean else '""'
    return or_query(words)
