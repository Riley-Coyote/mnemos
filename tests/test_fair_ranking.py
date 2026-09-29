"""Fair ranking (WP-R08c): common words, and long items, no longer win.

Recall fuses its words list and its meaning list by reciprocal rank, which
keeps each list's order and nothing of bm25's weight. So a match on a word
most memories hold counted as much as a match on a rare one: on a copy of the
live store "Riley" is in 268 of the 464 live memories of its scope, and each
of the five memories recall returned for "how does Riley like to be told about
mistakes" matched by words on that word alone (one was 18th by meaning).
With meaning to decide, a word in more than a quarter of the live memories
in scope is now left out of the words lists, and the cue counts a shared word
as distinctive only below that cut; its word path needs one in at most 2%.
Meaning is ordered fairly to length: by the best passage less
λ · ln(the passages), while every floor reads the best passage itself.

None of these tests needs sentence-transformers or torch: a fake model gives
each text a vector by the concepts it names, so every cosine is exact.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import mnemos.store.embedding_index as ei
import mnemos.store.fts as fts
from mnemos.core.engram import Engram
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.store.embedding_index import EmbeddingIndex, passages
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}


def cue_memories(*args, **kwargs):
    from mnemos.simple_runtime import cue_memories as query

    return query(*args, **kwargs)


def word_shares(*args, **kwargs):
    return fts.word_shares(*args, **kwargs)


# ── A model whose meaning is controlled ──

_CONCEPTS = (
    {"plain", "brief", "replies", "jargon", "talk"},
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
)


def concept_vector(text: str) -> list[float]:
    """Counts of each concept's words, and a little of nothing in particular."""
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in _CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def cosine(a: str, b: str) -> float:
    return sum(x * y for x, y in zip(concept_vector(a), concept_vector(b)))


class _Vector(list):
    def tolist(self):
        return list(self)


class _ConceptModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector(concept_vector(texts))
        return [_Vector(concept_vector(text)) for text in texts]


class _ConceptEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _ConceptModel()
        return self._model


