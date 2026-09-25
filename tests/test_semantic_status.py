"""Semantic recall must say whether it is on, and why not.

On 2026-09-24 a store held 7,204 embeddings while `import
sentence_transformers` failed and recall quietly seeded from keywords alone.
The import error was swallowed as if the package were absent, doctor told the
user to install a package that was installed, and the error that was visible
("Could not import module 'AutoModelForSequenceClassification'") hid the real
one two causes down: a stray profile.py shadowing the standard library.

None of these tests needs the real sentence-transformers or torch: a fake
package reproduces the failure, and a fake model stands in for a working one.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
import sys
import textwrap

import pytest

import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.simple_runtime import MnemosRuntime, format_health_card
from mnemos.store.embedding_index import EmbeddingIndex

ROOT_CAUSE = "module 'profile' has no attribute 'run'"
SURFACE = "Could not import module 'AutoModelForSequenceClassification'"

# What transformers raised on the day: the real cause wrapped twice.
_BROKEN_PACKAGE = textwrap.dedent(
    f"""
    try:
        try:
            raise AttributeError(
                "{ROOT_CAUSE} (consider renaming '/tmp/scratch/profile.py')"
            )
        except AttributeError as exc:
            raise ModuleNotFoundError(
                "Could not import module 'GenerationMixin'. "
                "Are this object's requirements defined correctly?"
            ) from exc
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "{SURFACE}. Are this object's requirements defined correctly?"
        ) from exc
    """
)


@pytest.fixture
def fresh_backend(monkeypatch):
    """Forget what this process learned about the local backend."""
    monkeypatch.setattr(ei, "_HAS_LOCAL", False)
    monkeypatch.setattr(ei, "_LOCAL_CHECKED", False, raising=False)
    monkeypatch.setattr(ei, "_LOCAL_UNAVAILABLE", None, raising=False)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)


@pytest.fixture
def broken_sentence_transformers(tmp_path, monkeypatch, fresh_backend):
    """An installed sentence-transformers whose import fails deep inside."""
    package = tmp_path / "site" / "sentence_transformers"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(_BROKEN_PACKAGE)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))


@pytest.fixture
def missing_sentence_transformers(monkeypatch, fresh_backend):
    """sentence-transformers simply not installed."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


# --- A working local backend without torch ---------------------------------

_CONCEPTS = (
    {"firefly", "fireflies", "bioluminescent", "beetles", "glowing", "lightning"},
    {"disk", "storage", "gigabytes"},
    {"garden", "gnome"},
)


