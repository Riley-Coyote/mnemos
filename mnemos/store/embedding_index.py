"""
Embedding index for semantic similarity search.

Supports two backends:
  1. Gemini API (gemini-embedding-2-preview, 3072 dims) — default, high quality
  2. Local sentence-transformers (all-MiniLM-L6-v2, 384 dims) — fallback if no API key

Backend selection:
  - If GEMINI_API_KEY is found (env var or .env files), uses Gemini API
  - Otherwise falls back to local sentence-transformers
  - If neither is available, all operations return empty results, and
    status() says why — recall then seeds from keywords alone, which must
    never happen without anyone being told

Embeddings stored in SQLite alongside engrams. A vector is only ever
compared with vectors from the same model.

The local model loads from the files already on this machine and nowhere else
(``local_files_only``). Loading it used to ask the Hugging Face Hub about it
first: on 2026-09-26 `mnemos doctor` sat over two minutes in an SSL read while
loading a model that was already cached, and every session's first meaning
search makes the same load. Downloading is one explicit step,
``mnemos embeddings download`` (``download_local_model``).

Recall's meaning index is ``passage_vectors``: each memory recall can return,
and each handoff, cut into passages (``passages``) with a vector for each, and
found by its best one. The model reads about 256 tokens, so one vector of a
long text says nothing about its second half; and it is trained on sentence
pairs and averages what it reads, so one vector of several ideas is a blur of
them. So a text is read twice over: in windows of a few sentences, and as a
whole (its first 700 characters). A memory whose impact holds a lesson the
agent wrote has that lesson as one more passage (``LESSON_PART``): it is found
by what it taught as well as by what happened. ``embeddings`` keeps one vector
per memory's whole text, as capture writes it, for linking and for recall
until a memory has passages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import time
import urllib.request
import urllib.error
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Recall's meaning index (schema v15, and created here too for an index opened
# on a database no store has migrated). One row per passage of a memory or a
# handoff; ``text_hash`` is the text the passages were cut from, so a text that
# changed is cut and embedded again, and ``scheme`` is how it was cut
# (``PASSAGE_SCHEME``), so a text cut an older way is cut again. Rebuildable
# from the words at any time; it is never memory itself.
PASSAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS passage_vectors (
    item_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    part INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    dims INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    scheme INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, model_name, part)
)
"""

# How the passages of a text are cut (``passages``), which every row records:
#   1  passages of up to 700 characters at paragraph, sentence and word breaks
#      (R08). A row written by code that names no scheme was cut this way.
#   2  windows of whole sentences, about 300 characters, each overlapping the
#      next by one sentence; and the text's first 700 characters, whole.
#   3  the same, and a memory's lesson (the impact the agent wrote, when its
#      words don't already say it) as one more passage, part ``LESSON_PART``.
# A row cut by an older scheme is stale: its item waits to be indexed again and
# is never found by it. On a copy of the live store, a standing rule of 548
# characters was one scheme-1 passage, which scored 0.24 against "plain
# language, brief replies, no jargon", under the floor (0.3 then); the window
# of its first three sentences scores 0.52, above anything else the query
# meets. Under scheme 2, on a copy of the live store, 29 of the 172 active
# memories with a lesson of the agent's had no lesson memory holding the same
# words; asked with each lesson's first sentence, recall's top ten held 15 of
# them, and under scheme 3 all 29.
# A newer scheme's rows count here, and this code never cuts them again. The
# text's own passages keep the hash of the text alone, as scheme 2 wrote it,
# so code on scheme 2 reads scheme-3 rows as current and never cuts them back.
PASSAGE_SCHEME = 3
# The part number of a memory's lesson passage: far above any text's own
# passages (0 to PASSAGE_LIMIT - 1). Its row's text_hash is the lesson's.
LESSON_PART = 1000
_FIRST_SCHEME = 1
_SCHEME_COLUMN = f"scheme INTEGER NOT NULL DEFAULT {_FIRST_SCHEME}"
# A window: whole sentences, together at most this long. A sentence longer
# than this alone is cut at spaces into near-equal pieces.
WINDOW_CHARS = 300
# The whole-text passage: a text's first this-many characters, about 150 to
# 200 of the model's 256 tokens.
PASSAGE_CHARS = 700
# Passages kept for one text: enough for its first ~16,000 characters however
# its sentences run. Windows advance least with sentences of about 100
# characters (two to a window, one shared), about 101 characters each: 159
# windows and the whole. A capture can run to 65,536; past this, the rest is
# found by its words.
PASSAGE_LIMIT = 160
# λ, the length penalty of an item's meaning score (``search_candidates``):
# its best passage's similarity less λ · ln(its passages). An item counts by
# its best passage, so a long text has more chances: a handoff of 16,000
# characters has up to 160, a short memory one. The penalty orders, and only
# that: a floor (recall's, the cue's gate) reads the best similarity itself,
# so a long item is ranked lower but never pushed under a floor. Chosen from
# 0, 0.01, 0.02 and 0.03 on copies (WP-R08c): at 0.02 the plain-words rule
# rose from 23rd to 17th for "how should I write my replies to Riley", the
# lab's 29 facts were unchanged, and the cue kept its 20 lines that bore on
# the lab's prompts (two of them, in one task, traded for two others that
# did too).
LENGTH_PENALTY = 0.02
# Ids asked about in one statement, well under SQLite's variable limit.
_ID_CHUNK = 400

# The write path (a capture, a correction, a handoff) never waits long on
# indexing. Each pass it makes may spend about this many seconds on embedding
# calls, all of them before one deadline, however they are batched; what does
# not finish waits for the scheduled `mnemos consolidate` or
# `mnemos embeddings index`. With a slow provider a handoff used to wait 120 s
# for the batch and then 30 s for each passage: about 14 minutes for 24. The
# local model's one-time load (from this machine only, once per process) is
# not an embedding call and is not counted.
WRITE_EMBED_SECONDS = 2.0
# A network backend's wait per call on the write path, and never past the
# pass's deadline. No retry, and no second try one passage at a time: a
# failure leaves the items waiting.
WRITE_NETWORK_TIMEOUT = 2.0
# Passages embedded per call on the write path, so the time budget can stop
# between calls (a local call of this size takes well under a tenth of a second).
_WRITE_CHUNK = 16