@pytest.fixture
def meaning(monkeypatch):
    """A working local backend, for every index made while the test runs."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


def _memory(content: str, **fields) -> Engram:
    scope = {**SCOPE, **fields.pop("scope", {})}
    return Engram(content=content, kind=fields.pop("kind", "semantic"),
                  owner_agent_id=scope["agent_id"], person_id=scope["person_id"],
                  project_scope=scope["project_scope"], **fields)


def _store(db: Path, memories: list[Engram], *, meaning_on: bool = True) -> Path:
    """A store holding ``memories``, each with a vector by the fake model."""
    store = EngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db)) if meaning_on else None
    for engram in memories:
        store.save_engram(engram)
        if index is not None:
            index.index_engram(engram.id, engram.content)
    if index is not None:
        index.close()
    store.close()
    return db


def _riley_fillers(count: int) -> list[Engram]:
    """Memories that mention Riley and mean nothing the tests ask about."""
    return [_memory(f"Riley moved the boxes to room {i} on the second floor.") for i in range(count)]


def _invoices(count: int) -> list[Engram]:
    return [_memory(f"Invoice {i} was paid in full on time.") for i in range(count)]


def _retrieve(db: Path, cue: str, **kwargs):
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    options = {key: kwargs.pop(key) for key in ("length_penalty", "common_share") if key in kwargs}
    try:
        retriever = ReactiveRetriever(store, embedding_index=index, reconsolidation_enabled=False,
                                      **options)
        return retriever.retrieve(cue, reconsolidate_results=False, **SCOPE, **kwargs)
    finally:
        index.close()
        store.close()


# ── The words side: a frequency cut ──

# "Riley" is in 41 of 121 memories; the answer shares no word with the question.
_QUESTION = "how does Riley want me to talk"
_ANSWER = "Plain words, brief replies, no jargon."
_RILEY_AND_A_LITTLE_MEANING = "Riley replies about the lamp."


def test_a_word_most_memories_hold_does_not_put_a_worse_match_first(tmp_path, meaning):
    """The answer is the closest in meaning. The other memory is less close,
    but it holds "Riley": searched, that word gave it a words rank on top of
    its meaning rank, and it came first. Left out, meaning decides."""
    answer = _memory(_ANSWER)
    riley = _memory(_RILEY_AND_A_LITTLE_MEANING)
    db = _store(tmp_path / "memory.db", [answer, riley, *_riley_fillers(40), *_invoices(79)])
    assert cosine(_QUESTION, _ANSWER) > cosine(_QUESTION, _RILEY_AND_A_LITTLE_MEANING) > 0.35

    results = _retrieve(db, _QUESTION)

    assert results[0].engram.id == answer.id, [r.engram.content for r in results[:3]]
    store = ReadOnlyEngramStore(str(db))
    try:
        retriever = ReactiveRetriever(store, embedding_index=EmbeddingIndex(db_path=str(db), read_only=True))
        assert retriever.search_terms(_QUESTION, **SCOPE) == (["talk"], ["Riley"])
    finally:
        store.close()


def test_when_every_word_is_common_meaning_decides_alone(tmp_path, meaning):
    """"Riley" and "lamp" are each in 40 of 140 memories. Asked "Riley's
    lamp", recall has no words list: what comes back came by meaning, and a
    memory that only shares a common word with the cue does not come back."""
    lamps = [_memory(f"The lamp in cabin {i} was lit at dusk.") for i in range(40)]
    fillers = _riley_fillers(40)
    db = _store(tmp_path / "memory.db", [*lamps, *fillers, *_invoices(60)])

    results = _retrieve(db, "Riley's lamp", max_results=None)

    assert results, "meaning found the lamps"
    assert {r.retrieval_path for r in results} == {"embedding"}
    assert not {r.engram.id for r in results} & {f.id for f in fillers}
    assert all("words_rank" not in r.score_breakdown for r in results)


def test_a_small_store_keeps_every_word(tmp_path, meaning):
    """Below a hundred live memories a share says little. Nothing is cut, and
    a word half of them hold is searched."""
    db = _store(tmp_path / "memory.db", [*_riley_fillers(15), *_invoices(15)])

    results = _retrieve(db, "Riley boxes", max_results=None)

    assert any(r.retrieval_path == "fts" for r in results)


def test_without_meaning_every_word_is_searched(tmp_path):
    """Keyword-only: the words are all there is, so none is cut."""
    fillers = _riley_fillers(40)
    db = _store(tmp_path / "memory.db", [*fillers, *_invoices(80)], meaning_on=False)
    store = ReadOnlyEngramStore(str(db))
    try:
        found = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
            "Riley", reconsolidate_results=False, **SCOPE,
        )
    finally:
        store.close()

    assert found and {r.engram.id for r in found} <= {f.id for f in fillers}


def test_shares_are_counted_from_the_index_and_kept_for_the_process(tmp_path):
    """A share is of the live memories in one scope, as the full-text index
    matches the word ("RILEY's" is "riley"); an archived memory, or one in
    another scope, is not counted. Counted once per process, and again when
    the scope's count of live memories changes."""
    fts._WORD_COUNTS.clear()
    live = [_memory("RILEY's notes, filed.")] + _riley_fillers(29) + _invoices(80)
    elsewhere = [_memory("Riley elsewhere.", scope={"project_scope": "other"}) for _ in range(20)]
    archived = [_memory("Riley, archived.", state="archived") for _ in range(5)]
    db = _store(tmp_path / "memory.db", [*live, *elsewhere, *archived], meaning_on=False)
    store = EngramStore(str(db))
    statements: list[str] = []
    store._get_conn().set_trace_callback(statements.append)
    try:
        first = word_shares(store, ["Riley", "invoice", "absent"], **SCOPE)
        counted = sum("MATCH" in s for s in statements)
        statements.clear()
        again = word_shares(store, ["riley", "invoice"], **SCOPE)
        recounted = sum("MATCH" in s for s in statements)
        store.save_engram(_memory("Riley again."))
        statements.clear()
        after = word_shares(store, ["riley"], **SCOPE)
        refreshed = sum("MATCH" in s for s in statements)
    finally:
        store._get_conn().set_trace_callback(None)
        store.close()

    assert first == {"riley": 30 / 110, "invoice": 80 / 110, "absent": 0.0}
    assert counted == 3
    assert again == {"riley": 30 / 110, "invoice": 80 / 110} and recounted == 0
    assert after == {"riley": 31 / 111} and refreshed == 1
    assert fts.common_words(store, ["Riley", "invoice", "absent"], **SCOPE) == {"riley", "invoice"}


