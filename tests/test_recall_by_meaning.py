"""Recall finds by meaning (WP-R08).

What recall did, on copies of the live store (the review of 2026-09-26 and
the lab's first baseline, 2026-09-27):

* the meaning search took its top 20 over all 3,746 stored vectors before it
  checked scope and state, so across twelve cues 14 of 240 meaning hits
  survived and 1 of 60 results arrived by meaning;
* every reached memory re-fired on every hop with no limit on its links, so
  the best keyword match for a real question finished 12th behind hubs;
* results were cut to the requested count before a filter dropped some, and
  a match by meaning alone was dropped unless it scored 1.35;
* three lists of common words disagreed;
* query recall never returned a handoff (0 of 28 facts that lived only in
  handoffs), and a superseded one could not be reached at all;
* a row showed 180 characters, and 38 of 69 recall calls fetched by id next;
* loading the cached embedding model could hang on a network call.

None of these tests needs sentence-transformers or torch: a fake model gives
each text a vector by the concepts it names, so meaning is exactly controlled.
"""

from __future__ import annotations

import io
import json
import math
import re
import sqlite3
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import mnemos.simple_runtime as simple_runtime
import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.embedding_index import EmbeddingIndex
from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
MODEL = "claude-opus-5-5"


# ── A model whose meaning is controlled ──

_CONCEPTS = (
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
    {"garden", "marigolds", "greenhouse", "bloom"},
    {"chapter", "reading", "bookshop", "novel"},
)


