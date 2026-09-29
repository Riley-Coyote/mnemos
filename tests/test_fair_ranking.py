"""Fair ranking (WP-R08c): common words, and long items, no longer win.

Recall fuses its words list and its meaning list by reciprocal rank, which
keeps each list's order and nothing of bm25's weight. So a match on a word
most memories hold counted as much as a match on a rare one: on a copy of the
live store "Riley" is in 268 of the 464 live memories of its scope, and each
of the five memories recall returned for "how does Riley like to be told about
mistakes" matched by words on that word alone (one was 18th by meaning).
With meaning to decide, a word in more than 8% of the live memories in scope
is now left out of the words lists, and the cue counts a shared word as
distinctive only below that cut (its word path needs a rarer one still). An
item's meaning score can also pay for its length: its best passage less
λ · ln(its passages).

None of these tests needs sentence-transformers or torch: a fake model gives
each text a vector by the concepts it names, so every cosine is exact.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
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
    """"Riley" is in 40 of 130 memories and "lamp" in 30. Asked "Riley's
    lamp", recall has no words list: what comes back came by meaning, and a
    memory that only shares a common word with the cue does not come back."""
    lamps = [_memory(f"The lamp in cabin {i} was lit at dusk.") for i in range(30)]
    fillers = _riley_fillers(40)
    db = _store(tmp_path / "memory.db", [*lamps, *fillers, *_invoices(60)])

    results = _retrieve(db, "Riley's lamp", max_results=None)

    assert results, "meaning found the lamps"
    assert {r.retrieval_path for r in results} == {"embedding"}
    assert not {r.engram.id for r in results} & {f.id for f in fillers}
    assert all("words_rank" not in r.score_breakdown for r in results)


def test_a_small_store_keeps_every_word(tmp_path, meaning):
    """Below a hundred live memories a share says little: 8% of 30 memories is
    under three. Nothing is cut, and a word half of them hold is searched."""
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
    """"shutters" is in 25 of 125 memories (20%): not distinctive, so a memory
    at cosine 0.37 is under the floor with no word path to let it in."""
    near = _memory(_WORD_PATH)
    db = _store(tmp_path / "memory.db", [near, *_shutters(24), *_invoices(100)])
    assert 0.35 < cosine(_CUE_MESSAGE, _WORD_PATH) < 0.40

    assert near.id not in _cue(db)
    assert near.id in _cue(db, floor=0.35), "it is in the pool: meaning found it"


def test_the_word_path_needs_a_word_under_its_own_cut(tmp_path, meaning):
    """"shutters" is in 8 of 125 memories (6.4%): distinctive, under the
    shipped word-path cut (8%, as close to the old gate as the cut allows),
    but not under 5% or 2%."""
    from mnemos import cue

    near = _memory(_WORD_PATH)
    db = _store(tmp_path / "memory.db", [near, *_shutters(7), *_invoices(117)])

    assert cue.CUE_WORD_CUT == 0.08
    assert near.id in _cue(db)
    assert near.id in _cue(db, word_cut=0.08)
    assert near.id not in _cue(db, word_cut=0.05)
    assert near.id not in _cue(db, word_cut=0.02)


# ── Length fairness on meaning ──

_LONG = " ".join(f"The lighthouse lamp keeper climbed step {i}." for i in range(1, 41))
_SHORT = "Lighthouse lamp keeper by the harbour."


def test_a_long_item_pays_for_its_passages_when_lambda_is_set(tmp_path, meaning):
    """Each of the long item's passages is as close to "beacon" as can be;
    the short one is a little less close, with one passage. At λ = 0 (the
    default) the long one leads; at λ = 0.03 it scores its best less
    0.03 · ln(its passages), and the floor and the order use that score."""
    long, short = _memory(_LONG), _memory(_SHORT)
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    for engram in (long, short):
        store.save_engram(engram)
    store.close()
    index = EmbeddingIndex(db_path=str(db))
    index.index_passages([(long.id, _LONG), (short.id, _SHORT)])
    count = len(passages(_LONG))
    assert count > 5 and len(passages(_SHORT)) == 1

    plain = dict(index.search_candidates("beacon", [long.id, short.id], k=2))
    fair = index.search_candidates("beacon", [long.id, short.id], k=2, length_penalty=0.03)
    floored = index.search_candidates("beacon", [long.id, short.id], k=2, floor=0.94,
                                      length_penalty=0.03)
    index.close()

    assert plain[long.id] > plain[short.id]
    assert [item for item, _ in fair] == [short.id, long.id]
    assert dict(fair)[long.id] == pytest.approx(plain[long.id] - 0.03 * math.log(count), abs=1e-3)
    assert dict(fair)[short.id] == pytest.approx(plain[short.id], abs=1e-4)
    assert [item for item, _ in floored] == [short.id]

    assert [r.engram.id for r in _retrieve(db, "beacon")] == [long.id, short.id]
    assert [r.engram.id for r in _retrieve(db, "beacon", length_penalty=0.03)] == [short.id, long.id]


def test_lambda_is_zero_unless_chosen(tmp_path, meaning):
    assert ei.LENGTH_PENALTY == 0.0


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