# ── The cue: distinctive only below the cut ──

_MESSAGE = "Riley asked whether the lighthouse log needs a brass lock"


def test_the_cue_does_not_count_a_common_word_as_distinctive(tmp_path):
    """Words alone (no answerer): a memory must share two distinctive words.
    One sharing "Riley" (34% of the memories) and "lighthouse" shares one."""
    riley_and_one = _memory("Riley keeps the lighthouse journal.")
    three = _memory("A brass lock for the lighthouse door.")
    db = _store(tmp_path / "memory.db", [riley_and_one, three, *_riley_fillers(40), *_invoices(79)],
                meaning_on=False)
    store = ReadOnlyEngramStore(str(db))
    try:
        offered = [line["id"] for line in cue_memories(store, None, _MESSAGE, **SCOPE)]
    finally:
        store.close()

    assert three.id in offered
    assert riley_and_one.id not in offered


# The message names two lighthouse words; the memory names two, and five of the
# harbour: cosine 0.37, over recall's floor (0.35) and under the cue's (0.40).
# The one word they share is "shutters".
_CUE_MESSAGE = "The lighthouse keeper asked whether the storm shutters close tonight in the fog"
_WORD_PATH = "Beacon lamp shutters: ferry pier boat timetable harbour."


def _shutters(count: int) -> list[Engram]:
    return [_memory(f"The shutters in room {i} were painted green.") for i in range(count)]


def _cue(db: Path, **kwargs) -> list[str]:
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        return [line["id"] for line in cue_memories(store, index, _CUE_MESSAGE, **SCOPE, **kwargs)]
    finally:
        index.close()
        store.close()


def test_a_common_shared_word_does_not_open_the_word_path(tmp_path, meaning):
    """"shutters" is in 40 of 125 memories (32%): not distinctive, so a memory
    at cosine 0.37 is under the floor with no word path to let it in."""
    near = _memory(_WORD_PATH)
    db = _store(tmp_path / "memory.db", [near, *_shutters(39), *_invoices(85)])
    assert 0.35 < cosine(_CUE_MESSAGE, _WORD_PATH) < 0.40

    assert near.id not in _cue(db)
    assert near.id in _cue(db, floor=0.35), "it is in the pool: meaning found it"


def test_the_word_path_needs_a_word_under_its_own_cut(tmp_path, meaning):
    """The word path needs a shared word in at most 2% of the live memories.
    "shutters" in 8 of 125 (6.4%) is distinctive but not that rare: the path
    opens at a cut of 8%, not at 5% or 2%. In 2 of 125 (1.6%), it opens."""
    from mnemos import cue

    near = _memory(_WORD_PATH)
    db = _store(tmp_path / "six.db", [near, *_shutters(7), *_invoices(117)])
    rare = _memory(_WORD_PATH)
    rare_db = _store(tmp_path / "one.db", [rare, *_shutters(1), *_invoices(123)])

    assert cue.CUE_WORD_CUT == 0.02
    assert near.id not in _cue(db)
    assert near.id in _cue(db, word_cut=0.08)
    assert near.id not in _cue(db, word_cut=0.05)
    assert rare.id in _cue(rare_db)


# ── Length fairness on meaning ──

_LONG = " ".join(f"The lighthouse lamp keeper climbed step {i}." for i in range(1, 41))
_SHORT = "Lighthouse lamp keeper by the harbour."


def _passage_store(db: Path, *texts: str) -> list[Engram]:
    """Memories of ``texts``, each cut into passages by the fake model."""
    memories = [_memory(text) for text in texts]
    store = EngramStore(str(db))
    for engram in memories:
        store.save_engram(engram)
    store.close()
    index = EmbeddingIndex(db_path=str(db))
    index.index_passages([(engram.id, engram.content) for engram in memories])
    index.close()
    return memories