def concept_vector(text: str) -> list[float]:
    """Similar meaning without shared words: "beacon" is near "lighthouse"."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    raw = [float(len(words & group)) for group in _CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


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
    return Engram(content=content, kind="semantic", owner_agent_id="nova",
                  person_id=fields.pop("person_id", "riley"), project_scope="demo", **fields)


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _link(store: EngramStore, source: Engram, target: Engram,
          relation: str = ConnectionRelation.SUPPORTS) -> None:
    store.save_connection(source.id, Connection(target_id=target.id, relation=relation,
                                                strength=1.0))


def _scores(results) -> dict[str, float]:
    return {r.engram.id: r.score for r in results}


def _rows(out: str) -> list[str]:
    return [line.split("] ", 1)[1] for line in out.splitlines() if line.startswith("- [")]


def _handoff_id(said: str) -> str:
    return said.split("Handoff ID: ", 1)[1].splitlines()[0]


def _passages(db, item_id: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT part, text_hash FROM passage_vectors WHERE item_id = ? ORDER BY part",
            (item_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ── 1. The meaning search runs over live memories in scope before its top ──


def test_meaning_search_scores_only_live_memories_in_scope_before_its_top(tmp_path, meaning):
    """Twenty-five memories elsewhere, and three this scope forgot, match the
    cue better than the one this scope holds. The top 20 over every vector
    left nothing in scope; now only what this scope may be shown is scored."""
    db = str(tmp_path / "memory.db")
    store = EngramStore(db)
    index = EmbeddingIndex(db_path=db)
    for i in range(25):
        decoy = _memory(f"beacon lamp number {i}", person_id="someone-else")
        store.save_engram(decoy)
        index.index_engram(decoy.id, decoy.content)
    for i in range(3):
        faded = _memory(f"the beacon lamp {i}", state="archived")
        store.save_engram(faded)
        index.index_engram(faded.id, faded.content)
    answer = _memory("a beacon on the pier by the ferry")
    store.save_engram(answer)
    index.index_engram(answer.id, answer.content)

    results = ReactiveRetriever(store, embedding_index=index,
                                reconsolidation_enabled=False).retrieve("lighthouse", **SCOPE)

    assert [r.engram.id for r in results] == [answer.id], [r.engram.content for r in results]
    assert results[0].retrieval_path == "embedding"


def test_search_candidates_scores_only_the_candidates(tmp_path, meaning):
    db = str(tmp_path / "memory.db")
    EngramStore(db).close()
    index = EmbeddingIndex(db_path=db)
    index.index_engram("engram_a", "the beacon lamp")
    index.index_engram("engram_b", "the beacon lamp")
    index.index_engram("engram_c", "a garden in bloom")

    assert [i for i, _ in index.search_candidates("lighthouse", {"engram_b", "engram_c"})] == [
        "engram_b", "engram_c",
    ]
    assert [i for i, _ in index.search_candidates("lighthouse", {"engram_b", "engram_c"},
                                                  floor=0.3)] == ["engram_b"]


# ── 2. Words and meaning are fused by reciprocal rank ──


def test_fusion_is_reciprocal_rank_with_polyphonics_weights():
    from mnemos.retrieval import reactive

    assert (reactive.WORDS_WEIGHT, reactive.MEANING_WEIGHT, reactive.FUSION_K) == (0.3, 0.5, 60)
    fused = reactive.fuse([(["a", "b"], 0.3), (["b", "c"], 0.5)])
    assert fused == pytest.approx({"a": 0.3 / 61, "b": 0.3 / 62 + 0.5 / 61, "c": 0.5 / 62})


def test_a_seed_starts_at_its_fused_score_and_neither_kind_is_locked_out(tmp_path, meaning):
    """One memory holds the cue's word and its meaning, one only its meaning,
    one only its word. The first leads; the others both come back, each where
    its ranks put it, relative to the best."""
    db = str(tmp_path / "memory.db")
    store = EngramStore(db)
    index = EmbeddingIndex(db_path=db)
    both = _memory("the lighthouse beacon")
    meaning_only = _memory("the beacon lamp keeper")
    words_only = _memory("lighthouse ferry timetable pier harbour boat")
    for engram in (both, meaning_only, words_only):
        store.save_engram(engram)
        index.index_engram(engram.id, engram.content)

    results = ReactiveRetriever(store, embedding_index=index,
                                reconsolidation_enabled=False).retrieve("lighthouse", **SCOPE)
    score = _scores(results)

    assert [r.engram.id for r in results] == [both.id, meaning_only.id, words_only.id]
    best = 0.3 / 61 + 0.5 / 61  # first by words and first by meaning
    assert score[both.id] == 1.0
    assert score[meaning_only.id] == pytest.approx((0.5 / 62) / best, abs=1e-3)
    assert score[words_only.id] == pytest.approx((0.3 / 62) / best, abs=1e-3)
    paths = {r.engram.id: r.retrieval_path for r in results}
    assert paths == {both.id: "fts", meaning_only.id: "embedding", words_only.id: "fts"}
    assert (results[0].score_breakdown["words_rank"], results[0].score_breakdown["meaning_rank"]) == (1, 1)


# ── 3. Resonance: from newly reached memories, divided by links, two hops ──


def test_a_memory_passes_activation_on_once(store):
    seed = _memory("lighthouse keeper")
    near = _memory("a note that shares no word with the cue")
    far = _memory("another quiet note")
    for engram in (seed, near, far):
        store.save_engram(engram)
    _link(store, seed, near)
    _link(store, near, far)

    score = _scores(ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "lighthouse keeper", **SCOPE))

    assert score[seed.id] == 1.0
    assert score[near.id] == pytest.approx(0.5), "the seed fired again on a later hop"
    assert score[far.id] == pytest.approx(0.125)


def test_a_memory_divides_what_it_sends_by_its_links(store):
    hub = _memory("lighthouse keeper")
    neighbours = [_memory(f"quiet neighbour number {i}") for i in range(4)]
    for engram in (hub, *neighbours):
        store.save_engram(engram)
    for neighbour in neighbours:
        _link(store, hub, neighbour)

    score = _scores(ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "lighthouse keeper", **SCOPE))

    for neighbour in neighbours:
        assert score[neighbour.id] == pytest.approx(0.5 / 4)


def test_resonance_stops_after_two_hops(store):
    seeds = [_memory(f"lighthouse keeper log {i}") for i in range(10)]
    first, second, third = (_memory(f"{word} note that names nothing asked")
                            for word in ("first", "second", "third"))
    for engram in (*seeds, first, second, third):
        store.save_engram(engram)
    for seed in seeds:
        _link(store, seed, first)
    _link(store, first, second)
    _link(store, second, third)

    found = {r.engram.id for r in ReactiveRetriever(store, reconsolidation_enabled=False)
             .retrieve("lighthouse keeper", max_results=None, **SCOPE)}

    assert {first.id, second.id} <= found
    assert third.id not in found, "activation went a third hop"


def test_the_best_match_is_not_outshone_by_well_linked_memories(store):
    """On a copy of the live store the best keyword match for "how does Riley
    like to be told about mistakes" finished 12th: memories matching less well,
    with 20 to 32 links each, gathered activation from one another every hop."""
    answer = _memory("lighthouse keeper")
    hubs = [_memory(f"the keeper of record number {i}") for i in range(6)]
    fillers = [_memory(f"filler memory {i}") for i in range(20)]
    for engram in (answer, *hubs, *fillers):
        store.save_engram(engram)
    for hub in hubs:
        for other in (*hubs, *fillers):
            if other is not hub:
                _link(store, hub, other, ConnectionRelation.CO_ACTIVATED)

    results = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "lighthouse keeper", **SCOPE)

    assert results[0].engram.id == answer.id, [(r.engram.content, r.score) for r in results[:3]]


def test_quiet_memories_neither_relay_nor_receive(store):
    seed = _memory("lighthouse keeper")
    dormant = _memory("a dormant note", state="dormant")
    beyond = _memory("reached only through the dormant note")
    quiet_seed = _memory("lighthouse keeper old log", state="dormant")
    past_quiet = _memory("reached only from the quiet seed")
    archived = _memory("an archived note", state="archived")
    for engram in (seed, dormant, beyond, quiet_seed, past_quiet, archived):
        store.save_engram(engram)
    _link(store, seed, dormant)
    _link(store, dormant, beyond)
    _link(store, quiet_seed, past_quiet)
    _link(store, seed, archived)

    found = {r.engram.id for r in ReactiveRetriever(store, reconsolidation_enabled=False)
             .retrieve("lighthouse keeper", max_results=None, **SCOPE)}

    assert quiet_seed.id in found, "the cue brings back a quiet memory it matches"
    assert not found & {dormant.id, beyond.id, past_quiet.id, archived.id}


# ── 4. Filter first, then cut ──


def test_a_caller_that_drops_a_result_still_gets_what_it_asked_for(store):
    for i in range(4):
        store.save_engram(_memory(f"lighthouse keeper entry {i}"))
    retriever = ReactiveRetriever(store, reconsolidation_enabled=False)
    everything = retriever.retrieve("lighthouse keeper", max_results=None, **SCOPE)
    dropped = everything[0].engram.id

    kept = retriever.retrieve("lighthouse keeper", max_results=2, **SCOPE,
                              keep=lambda result: result.engram.id != dropped)

    assert [r.engram.id for r in kept] == [r.engram.id for r in everything[1:3]]


def test_recall_returns_the_count_asked_for_when_matches_remain(tmp_path, meaning):
    """Asked for three, recall showed one: the retriever's top three held two
    matches by meaning alone, which the runtime then dropped."""
    rt = _runtime(tmp_path / "memory.db")
    rt.capture("lighthouse keeper")
    for i in range(2):
        rt.capture(f"lighthouse keeper with a longer note {i} about the weather, the tides "
                   "and the gulls over the point")
    rt.capture("beacon lamp")
    rt.capture("the beacon lamp")

    durable = rt.recall("lighthouse keeper", max_results=3).split("Durable memories:", 1)[-1]
    assert len(_rows(durable)) == 3, durable


# ── 5. One list of common words ──


def test_one_list_of_common_words():
    import mnemos.identity_diff as identity_diff
    from mnemos.store import fts

    assert not hasattr(simple_runtime, "_STOPWORDS")
    assert not hasattr(identity_diff, "_STOPWORDS")
    cue = "whatever notes about the ferry memory"
    assert fts.search_words(cue) == ["ferry"], "seeding searched a word the filters ignore"
    assert simple_runtime._named_terms(cue) == {"ferry"}
    assert identity_diff._tokens(cue) == {"ferry"}


# ── 6. A match by meaning alone comes back ──


def test_a_match_by_meaning_alone_comes_back_from_recall(tmp_path, meaning):
    rt = _runtime(tmp_path / "memory.db")
    rt.capture("The beacon on the point burns all night.")
    rt.capture("Marigolds bloom beside the greenhouse door every June.")

    out = rt.recall("lighthouse")

    assert "The beacon on the point burns all night." in out, out
    assert "Marigolds" not in out


# ── 7. Handoffs join recall ──


def test_recall_finds_a_handoff_by_its_words_and_says_whose_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")
    rt = _runtime(tmp_path / "memory.db")
    rt.introduce(agent_model=MODEL)
    rt.handoff("The harbour survey is half done; the east pier still needs measuring.",
               signed_as=MODEL)

    out = rt.recall("east pier survey")

    assert "Handoffs:" in out, out
    row = out.split("Handoffs:", 1)[1]
    assert "The harbour survey is half done" in row
    today = datetime.now(timezone.utc).date().isoformat()
    assert f"kind=handoff; left {today}: Yours (Opus 5.5)" in row


def test_an_older_handoff_is_found_and_says_a_newer_one_replaced_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")
    rt = _runtime(tmp_path / "memory.db")
    rt.handoff("First pass: the lighthouse lamp was rewired on Tuesday.", signed_as=MODEL)
    rt.handoff("Second pass: the ferry timetable moved to the kitchen drawer.", signed_as=MODEL)

    out = rt.recall("lighthouse lamp rewired")

    assert "the lighthouse lamp was rewired on Tuesday" in out, out
    assert "kind=older handoff, replaced by a newer one from its session" in out
    assert "ferry timetable" not in out


def test_a_handoff_is_found_by_its_meaning(tmp_path, meaning):
    rt = _runtime(tmp_path / "memory.db")
    handoff_id = _handoff_id(rt.handoff("Tonight the beacon on the point needs a new lamp."))
    assert _passages(tmp_path / "memory.db", handoff_id), "the handoff was not indexed as written"

    out = rt.recall("lighthouse")

    assert "Handoffs:" in out and "the beacon on the point" in out, out


def test_a_handoff_never_relays_and_never_becomes_a_memory(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt.capture("The lighthouse keeper logs every storm.")
    handoff_id = _handoff_id(rt.handoff("Next: ask the lighthouse keeper about the storm log."))

    def count(sql: str) -> int:
        return rt._store._get_conn().execute(sql, (handoff_id,) * sql.count("?")).fetchone()[0]

    memories = count("SELECT COUNT(*) FROM engrams WHERE id != ?")
    out = rt.recall("lighthouse keeper storm")

    assert "Handoffs:" in out and "ask the lighthouse keeper" in out, out
    assert count("SELECT COUNT(*) FROM engrams WHERE id != ?") == memories
    assert count("SELECT COUNT(*) FROM connections WHERE source_id = ? OR target_id = ?") == 0
    assert count("SELECT COUNT(*) FROM engrams WHERE content LIKE '%ask the lighthouse%' "
                 "AND id != ?") == 0


def test_a_forgotten_handoff_stays_gone(tmp_path):
    rt = _runtime(tmp_path / "memory.db")
    handoff_id = _handoff_id(rt.handoff("The lighthouse lamp was rewired on Tuesday."))
    rt.correct(correction="", target_id=handoff_id, action="forget")

    assert "rewired" not in rt.recall("lighthouse lamp rewired")


def test_recall_by_id_reaches_any_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")
    rt = _runtime(tmp_path / "memory.db")
    older = _handoff_id(rt.handoff("First pass: the lighthouse lamp was rewired on Tuesday."))
    rt.handoff("Second pass: the ferry timetable moved to the kitchen drawer.")

    out = rt.recall(older)

    assert "the lighthouse lamp was rewired on Tuesday" in out
    assert "no longer active" in out


def test_another_scopes_handoff_never_comes_back(tmp_path):
    db = tmp_path / "memory.db"
    _runtime(db).handoff("The lighthouse lamp was rewired on Tuesday.")
    elsewhere = MnemosRuntime(db_path=str(db), agent_id="nova", person_id="riley",
                              project_scope="other", use_dedicated_model=False)

    assert "rewired" not in elsewhere.recall("lighthouse lamp rewired")


# ── 8. A row is enough to use ──


def test_a_row_carries_up_to_300_characters_of_its_own_words(tmp_path):
    rt = _runtime(tmp_path / "memory.db")
    medium = ("The lighthouse keeper keeps the logbook in the tin box under the stairs, "
              "with the tide tables and the spare lamp wicks, and she writes in it at dusk "
              "and at dawn, whatever the weather, in pencil because ink runs in the damp air.")
    long = " ".join(f"lighthouse sentence {i} of a long account of the season." for i in range(30))
    rt.capture(medium, impact="What changed: the log is kept in two places now.")
    rt.capture(long)
    rt.capture("The lighthouse lens came from Paris.", context="Asked about its history.")

    rows = _rows(rt.recall("lighthouse", max_results=5))

    assert 180 < len(medium) <= 300
    assert medium in rows, "a 250-character memory was cut, or shown as its impact"
    cut = next(row for row in rows if row.startswith("lighthouse sentence 0"))
    assert cut.endswith("…") and len(cut) <= 300
    assert long.startswith(cut[:-1])
    assert "The lighthouse lens came from Paris." in rows, "a row carried the capture's context"


# ── 9. The model loads from this machine only ──


def _fake_sentence_transformers(monkeypatch, *, release: threading.Event, calls: list):
    """A SentenceTransformer that hangs, as a slow Hub did, unless told to use
    local files only, and then finds no model on disk."""
    module = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name, **kwargs):
            calls.append(kwargs)
            if not kwargs.get("local_files_only"):
                release.wait(60)  # the network hangs
                raise OSError("network timed out")
            raise OSError("the model is not in the local cache")

    module.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


def _finishes(call, seconds: float = 10.0) -> bool:
    done = threading.Event()

    def run():
        try:
            call()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds)


def _in_its_own_runtime(db, call):
    """``call(runtime)`` with a runtime of its own: a store's connection
    belongs to the thread that opened it."""
    def run():
        runtime = _runtime(db)
        try:
            call(runtime)
        finally:
            runtime.close()
    return run


def test_a_hanging_network_blocks_neither_recall_health_nor_doctor(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt.capture("The lighthouse lamp was rewired on Tuesday.")
    rt.close()
    release, calls = threading.Event(), []
    _fake_sentence_transformers(monkeypatch, release=release, calls=calls)
    try:
        assert _finishes(_in_its_own_runtime(db, lambda rt: rt.recall("lighthouse lamp"))), (
            "recall waited on the network"
        )
        assert _finishes(_in_its_own_runtime(db, lambda rt: rt.health())), (
            "health waited on the network"
        )
        assert _finishes(lambda: main(["doctor", "--db-path", str(db), "--agent-id", "nova",
                                       "--person-id", "riley", "--project-scope", "demo"])), (
            "doctor waited on the network"
        )
        rt = _runtime(db)
        assert "rewired on Tuesday" in rt.recall("lighthouse lamp"), "recall stopped finding by words"
        semantic = rt.semantic_status()
        rt.close()
        assert calls and all(call.get("local_files_only") for call in calls)
        assert semantic["active"] is False
        assert "mnemos embeddings download" in semantic["reason"]
    finally:
        release.set()


def test_an_older_sentence_transformers_loads_the_cached_copy_by_its_path(monkeypatch):
    """Before local_files_only, a model's cached copy is loaded by its path,
    which never asks the Hub."""
    loaded, asked = [], []
    module = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name_or_path):
            loaded.append(name_or_path)

    module.SentenceTransformer = SentenceTransformer
    hub = types.ModuleType("huggingface_hub")

    def snapshot_download(repo_id, **kwargs):
        asked.append((repo_id, kwargs))
        return f"/cache/{repo_id}"

    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    ei._load_local_model("all-MiniLM-L6-v2")

    assert asked == [("sentence-transformers/all-MiniLM-L6-v2", {"local_files_only": True})]
    assert loaded == ["/cache/sentence-transformers/all-MiniLM-L6-v2"]


def test_downloading_is_one_explicit_step(monkeypatch, capsys):
    calls = []
    module = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name, **kwargs):
            calls.append((name, kwargs))

    module.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)

    assert main(["embeddings", "download"]) == 0
    assert calls == [("all-MiniLM-L6-v2", {})], "the download step must be allowed the network"
    assert "Downloaded" in capsys.readouterr().out


# ── The passage index ──


def test_long_texts_are_cut_into_passages_the_model_reads_whole():
    assert ei.passages("") == []
    assert ei.passages("A short note.") == ["A short note."]
    text = "\n\n".join(" ".join(f"Sentence {p}.{s} runs on for a while." for s in range(12))
                       for p in range(6))
    parts = ei.passages(text)
    assert len(parts) > 1 and all(len(part) <= ei.PASSAGE_CHARS for part in parts)
    # Words repeat across passages since R08b (windows overlap, and the whole
    # text is kept too: test_passages_the_model_can_read.py), but none is cut.
    words = set(text.split())
    assert all(set(part.split()) <= words for part in parts), "a passage cut a word"
    assert parts[-1].endswith(text.split()[-1]), "the passages stopped before the end"
    assert len(ei.passages("word " * 40_000)) == ei.PASSAGE_LIMIT


def test_maintenance_indexes_what_is_missing_a_little_at_a_time(tmp_path, meaning, monkeypatch):
    """Lessons never had a vector (on the live store 176 live memories, all of
    them lessons, and all 259 handoffs), so nothing could find them by meaning."""
    monkeypatch.setattr(simple_runtime, "AUTO_INDEX_BUDGET", 3)
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt._ensure_init()
    lessons = [_memory(f"lesson {i}: check the beacon before the storm") for i in range(5)]
    for lesson in lessons:
        rt._store.save_engram(lesson)

    # (The cycle's own dream-journal note is waiting to be indexed too.)
    first = rt.maintain(auto=True)
    assert re.search(r"Recall index: 3 memories or handoffs made findable by meaning "
                     r"\(3 passages\); [1-9]\d* still waiting", first), first
    assert sum(bool(_passages(db, lesson.id)) for lesson in lessons) <= 3
    rest = rt.maintain()
    assert "Recall index:" in rest and "still waiting" not in rest, rest
    assert all(_passages(db, lesson.id) for lesson in lessons)
    assert "Recall index:" not in rt.maintain(), "indexed what already had passages"
    status = rt.semantic_status()
    assert status["memories_searchable"] == status["memories"] == 5
    assert "lesson 0: check the beacon" in rt.recall("lighthouse")


def test_a_handoff_whose_words_change_is_indexed_again(tmp_path, meaning):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    handoff_id = _handoff_id(rt.handoff("The beacon needs a new lamp."))
    before = _passages(db, handoff_id)
    rt.correct(correction="The ferry pier needs new boards.", target_id=handoff_id)
    rt.maintain()

    assert _passages(db, handoff_id) != before
    assert "ferry pier" in rt.recall("harbour")


def test_older_code_gives_nothing_passages(tmp_path, meaning):
    """How memory is indexed for meaning is a rule newer code may replace:
    code older than the store records the agent's words and indexes nothing."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    current = _handoff_id(rt.handoff("The beacon needs a new lamp."))
    assert _passages(db, current), "current code did not index its handoff"
    rt.close()
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'min_code_version'")
    conn.commit()
    conn.close()

    rt = _runtime(db)
    older = _handoff_id(rt.handoff("The ferry pier needs new boards."))
    rt.capture("The lighthouse keeper retired in May.")
    rt.maintain()

    assert _passages(db, older) == []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        indexed = {row[0] for row in conn.execute("SELECT DISTINCT item_id FROM passage_vectors")}
    finally:
        conn.close()
    assert indexed == {current}