def concept_vector(text: str) -> list[float]:
    """Similar meaning, no shared words: 'bioluminescent beetles' ~ 'fireflies'."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    return [1.0 if words & group else 0.0 for group in _CONCEPTS] + [0.05]


class _Vector(list):
    def tolist(self):
        return list(self)


class _FakeModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector(concept_vector(texts))
        return [_Vector(concept_vector(text)) for text in texts]


class FakeLocalEmbedder(ei._LocalEmbedder):
    """The real local embedder with the model swapped for concept vectors."""

    def _get_model(self):
        if self._model is None:
            self._model = _FakeModel()
        return self._model


@pytest.fixture
def working_local_backend(monkeypatch, fresh_backend):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", FakeLocalEmbedder)


def _runtime(tmp_path):
    return MnemosRuntime(
        db_path=str(tmp_path / "memory.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )


def _pack(values):
    return struct.pack(f"{len(values)}f", *values)


def _embeddings_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings (engram_id TEXT PRIMARY KEY, "
        "embedding BLOB NOT NULL, model_name TEXT NOT NULL, dims INTEGER NOT NULL)"
    )
    for engram_id, blob, model, dims in rows:
        conn.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?)", (engram_id, blob, model, dims)
        )
    conn.commit()
    conn.close()


# --- Why semantic recall is off ---------------------------------------------


def test_a_broken_import_is_reported_with_its_root_cause(broken_sentence_transformers):
    index = EmbeddingIndex(db_path=None)

    assert index.available is False
    reason = index.unavailable_reason
    assert "installed but failed to import" in reason
    assert ROOT_CAUSE in reason, "the cause that says what to fix was dropped"
    assert SURFACE in reason, "the message the user actually saw should stay recognizable"
    assert "GEMINI_API_KEY" in reason
    assert reason.index(ROOT_CAUSE) < reason.index(SURFACE), "lead with the root cause"


def test_a_broken_import_is_logged_once_not_swallowed(broken_sentence_transformers, caplog):
    with caplog.at_level(logging.WARNING, logger="mnemos.store.embedding_index"):
        EmbeddingIndex(db_path=None)
        EmbeddingIndex(db_path=None)
        EmbeddingIndex(db_path=None)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert ROOT_CAUSE in warnings[0].getMessage()


def test_a_missing_package_is_not_reported_as_a_fault(missing_sentence_transformers, caplog):
    with caplog.at_level(logging.INFO, logger="mnemos.store.embedding_index"):
        index = EmbeddingIndex(db_path=None)

    assert index.available is False
    assert "not installed" in index.unavailable_reason
    assert "mnemos-continuity[embeddings]" in index.unavailable_reason
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_doctor_names_the_root_cause_instead_of_saying_install_it(
    tmp_path, capsys, broken_sentence_transformers
):
    """Doctor used to say "keyword only — pip install ..." for a package that
    was installed and failing, which sends the user to fix the wrong thing."""
    runtime = _runtime(tmp_path)
    runtime.capture("Riley keeps a garden gnome by the door")
    runtime.close()

    result = main([
        "doctor", "--db-path", str(tmp_path / "memory.db"),
        "--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo",
    ])
    out = capsys.readouterr().out

    assert result == 0
    assert "Semantic:     OFF" in out
    assert ROOT_CAUSE in out
    assert "failed to import" in out
    semantic_block = out[out.index("Semantic:"):].split("Simple tools:")[0]
    assert "pip install" not in semantic_block
    assert "checked in Python" in semantic_block


def test_health_says_semantic_recall_is_off_and_why(tmp_path, broken_sentence_transformers):
    runtime = _runtime(tmp_path)
    try:
        runtime.capture("Riley keeps a garden gnome by the door")
        data = runtime.health()
        card = format_health_card(data)
    finally:
        runtime.close()

    semantic = data["semantic"]
    assert semantic["active"] is False
    assert ROOT_CAUSE in semantic["reason"]
    assert "Semantic:      OFF — recall finds memories by keyword only" in card
    assert f"why: {semantic['reason']}" in card


def test_health_raises_attention_when_stored_embeddings_go_unused(
    tmp_path, broken_sentence_transformers
):
    """The exact shape of 2026-09-24: thousands of embeddings, none usable."""
    runtime = _runtime(tmp_path)
    runtime.capture("Riley keeps a garden gnome by the door")
    runtime.close()
    _embeddings_db(tmp_path / "memory.db", [
        (f"engram_{i}", _pack([0.1] * 384), "all-MiniLM-L6-v2", 384) for i in range(3)
    ])

    runtime = _runtime(tmp_path)
    try:
        card = format_health_card(runtime.health())
    finally:
        runtime.close()

    assert "ATTENTION — semantic recall is off, but this store holds 3 embeddings" in card
    assert ROOT_CAUSE in card


def test_health_reports_semantic_on_with_what_it_can_reach(tmp_path, working_local_backend):
    runtime = _runtime(tmp_path)
    try:
        runtime.capture("Fireflies flashed in unison over the creek at Elkmont")
        runtime.capture("Riley keeps a garden gnome by the door")
        data = runtime.health()
        card = format_health_card(data)
    finally:
        runtime.close()
    _embeddings_db(tmp_path / "memory.db", [
        ("legacy_1", _pack([0.1] * 8), "gemini-embedding-2", 8),
    ])
    runtime = _runtime(tmp_path)
    try:
        later = runtime.health()["semantic"]
    finally:
        runtime.close()

    semantic = data["semantic"]
    assert semantic["active"] is True
    assert semantic["backend"] == "local"
    assert semantic["model"] == "all-MiniLM-L6-v2"
    assert semantic["verified"] is True, "captures embedded, so embedding was exercised"
    assert semantic["memories_searchable"] == semantic["memories"] >= 1
    assert re.search(
        r"Semantic:      on — local model all-MiniLM-L6-v2, (\d+) of \1 active memories "
        r"searchable by meaning",
        card,
    ), card
    # Vectors from another model are counted, and reported as skipped.
    assert later["embeddings_by_model"]["gemini-embedding-2"] == 1
    assert later["embeddings_usable"] == later["embeddings_stored"] - 1


def test_reading_semantic_status_creates_no_store(tmp_path, working_local_backend):
    missing = tmp_path / "nowhere" / "memory.db"
    index = EmbeddingIndex(db_path=None)
    index._db_path = str(missing)

    status = index.status(candidate_ids={"engram_a"})

    assert status["embeddings_stored"] == 0
    assert status["memories_searchable"] == 0
    assert not missing.exists()


def test_a_model_that_fails_to_load_turns_semantic_off_with_the_reason(
    tmp_path, monkeypatch, fresh_backend, caplog
):
    """An import can succeed while the model never loads (no network for the
    first download, a corrupt cache). Every embedding then failed inside
    callers that swallow errors, and the index still reported itself on."""
    attempts = []

    class UnloadableEmbedder(ei._LocalEmbedder):
        def _get_model(self):
            attempts.append(1)
            raise OSError("We couldn't connect to 'https://huggingface.co' to load this model")

    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", UnloadableEmbedder)
    index = EmbeddingIndex(db_path=str(tmp_path / "memory.db"))

    with caplog.at_level(logging.WARNING, logger="mnemos.store.embedding_index"):
        assert index.index_engram("engram_a", "anything") is False
        assert index.search("anything") == []
        assert index.search("anything else") == []

    assert index.available is False
    assert "failed to load" in index.unavailable_reason
    assert "couldn't connect" in index.unavailable_reason
    assert len(attempts) == 1, "a model that failed to load must not be retried on every call"
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_a_gemini_failure_is_kept_without_the_api_key(monkeypatch, fresh_backend):
    import urllib.error
    import urllib.request

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("refused for key=SECRET-KEY-123")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    index = EmbeddingIndex(db_path=None, gemini_api_key="SECRET-KEY-123")

    assert index.verify() is False
    status = index.status()
    assert status["active"] is True  # a network failure may be transient
    assert "refused" in status["last_error"]
    assert "SECRET-KEY-123" not in status["last_error"]


# --- Only vectors from the same model are compared ----------------------------


def _index_with(tmp_path, rows, model="all-MiniLM-L6-v2"):
    db = tmp_path / "vectors.db"
    _embeddings_db(db, rows)
    index = EmbeddingIndex(db_path=None)
    index._db_path = str(db)
    index._embedder = FakeLocalEmbedder(model)
    index._available = True
    return index


def test_search_skips_vectors_of_another_size(tmp_path, working_local_backend):
    """Stores mix 384-value MiniLM vectors with 3072-value Gemini ones. A
    vector of another size must be skipped, never scored or crashed on — even
    one filed under the query's own model name. (The size check predates
    this change; this pins it.)"""
    query = concept_vector("bioluminescent beetles")
    index = _index_with(tmp_path, [
        ("minilm_firefly", _pack(query), "all-MiniLM-L6-v2", len(query)),
        ("gemini_row", _pack([1.0] * 3072), "gemini-embedding-2", 3072),
        ("odd_size", _pack([1.0] * 8), "all-MiniLM-L6-v2", 8),
    ])

    results = index.search("bioluminescent beetles", k=10)

    assert [eid for eid, _ in results] == ["minilm_firefly"]


def test_search_skips_same_size_vectors_from_another_model(tmp_path, working_local_backend):
    """gemini-embedding-2 and gemini-embedding-2-preview both produce 3072
    values, and Riley's store holds both. Size alone cannot tell them apart,
    so a query embedded by one was scored in the other's space."""
    query = concept_vector("bioluminescent beetles")
    unrelated = concept_vector("garden gnome")
    index = _index_with(tmp_path, [
        ("same_model", _pack(unrelated), "all-MiniLM-L6-v2", len(unrelated)),
        # Identical numbers from a different model: meaningless, and it would
        # have ranked first with a perfect score.
        ("other_model", _pack(query), "paraphrase-MiniLM-L3-v2", len(query)),
    ])

    results = index.search("bioluminescent beetles", k=10)

    assert "other_model" not in [eid for eid, _ in results]
    assert [eid for eid, _ in results] == ["same_model"]