# Where a sentence ends: a run of . ! ? or …, and any closing quotes or
# brackets after it, before a space. A line break ends one too.
_SENTENCE_END = re.compile("[.!?…]+[\"'”’)\\]]*(?=\\s)")
# Words whose last period ends no sentence.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf"})

# Failures already logged in this process. Each is said once, not on every
# capture and recall that runs into it.
_LOGGED: set[str] = set()


def _log_once(key: str, level: int, message: str, *args: Any) -> None:
    if key in _LOGGED:
        return
    _LOGGED.add(key)
    log.log(level, message, *args)


def _clip(text: str, limit: int = 600) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_hash(text: str) -> str:
    """Which text a set of passages was cut from."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _lesson_hash(lesson: str | None) -> str | None:
    """Which lesson a lesson passage was cut from; None for no lesson."""
    return text_hash(lesson) if (lesson or "").strip() else None


def passages(text: str) -> list[str]:
    """``text`` as recall's meaning index reads it (scheme 2): first the whole
    text, as far as its first ``PASSAGE_CHARS`` characters; then windows of
    whole sentences, each at most ``WINDOW_CHARS`` long and beginning with the
    sentence the one before it ended with, when the two fit in one window. A
    text that is one window is one passage. At most ``PASSAGE_LIMIT`` in all.

    An item is found by its best passage, so a question can meet a text's gist
    or one of its sentences: a window holds one idea or a few, which the model
    can read without blurring them into the rest."""
    whole = " ".join((text or "").split())
    if not whole:
        return []
    windows = _windows([piece for sentence in _sentences(text) for piece in _pieces(sentence)])
    if len(whole) > PASSAGE_CHARS:
        cut = whole.rfind(" ", 0, PASSAGE_CHARS + 1)
        whole = whole[:cut if cut > 0 else PASSAGE_CHARS]
    if windows == [whole]:
        return windows
    return [whole, *windows][:PASSAGE_LIMIT]


def lesson_passage(lesson: str) -> str:
    """A memory's lesson as one passage: on one line, its first
    ``PASSAGE_CHARS`` characters, like a text's whole-text passage."""
    whole = " ".join((lesson or "").split())
    if len(whole) > PASSAGE_CHARS:
        cut = whole.rfind(" ", 0, PASSAGE_CHARS + 1)
        whole = whole[:cut if cut > 0 else PASSAGE_CHARS]
    return whole


def _sentences(text: str) -> list[str]:
    """``text``'s sentences, in order, each on one line with single spaces.
    A line break ends a sentence, and so does ``_SENTENCE_END``, except the
    period of an abbreviation or of a list's number ("1.")."""
    found: list[str] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        start = 0
        for end in _SENTENCE_END.finditer(line):
            if end.group() == ".":
                word = line[:end.start()].rsplit(" ", 1)[-1].lstrip("([{\"'“‘").lower()
                if word in _ABBREVIATIONS or line[start:end.start()].strip().isdigit():
                    continue
            if line[start:end.end()].strip():
                found.append(line[start:end.end()].strip())
            start = end.end()
        if line[start:].strip():
            found.append(line[start:].strip())
    return found


