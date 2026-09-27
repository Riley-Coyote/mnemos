"""A memory is found by what it taught, as well as by what happened.

Recall searched a memory's words and its words' meaning, and never its impact:
the lesson the agent wrote about it. A memory whose words are about a report
and whose lesson is "avoid jargon" was found by no search for "avoid jargon".
On a copy of the live store, 29 of the 172 active memories with a lesson of
the agent's had no lesson memory holding the same words; asked with each
lesson's first sentence, recall's top ten held 15 of them (all 29 after). Now the lesson is one more passage of its
memory in the meaning index (passage scheme 3) and one more list recall ranks
by words. A server's placeholder is not a lesson and is never either.

None of these tests needs sentence-transformers or torch: a fake model gives
each text a vector by the concepts it names.
"""

from __future__ import annotations

import math
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import mnemos.store.embedding_index as ei
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime, index_for_recall
from mnemos.store.embedding_index import EmbeddingIndex
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
MODEL = "claude-opus-5-5"

_CONCEPTS = (
    {"jargon", "plain", "wording", "simple", "everyday"},
    {"report", "quarterly", "budget", "hiring"},
    {"lighthouse", "beacon", "lamp", "keeper"},
)

# What happened is about a report; what it taught is about plain words.
REPORT = "Wrote the quarterly report for the team: the budget, the hiring plan and the office move."
LESSON = "Avoid jargon: plain words land better with Riley."
# Shares a word with the lesson and none with the report.
BY_WORDS = "avoid jargon"
# Shares no word with either; means what the lesson means.
BY_MEANING = "keep the wording simple and everyday"


def concept_vector(text: str) -> list[float]:
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in _CONCEPTS] + [0.05]
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
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


def _memory(content: str, **fields) -> Engram:
    return Engram(content=content, kind="episodic", owner_agent_id="nova", person_id="riley",
                  project_scope="demo", **fields)


