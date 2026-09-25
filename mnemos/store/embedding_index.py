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
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
import urllib.request
import urllib.error
from collections.abc import Collection
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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

    def embed(self, text: str) -> list[float] | None:
        """Generate embedding for a single text."""
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
            resp = urllib.request.urlopen(req, timeout=30)
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
    
    def batch_embed(self, texts: list[str]) -> list[list[float] | None]:
        """Embed multiple texts via batchEmbedContents API."""
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
            payload = json.dumps({"requests": batch}).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            
            try:
                resp = urllib.request.urlopen(req, timeout=120)
                data = json.loads(resp.read())
                for emb in data.get("embeddings", []):
                    values = emb.get("values", [])
                    if values:
                        self._dims = len(values)
                        all_results.append(values)
                    else:
                        all_results.append(None)
            except Exception:
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
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
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
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._embedder: _GeminiEmbedder | _LocalEmbedder | None = None
        self._available = False
        # Why semantic search is off, when it is; what the latest embedding
        # attempt hit, when it failed; whether one has succeeded here yet.
        self._unavailable_reason: str | None = None
        self._last_error: str | None = None
        self._verified = False

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

        if self._available and db_path:
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
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection | None:
        if not self._db_path:
            return None
        if self._conn is None:
            self._conn = sqlite3.connect(str(Path(self._db_path).expanduser()))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _embed(self, text: str) -> list[float] | None:
        if not self._available or not self._embedder:
            return None
        try:
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
            # way, slowly. Stop trying, and say so.
            self._available = False
            self._unavailable_reason = (
                f"the local model {embedder.model_name} failed to load: {detail}"
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
        if candidate_ids is not None:
            status["memories"] = len(candidate_ids)
            status["memories_searchable"] = (
                len(self._ids_with_vectors(model) & set(candidate_ids))
                if model and self._available else 0
            )
        return status

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
        conn = self._existing_conn()
        if conn is None:
            return set()
        try:
            rows = conn.execute(
                "SELECT engram_id FROM embeddings WHERE model_name = ?", (model,)
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row[0] for row in rows}

    def index_engram(self, engram_id: str, content: str) -> bool:
        if not self._available or not self._embedder:
            return False

        values = self._embed(content)
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

    def batch_index(self, items: list[tuple[str, str]]) -> int:
        if not self._available or not self._embedder:
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