def test_a_long_item_is_ranked_by_its_best_passage_less_lambda_ln_passages(tmp_path, meaning):
    """Each of the long item's passages is as close to "beacon" as can be; the
    short one is a little less close, with one passage. At λ = 0 the long one
    leads; at λ = 0.03 its best less 0.03 · ln(its passages) puts it second,
    and the top one taken is the short one. The numbers stay similarities."""
    db = tmp_path / "memory.db"
    long, short = _passage_store(db, _LONG, _SHORT)
    count = len(passages(_LONG))
    assert count > 5 and len(passages(_SHORT)) == 1
    ids = [long.id, short.id]
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        plain = index.search_candidates("beacon", ids, k=2, length_penalty=0)
        fair = index.search_candidates("beacon", ids, k=2, length_penalty=0.03)
        top = index.search_candidates("beacon", ids, k=1, length_penalty=0.03)
    finally:
        index.close()

    assert [item for item, _ in plain] == [long.id, short.id]
    assert dict(plain)[long.id] - 0.03 * math.log(count) < dict(plain)[short.id]
    assert [item for item, _ in fair] == [short.id, long.id]
    assert dict(fair) == dict(plain)
    assert [item for item, _ in top] == [short.id]

    assert [r.engram.id for r in _retrieve(db, "beacon", length_penalty=0)] == [long.id, short.id]
    reordered = _retrieve(db, "beacon", length_penalty=0.03)
    assert [r.engram.id for r in reordered] == [short.id, long.id]
    assert reordered[1].score_breakdown["similarity"] == dict(plain)[long.id]


# Forty sentences, each naming one lighthouse word and two of the harbour, and
# sharing no word with the question or the message below.
_PIERS = " ".join(f"The lighthouse at pier {i} guides the ferry." for i in range(1, 41))
_QUESTION_NEAR_THE_FLOOR = "talk brief lamp beacon keeper"  # cosine 0.37: recall's floor is 0.35
_MESSAGE_NEAR_THE_FLOOR = "Tell me about the beacon keeper and the lamp tonight"  # 0.45: the cue's is 0.40


def test_lambda_reorders_but_never_pushes_under_a_floor(tmp_path, meaning):
    """Its best passage clears recall's floor and the cue's; its best less
    λ · ln(its passages), at λ = 0.03, would clear neither. Recall still
    finds it by meaning and the cue still offers it: a floor reads the best
    similarity, and λ only orders."""
    db = tmp_path / "memory.db"
    (piers,) = _passage_store(db, _PIERS)
    count = len(passages(_PIERS))
    near_recall = cosine(_QUESTION_NEAR_THE_FLOOR, "The lighthouse at pier 1 guides the ferry.")
    near_cue = cosine(_MESSAGE_NEAR_THE_FLOOR, "The lighthouse at pier 1 guides the ferry.")
    assert near_recall - 0.03 * math.log(count) < 0.35 <= near_recall
    assert near_cue - 0.03 * math.log(count) < 0.40 <= near_cue

    found = _retrieve(db, _QUESTION_NEAR_THE_FLOOR, length_penalty=0.03)
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        lines = cue_memories(store, index, _MESSAGE_NEAR_THE_FLOOR, length_penalty=0.03, **SCOPE)
    finally:
        index.close()
        store.close()

    assert [(r.engram.id, r.retrieval_path) for r in found] == [(piers.id, "embedding")]
    assert found[0].score_breakdown["similarity"] >= 0.35
    assert [line["id"] for line in lines] == [piers.id]
    assert lines[0]["similarity"] >= 0.40


def test_lambda_is_two_hundredths(tmp_path, meaning):
    assert ei.LENGTH_PENALTY == 0.02


# ── What the cut must not reach ──

# Memories that name the harbour and mention Riley: nothing like the question
# "Riley" in meaning (a name alone means nothing to the fake model), so only
# words can find what the question is after.
def _riley_at_the_harbour(count: int) -> list[Engram]:
    return [_memory(f"Riley moored the ferry at pier {i}.") for i in range(count)]


def _timetables(count: int) -> list[Engram]:
    return [_memory(f"The ferry timetable for day {i}.") for i in range(count)]


