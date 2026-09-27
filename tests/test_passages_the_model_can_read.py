"""Passages the model can read (WP-R08b).

R08 cut memories into passages of up to 700 characters. all-MiniLM-L6-v2 is
trained on sentence pairs and averages what it reads, so a passage holding
several ideas embeds as a blur of them. On a copy of the live store the
plain-words rule (548 characters, one passage) scored 0.24 against "plain
language, brief replies, no jargon", under the floor (0.3 then), so meaning
never found it; its first sentences alone scored 0.52, above anything the query
met.

Now a text is read as windows of whole sentences, about 300 characters, each
overlapping the next by one sentence, and as a whole (its first 700
characters), and found by its best passage. Each passage row records the
scheme it was cut by, and rows cut the old way are stale: the item waits to be
indexed again and is never found by them.

A fake model gives each text a vector by the concepts it names, so meaning is
exactly controlled; no test needs sentence-transformers or torch.
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import mnemos.retrieval.reactive as reactive
import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.core.engram import Engram
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.simple_runtime import MnemosRuntime, recall_index_items
from mnemos.store.embedding_index import EmbeddingIndex
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
MODEL_NAME = "all-MiniLM-L6-v2"


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


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


class _Vector(list):
    def tolist(self):
        return list(self)


class _ConceptModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector(concept_vector(texts))
        return [_Vector(concept_vector(text)) for text in texts]


class ConceptEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _ConceptModel()
        return self._model


@pytest.fixture
def meaning(monkeypatch):
    """A working local backend, for every index made while the test runs."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _memory(content: str) -> Engram:
    return Engram(content=content, kind="semantic", owner_agent_id="nova",
                  person_id="riley", project_scope="demo")


def _rows(db, item_id: str) -> list[tuple]:
    """Each stored passage of ``item_id``: its part and, when the table marks
    it, its scheme."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(passage_vectors)")}
        scheme = "scheme" if "scheme" in columns else "NULL"
        return conn.execute(
            f"SELECT part, {scheme} FROM passage_vectors WHERE item_id = ? ORDER BY part",
            (item_id,),
        ).fetchall()
    finally:
        conn.close()


def _flat(text: str) -> str:
    return " ".join(text.split())


# A sentence with no word any concept knows: it takes no side.
def _quiet(day: str) -> str:
    return f"Nothing much happened on the island during the long quiet days of autumn, day {day}."


# ── 1. Windows of whole sentences, overlapping by one ──


def _log_line(i: int) -> str:
    """A 95-character sentence: three fit in a window of 300, four don't."""
    line = f"Entry {i:02d} of the storm log says the wind turned east at dusk and the sea rose over the old pier."
    assert len(line) == 95
    return line


def test_a_text_is_read_in_windows_of_whole_sentences_that_overlap_by_one():
    sentences = [_log_line(i) for i in range(10)]
    text = " ".join(sentences)

    windows = ei.passages(text)[1:]

    # Three sentences to a window, each window starting on the last sentence
    # of the one before, through to the end.
    runs = [[i, i + 1, i + 2] for i in (0, 2, 4, 6)] + [[8, 9]]
    assert windows == [" ".join(sentences[i] for i in run) for run in runs]
    assert all(len(window) <= ei.WINDOW_CHARS == 300 for window in windows)


def test_sentences_end_at_stops_and_line_breaks_not_at_abbreviations_or_list_numbers():
    text = ("1. First, check the lamp (e.g. at dusk) before the tide.\n"
            "2. Then light it! Does it burn all night? It does.\nDone")

    assert ei._sentences(text) == [
        "1. First, check the lamp (e.g. at dusk) before the tide.",
        "2. Then light it!", "Does it burn all night?", "It does.", "Done",
    ]


def test_a_sentence_longer_than_a_window_is_cut_at_spaces_and_no_shorter_one_is():
    long = ("The keeper counted " + ", ".join(f"boat number {i}" for i in range(48))
            + " before the storm reached the harbour wall.")

    pieces = ei.passages(long)[1:]

    assert " ".join(pieces) == long, "a piece lost, added or repeated words, or cut one"
    assert len(long) > 2 * ei.WINDOW_CHARS
    assert len(pieces) == 3 and all(len(piece) <= ei.WINDOW_CHARS for piece in pieces)
    assert max(map(len, pieces)) - min(map(len, pieces)) < 30, [len(piece) for piece in pieces]
    # The shorter sentences beside it stay whole.
    first, last = "The tide came in slowly that evening.", "The lamp was lit at nine."
    windows = ei.passages(f"{first} {long} {last}")[1:]
    assert all(len(window) <= ei.WINDOW_CHARS for window in windows)
    assert windows[0].startswith(first) and windows[-1].endswith(last), windows
    # So does a sentence as long as a window, and no longer.
    whole = ("The keeper counted " + ", ".join(f"boat {i}" for i in range(60)))[:ei.WINDOW_CHARS - 1] + "."
    assert len(whole) == ei.WINDOW_CHARS
    assert ei.passages(whole) == [whole]