def test_a_v14_store_gains_the_passage_table_after_a_verified_backup(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt.capture("The lighthouse lamp was rewired on Tuesday.")
    rt.close()
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE IF EXISTS passage_vectors")
    conn.execute("UPDATE meta SET value = '14' WHERE key = 'schema_version'")
    conn.execute("UPDATE meta SET value = '7' WHERE key = 'min_code_version'")
    conn.commit()
    conn.close()

    EngramStore(str(db)).close()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    assert "passage_vectors" in tables
    # Whatever the schema is now: this store was at 14, and it is migrated once,
    # after one verified recovery point named for the version it moved to.
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert SCHEMA_VERSION >= 15
    assert meta["min_code_version"] == str(MAINTENANCE_CODE_VERSION)
    assert MAINTENANCE_CODE_VERSION >= 8
    assert len(list((tmp_path / "backups").glob(f"memory.pre-v{SCHEMA_VERSION}-*.db"))) == 1


# ── Read back in another process ──

_RECALL = """
import sys
from mnemos.simple_runtime import MnemosRuntime

runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    print(runtime.recall(sys.argv[2]))
finally:
    runtime.close()
"""


def test_a_handoff_and_a_capture_are_recalled_in_another_process(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt.capture("The lighthouse keeper logs every storm in pencil.")
    rt.handoff("Next: read the lighthouse storm log with the keeper.", signed_as=MODEL)
    rt.close()
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", _RECALL, str(db), "lighthouse storm log"],
        capture_output=True, text=True, timeout=180,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
             "PYTHONPATH": ":".join(sys.path)},
    )

    assert done.returncode == 0, done.stderr
    assert "The lighthouse keeper logs every storm in pencil." in done.stdout
    assert "Handoffs:" in done.stdout and "read the lighthouse storm log" in done.stdout
    assert Path(db).exists()