def test_a_shared_store_is_searched_by_every_word(tmp_path, meaning):
    """"Riley" is in 40 of 120 memories in this scope, so this scope's search
    leaves it out. A memory another agent shared matches only "Riley": a
    share of this scope says nothing about that store, and meaning never
    searches it, so it is searched by every word and found."""
    db = _store(tmp_path / "memory.db", [*_riley_at_the_harbour(40), *_timetables(80)])
    shared_db = tmp_path / "shared.db"
    shared = Engram(content="Riley's glossary.", kind="semantic", owner_agent_id="orla",
                    person_id="riley", project_scope="demo", visibility="shared")
    other = EngramStore(str(shared_db))
    other.save_engram(shared)
    other.close()

    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    others = EngramStore(str(shared_db))
    try:
        retriever = ReactiveRetriever(store, embedding_index=index, shared_store=others,
                                      reconsolidation_enabled=False)
        assert retriever.search_terms("Riley", **SCOPE) == ([], ["Riley"])
        found = retriever.retrieve("Riley", reconsolidate_results=False, **SCOPE)
    finally:
        others.close()
        index.close()
        store.close()

    assert [r.engram.id for r in found] == [shared.id]


def test_a_memory_meaning_cannot_find_is_searched_by_every_word(tmp_path, meaning):
    """Two memories hold only "Riley", which 40 of 122 memories share: one was
    never indexed, one was indexed by another model. Meaning can't find
    either, so the cut doesn't hold for them: each is found by its words.
    The ones meaning can find, sharing only "Riley", still aren't."""
    fillers = _riley_at_the_harbour(40)
    db = _store(tmp_path / "memory.db", [*fillers, *_timetables(80)])
    unindexed = _memory("Riley, again.")
    elsewhere = _memory("Riley, once more.")
    store = EngramStore(str(db))
    store.save_engram(unindexed)
    store.save_engram(elsewhere)
    store._get_conn().execute(
        "INSERT INTO embeddings (engram_id, embedding, model_name, dims) VALUES (?, ?, ?, ?)",
        (elsewhere.id, b"\x00" * 16, "another-model", 4),
    )
    store._get_conn().commit()
    store.close()

    found = _retrieve(db, "Riley", max_results=None)

    by_words = {r.engram.id for r in found if r.retrieval_path == "fts"}
    assert {unindexed.id, elsewhere.id} <= by_words, [r.engram.content for r in found]
    assert not by_words & {f.id for f in fillers}


class _NoQueryVector(_ConceptEmbedder):
    """A backend that indexed everything, then can't embed a cue: a network
    that times out, a model that won't load."""

    def embed(self, text):
        raise TimeoutError("the embedding service did not answer")


class _EmptyQueryVector(_ConceptEmbedder):
    def embed(self, text):
        return None


@pytest.mark.parametrize("failing", [_NoQueryVector, _EmptyQueryVector])
def test_when_the_cue_cannot_be_embedded_nothing_is_cut(tmp_path, meaning, monkeypatch, failing):
    """Every memory has a vector, but the cue's own embedding fails, so
    meaning doesn't run for it. "Riley", in 41 of 121 memories, is searched
    all the same, and the memory only it matches is found by its words."""
    only_riley = _memory("Riley, at last.")
    db = _store(tmp_path / "memory.db", [only_riley, *_riley_at_the_harbour(40), *_timetables(80)])
    monkeypatch.setattr(ei, "_LocalEmbedder", failing)

    found = _retrieve(db, "Riley", max_results=None)

    assert only_riley.id in {r.engram.id for r in found if r.retrieval_path == "fts"}


@pytest.mark.parametrize("failing", [_NoQueryVector, _EmptyQueryVector])
def test_when_the_message_cannot_be_embedded_the_cue_answers_from_words(
    tmp_path, meaning, monkeypatch, failing,
):
    """The same for the cue: its message can't be embedded, so it answers
    as it does without meaning, from words alone (two distinctive words
    shared), instead of offering nothing."""
    lock = _memory("A brass lock for the harbour gate.")
    db = _store(tmp_path / "memory.db", [lock, *_riley_at_the_harbour(40), *_timetables(80)])
    monkeypatch.setattr(ei, "_LocalEmbedder", failing)
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        lines = cue_memories(store, index, "Riley asked whether the brass lock is fixed", **SCOPE)
    finally:
        index.close()
        store.close()

    assert [line["id"] for line in lines] == [lock.id]
    assert lines[0]["similarity"] is None and lines[0]["shared"] == ["brass", "lock"]