# ── 2. The whole text too ──


def test_the_whole_text_is_one_more_passage():
    paragraphs = [" ".join(_log_line(10 * p + i) for i in range(4)) for p in range(3)]
    text = "\n\n".join(paragraphs)
    assert len(text) > ei.PASSAGE_CHARS

    parts = ei.passages(text)

    flat = _flat(text)
    first_700 = flat[:flat.rfind(" ", 0, ei.PASSAGE_CHARS + 1)]
    assert parts[0] == first_700, "the first passage is not the text's first 700 characters"
    assert all(len(part) <= ei.WINDOW_CHARS for part in parts[1:])

    middle = " ".join(_log_line(i) for i in range(5))
    assert ei.WINDOW_CHARS < len(middle) <= ei.PASSAGE_CHARS
    assert ei.passages(middle)[0] == middle and len(ei.passages(middle)) > 2
    short = "The keeper lit the lamp at nine. The tide was out."
    assert ei.passages(short) == [short], "a text of one window is one passage"
    assert ei.passages("") == []


def test_a_16k_handoff_is_windowed_to_its_end_within_the_limit():
    """Windows advance least with sentences of about 100 characters: two to a
    window, one shared. A handoff of 16,000 such characters still reaches its
    last sentence."""
    sentences = [
        f"Line {i:05d}: the keeper wrote down the tide, the wind and the lamp, and saw that all was well at sea."
        for i in range(159)
    ]
    assert {len(sentence) for sentence in sentences} == {100}
    text = " ".join(sentences)
    assert len(text) > 16_000

    parts = ei.passages(text)

    assert len(parts) <= ei.PASSAGE_LIMIT
    assert parts[-1].endswith(sentences[-1]), "the windows stopped before the end"
    assert len(ei.passages("word " * 40_000)) == ei.PASSAGE_LIMIT


# ── 3. An item counts by its best passage: its gist, or one sentence ──


_ONE_SENTENCE_AWAY = " ".join([
    "The garden greenhouse was full of marigolds in bloom this week.",
    _quiet("one"), _quiet("two"),
    "At night someone climbed the stairs to light the beacon.",
    _quiet("three"), _quiet("four"), _quiet("five"),
])


def test_one_sentence_or_the_gist_finds_a_memory_at_its_best_passage(tmp_path, meaning):
    """Read whole, the memory is a garden with one beacon in it; one of its
    windows is the beacon alone. A question about either is met at full
    strength."""
    db = str(tmp_path / "memory.db")
    EngramStore(db).close()
    index = EmbeddingIndex(db_path=db)
    garden = "engram_garden"
    index.index_passages([(garden, _ONE_SENTENCE_AWAY)])
    parts = ei.passages(_ONE_SENTENCE_AWAY)

    one_sentence = dict(index.search_candidates("lighthouse", {garden}))
    gist = dict(index.search_candidates("the lighthouse by the garden greenhouse", {garden}))

    assert cosine(concept_vector("lighthouse"), concept_vector(parts[0])) < 0.3, "premise: the whole is a blur"
    assert one_sentence[garden] == pytest.approx(max(
        cosine(concept_vector("lighthouse"), concept_vector(part)) for part in parts
    ), abs=1e-3)
    assert one_sentence[garden] > 0.99
    assert gist[garden] == pytest.approx(cosine(
        concept_vector("the lighthouse by the garden greenhouse"), concept_vector(parts[0]),
    ), abs=1e-3)
    assert gist[garden] > 0.95


_RECALL = """
import sys
import mnemos.store.embedding_index as ei
from mnemos.simple_runtime import MnemosRuntime
from tests.test_passages_the_model_can_read import ConceptEmbedder

ei._check_local_deps = lambda: True
ei._LocalEmbedder = ConceptEmbedder
runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    print(runtime.recall(sys.argv[2]))
finally:
    runtime.close()
"""