def _pieces(sentence: str) -> list[str]:
    """``sentence`` whole, or, when it alone is longer than a window, cut at
    spaces into the fewest pieces that fit, as near equal as the spaces allow
    (a word longer than a window is cut where it must be)."""
    pieces: list[str] = []
    while len(sentence) > WINDOW_CHARS:
        count = -(-len(sentence) // WINDOW_CHARS)
        cut = sentence.rfind(" ", 0, -(-len(sentence) // count) + 1)
        if cut <= 0:
            cut = sentence.rfind(" ", 0, WINDOW_CHARS + 1)
        if cut <= 0:
            cut = WINDOW_CHARS
        pieces.append(sentence[:cut].strip())
        sentence = sentence[cut:].strip()
    if sentence:
        pieces.append(sentence)
    return pieces


def _windows(sentences: list[str]) -> list[str]:
    """``sentences`` packed in order into windows of at most ``WINDOW_CHARS``,
    each window beginning with the last sentence of the one before when that
    sentence and the next fit in one window (otherwise it could only repeat)."""
    windows: list[str] = []
    first, count = 0, len(sentences)
    while first < count:
        end, length = first + 1, len(sentences[first])
        while end < count and length + 1 + len(sentences[end]) <= WINDOW_CHARS:
            length += 1 + len(sentences[end])
            end += 1
        windows.append(" ".join(sentences[first:end]))
        if end >= count:
            break
        shared = end - 1
        overlaps = shared > first and len(sentences[shared]) + 1 + len(sentences[end]) <= WINDOW_CHARS
        first = shared if overlaps else end
    return windows


def _describe_failure(exc: BaseException) -> str:
    """Name what actually failed, in one line.

    Libraries wrap the real error. transformers turns any failure while
    loading a model class into "Could not import module 'X'", twice over, so
    the message that says what to fix sits at the bottom of the chain. On
    2026-09-24 that bottom was a stray profile.py shadowing the standard
    library, reported as a torch/transformers incompatibility because only
    the top was visible. Lead with the root cause; keep the top for
    recognition.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain) and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__

    def one(error: BaseException) -> str:
        text = " ".join(str(error).split())
        return f"{type(error).__name__}: {text}" if text else type(error).__name__

    described = one(chain[-1])
    if len(chain) > 1:
        described += f" (surfaced as {one(chain[0])})"
    return _clip(described)


# --- Key resolution (shared with mnemos/llm.py) ---

def _load_env_key(key_name: str) -> str | None:
    """Load a key from the environment or the configured .env locations.

    Delegates to llm._load_env_key so the search path (MNEMOS_ENV_PATHS,
    then cwd/.env and ~/.mnemos/.env) and MNEMOS_DISABLE_DOTENV are
    honored in one place.
    """
    from ..llm import _load_env_key as _shared_load_env_key

    return _shared_load_env_key(key_name) or None


# --- Gemini API embedding ---

class _GeminiEmbedder:
    """Generates embeddings via Google Gemini API."""
    
    def __init__(self, api_key: str, model: str = "gemini-embedding-2-preview"):
        self._api_key = api_key
        self._model = model
        self._dims = 3072
        # Why the latest call returned nothing. The API is called with the key
        # in the URL, so the key is scrubbed from anything kept here.
        self.last_error: str | None = None

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model_name(self) -> str:
        return self._model

    def _failed(self, error: BaseException | str) -> None:
        text = error if isinstance(error, str) else _describe_failure(error)
        self.last_error = text.replace(self._api_key, "<key>") if self._api_key else text

    def embed(self, text: str, *, timeout: float = 30) -> list[float] | None:
        """Generate embedding for a single text, waiting at most ``timeout``
        seconds on the network."""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:embedContent?key={self._api_key}"
        )
        payload = json.dumps({
            "model": f"models/{self._model}",
            "content": {"parts": [{"text": text}]}
        }).encode()
        
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read())
            values = data.get("embedding", {}).get("values", [])
            if values:
                self._dims = len(values)
                self.last_error = None
                return values
            self._failed("the Gemini response held no embedding values")
            return None
        except Exception as exc:
            self._failed(exc)
            return None
    
    def batch_embed(
        self, texts: list[str], *, timeout: float = 120, fallback: bool = True,
        deadline: float | None = None,
    ) -> list[list[float] | None]:
        """Embed multiple texts via batchEmbedContents API, waiting at most
        ``timeout`` seconds on each request. A failed request is retried one
        text at a time, unless ``fallback`` is False (the write path): then its
        texts come back as None, and wait for a later pass. With ``deadline``
        (a ``time.monotonic()`` moment), no request waits past it and none
        starts after it: the texts left come back as None."""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:batchEmbedContents?key={self._api_key}"
        )
        
        requests_list = []
        for text in texts:
            requests_list.append({
                "model": f"models/{self._model}",
                "content": {"parts": [{"text": text}]}
            })
        
        # Gemini batch API has a limit of 100 per request
        all_results: list[list[float] | None] = []
        batch_size = 100
        
        for i in range(0, len(requests_list), batch_size):
            batch = requests_list[i:i + batch_size]
            wait = timeout
            if deadline is not None:
                wait = min(timeout, deadline - time.monotonic())
                if wait <= 0:
                    all_results.extend([None] * (len(requests_list) - i))
                    break
            payload = json.dumps({"requests": batch}).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}
            )

            try:
                resp = urllib.request.urlopen(req, timeout=wait)
                data = json.loads(resp.read())
                for emb in data.get("embeddings", []):
                    values = emb.get("values", [])
                    if values:
                        self._dims = len(values)
                        all_results.append(values)
                    else:
                        all_results.append(None)
            except Exception as exc:
                if not fallback:
                    self._failed(exc)
                    all_results.extend([None] * len(batch))
                    continue
                # Fall back to individual calls for this batch
                for r in batch:
                    text = r["content"]["parts"][0]["text"]
                    all_results.append(self.embed(text))
        
        return all_results


# --- Local sentence-transformers fallback ---

# Whether local embeddings can run in this process, once checked.
_HAS_LOCAL = False
_LOCAL_CHECKED = False
# Why they cannot, when they cannot. status() reports it and `mnemos doctor`
# and the health card print it.
_LOCAL_UNAVAILABLE: str | None = None

_LOCAL_EXTRA_HINT = "pip install 'mnemos-continuity[embeddings]'"


def _check_local_deps() -> bool:
    """Whether local sentence-transformers can be imported in this process.

    Checked once per process. A failed import used to be swallowed as if the
    package were simply absent, so recall quietly seeded from keywords while
    thousands of stored embeddings sat unused. Not installed is a supported,
    keyword-only setup; installed but broken is a fault, and is logged once
    with its root cause.
    """
    global _HAS_LOCAL, _LOCAL_CHECKED, _LOCAL_UNAVAILABLE
    if _LOCAL_CHECKED or _HAS_LOCAL:
        return _HAS_LOCAL
    _LOCAL_CHECKED = True
    try:
        import sentence_transformers  # noqa: F401
        import numpy  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name in ("sentence_transformers", "numpy"):
            _LOCAL_UNAVAILABLE = f"sentence-transformers is not installed ({_LOCAL_EXTRA_HINT})"
            _log_once(
                "local-import", logging.INFO,
                "Local embeddings unavailable: %s.", _LOCAL_UNAVAILABLE,
            )
            return False
        _local_import_failed(exc)
        return False
    except Exception as exc:
        # transformers and torch raise far more than ImportError while
        # importing (AttributeError, OSError, RuntimeError). Anything escaping
        # here would take the whole memory runtime down with it.
        _local_import_failed(exc)
        return False
    _HAS_LOCAL = True
    _LOCAL_UNAVAILABLE = None
    return True


def _local_import_failed(exc: BaseException) -> None:
    global _LOCAL_UNAVAILABLE
    _LOCAL_UNAVAILABLE = (
        f"sentence-transformers is installed but failed to import: {_describe_failure(exc)}"
    )
    _log_once(
        "local-import", logging.WARNING,
        "Semantic recall is off in this process: %s. Without GEMINI_API_KEY, "
        "recall seeds from keywords only.", _LOCAL_UNAVAILABLE,
    )


class _LocalEmbedder:
    """Generates embeddings via local sentence-transformers."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model: Any = None
        self._dims = 384

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _get_model(self):
        if self._model is None:
            self._model = _load_local_model(self._model_name)
        return self._model
    
    def embed(self, text: str) -> list[float] | None:
        model = self._get_model()
        if model is None:
            return None
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    
    def batch_embed(self, texts: list[str]) -> list[list[float] | None]:
        model = self._get_model()
        if model is None:
            return [None] * len(texts)
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [v.tolist() for v in vecs]


_DOWNLOAD_HINT = "if it was never downloaded, run: mnemos embeddings download"


def _hub_repo(model_name: str) -> str:
    """The Hub repository a sentence-transformers model name stands for."""
    return model_name if "/" in model_name else f"sentence-transformers/{model_name}"


def _load_local_model(model_name: str) -> Any:
    """The local model, from files already on this machine, never the network.

    A hanging network must not block recall, health or doctor, and the load
    happens on a session's first meaning search. A model that is not on disk
    fails at once, and semantic recall reports that it is off and why.
    """
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(model_name, local_files_only=True)
    except TypeError:
        # sentence-transformers before local_files_only: load the cached copy
        # by its path, which never asks the Hub.
        from huggingface_hub import snapshot_download

        return SentenceTransformer(
            snapshot_download(repo_id=_hub_repo(model_name), local_files_only=True)
        )


def download_local_model(model_name: str = "all-MiniLM-L6-v2") -> str:
    """Download the local embedding model into the Hugging Face cache, and
    return its name.

    The one step that uses the network, taken only when someone asks for it
    (``mnemos embeddings download``); every other load is local only.
    """
    if not _check_local_deps():
        raise RuntimeError(_LOCAL_UNAVAILABLE or "sentence-transformers is unavailable")
    from sentence_transformers import SentenceTransformer

    SentenceTransformer(model_name)
    return model_name


def _batches(todo: list[tuple[Any, ...]], size: int) -> list[list[tuple[Any, ...]]]:
    """``todo`` (items whose third member is their passages) in order, in
    batches of whole items holding at most ``size`` passages each (an item
    with more is a batch of its own)."""
    batches: list[list[tuple[Any, ...]]] = []
    current: list[tuple[Any, ...]] = []
    count = 0
    for item in todo:
        if current and count + len(item[2]) > size:
            batches.append(current)
            current, count = [], 0
        current.append(item)
        count += len(item[2])
    if current:
        batches.append(current)
    return batches


def _similarities(
    query_values: list[float], rows: list[tuple[str, bytes, int]],
) -> list[tuple[str, float]]:
    """Cosine similarity of the query with each stored vector: ``(id, sim)``.
    A row of another size, or whose bytes disagree with its own dims, is
    skipped, never scored (see ``EmbeddingIndex.search``)."""
    dims = len(query_values)
    usable = [
        (item_id, blob) for item_id, blob, stored_dims in rows
        if stored_dims == dims and len(blob) == struct.calcsize(f"{dims}f")
    ]
    if not usable:
        return []
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        query = np.asarray(query_values, dtype=np.float64)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0:
            return []
        matrix = np.frombuffer(
            b"".join(blob for _, blob in usable), dtype=np.float32,
        ).reshape(len(usable), dims).astype(np.float64)
        norms = np.linalg.norm(matrix, axis=1)
        dots = matrix @ (query / query_norm)
        return [
            (item_id, float(dot / norm))
            for (item_id, _), dot, norm in zip(usable, dots, norms)
            if norm > 0
        ]
    query_norm = sum(v * v for v in query_values) ** 0.5
    if query_norm == 0:
        return []
    unit = [v / query_norm for v in query_values]
    scored = []
    for item_id, blob in usable:
        stored = struct.unpack(f"{dims}f", blob)
        norm = sum(v * v for v in stored) ** 0.5
        if norm:
            scored.append((item_id, sum(q * s for q, s in zip(unit, stored)) / norm))
    return scored


# --- Main index class ---

class EmbeddingIndex:
    """Embedding index for semantic similarity search.

    Backend auto-selection:
      1. Gemini API if GEMINI_API_KEY available (3072 dims, high quality)
      2. Local sentence-transformers fallback (384 dims)
      3. Disabled if neither available

    Usage:
        index = EmbeddingIndex(db_path="~/.mnemos/memory.db")
        index.index_engram("engram_abc123", "The user prefers dark mode")
        results = index.search("UI theme preferences", k=5)
    """

    def __init__(
        self,
        db_path: str | None = None,
        model_name: str | None = None,
        gemini_api_key: str | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self._db_path = db_path
        # A read-only index (doctor, read-only runtimes) opens the database
        # read-only, creates no table and writes no vector.
        self._read_only = read_only
        self._conn: sqlite3.Connection | None = None
        self._embedder: _GeminiEmbedder | _LocalEmbedder | None = None
        self._available = False
        # Why semantic search is off, when it is; what the latest embedding
        # attempt hit, when it failed; whether one has succeeded here yet.
        self._unavailable_reason: str | None = None
        self._last_error: str | None = None
        self._verified = False
        # Whether passage_vectors marks each row's scheme (``_marks_scheme``).
        self._scheme_marked = False

        # Resolve Gemini API key
        api_key = gemini_api_key or _load_env_key("GEMINI_API_KEY")

        if api_key:
            gemini_model = model_name or os.environ.get(
                "MNEMOS_EMBEDDING_MODEL", "gemini-embedding-2-preview"
            )
            self._embedder = _GeminiEmbedder(api_key, gemini_model)
            self._available = True
        elif _check_local_deps():
            local_model = model_name or "all-MiniLM-L6-v2"
            self._embedder = _LocalEmbedder(local_model)
            self._available = True
        else:
            self._unavailable_reason = (
                f"no GEMINI_API_KEY is set, and {_LOCAL_UNAVAILABLE or 'local embeddings are unavailable'}"
            )

        if self._available and db_path and not read_only:
            self._init_table()

    def _init_table(self) -> None:
        conn = self._get_conn()
        if conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    engram_id TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    model_name TEXT NOT NULL,
                    dims INTEGER NOT NULL
                )
            """)
            conn.execute(PASSAGE_TABLE_SQL)
            conn.commit()
            self._marks_scheme(conn, add=True)

    def _marks_scheme(self, conn: sqlite3.Connection, *, add: bool = False) -> bool:
        """Whether ``passage_vectors`` records each row's scheme. Without the
        column (a table R08's code made) no row counts: every item waits.

        With ``add``, a writable index adds it, marking each row there scheme
        1, in one transaction; the value is written into every row, because
        some SQLite builds report an added column's default as NULL in
        ``integrity_check``, which the store runs on every open. A column
        another process added first, or a lock held past the wait, leaves the
        table as it finds it, and a later pass adds it. Never raises."""
        if self._scheme_marked:
            return True
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(passage_vectors)")}
            if columns and "scheme" not in columns and add and not self._read_only:
                try:
                    if not conn.in_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    conn.execute(f"ALTER TABLE passage_vectors ADD COLUMN {_SCHEME_COLUMN}")
                    conn.execute(f"UPDATE passage_vectors SET scheme = {_FIRST_SCHEME}")
                    conn.commit()
                except sqlite3.Error:
                    if conn.in_transaction:
                        conn.rollback()
                columns = {row[1] for row in conn.execute("PRAGMA table_info(passage_vectors)")}
        except sqlite3.Error:
            return False
        self._scheme_marked = "scheme" in columns
        return self._scheme_marked

    def _get_conn(self) -> sqlite3.Connection | None:
        if not self._db_path:
            return None
        if self._conn is None:
            path = Path(self._db_path).expanduser()
            if self._read_only:
                self._conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            else:
                self._conn = sqlite3.connect(str(path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _embed(self, text: str, *, timeout: float | None = None) -> list[float] | None:
        """One text's vector, or None. ``timeout`` bounds a network backend's
        wait (the write path); the local model has no network to wait on."""
        if not self._available or not self._embedder:
            return None
        try:
            if timeout is not None and isinstance(self._embedder, _GeminiEmbedder):
                values = self._embedder.embed(text, timeout=timeout)
            else:
                values = self._embedder.embed(text)
        except Exception as exc:
            self._embedding_failed(_describe_failure(exc))
            return None
        if values is None:
            self._embedding_failed(
                getattr(self._embedder, "last_error", None) or "the backend returned no vector"
            )
            return None
        self._verified = True
        self._last_error = None
        return values

    def _embedding_failed(self, detail: str) -> None:
        """Keep and log (once) why an embedding attempt produced nothing.

        Every caller swallows embedding failures so memory keeps working
        without them, which is right, but it meant a model that never loaded
        looked exactly like a healthy index.
        """
        embedder = self._embedder
        if isinstance(embedder, _LocalEmbedder) and not embedder.loaded:
            # The model never loaded, so every later call would fail the same
            # way, slowly. Stop trying, and say so. It loads only from files
            # on this machine, so a model never downloaded fails here, at once.
            self._available = False
            self._unavailable_reason = (
                f"the local model {embedder.model_name} failed to load from this "
                f"machine ({_DOWNLOAD_HINT}): {detail}"
            )
            _log_once(
                "local-model-load", logging.WARNING,
                "Semantic recall is off in this process: %s", self._unavailable_reason,
            )
            return
        self._last_error = detail
        _log_once(
            f"embed-failed:{self.backend}", logging.WARNING,
            "Embedding failed with %s: %s. Affected memories are stored without "
            "a vector and affected recalls seed from keywords only.",
            self.backend, detail,
        )

    def _to_bytes(self, values: list[float]) -> bytes:
        return struct.pack(f'{len(values)}f', *values)
    
    def _from_bytes(self, data: bytes, dims: int) -> list[float]:
        return list(struct.unpack(f'{dims}f', data))

    @property
    def available(self) -> bool:
        return self._available
    
    @property
    def backend(self) -> str:
        if isinstance(self._embedder, _GeminiEmbedder):
            return f"gemini ({self._embedder.model_name})"
        elif isinstance(self._embedder, _LocalEmbedder):
            return f"local ({self._embedder.model_name})"
        return "none"

    @property
    def unavailable_reason(self) -> str | None:
        """Why semantic search is off in this process, or None when it is on."""
        return None if self._available else (
            self._unavailable_reason or "no embedding backend is configured"
        )

    def verify(self) -> bool:
        """Embed a short probe now, so status() reports a real attempt.

        Costs a model load on first use, which is why only `mnemos doctor`
        calls it; the server reports what its own recalls have seen.
        """
        return self._embed("mnemos semantic recall check") is not None

    def status(self, candidate_ids: Collection[str] | None = None) -> dict[str, Any]:
        """Plain facts about semantic search here. Reads, never writes.

        ``active`` is about this process: a backend is selected and has not
        failed. The counts are about the store: how many stored vectors came
        from the model this process embeds with — the only ones it can
        compare against — and how many came from other models. With
        ``candidate_ids`` (the memories recall may return), also how many of
        those have a comparable vector.
        """
        model = self._embedder.model_name if self._embedder else None
        backend = None
        if isinstance(self._embedder, _GeminiEmbedder):
            backend = "gemini"
        elif isinstance(self._embedder, _LocalEmbedder):
            backend = "local"
        by_model = self._stored_by_model()
        status: dict[str, Any] = {
            "active": self._available,
            "backend": backend,
            "model": model,
            "reason": self.unavailable_reason,
            "verified": self._verified,
            "last_error": self._last_error,
            "embeddings_stored": sum(by_model.values()),
            "embeddings_usable": by_model.get(model, 0) if model and self._available else 0,
            "embeddings_by_model": by_model,
        }
        status["passages_stored"] = self._passages_stored(model) if model else 0
        if candidate_ids is not None:
            status["memories"] = len(candidate_ids)
            status["memories_searchable"] = (
                len(self._ids_with_vectors(model) & set(candidate_ids))
                if model and self._available else 0
            )
        return status

    def _passages_stored(self, model: str) -> int:
        """Passages recall can use: this model's, cut by this scheme or a newer one."""
        conn = self._existing_conn()
        if conn is None or not self._marks_scheme(conn):
            return 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM passage_vectors WHERE model_name = ? AND scheme >= ?",
                (model, PASSAGE_SCHEME),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # no passage table: nothing was cut into passages here
        return int(row[0]) if row else 0

    def _existing_conn(self) -> sqlite3.Connection | None:
        """The connection, but only for a database that already exists.

        sqlite3.connect creates a missing file, and a status read must never
        bring a store into being.
        """
        if not self._db_path or not Path(self._db_path).expanduser().exists():
            return None
        return self._get_conn()

    def _stored_by_model(self) -> dict[str, int]:
        conn = self._existing_conn()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT model_name, COUNT(*) FROM embeddings GROUP BY model_name"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # no embeddings table: nothing was ever embedded here
        return {row[0]: int(row[1]) for row in rows}

    def _ids_with_vectors(self, model: str) -> set[str]:
        """Ids recall can compare with a query by this model: a whole-text
        vector, or passages cut by this scheme or a newer one."""
        conn = self._existing_conn()
        if conn is None:
            return set()
        queries = [("SELECT engram_id FROM embeddings WHERE model_name = ?", (model,))]
        if self._marks_scheme(conn):
            queries.append((
                "SELECT DISTINCT item_id FROM passage_vectors WHERE model_name = ? AND scheme >= ?",
                (model, PASSAGE_SCHEME),
            ))
        ids: set[str] = set()
        for sql, params in queries:
            try:
                ids.update(row[0] for row in conn.execute(sql, params).fetchall())
            except sqlite3.OperationalError:
                continue  # that table was never created here
        return ids

    def index_engram(self, engram_id: str, content: str) -> bool:
        if not self._available or not self._embedder or self._read_only:
            return False

        # Called on the write path (Encoder.finish, after a capture): a slow
        # provider must not hold the capture. Without the vector, the memory
        # waits for passages, which recall uses first anyway.
        values = self._embed(content, timeout=WRITE_NETWORK_TIMEOUT)
        if values is None:
            return False

        conn = self._get_conn()
        if conn is None:
            return False

        conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(engram_id, embedding, model_name, dims) VALUES (?, ?, ?, ?)",
            (engram_id, self._to_bytes(values), self._embedder.model_name, len(values)),
        )
        conn.commit()
        return True

    def search(
        self,
        query: str,
        k: int = 10,
        exclude_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        if not self._available or not self._embedder:
            return []

        query_values = self._embed(query)
        if query_values is None:
            return []

        conn = self._get_conn()
        if conn is None:
            return []

        # Only vectors from the model that embedded the query share its space.
        # Equal size is not enough: gemini-embedding-2 and
        # gemini-embedding-2-preview both produce 3072 values and one store
        # can hold both, so a size check alone would score one model's
        # vectors in the other's space. It also stops every search from
        # reading and unpacking vectors it could never use.
        rows = conn.execute(
            "SELECT engram_id, embedding, dims FROM embeddings WHERE model_name = ?",
            (self._embedder.model_name,),
        ).fetchall()

        if not rows:
            return []

        exclude = exclude_ids or set()
        results: list[tuple[str, float]] = []

        # Normalize query vector
        q_norm = sum(v * v for v in query_values) ** 0.5
        if q_norm == 0:
            return []
        query_normalized = [v / q_norm for v in query_values]

        for row in rows:
            eid = row["engram_id"]
            if eid in exclude:
                continue

            dims = row["dims"]
            blob = row["embedding"]
            # Skip, never score, a vector of another size — and skip a row
            # whose bytes disagree with its own dims rather than letting one
            # bad row raise out of the whole search, which callers swallow as
            # "no semantic seeds" for every query.
            if dims != len(query_normalized) or len(blob) != struct.calcsize(f"{dims}f"):
                continue
            stored = self._from_bytes(blob, dims)

            # Cosine similarity
            s_norm = sum(v * v for v in stored) ** 0.5
            if s_norm == 0:
                continue
            
            dot = sum(q * s for q, s in zip(query_normalized, stored))
            similarity = dot / s_norm
            results.append((eid, round(similarity, 4)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]

    def embed_query(self, text: str) -> list[float] | None:
        """``text``'s vector, as recall compares it with the passages; None
        when embedding it failed (a network backend that timed out, a model
        that won't load): then meaning can't run for it, and a caller should
        act as if it were off. The failure is kept and logged once, as any."""
        return self._embed(text)

    def search_candidates(
        self,
        query: str,
        candidates: Collection[str],
        *,
        k: int = 30,
        floor: float = 0.0,
        texts: Mapping[str, str] | None = None,
        length_penalty: float | None = None,
        query_vector: Sequence[float] | None = None,
    ) -> list[tuple[str, float]]:
        """The ``candidates`` closest in meaning to ``query``: at most ``k``,
        none whose best similarity is below ``floor``, each with that best
        similarity, in the order of its meaning score: the best similarity
        less ``length_penalty`` (λ; None: ``LENGTH_PENALTY``) times the log of
        how many of its vectors were scored. The penalty orders and chooses
        the top ``k``; the floor and the numbers are the similarity's.

        Only the candidates are scored, and only then is the top taken. Recall
        passes what it may return: the memories live in the caller's scope and
        the handoffs. ``search`` takes its top over every stored vector; on a
        real store 3,746 of them, 202 in scope, so across twelve cues 14 of 240
        meaning hits survived the scope check afterwards.

        Each candidate counts by its best vector from this model: its passages
        (``passages``: its sentence windows and its whole text), or its
        whole-text vector while it has no passages yet. Passages cut by an
        older scheme are stale and never count: the item waits to be indexed
        again, like one with none. ``texts`` gives the words some candidates
        hold now (recall passes the notes'): passages cut from other words are
        stale too and never count, so a note rewritten in place (by code older
        than this, say) is never found by its old meaning. It waits as well.
        ``query_vector`` is ``query``'s own vector (``embed_query``), when the
        caller has it: then nothing is embedded here.
        """
        if not self._available or not self._embedder or not candidates:
            return []
        wanted = set(candidates)
        conn = self._existing_conn()
        if conn is None:
            return []
        query_values = list(query_vector) if query_vector is not None else self._embed(query)
        if query_values is None:
            return []
        model = self._embedder.model_name
        cut = self._rows_for(
            conn, "SELECT item_id, embedding, dims, text_hash FROM passage_vectors "
            "WHERE model_name = ? AND scheme >= ? AND item_id IN ({})",
            (model, PASSAGE_SCHEME), wanted,
        ) if self._marks_scheme(conn) else []
        now = {item_id: text_hash(text) for item_id, text in (texts or {}).items()}
        rows = [
            (item_id, blob, dims) for item_id, blob, dims, digest in cut
            if item_id not in now or now[item_id] == digest
        ]
        without = wanted - {row[0] for row in cut}
        if without:
            rows += self._rows_for(
                conn, "SELECT engram_id, embedding, dims FROM embeddings "
                "WHERE model_name = ? AND engram_id IN ({})", (model,), without,
            )
        best: dict[str, float] = {}
        scored: dict[str, int] = {}
        for item_id, similarity in _similarities(query_values, rows):
            scored[item_id] = scored.get(item_id, 0) + 1
            if similarity > best.get(item_id, -2.0):
                best[item_id] = similarity
        penalty = LENGTH_PENALTY if length_penalty is None else float(length_penalty)
        ranked = sorted(
            (item_id for item_id, similarity in best.items() if similarity >= floor),
            key=lambda item_id: -round(best[item_id] - penalty * math.log(scored[item_id]), 4),
        )
        return [(item_id, round(best[item_id], 4)) for item_id in ranked[:k]]

    def searchable(
        self, ids: Collection[str], *, texts: Mapping[str, str] | None = None,
    ) -> set[str]:
        """Which of ``ids`` meaning can find now: the ones ``search_candidates``
        would score, with vectors from this model (passages cut by this scheme
        or a newer one, from the words ``texts`` gives where it gives them, or
        else a whole-text vector). None while semantic search is off. A
        backend being configured says nothing about this: a memory not yet
        indexed, one that failed, or a store indexed by another model has no
        vector to find it by. Reads only; embeds nothing."""
        if not self._available or not self._embedder or not ids:
            return set()
        conn = self._existing_conn()
        if conn is None:
            return set()
        wanted = set(ids)
        model = self._embedder.model_name
        cut = self._rows_for(
            conn, "SELECT DISTINCT item_id, text_hash FROM passage_vectors "
            "WHERE model_name = ? AND scheme >= ? AND item_id IN ({})",
            (model, PASSAGE_SCHEME), wanted,
        ) if self._marks_scheme(conn) else []
        now = {item_id: text_hash(text) for item_id, text in (texts or {}).items()}
        found = {item_id for item_id, digest in cut if item_id not in now or now[item_id] == digest}
        without = wanted - {row[0] for row in cut}
        if without:
            found.update(row[0] for row in self._rows_for(
                conn, "SELECT engram_id FROM embeddings "
                "WHERE model_name = ? AND engram_id IN ({})", (model,), without,
            ))
        return found

    def lessons_searchable(self, lessons: Mapping[str, str]) -> set[str]:
        """Which of ``lessons`` (memory id to the lesson's words now) meaning
        can find by the lesson itself: a lesson passage (``LESSON_PART``) from
        this model, cut by this scheme or a newer one, from these words. A
        memory's other passages say nothing about its lesson's: written after
        the capture, the lesson waits for its own. Reads only."""
        if not self._available or not self._embedder or not lessons:
            return set()
        stored = self.lesson_hashes(lessons)
        return {
            item_id for item_id, lesson in lessons.items()
            if stored.get(item_id) is not None and stored[item_id] == _lesson_hash(lesson)
        }

    @staticmethod
    def _rows_for(
        conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], ids: Collection[str],
    ) -> list[tuple[Any, ...]]:
        """``sql``'s rows for ``ids`` (its ``params`` first), asked in chunks;
        none when the table was never created here."""
        rows: list[tuple[Any, ...]] = []
        ordered = sorted(ids)
        for start in range(0, len(ordered), _ID_CHUNK):
            chunk = ordered[start:start + _ID_CHUNK]
            try:
                rows.extend(
                    tuple(row) for row in conn.execute(
                        sql.format(", ".join("?" for _ in chunk)), (*params, *chunk),
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                return rows
        return rows

    def passage_hashes(self, item_ids: Iterable[str]) -> dict[str, str]:
        """For each of ``item_ids`` with passages from this model, cut by this
        scheme or a newer one, the text they were cut from (``text_hash``).
        An item missing here waits to be indexed. Whether an item waits is
        ``waiting``'s to say: it also asks about the lesson."""
        return self._part_hashes(item_ids, 0)

    def lesson_hashes(self, item_ids: Iterable[str]) -> dict[str, str]:
        """For each of ``item_ids`` with a lesson passage from this model, cut
        by this scheme or a newer one, the lesson it was cut from."""
        return self._part_hashes(item_ids, LESSON_PART)

    def _part_hashes(self, item_ids: Iterable[str], part: int) -> dict[str, str]:
        if not self._embedder:
            return {}
        conn = self._existing_conn()
        if conn is None or not self._marks_scheme(conn):
            return {}
        return {
            item_id: digest for item_id, digest in self._rows_for(
                conn, "SELECT item_id, text_hash FROM passage_vectors "
                "WHERE model_name = ? AND part = ? AND scheme >= ? AND item_id IN ({})",
                (self._embedder.model_name, part, PASSAGE_SCHEME), set(item_ids),
            )
        }

    def waiting(
        self,
        items: Iterable[tuple[str, str]],
        *,
        lessons: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The ids of ``items`` (``(id, text)``) whose passages from this model,
        cut by this scheme or a newer one, are not those of their words now:
        none, cut from other words, or without the lesson ``lessons`` gives the
        item (or with one it no longer has, or another). Every count of what
        waits (indexing, health, the watchdog) asks this."""
        items = [(item_id, text) for item_id, text in items if (text or "").strip()]
        ids = [item_id for item_id, _ in items]
        stored, taught = self.passage_hashes(ids), self.lesson_hashes(ids)
        lessons = lessons or {}
        return [
            item_id for item_id, text in items
            if stored.get(item_id) != text_hash(text)
            or taught.get(item_id) != _lesson_hash(lessons.get(item_id))
        ]

    def index_passages(
        self,
        items: Iterable[tuple[str, str]],
        *,
        lessons: Mapping[str, str] | None = None,
        budget: int | None = None,
        seconds: float | None = None,
    ) -> dict[str, int]:
        """Cut each ``(item_id, text)`` into passages and store a vector for
        each, marked with this scheme, replacing what this model stored for
        that item before; items whose passages already match their text, cut
        by this scheme or a newer one, are skipped (``waiting``). ``lessons``
        gives a memory's lesson (``written_lesson``), stored as one more
        passage, part ``LESSON_PART``, with its own hash: an item whose lesson
        changed is cut again although its words did not.

        ``budget`` bounds the passages embedded in one call. An item is never
        half-written: one whose passages do not fit in what is left of the
        budget, even the first, is not started, costs no embedding call and
        waits for a pass with room (the scheduled job's budget holds any item
        whole, and ``mnemos embeddings index`` has none). ``seconds`` (the
        write path: ``WRITE_EMBED_SECONDS``) sets one deadline for the pass:
        embedding calls go in small batches, no request starts after the
        deadline, a network backend waits on each until it at most (and at
        most ``WRITE_NETWORK_TIMEOUT``) with no retry, the first batch that
        yields nothing ends the pass, and what the time does not cover waits.
        The local model's one-time load comes before the clock. Nothing here
        raises: a failure leaves items waiting. Returns how many items and
        passages were written and how many items still wait.
        """
        done = {"items": 0, "passages": 0, "waiting": 0}
        if not self._available or not self._embedder or self._read_only:
            return done
        conn = self._get_conn()
        if conn is None:
            return done
        items = [(item_id, text) for item_id, text in items if (text or "").strip()]
        if not self._marks_scheme(conn, add=True):
            # A table that cannot mark its rows' scheme yet: what this pass
            # wrote would count as an older cut. Everything waits for the next.
            done["waiting"] = len(items)
            return done
        lessons = lessons or {}
        stale = set(self.waiting(items, lessons=lessons))
        todo: list[tuple[str, str, list[str], str | None]] = []
        planned = 0
        for item_id, text in items:
            if item_id not in stale:
                continue
            parts = passages(text)
            taught = _lesson_hash(lessons.get(item_id))
            if taught is not None:
                parts = [*parts, lesson_passage(lessons[item_id])]
            if budget is not None and planned + len(parts) > budget:
                # Up to 161 passages (a long handoff) against 64 on a
                # capture's automatic pass: it waits, and what fits goes on.
                done["waiting"] += 1
                continue
            todo.append((item_id, text_hash(text), parts, taught))
            planned += len(parts)
        if not todo:
            return done
        deadline: float | None = None
        if seconds is None:
            batches = [todo]
        else:
            if isinstance(self._embedder, _LocalEmbedder):
                try:
                    self._embedder._get_model()
                except Exception as exc:
                    self._embedding_failed(_describe_failure(exc))
                    done["waiting"] += len(todo)
                    return done
            batches = _batches(todo, _WRITE_CHUNK)
            # One deadline for the whole pass. Each batch used to get its own
            # wait, and a batch of more than 100 passages is two requests to
            # a network backend, each given all of it: a provider that never
            # answered held a 2 s write for 4 s.
            deadline = time.monotonic() + seconds
        embedded: list[tuple[str, str, list[list[float]], str | None]] = []
        for number, batch in enumerate(batches):
            if deadline is not None and time.monotonic() >= deadline:
                done["waiting"] += sum(len(rest) for rest in batches[number:])
                break
            flat = [part for _, _, parts, _ in batch for part in parts]
            try:
                vectors = self._embed_many(flat, deadline=deadline)
            except Exception as exc:
                self._embedding_failed(_describe_failure(exc))
                done["waiting"] += sum(len(rest) for rest in batches[number:])
                break
            position = 0
            kept = 0
            for item_id, digest, parts, taught in batch:
                values = vectors[position:position + len(parts)]
                position += len(parts)
                if len(values) != len(parts) or any(v is None for v in values):
                    done["waiting"] += 1
                    continue
                embedded.append((item_id, digest, values, taught))
                kept += 1
            if not kept:
                failure = getattr(self._embedder, "last_error", None)
                if failure:
                    self._embedding_failed(failure)
                if seconds is not None:
                    # Nothing came back: fail fast, and the rest waits.
                    done["waiting"] += sum(len(rest) for rest in batches[number + 1:])
                    break
        if not embedded:
            return done
        model = self._embedder.model_name
        written = {"items": 0, "passages": 0}
        try:
            for item_id, digest, values, taught in embedded:
                conn.execute(
                    "DELETE FROM passage_vectors WHERE item_id = ? AND model_name = ?",
                    (item_id, model),
                )
                # The text's own passages keep the text's hash, as scheme 2
                # wrote it; the lesson's row keeps the lesson's.
                rows = [(part, digest, v) for part, v in enumerate(values)]
                if taught is not None:
                    rows[-1] = (LESSON_PART, taught, values[-1])
                conn.executemany(
                    "INSERT INTO passage_vectors "
                    "(item_id, model_name, part, text_hash, dims, embedding, scheme) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (item_id, model, part, hashed, len(v), self._to_bytes(v), PASSAGE_SCHEME)
                        for part, hashed, v in rows
                    ],
                )
                written["items"] += 1
                written["passages"] += len(values)
            conn.commit()
        except sqlite3.Error as exc:
            # Another writer holds the lock, or the table is missing. Nothing
            # of this call was kept; it waits for the next one, and recall
            # meanwhile uses the whole-text vectors and the words.
            conn.rollback()
            _log_once(
                "passages-write", logging.WARNING,
                "Passage vectors were not written: %s", _describe_failure(exc),
            )
            done["waiting"] += len(embedded)
            return done
        done.update(written)
        return done

    def _embed_many(self, texts: list[str], *, deadline: float | None = None) -> list[Any]:
        """Vectors for ``texts``. With ``deadline`` (the write path: a
        ``time.monotonic()`` moment), a network backend starts no request
        after it, waits on each until it at most (and at most
        ``WRITE_NETWORK_TIMEOUT``), and does not retry one text at a time: a
        text it could not embed gives None."""
        if deadline is not None and isinstance(self._embedder, _GeminiEmbedder):
            return self._embedder.batch_embed(
                texts, timeout=WRITE_NETWORK_TIMEOUT, fallback=False, deadline=deadline,
            )
        return self._embedder.batch_embed(texts)

    def batch_index(self, items: list[tuple[str, str]]) -> int:
        if not self._available or not self._embedder or self._read_only:
            return 0

        conn = self._get_conn()
        if conn is None:
            return 0

        texts = [content for _, content in items]
        try:
            all_values = self._embedder.batch_embed(texts)
        except Exception as exc:
            self._embedding_failed(_describe_failure(exc))
            return 0

        count = 0
        for (engram_id, _), values in zip(items, all_values):
            if values is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(engram_id, embedding, model_name, dims) VALUES (?, ?, ?, ?)",
                (engram_id, self._to_bytes(values), self._embedder.model_name, len(values)),
            )
            count += 1

        conn.commit()
        return count

    def remove(self, engram_id: str) -> None:
        if self._read_only:
            return
        conn = self._get_conn()
        if conn:
            conn.execute("DELETE FROM embeddings WHERE engram_id = ?", (engram_id,))
            conn.commit()

    def count(self) -> int:
        conn = self._get_conn()
        if conn is None:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