def test_a_lesson_without_its_own_vector_is_searched_by_every_word(tmp_path, meaning):
    """Three memories whose words are about the harbour and whose lessons
    name Riley, who is in 40 of 123 memories. Each memory's words have a
    vector. Meaning can find only one lesson by the lesson itself: one was
    written after its memory was indexed, one was rewritten since. Those two
    are found by the common word in their lessons; the third, whose lesson
    meaning can find, is left to meaning."""
    unindexed = _memory("The ferry timetable changed at pier 7.",
                        impact="Riley wants the times written on the board.", impact_source="agent")
    rewritten = _memory("The harbour boat log for pier 9.",
                        impact="Keep the boat log dry.", impact_source="agent")
    current = _memory("The ferry ropes at pier 3.",
                      impact="Riley checks the ropes before a storm.", impact_source="agent")
    db = _store(tmp_path / "memory.db", [unindexed, *_riley_at_the_harbour(40), *_timetables(80)])
    store = EngramStore(str(db))
    for engram in (rewritten, current):
        store.save_engram(engram)
    store.close()
    index = EmbeddingIndex(db_path=str(db))
    index.index_passages(
        [(rewritten.id, rewritten.content), (current.id, current.content)],
        lessons={rewritten.id: rewritten.impact, current.id: current.impact},
    )
    index.close()
    store = EngramStore(str(db))
    rewritten.impact = "Riley keeps the boat log now."
    store.save_engram(rewritten)
    store.close()

    found = _retrieve(db, "Riley", max_results=None)

    by_words = {r.engram.id for r in found if r.retrieval_path == "fts"}
    assert {unindexed.id, rewritten.id} <= by_words, [r.engram.content for r in found]
    assert current.id not in by_words


# "tidewater" is in the lessons of 40 of the 120 memories below and in none of
# their words: the full-text index holds no lesson.
_TIDEWATER_MESSAGE = "The lighthouse keeper asked whether the tidewater gauge reads high tonight"


def _lesson_store(db: Path, *extra: Engram) -> list[Engram]:
    """120 memories about the harbour, 40 of whose lessons name the
    tidewater, and ``extra``: each memory with its words and its lesson cut
    into passages, so meaning can find every one."""
    tide = [_memory(f"The ferry waited at pier {i}.", impact="Check the tidewater first.",
                    impact_source="agent") for i in range(40)]
    memories = [*tide, *_timetables(80), *extra]
    store = EngramStore(str(db))
    for engram in memories:
        store.save_engram(engram)
    lessons = store.live_memory_lessons(**SCOPE)
    store.close()
    index = EmbeddingIndex(db_path=str(db))
    index.index_passages([(engram.id, engram.content) for engram in memories], lessons=lessons)
    index.close()
    return tide


def test_a_word_most_lessons_hold_is_cut_from_the_lesson_ranking(tmp_path, meaning):
    """A word's share counts the memories holding it in their words or in
    their lesson, each once. "tidewater", in the lessons of 40 of 120
    memories and in no memory's words, is common, and no memory is found by
    it."""
    db = tmp_path / "memory.db"
    tide = _lesson_store(db)

    found = _retrieve(db, "tidewater", max_results=None)

    assert not {r.engram.id for r in found if r.retrieval_path == "fts"} & {t.id for t in tide}
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        assert word_shares(store, ["tidewater"], **SCOPE) == {"tidewater": 40 / 120}
        retriever = ReactiveRetriever(store, embedding_index=index)
        assert retriever.search_terms("tidewater", **SCOPE) == ([], ["tidewater"])
    finally:
        index.close()
        store.close()


def test_a_word_most_lessons_hold_does_not_open_the_word_path(tmp_path, meaning):
    """A memory at cosine 0.37 with the message, under the cue's floor,
    shares one word with it, "tidewater", in its lesson. Common by the
    lessons, the word can't open the path meant for rare ones."""
    near = _memory(_WORD_PATH, impact="Read the tidewater first.", impact_source="agent")
    db = tmp_path / "memory.db"
    _lesson_store(db, near)
    assert 0.35 < cosine(_TIDEWATER_MESSAGE, _WORD_PATH) < 0.40
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        lines = cue_memories(store, index, _TIDEWATER_MESSAGE, **SCOPE)
        in_the_pool = cue_memories(store, index, _TIDEWATER_MESSAGE, floor=0.35, **SCOPE)
    finally:
        index.close()
        store.close()

    assert near.id not in [line["id"] for line in lines]
    assert near.id in [line["id"] for line in in_the_pool], "meaning found it: it is in the pool"