def test_a_memory_one_sentence_from_the_question_is_found_in_another_process(tmp_path, meaning):
    """The plain-words rule in miniature: whole, the memory is under the floor
    for the question, and it holds none of its words, so recall never found
    it. Its window is the answer. Captured and indexed here; recalled by a
    fresh process."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt.capture(_ONE_SENTENCE_AWAY)
    rt.maintain()
    rt.close()
    home = tmp_path / "home"
    home.mkdir()

    done = subprocess.run(
        [sys.executable, "-c", _RECALL, str(db), "lighthouse"],
        capture_output=True, text=True, timeout=180,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
             "PYTHONPATH": ":".join(sys.path)},
    )

    assert done.returncode == 0, done.stderr
    assert "Durable memories:" in done.stdout, done.stdout
    assert "The garden greenhouse was full of marigolds" in done.stdout, done.stdout


# ── 4. Passages cut the old way are stale: waiting, and never matched ──


def _cut_the_old_way(db, item_id: str, words: str, meaning_of: str) -> None:
    """Leave ``item_id`` one passage as R08's code wrote them: no scheme
    named, the hash of the words it holds, and, so that a match by it shows,
    the meaning of ``meaning_of``."""
    vector = concept_vector(meaning_of)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM passage_vectors WHERE item_id = ?", (item_id,))
        conn.execute(
            "INSERT INTO passage_vectors (item_id, model_name, part, text_hash, dims, embedding) "
            "VALUES (?, ?, 0, ?, ?, ?)",
            (item_id, MODEL_NAME, ei.text_hash(words), len(vector),
             struct.pack(f"{len(vector)}f", *vector)),
        )
        conn.commit()
    finally:
        conn.close()


def _waiting(db) -> int:
    rt = _runtime(db)
    try:
        return rt.health()["watchdog"]["checks"]["recall_index"]["waiting"]
    finally:
        rt.close()


def test_passages_cut_the_old_way_are_stale_and_wait_to_be_indexed_again(tmp_path, meaning, capsys):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt._ensure_init()
    lesson = _memory("The garden greenhouse was full of marigolds in bloom.")
    rt._store.save_engram(lesson)  # a lesson: no whole-text vector of its own
    handoff = rt.handoff("Next: check the ferry timetable at the pier on Monday.")
    handoff_id = handoff.split("Handoff ID: ", 1)[1].splitlines()[0]
    rt.maintain()
    words = dict(recall_index_items(rt._store, **SCOPE))
    rt.close()
    assert _waiting(db) == 0, "premise: everything was indexed"

    for item_id in (lesson.id, handoff_id):
        _cut_the_old_way(db, item_id, words[item_id], meaning_of="the lighthouse beacon lamp keeper")

    rt = _runtime(db)
    try:
        by_old_meaning = rt.recall("lighthouse")
        status = rt.semantic_status()
    finally:
        rt.close()
    assert "marigolds" not in by_old_meaning and "ferry timetable" not in by_old_meaning, by_old_meaning
    assert (status["memories"], status["memories_searchable"]) == (1, 0), status
    assert (status["handoffs"], status["handoffs_searchable"]) == (1, 0), status
    assert _waiting(db) == 2, "the stale items are not counted as waiting"

    assert main(["--db-path", str(db), *SCOPE_ARGS, "embeddings", "index"]) == 0
    said = capsys.readouterr().out

    assert re.search(r"Indexed 2 memories and handoffs \(\d+ passages\)", said), said
    assert "0 still waiting." in said, said
    assert _rows(db, lesson.id) == [(0, ei.PASSAGE_SCHEME)]
    assert _rows(db, handoff_id) == [(0, ei.PASSAGE_SCHEME)]
    assert _waiting(db) == 0
    rt = _runtime(db)
    try:
        assert "marigolds" in rt.recall("garden")
        assert "ferry timetable" in rt.recall("harbour")
        assert "marigolds" not in rt.recall("lighthouse")
    finally:
        rt.close()


_R08_TABLE = """
CREATE TABLE passage_vectors (
    item_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    part INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    dims INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (item_id, model_name, part)
)
"""


def test_an_r08_table_gains_the_scheme_mark_and_every_store_still_opens(tmp_path, meaning):
    """The live store's table has no scheme column. A read-only look counts
    none of its rows and changes nothing; the first writable index adds the
    column with every old row marked scheme 1, written into each row, so the
    store's integrity check (run on every open) still passes."""
    db = str(tmp_path / "memory.db")
    EngramStore(db).close()
    words = "The keeper lit the beacon at nine."
    vector = concept_vector(words)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE passage_vectors")
    conn.execute(_R08_TABLE)
    for item_id in ("engram_a", "engram_b"):
        conn.execute("INSERT INTO passage_vectors VALUES (?, ?, 0, ?, ?, ?)",
                     (item_id, MODEL_NAME, ei.text_hash(words), len(vector),
                      struct.pack(f"{len(vector)}f", *vector)))
    conn.commit()
    conn.close()

    looking = EmbeddingIndex(db_path=db, read_only=True)
    assert looking.passage_hashes(["engram_a", "engram_b"]) == {}
    assert looking.search_candidates("lighthouse", {"engram_a", "engram_b"}) == []
    looking.close()
    assert "scheme" not in _columns(db), "a read-only index changed the table"

    index = EmbeddingIndex(db_path=db)

    assert "scheme" in _columns(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT scheme FROM passage_vectors").fetchall() == [(1,), (1,)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()
    EngramStore(db).close()
    assert index.passage_hashes(["engram_a", "engram_b"]) == {}
    assert index.index_passages([("engram_a", words)])["items"] == 1
    assert _rows(db, "engram_a") == [(0, ei.PASSAGE_SCHEME)]
    assert EmbeddingIndex(db_path=db).passage_hashes(["engram_a"]) == {"engram_a": ei.text_hash(words)}


def _columns(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(passage_vectors)")}
    finally:
        conn.close()


def test_a_newer_schemes_passages_count_and_are_never_cut_again(tmp_path, meaning):
    """This code becomes the old code: passages a newer scheme cut are used
    as they are, never cut again the older way, while the words match."""
    db = str(tmp_path / "memory.db")
    EngramStore(db).close()
    index = EmbeddingIndex(db_path=db)
    words = "The keeper lit the beacon at nine."
    index.index_passages([("engram_a", words)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE passage_vectors SET scheme = ?", (ei.PASSAGE_SCHEME + 1,))
    conn.commit()
    conn.close()

    again = index.index_passages([("engram_a", words)])

    assert again == {"items": 0, "passages": 0, "waiting": 0}
    assert _rows(db, "engram_a") == [(0, ei.PASSAGE_SCHEME + 1)]
    assert [item for item, _ in index.search_candidates("lighthouse", {"engram_a"})] == ["engram_a"]
    assert index.index_passages([("engram_a", "The ferry leaves the pier at ten.")])["items"] == 1
    assert _rows(db, "engram_a") == [(0, ei.PASSAGE_SCHEME)], "words that changed are cut again"


# ── 5. The meaning floor: 0.35 ──


_AT_THE_FLOOR = "Apples keep best in a cool cellar."
_UNDER_THE_FLOOR = "Pears ripen slowly on the windowsill."


class _FixedModel:
    """Vectors whose cosines with the cue are exact whatever the float width:
    7 / sqrt(49 + 324 + 25 + 1 + 1) is 7/20, 0.35 to the last bit; 1/3 lies
    between the old floor and the new one."""

    vectors = {
        "lighthouse": [1.0, 0.0, 0.0, 0.0, 0.0],
        _AT_THE_FLOOR: [7.0, 18.0, 5.0, 1.0, 1.0],
        _UNDER_THE_FLOOR: [1.0, 2.0, 2.0, 0.0, 0.0],
    }

    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        other = [0.0, 0.0, 0.0, 0.0, 1.0]
        if isinstance(texts, str):
            return _Vector(self.vectors.get(texts, other))
        return [_Vector(self.vectors.get(text, other)) for text in texts]


class _FixedEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _FixedModel()
        return self._model


def test_meaning_seeds_a_memory_at_0_35_and_none_between_0_30_and_0_35(tmp_path, monkeypatch):
    """Read in windows, more of every text clears a floor: at 0.3 a question
    that 15 items cleared was cleared by 41 on a copy of the lab's store, and
    the lesson it was about fell out of the top ten. Neither memory shares a
    word with the cue, so only meaning could seed them."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _FixedEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)
    db = str(tmp_path / "memory.db")
    store = EngramStore(db)
    index = EmbeddingIndex(db_path=db)
    at, under = _memory(_AT_THE_FLOOR), _memory(_UNDER_THE_FLOOR)
    for memory in (at, under):
        store.save_engram(memory)
    index.index_passages([(memory.id, memory.content) for memory in (at, under)])
    assert dict(index.search_candidates("lighthouse", {at.id, under.id})) == {
        at.id: 0.35, under.id: 0.3333,
    }, "premise: one memory exactly at 0.35, one between 0.30 and 0.35"

    results = ReactiveRetriever(store, embedding_index=index,
                                reconsolidation_enabled=False).retrieve("lighthouse", **SCOPE)

    assert [result.engram.id for result in results] == [at.id], [
        (result.engram.content, result.score_breakdown) for result in results
    ]
    assert results[0].retrieval_path == "embedding"
    assert results[0].score_breakdown["similarity"] == 0.35
    assert reactive.MEANING_FLOOR == 0.35