def test_one_corrupt_row_does_not_abort_the_whole_search(tmp_path, working_local_backend):
    """A row whose bytes disagree with its dims raised struct.error out of
    search(), and the retriever swallows that as "no semantic seeds" — for
    every query, because of one row."""
    query = concept_vector("bioluminescent beetles")
    index = _index_with(tmp_path, [
        ("corrupt", _pack(query[:-1]), "all-MiniLM-L6-v2", len(query)),
        ("healthy", _pack(query), "all-MiniLM-L6-v2", len(query)),
    ])

    results = index.search("bioluminescent beetles", k=10)

    assert [eid for eid, _ in results] == ["healthy"]


# --- Results say which seeds came from meaning ------------------------------


def test_seeds_found_by_meaning_are_labelled_embedding(tmp_path, working_local_backend):
    """Every seed was labelled "fts", so nothing could show whether embeddings
    contributed anything at all."""
    runtime = _runtime(tmp_path)
    try:
        runtime.capture("Fireflies flashed in unison over the creek at Elkmont")
        runtime.capture("Riley keeps a garden gnome by the door")
        sc = runtime.scope
        by_meaning = runtime._retriever.retrieve(
            cue="bioluminescent beetles", agent_id=sc.agent_id,
            person_id=sc.person_id, project_scope=sc.project_scope,
        )
        by_keyword = runtime._retriever.retrieve(
            cue="garden gnome", agent_id=sc.agent_id,
            person_id=sc.person_id, project_scope=sc.project_scope,
        )
    finally:
        runtime.close()

    firefly = [r for r in by_meaning if "Fireflies" in r.engram.content]
    assert firefly, "the meaning match was not retrieved at all"
    assert firefly[0].retrieval_path == "embedding"
    assert firefly[0].score_breakdown["similarity"] > 0.3
    gnome = [r for r in by_keyword if "gnome" in r.engram.content]
    assert gnome and gnome[0].retrieval_path == "fts"
