"""A seed starts as bright as it matched the cue.

Recall seeds from every word of the cue, OR'd together, and spreads activation
from the seeds through the graph. Every seed used to start at 1.0, so a memory
that shared only "the" with the cue started as bright as the one that answered
it. With a few dozen such seeds, the most-linked memories collected light from
all of them and came back for nearly every cue. On a copy of a real store, a
rule captured one afternoon did not come back the next morning when recalled by
its own words: five heavily linked memories scored 10 to 12, the rule 1.7.

Now a keyword seed starts at its bm25 rank relative to the best match of the
same search, and a meaning seed at its similarity.
"""

from __future__ import annotations

from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.retrieval.reactive import ReactiveRetriever

SCOPE = dict(agent_id="default", person_id="user", project_scope="global")


def _memory(content: str) -> Engram:
    return Engram(content=content, kind="semantic", owner_agent_id="default",
                  person_id="user", project_scope="global")


def test_common_words_no_longer_outshine_the_answer(store):
    """Twelve memories share only "the" with the cue, and all of them link to one
    hub. The memory that holds the cue's rare word must come back first."""
    hub = _memory("the notes about the weather this week")
    answer = _memory("lighthouse keeper logbook")
    fillers = [_memory(f"the filler memory number {i} about the day") for i in range(12)]
    for filler in fillers:
        filler.connections.append(Connection(
            target_id=hub.id, relation=ConnectionRelation.CO_ACTIVATED,
            strength=0.9, formed_by="retrieval",
        ))
    for engram in [hub, answer, *fillers]:
        store.save_engram(engram)

    results = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "the lighthouse", **SCOPE)

    assert results, "recall returned nothing"
    assert results[0].engram.id == answer.id, (
        f"expected the lighthouse memory first, got {results[0].engram.content!r} "
        f"at {results[0].score} (the answer scored "
        f"{next((r.score for r in results if r.engram.id == answer.id), None)})"
    )
    assert results[0].score == 1.0


def test_a_weaker_keyword_match_starts_dimmer(store):
    """The best keyword match starts at 1.0; a weaker one, below it."""
    strong = _memory("lighthouse keeper")
    weak = _memory("a long account of a coastal walk that passed an old lighthouse "
                   "on the way to the harbour, with notes on the tide and the birds")
    for engram in (strong, weak):
        store.save_engram(engram)

    results = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "lighthouse keeper", **SCOPE)
    score = {r.engram.id: r.score for r in results}

    assert score[strong.id] == 1.0
    assert 0.0 < score[weak.id] < 1.0


class _MeaningIndex:
    """An embedding index that finds one memory by meaning, at a known similarity."""

    def __init__(self, engram_id: str, similarity: float):
        self.engram_id, self.similarity = engram_id, similarity

    def search(self, cue, k=20, exclude_ids=None):
        return [(self.engram_id, self.similarity)]


def test_a_meaning_seed_starts_at_its_similarity(store):
    """A memory found only by meaning starts at its similarity, not at 1.0."""
    unnamed = _memory("an entry that shares no words with the cue")
    store.save_engram(unnamed)

    retriever = ReactiveRetriever(store, embedding_index=_MeaningIndex(unnamed.id, 0.42),
                                  reconsolidation_enabled=False)
    results = retriever.retrieve("lighthouse keeper", **SCOPE)

    assert [r.engram.id for r in results] == [unnamed.id]
    assert results[0].score == 0.42


def test_ranked_search_is_the_same_search_with_its_ranks(store):
    """search_fts_ranked returns what search_fts returns, in the same order, each
    with FTS5's bm25 rank: negative, best first."""
    for text in ("lighthouse keeper", "lighthouse", "a keeper of bees", "harbour master"):
        store.save_engram(_memory(text))

    query = '"lighthouse" OR "keeper"'
    ranked = store.search_fts_ranked(query, limit=10, **SCOPE)
    plain = store.search_fts(query, limit=10, **SCOPE)

    assert [e.id for e, _ in ranked] == [e.id for e in plain]
    ranks = [rank for _, rank in ranked]
    assert ranks == sorted(ranks) and all(rank < 0 for rank in ranks)
    assert ranked[0][0].content == "lighthouse keeper"
