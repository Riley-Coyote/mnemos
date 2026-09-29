"""
Core retrieval for Mnemos: found by words and by meaning, then resonance.

Pipeline:
1. SEED from three ranked lists, each over only what the caller may be shown:
   - words among memories: the ones live in its scope that hold the cue's
     words, in their own words (FTS5 bm25; active, then up to
     ``DORMANT_SEED_LIMIT`` dormant, on one scale) or in their lessons (the
     impacts the agent wrote that a memory's words don't already say; bm25
     over those lessons), so a memory is found by what it taught as well as
     by what happened. Each memory is in this list once, at the better of its
     two ranks (``better_rank``): two votes for the same words would put it
     above a memory whose words match better;
   - words among the notes the caller passes (handoffs; bm25 over those notes);
   - meaning: memories and notes alike, each by its closest passage (a
     memory's lesson is one of them), above a similarity floor, and only then
     the top taken.
   The lists are fused by reciprocal rank (words 0.3, meaning 0.5, k = 60, as
   Polyphonic fuses its seeds), so neither kind of match is locked out. A seed
   starts at its fused score relative to the best one.
   Fusion keeps only each list's order, so a match on a word most memories
   hold would count as much as one on a rare word. With meaning to decide, a
   word held by more than ``COMMON_SHARE`` of the live memories in the scope
   is left out of every words list (``search_terms``); when every word is,
   there are no words lists and meaning decides alone (WP-R08c).
2. RESONANCE through the connection graph, among memories only: each hop,
   only memories reached for the first time pass activation on, a memory's
   contribution is divided by its number of links, and it stops after two
   hops. Only active memories relay or receive; a note never takes part.
3. Emotional bias applied multiplicatively.
4. Filter, then cut: threshold, confidence floor and the caller's ``keep``
   first, then ``max_results``, so what a filter drops is replaced from the
   ranking.
5. Reconsolidation of the memories returned, once per session per memory. A
   caller that filters further retrieves without it and reinforces what it
   finally shows (``ReactiveRetriever.reinforce``).

Why, from the review of 2026-09-26 on a copy of a real store: the meaning
search took its top 20 over all 3,746 stored vectors before checking scope, so
across twelve cues 14 of 240 meaning hits survived and 1 of 60 results came by
meaning; and every reached memory re-fired on every hop with no fan limit, so
the best keyword match for "how does Riley like to be told about mistakes"
finished 12th behind memories with 20 to 32 links.

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
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..code_version import MAINTENANCE_CODE_VERSION
from ..core.engram import Engram
from ..core.emotional_state import EmotionalState
from ..core.types import ConnectionRelation
from .reconsolidation import reconsolidate
from ..store.fts import (
    COMMON_SHARE,
    common_words,
    or_query,
    rank_by_words,
    search_among,
    search_words,
)

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

# Reciprocal rank fusion, as Polyphonic fuses its trigram and vector seeds
# (site/supabase/functions/_shared/embeddings.ts): each list a result is in
# adds weight / (FUSION_K + its rank there), ranks counted from 1.
WORDS_WEIGHT = 0.3
MEANING_WEIGHT = 0.5
FUSION_K = 60
# How far down each list reaches.
WORD_SEEDS = 30
MEANING_SEEDS = 30
# Below this cosine similarity, nothing is a match by meaning. 0.35 since
# passages became sentence windows (R08b): read in windows, more of every text
# clears a floor, and at 0.3 a question that 15 items cleared was cleared by
# 41, which pushed the lesson it was about out of the top ten. The lab's
# replay on its snapshot of 2026-09-27 (mnemos-lab notebook,
# 2026-09-27-r08b-reach-replay), facts in the top ten, a newer handoff that
# carries a fact counting for it: of the 42 held-out facts memory holds, 35
# on main at 0.3, 35 with windows at 0.3, 36 with windows at 0.35; of the 29
# development facts, 22, 20 and 22.
MEANING_FLOOR = 0.35
# Resonance reaches at most this many links from a seed.
SPREAD_HOPS = 2


def fuse(lists: Iterable[tuple[Sequence[str], float]], k: int = FUSION_K) -> dict[str, float]:
    """Reciprocal rank fusion: for each id, the sum over the ranked lists it is
    in of ``weight / (k + rank)``, with ranks counted from 1. A list naming an
    id twice counts its first place."""
    scores: dict[str, float] = {}
    for ids, weight in lists:
        seen: set[str] = set()
        for rank, item_id in enumerate(ids, start=1):
            if item_id in seen:
                continue
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return scores


def better_rank(*lists: Sequence[str]) -> list[str]:
    """One ranking from several rankings of the same kind of item: each item
    once, at its best rank in any of them, a tie going to the earlier list.
    A memory matched by its own words and by its lesson's words is one match
    by words, not two."""
    best: dict[str, tuple[int, int]] = {}
    for order, ids in enumerate(lists):
        for rank, item_id in enumerate(ids, start=1):
            if item_id not in best or (rank, order) < best[item_id]:
                best[item_id] = (rank, order)
    return sorted(best, key=best.__getitem__)


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
    """A scored retrieval result: a memory (``engram``), or a note the caller
    asked to have ranked with the memories (``note``, a handoff), which is
    never an engram and has none.

    ``retrieval_path`` says how it was reached: "fts" for a match by words
    (whether or not its meaning matched too), "embedding" for one found by
    meaning alone, "resonance" for a memory reached only through connections.
    ``score_breakdown`` holds its ranks in the lists it was fused from
    (``words_rank``, ``meaning_rank``), its ``similarity`` and its ``fused``
    score.
    """

    engram: Engram | None
    score: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    retrieval_path: str = "fts"
    note: dict[str, Any] | None = None

    @property
    def item_id(self) -> str:
        """The memory's id, or the note's."""
        if self.engram is not None:
            return self.engram.id
        return str((self.note or {}).get("id", ""))


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
    """Memory retrieval: found by words and by meaning, then resonance.

    Three ranked lists (memories by their words or their lessons' words,
    notes by their words, both by their meaning) are fused by reciprocal rank
    into seeds, activation spreads from the seeds through typed connections,
    and what lights up is ranked, filtered and cut.

    Usage:
        retriever = ReactiveRetriever(store)
        results = retriever.retrieve("What does the user think about dark mode?")
    """

    def __init__(
        self,
        store: EngramStore,
        embedding_index: Any | None = None,
        shared_store: Any | None = None,
        activation_depth: int = SPREAD_HOPS,
        activation_decay: float = 0.5,
        activation_threshold: float = 0.1,
        reconsolidation_enabled: bool = True,
        confidence_floor: float = 0.3,
        common_share: float | None = COMMON_SHARE,
        length_penalty: float | None = None,
    ) -> None:
        self._store = store
        self._embedding_index = embedding_index
        self._shared_store = shared_store
        self._depth = activation_depth
        self._decay = activation_decay
        self._threshold = activation_threshold
        self._reconsolidation_enabled = reconsolidation_enabled
        self._confidence_floor = confidence_floor
        # A word in more than this share of the live memories in scope is left
        # out of the words lists when meaning can decide (None: none is).
        self._common_share = common_share
        # λ of the meaning order (``EmbeddingIndex.search_candidates``); None
        # leaves it to the index (``LENGTH_PENALTY``). It never moves a floor.
        self._length_penalty = length_penalty

    def search_terms(
        self, cue: str, *, agent_id: str, person_id: str, project_scope: str,
        lessons: dict[str, str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """The words of ``cue`` recall searches for (``search_words``), and
        those it leaves out: the ones in more than ``common_share`` of the
        live memories in the scope, counted from the index (``word_shares``).

        Only when meaning can decide: without an embedding index, or with one
        that is off, the words are all there is, and every word is searched.
        A scope with fewer than ``COMMON_MIN_MEMORIES`` live memories cuts
        none. Both lists keep the cue's order. What is left out is left out
        only for what meaning can find: ``retrieve`` searches a memory or a
        handoff with no vector of the index's model by every word, and a
        shared store always by every word."""
        words = search_words(cue)
        index = self._embedding_index
        if (
            not words
            or self._common_share is None
            or index is None
            or not getattr(index, "available", True)
        ):
            return words, []
        common = common_words(
            self._store, words, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope, share=self._common_share, lessons=lessons,
        )
        return (
            [word for word in words if word.lower() not in common],
            [word for word in words if word.lower() in common],
        )

    def retrieve(
        self,
        cue: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        max_results: int | None = 10,
        emotional_state: EmotionalState | None = None,
        *,
        reconsolidate_results: bool = True,
        keep: Callable[[RetrievalResult], bool] | None = None,
        notes: Iterable[dict[str, Any]] | None = None,
        query_vector: Sequence[float] | None = None,
    ) -> list[RetrievalResult]:
        """Retrieve memories for ``cue``, best first.

        Pipeline:
        1. Seeds from three ranked lists, fused by reciprocal rank (see the
           module docstring): each seed starts at its fused score relative to
           the best.
        2. Resonance, among memories only: from memories newly reached, each
           contribution divided by the sender's links, at most two hops.
        3. Emotional bias (multiplicative boost for congruent tags).
        4. Filter, then cut: threshold, confidence floor and ``keep`` first,
           then ``max_results`` (None: every result), so the list backfills
           from the ranking instead of coming back short.
        5. Reconsolidate the memories returned (see ``reinforce``), unless
           ``reconsolidate_results`` is False: then the results come back and
           nothing is written (no access count, strength or co-activation
           link). A caller that filters the results further retrieves this way
           and reinforces only what it shows. Code older than the store
           retrieves this way too, because how a return changes a memory is a
           rule newer code may have replaced.

        ``notes`` (hypomnema rows with ``id`` and ``content``: the handoffs)
        are ranked with the memories, by their words and their meaning. They
        come back as results with ``note`` set and no engram, and never take
        part in resonance: they relay nothing and receive nothing.

        ``query_vector`` is the cue's own vector, when the caller has it
        (``EmbeddingIndex.embed_query``); otherwise the cue is embedded here,
        once, before anything is decided. With no vector (the embedding
        failed), meaning is off for this cue: nothing is cut from the words.
        """
        if not cue or not cue.strip():
            return []
        scope = {"agent_id": agent_id, "person_id": person_id, "project_scope": project_scope}
        engrams: dict[str, Engram] = {}

        # The cue's vector first. The words cut hands the decision to meaning,
        # so it holds only when meaning runs for this cue: a remote timeout or
        # a model that won't load leaves no vector, and then every word is
        # searched, as if meaning were off. An index that embeds only inside
        # its own search (a stand-in) can't say, so nothing is cut for it.
        index = self._embedding_index
        vector = query_vector
        embeds_first = index is not None and hasattr(index, "embed_query")
        if vector is None and embeds_first and getattr(index, "available", False):
            vector = self._cue_vector(cue)
        meaning_runs = index is not None and (vector is not None or not embeds_first)

        # 1a. WORDS among memories: the live ones in scope holding the cue's
        # words that mean something (_to_fts_query), active then dormant. Both
        # searches run one query over one index, so their bm25 ranks share a
        # scale and merge into one list. The words too common here to say
        # anything are left out of the words lists (search_terms), but only
        # for what meaning can find: a memory or a handoff with no vector of
        # the index's model (not indexed yet, failed, or indexed by another
        # model) is searched with every word, on the same scale. With every
        # word left out and meaning able to find everything, there are no
        # words lists, and meaning decides alone. A cue with no word to search
        # is searched as a phrase, as before.
        # The lessons, read once: ranked by their words below, and counted in
        # each word's share (a word most lessons hold is common too).
        lessons_of = getattr(self._store, "live_memory_lessons", None)
        lessons = lessons_of(**scope) if lessons_of is not None else {}
        terms, common = (
            self.search_terms(cue, lessons=lessons, **scope)
            if vector is not None else (search_words(cue), [])
        )
        every_word = _to_fts_query(cue)
        note_by_id = {str(note["id"]): note for note in notes or []}
        note_texts = {item_id: note.get("content") or "" for item_id, note in note_by_id.items()}
        live: set[str] | None = None
        unseen: set[str] = set()
        unseen_notes: set[str] = set()
        if common:
            live = self._store.live_engram_ids(**scope)
            unseen, unseen_notes = self._unseen_by_meaning(live, note_texts)
        memory_query = or_query(terms) if terms else (None if common else every_word)
        none_found = bool(live) and unseen >= live
        if none_found:
            memory_query = every_word  # meaning can find none of them: nothing is cut
        ranked: list[tuple[Engram, float]] = []
        dormant: list[tuple[Engram, float]] = []
        if memory_query is not None:
            ranked = self._store.search_fts_ranked(memory_query, limit=WORD_SEEDS, **scope)
            dormant = self._store.search_fts_ranked(
                memory_query, limit=DORMANT_SEED_LIMIT, state="dormant", **scope,
            )
        if unseen and not none_found:
            ranked = _merged(
                ranked, self._words_among(every_word, unseen, "active", WORD_SEEDS, scope),
                WORD_SEEDS,
            )
            dormant = _merged(
                dormant, self._words_among(every_word, unseen, "dormant", DORMANT_SEED_LIMIT, scope),
                DORMANT_SEED_LIMIT,
            )
        ranked += dormant
        ranked.sort(key=lambda found: found[1])
        memory_words: list[str] = []
        for engram, _rank in ranked:
            if engram.owner_agent_id != agent_id or engram.id in engrams:
                continue
            engrams[engram.id] = engram
            memory_words.append(engram.id)

        # 1a'. WORDS among their lessons: the impacts the agent wrote, which
        # the full-text index doesn't hold. A memory whose content is about a
        # report and whose lesson says "avoid jargon" is found by "avoid
        # jargon". Only live memories in the scope have them here. Each
        # memory then counts once by words, at the better of its two ranks.
        lesson_words: list[str] = []
        if lessons:
            # A lesson is found by meaning only by its own passage, cut from
            # its words now: one written after the capture waits for it.
            every_lesson_word = set(unseen)
            if common:
                every_lesson_word |= set(lessons) - self._lessons_found_by_meaning(lessons)
            for item_id, _score in rank_by_words(
                cue, lessons, limit=WORD_SEEDS, terms=terms, every_word_for=every_lesson_word,
            ):
                if item_id not in engrams:
                    engram = self._store.get_engram_in_scope(item_id, **scope)
                    if engram is None or engram.state not in ("active", "dormant"):
                        continue
                    engrams[item_id] = engram
                lesson_words.append(item_id)
        memory_words = better_rank(memory_words, lesson_words)

        # Shared memories (cross-agent), ranked by words within their own
        # search. Always by every word: how common a word is in this scope
        # says nothing about another store, and meaning never searches it.
        shared_words: list[str] = []
        if self._shared_store:
            try:
                if hasattr(self._shared_store, "search_fts_ranked"):
                    shared = self._shared_store.search_fts_ranked(every_word, limit=20)
                else:
                    shared = [(e, -1.0) for e in self._shared_store.search_fts(every_word, limit=20)]
                for engram, _rank in shared:
                    if engram.visibility in ("shared", "public") and engram.id not in engrams:
                        engrams[engram.id] = engram
                        shared_words.append(engram.id)
            except Exception:
                pass  # Shared store is optional

        # 1b. WORDS among the notes the caller passes (handoffs), by bm25 over them
        note_words = [
            item_id for item_id, _score in rank_by_words(
                cue, note_texts, limit=WORD_SEEDS, terms=terms, every_word_for=unseen_notes,
            )
        ] if note_by_id else []

        # 1c. MEANING, over only what may be returned: the memories live in the
        # scope and the notes, each by its closest passage, and only then the
        # top taken. Kept apart so results say which came by meaning.
        similarity: dict[str, float] = {}
        meaning: list[str] = []
        if meaning_runs:
            try:
                candidates = (live if live is not None else self._store.live_engram_ids(**scope)) | set(note_by_id)
                for item_id, sim in self._meaning_hits(cue, candidates, note_texts, vector):
                    if item_id not in note_by_id and item_id not in engrams:
                        engram = self._store.get_engram_in_scope(item_id, **scope)
                        if engram is None or engram.state not in ("active", "dormant"):
                            continue
                        engrams[item_id] = engram
                    similarity[item_id] = sim
                    meaning.append(item_id)
            except Exception as exc:
                # Meaning is optional — words still work — but a failure here
                # is a bug, not a missing backend (the index reports those
                # itself), so say it once instead of hiding it.
                _log_seed_failure_once(exc)

        # 1d. FUSE: reciprocal rank over the lists. Each seed starts at
        # its fused score relative to the best match, and a dormant memory at
        # half of that, so an equal active match comes first.
        fused = fuse([
            (memory_words, WORDS_WEIGHT),
            (shared_words, WORDS_WEIGHT),
            (note_words, WORDS_WEIGHT),
            (meaning, MEANING_WEIGHT),
        ])
        if not fused:
            return []
        best = max(fused.values())
        seed_activation: dict[str, float] = {}
        for item_id, score in fused.items():
            engram = engrams.get(item_id)
            share = DORMANT_SEED_SHARE if engram is not None and engram.state == "dormant" else 1.0
            seed_activation[item_id] = share * score / best

        # 2. RESONANCE, among memories. Each hop only memories reached for the
        # first time pass activation on; a memory already lit never fires
        # again. What one sends is divided by its number of links, so a hub
        # lights each neighbour a little rather than all of them fully. Quiet
        # memories (dormant, archived) relay nothing and receive nothing: only
        # the cue brings a dormant one back.
        activation = {item_id: act for item_id, act in seed_activation.items() if item_id in engrams}
        frontier = {
            item_id: act for item_id, act in activation.items()
            if engrams[item_id].state not in _QUIET_STATES
        }
        reached = set(activation)
        for hop in range(1, self._depth + 1):
            hop_decay = self._decay ** hop
            arriving: dict[str, float] = defaultdict(float)
            for sender, sender_activation in frontier.items():
                if sender_activation < self._threshold:
                    continue
                links = self._store.get_connections(sender)
                # Cross-DB connections: also check shared store
                if self._shared_store:
                    try:
                        links = links + self._shared_store.get_connections(sender)
                    except Exception:
                        pass
                if not links:
                    continue
                fan = len(links)
                for link in links:
                    if self._store.engram_state_in_scope(link.target_id, **scope) != "active":
                        continue  # out of scope, or quiet
                    sent = (
                        sender_activation * hop_decay * link.strength
                        * _RELATION_WEIGHTS.get(link.relation, 0.5) / fan
                    )
                    if sent > self._threshold * 0.5:
                        arriving[link.target_id] += sent
            for target, amount in arriving.items():
                activation[target] = activation.get(target, 0.0) + amount
            frontier = {target: amount for target, amount in arriving.items() if target not in reached}
            reached.update(arriving)
            if not frontier:
                break

        # 3. EMOTIONAL BIAS: multiplicative boost for congruent engrams
        if emotional_state:
            bias = emotional_state.get_retrieval_bias()
            if bias:
                for eid in list(activation.keys()):
                    engram = engrams.get(eid) or self._store.get_engram_in_scope(eid, **scope)
                    if engram and engram.tags:
                        overlap = sum(bias.get(tag, 0.0) for tag in engram.tags)
                        if overlap > 0:
                            activation[eid] *= (1.0 + min(0.5, overlap))

        # 4. LOAD, then FILTER, then CUT
        words_rank: dict[str, int] = {}
        for ranked_ids in (memory_words, shared_words, note_words):
            for rank, item_id in enumerate(ranked_ids, start=1):
                words_rank.setdefault(item_id, rank)
        meaning_rank = {item_id: rank for rank, item_id in enumerate(meaning, start=1)}

        def breakdown(item_id: str, level: float) -> tuple[dict[str, Any], str]:
            detail: dict[str, Any] = {
                "activation": round(level, 4),
                "is_seed": item_id in seed_activation,
            }
            if item_id in fused:
                detail["fused"] = round(fused[item_id], 6)
            if item_id in words_rank:
                detail["words_rank"] = words_rank[item_id]
            if item_id in meaning_rank:
                detail["meaning_rank"] = meaning_rank[item_id]
                detail["similarity"] = similarity[item_id]
            if item_id in words_rank:
                path = "fts"
            elif item_id in meaning_rank:
                path = "embedding"
            else:
                path = "resonance"
            return detail, path

        results: list[RetrievalResult] = []
        for eid, level in activation.items():
            if level < self._threshold:
                continue
            engram = engrams.get(eid) or self._store.get_engram_in_scope(eid, **scope)
            # Cross-DB: check shared store if not found in private
            if not engram and self._shared_store:
                engram = self._shared_store.get_engram(eid)
            # An active memory, or a dormant one the cue itself matched.
            if not engram or not (
                engram.state == "active"
                or (engram.state == "dormant" and eid in seed_activation)
            ):
                continue
            # Allow own engrams + shared/public from other agents
            if engram.owner_agent_id != agent_id and engram.visibility == "private":
                continue
            if engram.source.confidence < self._confidence_floor:
                continue
            detail, path = breakdown(eid, level)
            results.append(RetrievalResult(
                engram=engram, score=round(level, 4), score_breakdown=detail, retrieval_path=path,
            ))
        for item_id, level in seed_activation.items():
            note = note_by_id.get(item_id)
            if note is None or level < self._threshold:
                continue
            detail, path = breakdown(item_id, level)
            results.append(RetrievalResult(
                engram=None, note=note, score=round(level, 4), score_breakdown=detail,
                retrieval_path=path,
            ))

        # Best first; between equal scores, the better fused seed first.
        results.sort(key=lambda r: (-r.score, -r.score_breakdown.get("fused", 0.0)))
        top: list[RetrievalResult] = []
        for result in results:
            if keep is not None and not keep(result):
                continue
            top.append(result)
            if max_results is not None and len(top) >= max_results:
                break

        # 5. RECONSOLIDATE the memories this call returns
        if reconsolidate_results:
            self.reinforce(top, cue, agent_id=agent_id)

        return top

    def _cue_vector(self, cue: str) -> list[float] | None:
        """The cue's vector from the index, or None when embedding it failed."""
        try:
            return self._embedding_index.embed_query(cue)
        except Exception as exc:
            _log_seed_failure_once(exc)
            return None

    def _lessons_found_by_meaning(self, lessons: dict[str, str]) -> set[str]:
        """The memories whose lesson meaning can find by the lesson's own
        passage (``EmbeddingIndex.lessons_searchable``). An index that can't
        say finds none."""
        found = getattr(self._embedding_index, "lessons_searchable", None)
        if found is None:
            return set()
        try:
            return found(lessons)
        except Exception as exc:
            _log_seed_failure_once(exc)
            return set()

    def _unseen_by_meaning(
        self, live: Collection[str], note_texts: dict[str, str],
    ) -> tuple[set[str], set[str]]:
        """The live memories and the notes meaning can't find now: no vector of
        the index's model to find them by (``EmbeddingIndex.searchable``). An
        index that can't say counts for none of them."""
        searchable = getattr(self._embedding_index, "searchable", None)
        if searchable is None:
            return set(live), set(note_texts)
        try:
            found = searchable(set(live) | set(note_texts), texts=note_texts)
        except Exception as exc:
            _log_seed_failure_once(exc)
            return set(live), set(note_texts)
        return set(live) - found, set(note_texts) - found

    def _words_among(
        self, query: str, ids: Collection[str], state: str, limit: int, scope: dict[str, str],
    ) -> list[tuple[Engram, float]]:
        """``search_fts_ranked`` among ``ids`` only: ``(engram, rank)``."""
        found: list[tuple[Engram, float]] = []
        for item_id, rank in search_among(self._store, query, ids, state=state, limit=limit, **scope):
            engram = self._store.get_engram_in_scope(item_id, **scope)
            if engram is not None:
                found.append((engram, rank))
        return found

    def _meaning_hits(
        self, cue: str, candidates: Collection[str], texts: dict[str, str] | None = None,
        vector: Sequence[float] | None = None,
    ) -> list[tuple[str, float]]:
        """The candidates closest in meaning to ``cue``, at most ``MEANING_SEEDS``,
        none below ``MEANING_FLOOR``, scored before the top is taken. ``texts``
        are the notes' words now: a passage cut from other words never counts.
        They come in the order of their meaning score, the best passage less λ
        times the log of their passages (``length_penalty``; the index's own
        by default); the floor reads the best passage itself."""
        index = self._embedding_index
        if hasattr(index, "search_candidates"):
            extra: dict[str, Any] = {}
            if self._length_penalty is not None:
                extra["length_penalty"] = self._length_penalty
            if vector is not None:
                extra["query_vector"] = vector
            return index.search_candidates(
                cue, candidates, k=MEANING_SEEDS, floor=MEANING_FLOOR, texts=texts, **extra,
            )
        if not hasattr(index, "search"):
            return []
        # An index without the scoped search (a stand-in, or an older one)
        return [
            (item_id, sim) for item_id, sim in index.search(cue, k=MEANING_SEEDS)
            if item_id in candidates and sim >= MEANING_FLOOR
        ][:MEANING_SEEDS]

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

        A note ranked with the memories (a handoff) is never reinforced and
        never linked: it is not a memory.
        """
        results = [r for r in results if r.engram is not None]
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


def _merged(
    first: list[tuple[Engram, float]], second: list[tuple[Engram, float]], limit: int,
) -> list[tuple[Engram, float]]:
    """Two rankings of one full-text index as one: each memory once, at its
    better rank, best first, at most ``limit``."""
    best: dict[str, tuple[Engram, float]] = {}
    for engram, rank in [*first, *second]:
        if engram.id not in best or rank < best[engram.id][1]:
            best[engram.id] = (engram, rank)
    return sorted(best.values(), key=lambda found: found[1])[:limit]


def _code_older_than(store: Any) -> bool:
    """Whether code newer than this has opened ``store`` (see code_version)."""
    minimum = store.min_code_version()
    return minimum is not None and minimum > MAINTENANCE_CODE_VERSION


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