def _runtime(db: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _captured(db: Path, content: str = REPORT, impact: str = LESSON) -> str:
    """A capture, through the tool's own path: its memory's id."""
    rt = _runtime(db)
    said = rt.capture(content, impact=impact, signed_as=MODEL)
    rt.close()
    return re.search(r"(engram_[A-Za-z0-9]+)", said).group(1)


def _rows(db: Path, item_id: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT part, scheme, text_hash FROM passage_vectors WHERE item_id = ? ORDER BY part",
            (item_id,),
        ).fetchall()
    finally:
        conn.close()


def _recalled(db: Path, query: str) -> str:
    rt = _runtime(db)
    try:
        return rt.recall(query, max_results=5)
    finally:
        rt.close()


# ── Recall ──

_RECALL = r'''
import sys
from mnemos.simple_runtime import MnemosRuntime
rt = MnemosRuntime(db_path=sys.argv[1], use_dedicated_model=False,
                   agent_id="nova", person_id="riley", project_scope="demo")
print(rt.recall(sys.argv[2], max_results=5))
'''


def test_a_memory_is_found_by_its_lesson_in_words_in_another_process(tmp_path):
    """Captured in one process, recalled in another by the lesson's words,
    which the memory's own words don't hold."""
    db = tmp_path / "memory.db"
    memory_id = _captured(db)
    home = tmp_path / "home"
    home.mkdir()

    done = subprocess.run(
        [sys.executable, "-c", _RECALL, str(db), BY_WORDS], capture_output=True, text=True,
        timeout=120, env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
                          "PYTHONPATH": ":".join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert done.returncode == 0, done.stderr
    assert memory_id in done.stdout, done.stdout
    assert "Wrote the quarterly report" in done.stdout


def test_a_memory_is_found_by_its_lesson_in_meaning(tmp_path, meaning):
    """The capture's own indexing gives the lesson a passage of its own, and a
    question that means the lesson, sharing no word with it, finds the
    memory by meaning alone."""
    db = tmp_path / "memory.db"
    memory_id = _captured(db)
    _captured(db, "The lighthouse keeper trims the beacon lamp every evening.", "")

    said = _recalled(db, BY_MEANING)

    assert memory_id in said, said
    rows = _rows(db, memory_id)
    assert (ei.LESSON_PART, ei.PASSAGE_SCHEME, ei.text_hash(LESSON)) in rows, rows
    assert all(scheme == 3 for _, scheme, _ in rows)


def test_the_cue_offers_a_memory_by_its_lesson_by_words_and_by_meaning(tmp_path, meaning):
    from mnemos.simple_runtime import cue_memories

    db = tmp_path / "memory.db"
    memory_id = _captured(db)
    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        by_words = cue_memories(store, None, "please avoid the jargon when you write this up", **SCOPE)
        by_meaning = cue_memories(store, index, "keep the wording simple and everyday for everyone",
                                  **SCOPE)
    finally:
        store.close()

    for lines in (by_words, by_meaning):
        assert [line["id"] for line in lines] == [memory_id], lines
        assert lines[0]["text"] == LESSON and lines[0]["lesson"]
    assert by_meaning[0]["similarity"] == pytest.approx(1.0, abs=0.01)


# ── Only the agent's lessons ──


def test_a_placeholder_impact_is_never_indexed_or_matched(tmp_path, meaning):
    """The agent's lesson is a passage and is found by its words; a phrase the
    server filled in (an old one, known by its words, or one marked as the
    server's) is neither, and neither is a lesson that only repeats the
    memory's words."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    taught = _memory(REPORT, impact=LESSON, impact_source="agent")
    filled = _memory("The lighthouse keeper logs the storms in pencil.",
                     impact="Recurring pattern worth carrying across sessions.")
    marked = _memory("The beacon lamp was trimmed at dusk.",
                     impact="Harbour ferry timetables change in winter.", impact_source="template")
    copied = _memory("Plain words land better with Riley.",
                     impact="Plain words land better with Riley.", impact_source="agent")
    for engram in (taught, filled, marked, copied):
        store.save_engram(engram)
    index = EmbeddingIndex(db_path=str(db))
    index_for_recall(store, index, **SCOPE)
    store.close()

    assert taught.id in _recalled(db, BY_WORDS)
    for words in ("recurring pattern worth carrying", "harbour ferry timetables winter"):
        said = _recalled(db, words)
        assert filled.id not in said and marked.id not in said, said
    assert any(part == ei.LESSON_PART for part, _, _ in _rows(db, taught.id))
    for engram in (filled, marked, copied):
        assert all(part != ei.LESSON_PART for part, _, _ in _rows(db, engram.id))
    store = EngramStore(str(db))
    assert store.live_memory_lessons(**SCOPE) == {taught.id: LESSON}
    store.close()


# ── The scheme ──


def test_old_scheme_rows_wait_and_are_cut_again_with_the_lesson(tmp_path, meaning, monkeypatch):
    """Rows cut by scheme 2 have no lesson passage: every item they cover
    waits, the watchdog says so, and the next pass cuts them again, the
    lesson with them."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    taught = _memory(REPORT, impact=LESSON, impact_source="agent")
    plain = _memory("The lighthouse keeper trims the beacon lamp every evening.")
    for engram in (taught, plain):
        store.save_engram(engram)
    index = EmbeddingIndex(db_path=str(db))
    monkeypatch.setattr(ei, "PASSAGE_SCHEME", 2)
    index.index_passages([(taught.id, taught.content), (plain.id, plain.content)])
    monkeypatch.undo()
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    assert {scheme for _, scheme, _ in _rows(db, taught.id)} == {2}
    from datetime import datetime, timezone

    from mnemos.watchdog import _recall_index

    assert _recall_index(store, SCOPE, datetime.now(timezone.utc), index)["waiting"] == 2
    done = index_for_recall(store, index, **SCOPE)

    assert done["items"] == 2
    items = [(taught.id, taught.content), (plain.id, plain.content)]
    assert index.waiting(items, lessons=store.live_memory_lessons(**SCOPE)) == []
    store.close()
    assert (ei.LESSON_PART, 3, ei.text_hash(LESSON)) in _rows(db, taught.id)
    assert {scheme for _, scheme, _ in _rows(db, plain.id)} == {3}


def test_a_lesson_written_later_has_its_memory_cut_again(tmp_path, meaning):
    """The impact often comes after the capture, when the agent answers what a
    memory taught. The words didn't change, and the memory waits all the same:
    its lesson is new."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    memory = _memory(REPORT)
    store.save_engram(memory)
    index = EmbeddingIndex(db_path=str(db))
    assert index_for_recall(store, index, **SCOPE)["items"] == 1

    memory.impact, memory.impact_source = LESSON, "agent"
    store.save_engram(memory)
    again = index_for_recall(store, index, **SCOPE)
    store.close()

    assert again["items"] == 1, "the new lesson has its memory cut again"
    assert (ei.LESSON_PART, 3, ei.text_hash(LESSON)) in _rows(db, memory.id)


def test_code_on_the_previous_scheme_reads_the_new_rows_as_current(tmp_path, meaning):
    """A text's own passages keep the hash of the text alone, as scheme 2 wrote
    it: code on scheme 2, which counts newer schemes' rows as current by that
    hash, never cuts these back, so old and new sessions don't take turns
    re-indexing the same memory. (A property kept, not a change: it holds on
    scheme 2 too.)"""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    taught = _memory(REPORT, impact=LESSON, impact_source="agent")
    store.save_engram(taught)
    index_for_recall(store, EmbeddingIndex(db_path=str(db)), **SCOPE)
    store.close()

    rows = _rows(db, taught.id)
    first = next(row for row in rows if row[0] == 0)
    assert first[2] == ei.text_hash(REPORT)


# ── One vote for the same words ──


def test_a_memory_matched_by_its_words_and_its_lesson_counts_once(tmp_path):
    """A memory whose own words and whose lesson both hold the cue's words is
    one match by words, at the better of its two ranks, not two: two votes
    for the same words put it above a memory whose words match better. The
    best match by its own words stays first, and the other's fused score is
    one words contribution, at its better rank."""
    from mnemos.retrieval.reactive import FUSION_K, WORDS_WEIGHT, ReactiveRetriever

    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    best = _memory("The lighthouse storm log: lighthouse storm log, every lighthouse storm.")
    both = _memory("The lighthouse keeper wrote in the log.",
                   impact="Keep the lighthouse storm log every night.", impact_source="agent")
    for engram in (best, both):
        store.save_engram(engram)

    found = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(
        "lighthouse storm log", **SCOPE,
    )
    store.close()

    names = {best.id: "best by its words", both.id: "words and lesson"}
    assert [names[r.engram.id] for r in found] == ["best by its words", "words and lesson"]
    by_id = {r.engram.id: r for r in found}
    assert by_id[best.id].score_breakdown["words_rank"] == 1
    assert by_id[both.id].score_breakdown["fused"] == round(WORDS_WEIGHT / (FUSION_K + 2), 6)