def test_a_correction_that_swaps_words_refreshes_the_shares(tmp_path):
    """"Riley" is in 30 of 110 memories (27%). Four corrections put "Casey"
    in its place, leaving 110 live memories: 26 of 110 (24%), under the
    cut. Counted afresh, though the number of live memories didn't change."""
    riley = _riley_fillers(30)
    db = _store(tmp_path / "memory.db", [*riley, *_invoices(80)], meaning_on=False)
    store = EngramStore(str(db))
    try:
        before = word_shares(store, ["riley"], **SCOPE)
        was_common = fts.common_words(store, ["Riley"], **SCOPE)
        for engram in riley[:4]:
            engram.content = engram.content.replace("Riley", "Casey")
            store.save_engram(engram)
        after = word_shares(store, ["riley"], **SCOPE)
        is_common = fts.common_words(store, ["Riley"], **SCOPE)
    finally:
        store.close()

    assert before == {"riley": 30 / 110} and was_common == {"riley"}
    assert after == {"riley": 26 / 110} and is_common == set()


_SWAP = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for engram_id in sys.argv[2:]:
    words = conn.execute("SELECT content FROM engrams WHERE id = ?", (engram_id,)).fetchone()[0]
    words = words.replace("Riley", "Casey")
    conn.execute("UPDATE engrams SET content = ? WHERE id = ?", (words, engram_id))
    conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram_id,))
    conn.execute("INSERT INTO engrams_fts (id, content) VALUES (?, ?)", (engram_id, words))
conn.commit()
"""


def test_another_process_swapping_words_refreshes_the_shares(tmp_path):
    """Another process corrects four memories on the same file, "Riley" to
    "Casey": 30 of 110 becomes 26 of 110, and this process counts afresh."""
    riley = _riley_fillers(30)
    db = _store(tmp_path / "memory.db", [*riley, *_invoices(80)], meaning_on=False)
    store = EngramStore(str(db))
    try:
        before = word_shares(store, ["riley"], **SCOPE)
        subprocess.run([sys.executable, "-c", _SWAP, str(db), *(e.id for e in riley[:4])], check=True)
        after = word_shares(store, ["riley"], **SCOPE)
        is_common = fts.common_words(store, ["Riley"], **SCOPE)
    finally:
        store.close()

    assert before == {"riley": 30 / 110}
    assert after == {"riley": 26 / 110} and is_common == set()


# ── An answerer on the old gate is not trusted ──


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME reached by a path short enough for a unix socket under it."""
    real = tmp_path / "home"
    real.mkdir()
    link = Path("/tmp") / f"mnf-{uuid.uuid4().hex[:10]}"
    os.symlink(real, link)
    monkeypatch.setenv("HOME", str(link))
    yield link
    os.unlink(link)


def test_the_hook_does_not_trust_an_answerer_on_the_old_gate(tmp_path, home):
    """A server still running the code before this change speaks protocol 1,
    whose gate let common words in. The hook answers from words instead."""
    from mnemos import cue

    db = _store(tmp_path / "memory.db", [_memory("A brass lock for the lighthouse door.")],
                meaning_on=False)
    key = cue.scope_key(str(db), **SCOPE)
    path = cue.answerer_path(os.getpid(), key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(5)
    reply = json.dumps({"v": 1, "ok": True, "meaning": True, "lines": [
        {"id": "engram_FROMTHEOLDGATE", "text": "x", "date": "2026-09-29", "key": "k"}]}).encode() + b"\n"

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            conn.sendall(reply)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    payload = {"session_id": "session-one", "hook_event_name": "UserPromptSubmit",
               "prompt": _MESSAGE, "cwd": "/tmp"}
    started = time.monotonic()
    block = cue.prompt_hook(payload, db_path=str(db), environ={"CLAUDE_PID": str(os.getpid())},
                            **SCOPE)
    thread.join(5)

    assert "FROMTHEOLDGATE" not in block
    assert time.monotonic() - started < 2
    assert cue.CUE_PROTOCOL == 2