# ── The index fills without sessions ──

_SCOPE_ARGS = ["--db-path", "{db}", "--agent-id", "nova", "--person-id", "riley",
               "--project-scope", "demo"]


def _cli(db, *command: str) -> list[str]:
    return [arg.format(db=db) for arg in _SCOPE_ARGS] + list(command)


def _memory_id(said: str) -> str:
    return re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", said).group(1)


def _state(db) -> dict:
    """Everything a maintenance cycle would change: memories, links, the log."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "engrams": conn.execute(
                "SELECT id, state, strength, stability, accessibility, access_count "
                "FROM engrams ORDER BY id").fetchall(),
            "connections": conn.execute("SELECT COUNT(*) FROM connections").fetchone(),
            "cycles": conn.execute("SELECT COUNT(*) FROM consolidation_log").fetchone(),
            "min_code_version": conn.execute(
                "SELECT value FROM meta WHERE key = 'min_code_version'").fetchone(),
        }
    finally:
        conn.close()


def test_embeddings_index_indexes_what_waits_and_then_nothing(tmp_path, monkeypatch, capsys):
    """Code older than the store, or a session without the model, leaves a
    capture and a handoff without passages. The command indexes what waits,
    and nothing else happens: no cycle, no decay, no links."""
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)  # written without the model
    rt = _runtime(db)
    memory_id = _memory_id(rt.capture("The beacon on the point burns all night."))
    handoff_id = _handoff_id(rt.handoff("Next: fit the new lamp in the beacon."))
    rt.close()
    assert not _passages(db, memory_id) and not _passages(db, handoff_id)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value = '7' WHERE key = 'min_code_version'")
    conn.commit()
    conn.close()
    before = _state(db)
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)

    assert main(_cli(db, "embeddings", "index")) == 0
    first = capsys.readouterr().out

    assert _passages(db, memory_id) and _passages(db, handoff_id), first
    indexed = int(re.search(r"Indexed (\d+) memor", first).group(1))
    assert indexed >= 2 and "0 still waiting." in first, first
    after = _state(db)
    assert after["min_code_version"] == (str(MAINTENANCE_CODE_VERSION),), "opened without the stamp"
    assert {k: v for k, v in after.items() if k != "min_code_version"} == {
        k: v for k, v in before.items() if k != "min_code_version"
    }, "the command did more than index"

    passages_after_first = (_passages(db, memory_id), _passages(db, handoff_id))
    assert main(_cli(db, "embeddings", "index")) == 0
    second = capsys.readouterr().out
    assert "Indexed 0 memories and handoffs (0 passages)" in second, second
    assert (_passages(db, memory_id), _passages(db, handoff_id)) == passages_after_first


def test_consolidate_indexes_within_its_budget(tmp_path, meaning, monkeypatch, capsys):
    monkeypatch.setattr(simple_runtime, "SCHEDULED_INDEX_BUDGET", 3, raising=False)
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    lessons = [_memory(f"lesson {i}: check the beacon before the storm") for i in range(5)]
    for lesson in lessons:
        store.save_engram(lesson)
    store.close()

    assert main(_cli(db, "consolidate")) == 0
    out = capsys.readouterr().out

    assert "Passes:" in out, "the cycle did not run"
    # The cycle linked the lessons, so it wrote its report first (WP-R19): a
    # note recall returns, indexed within the same budget, ahead of memories.
    assert "Maintenance report: written" in out, out
    assert ("Recall index: 3 memories and handoffs indexed (3 passages, at most 3 a run); "
            "3 still waiting") in out, out
    assert sum(bool(_passages(db, lesson.id)) for lesson in lessons) == 2


def test_a_missing_model_does_not_stop_consolidation(tmp_path, monkeypatch, capsys):
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    for i in range(2):
        store.save_engram(_memory(f"lesson {i}: check the beacon before the storm"))
    store.close()
    release, calls = threading.Event(), []
    _fake_sentence_transformers(monkeypatch, release=release, calls=calls)  # no model on disk
    try:
        assert main(_cli(db, "consolidate")) == 0
    finally:
        release.set()
    out = capsys.readouterr().out

    assert "Passes:" in out and _state(db)["cycles"][0] >= 1, "consolidation stopped"
    assert ("Recall index: skipped: the local model all-MiniLM-L6-v2 could not be loaded "
            "from this machine; run: mnemos embeddings download") in out, out
    assert calls and all(call.get("local_files_only") for call in calls)
    rt = _runtime(db)
    card = simple_runtime.format_health_card(rt.health())
    rt.close()
    said = [line for line in card.splitlines() if line.startswith("Recall index:")]
    assert len(said) == 1 and "mnemos consolidate" in said[0], card
    assert "mnemos embeddings download" in said[0]


# ── A write never waits long on indexing (review of PR #90) ──


class _Gemini:
    """``urllib.request.urlopen`` for the Gemini API. It answers with concept
    vectors, hangs until its timeout (a slow provider), or refuses at once (a
    failing one), and records the timeout of every call."""

    def __init__(self) -> None:
        self.mode = "answer"
        self.calls: list[float | None] = []
        self.release = threading.Event()

    def __call__(self, request, timeout=None):
        self.calls.append(timeout)
        if self.mode == "hang":
            self.release.wait(timeout if timeout is not None else 600)
            raise urllib.error.URLError("timed out")
        if self.mode == "refuse":
            raise urllib.error.URLError("connection refused")
        payload = json.loads(request.data)
        if "requests" in payload:
            body = {"embeddings": [
                {"values": concept_vector(item["content"]["parts"][0]["text"])}
                for item in payload["requests"]
            ]}
        else:
            body = {"embedding": {"values": concept_vector(payload["content"]["parts"][0]["text"])}}
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def gemini(monkeypatch):
    """Recall's embedding backend is Gemini, and the network is ``_Gemini``.
    The write path's waits are shortened (0.3 s) so a hang costs little here;
    the rule under test is that a write waits no longer than they say."""
    fake = _Gemini()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(ei, "WRITE_NETWORK_TIMEOUT", 0.3, raising=False)
    monkeypatch.setattr(simple_runtime, "WRITE_EMBED_SECONDS", 0.3, raising=False)
    yield fake
    fake.release.set()


_LONG_HANDOFF = " ".join(f"Sentence {i} is about the beacon lamp on the point." for i in range(40))


def test_a_slow_provider_never_holds_a_write(tmp_path, gemini, capsys):
    """With a provider that hung, a handoff waited 120 s for the batch, then
    30 s a passage: about 14 minutes for 24 passages. A write now waits about
    its budget; what it could not index waits for `mnemos embeddings index` or
    the scheduled job, whose requests each wait at most NETWORK_TIMEOUT
    (WP-R19; they waited 120 s)."""
    db = tmp_path / "memory.db"
    said: dict = {}
    stop = threading.Event()

    def write(rt):
        # While the provider answers, a capture runs the maintenance cycle, so
        # the next capture, minutes sooner than the activity gate allows,
        # skips it and waits on its own indexing alone. (The cycle's
        # connection discovery has a budget of its own: WP-R19.)
        rt.capture("The lighthouse keeper logs every storm.")
        gemini.mode = "hang"
        started = time.monotonic()
        said["handoff"] = rt.handoff(_LONG_HANDOFF)
        said["handoff seconds"] = time.monotonic() - started
        if stop.is_set():
            return
        started = time.monotonic()
        said["capture"] = rt.capture("The ferry pier needs new boards before the regatta.")
        said["capture seconds"] = time.monotonic() - started

    worker = threading.Thread(target=_in_its_own_runtime(db, write), daemon=True)
    worker.start()
    worker.join(15)
    try:
        assert not worker.is_alive(), "a write waited on the provider"
    finally:
        stop.set()
        gemini.release.set()
        worker.join(60)
    assert said["handoff seconds"] < 2, said
    assert said["capture seconds"] < 2, said
    handoff_id = _handoff_id(said["handoff"])
    assert len(ei.passages(_LONG_HANDOFF)) >= 3
    assert _passages(db, handoff_id) == [], "the handoff should wait, unindexed"
    waited = [t for t in gemini.calls if t is not None and t < 1]
    assert waited and all(t <= 0.3 for t in waited), gemini.calls

    gemini.mode = "answer"
    gemini.calls.clear()
    assert main(_cli(db, "embeddings", "index")) == 0
    assert _passages(db, handoff_id), capsys.readouterr().out
    assert gemini.calls and set(gemini.calls) == {2.0}, gemini.calls  # NETWORK_TIMEOUT


def test_a_failing_provider_fails_no_write_and_is_asked_once(tmp_path, gemini):
    db = tmp_path / "memory.db"
    gemini.mode = "refuse"
    rt = _runtime(db)

    said = rt.handoff(_LONG_HANDOFF)

    assert said.startswith("Session handoff saved exactly as written."), said
    handoff_id = _handoff_id(said)
    assert rt._store.get_hypomnema_entry(handoff_id, **SCOPE)["content"] == _LONG_HANDOFF
    assert len(gemini.calls) == 1, f"the write retried: {len(gemini.calls)} calls"
    assert _passages(db, handoff_id) == []
    assert "Captured continuity." in rt.capture("The ferry pier needs new boards.")


# ── A correction in place refreshes the index (review of PR #90) ──


def test_a_correction_in_place_moves_a_handoffs_meaning(tmp_path, meaning):
    """A handoff corrected by its id kept the passages of its old words: it
    was found by what it no longer said, and not by what it says."""
    rt = _runtime(tmp_path / "memory.db")
    handoff_id = _handoff_id(rt.handoff("Tonight the beacon on the point needs a new lamp."))
    assert "the beacon on the point" in rt.recall("lighthouse"), "premise: found by its meaning"

    rt.correct(correction="The ferry pier needs new boards before the regatta.",
               target_id=handoff_id)
    old_sense = rt.recall("lighthouse")
    new_sense = rt.recall("harbour")

    assert "ferry pier" not in old_sense, old_sense
    assert "The ferry pier needs new boards" in new_sense, new_sense


def test_a_correction_the_budget_cannot_index_leaves_no_stale_passages(tmp_path, meaning,
                                                                        monkeypatch):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    handoff_id = _handoff_id(rt.handoff("Tonight the beacon on the point needs a new lamp."))
    assert _passages(db, handoff_id)

    def busy(self, texts, **kwargs):
        raise RuntimeError("the model is busy")

    monkeypatch.setattr(_ConceptModel, "encode", busy)
    rt.correct(correction="The ferry pier needs new boards before the regatta.",
               target_id=handoff_id)

    assert _passages(db, handoff_id) == [], "stale passages outlived the correction"
    assert "ferry pier" not in rt.recall("lighthouse")


def test_a_note_rewritten_by_older_code_is_not_found_by_its_old_meaning(tmp_path, meaning):
    """Code older than this rewrites a note's words and leaves its passages:
    recall counts no passage cut from words the note no longer holds."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    handoff_id = _handoff_id(rt.handoff("Tonight the beacon on the point needs a new lamp."))
    rt.close()
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE hypomnema_entries SET content = ? WHERE id = ?",
                 ("The ferry pier needs new boards before the regatta.", handoff_id))
    conn.commit()
    conn.close()
    assert _passages(db, handoff_id), "premise: the old passages are still there"

    rt = _runtime(db)
    assert "ferry pier" not in rt.recall("lighthouse")
