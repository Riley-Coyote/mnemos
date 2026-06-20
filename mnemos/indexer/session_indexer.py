"""
Session indexer — ingest conversation transcripts into Mnemos.

Reads recent session transcripts, extracts structured memories via LLM,
and feeds them into Mnemos via the encoder. Tracks indexing state to avoid
re-processing.

Usage:
    indexer = SessionIndexer(agent_id="myagent", db_path="~/.mnemos/myagent.db")
    indexer.run()       # Index recent sessions
    indexer.backfill()  # Index sessions from last 24h
    indexer.status()    # Show indexing state
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("mnemos.indexer")

# ---------------------------------------------------------------------------
# Defaults & Constants
# ---------------------------------------------------------------------------

# Map extraction memory types to Mnemos engram kinds
TYPE_TO_KIND = {
    "fact": "semantic",
    "event": "episodic",
    "decision": "semantic",
    "pattern": "semantic",
    "lesson": "semantic",
    "context": "episodic",
    "relationship": "semantic",
    "impression": "episodic",
    "analysis": "semantic",
}

# Extra tags by extraction type
TYPE_TAGS = {
    "decision": ["decision"],
    "lesson": ["lesson"],
    "pattern": ["pattern"],
    "relationship": ["relationship"],
}

DEFAULT_MAX_MEMORIES_PER_SESSION = 15
DEFAULT_MIN_SESSION_MESSAGES = 6
DEFAULT_SESSION_WINDOW_HOURS = 6
DEFAULT_BACKFILL_WINDOW_HOURS = 24
DEFAULT_MIN_SESSION_SIZE_BYTES = 5000
DEFAULT_MAX_CHUNK_CHARS = 12000
DEFAULT_MAX_CHUNKS_PER_SESSION = 8
DEFAULT_LLM_RETRIES = 2
DEFAULT_EXTRACTION_MODEL = "deepseek/deepseek-v3.2"
DEFAULT_CLASSIFICATION_MODEL = "google/gemini-2.5-flash"
DEFAULT_FALLBACK_MODEL = "google/gemini-2.5-flash"
DEFAULT_ENCODE_TIMEOUT = 30


class SessionIndexer:
    """Extract memories from session transcripts and encode them into Mnemos.

    Args:
        agent_id: Unique agent identifier. Falls back to ``MNEMOS_AGENT_ID`` env var,
            then ``"default"``.
        db_path: Path to the Mnemos SQLite database. Falls back to
            ``MNEMOS_DB`` env var, then ``~/.mnemos/{agent_id}.db``.
        sessions_dirs: List of directories to search for ``.jsonl`` session files.
            Falls back to ``MNEMOS_SESSIONS_DIRS`` (colon-separated) env var, then
            common OpenClaw session locations.
        known_projects: Project names for the extraction prompt. Falls back to
            ``MNEMOS_KNOWN_PROJECTS`` (comma-separated) env var.
        active_projects: Active project subset. Falls back to
            ``MNEMOS_ACTIVE_PROJECTS`` (comma-separated) env var.
        user_name: Name shown for ``user`` role in transcripts (default ``"User"``).
        agent_name: Name shown for ``assistant`` role in transcripts (default ``agent_id``).
        extraction_model: OpenRouter model for memory extraction.
        classification_model: OpenRouter model for connection classification during batch encoding.
        fallback_model: Fallback model when primary fails.
        openrouter_api_key: API key. Falls back to ``OPENROUTER_API_KEY`` env var.
        config: Optional dict to override any of the above or tuning constants.
    """

    def __init__(
        self,
        agent_id: str | None = None,
        db_path: str | None = None,
        sessions_dirs: list[str | Path] | None = None,
        known_projects: list[str] | None = None,
        active_projects: list[str] | None = None,
        user_name: str | None = None,
        agent_name: str | None = None,
        extraction_model: str | None = None,
        classification_model: str | None = None,
        fallback_model: str | None = None,
        openrouter_api_key: str | None = None,
        llm_client=None,
        config: dict[str, Any] | None = None,
    ) -> None:
        cfg = config or {}

        self.agent_id = (
            agent_id
            or cfg.get("agent_id")
            or os.environ.get("MNEMOS_AGENT_ID")
            or "default"
        )
        self.db_path = (
            db_path
            or cfg.get("db_path")
            or os.environ.get("MNEMOS_DB")
            or str(Path.home() / ".mnemos" / f"{self.agent_id}.db")
        )
        self.user_name = user_name or cfg.get("user_name", "User")
        self.agent_name = agent_name or cfg.get("agent_name", self.agent_id.capitalize())

        # Session directories
        if sessions_dirs:
            self._sessions_dirs = [Path(d) for d in sessions_dirs]
        elif cfg.get("sessions_dirs"):
            self._sessions_dirs = [Path(d) for d in cfg["sessions_dirs"]]
        elif os.environ.get("MNEMOS_SESSIONS_DIRS"):
            self._sessions_dirs = [
                Path(d) for d in os.environ["MNEMOS_SESSIONS_DIRS"].split(":")
            ]
        else:
            self._sessions_dirs = [
                Path.home() / ".openclaw" / "agents" / "main" / "sessions",
                Path.home() / ".openclaw" / "sessions",
            ]

        # Projects
        self.known_projects = (
            known_projects
            or cfg.get("known_projects")
            or _env_list("MNEMOS_KNOWN_PROJECTS")
            or []
        )
        self.active_projects = (
            active_projects
            or cfg.get("active_projects")
            or _env_list("MNEMOS_ACTIVE_PROJECTS")
            or []
        )

        # LLM models
        self.extraction_model = (
            extraction_model or cfg.get("extraction_model", DEFAULT_EXTRACTION_MODEL)
        )
        self.classification_model = (
            classification_model or cfg.get("classification_model", DEFAULT_CLASSIFICATION_MODEL)
        )
        self.fallback_model = (
            fallback_model or cfg.get("fallback_model", DEFAULT_FALLBACK_MODEL)
        )
        self._api_key = (
            openrouter_api_key
            or cfg.get("openrouter_api_key")
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )
        # Optional injected LLM client (e.g. subscription ClaudeCLIClient).
        # When set, extraction routes through it instead of the OpenRouter API.
        self._llm_client = llm_client or cfg.get("llm_client")

        # Tuning constants
        self.max_memories_per_session = cfg.get("max_memories_per_session", DEFAULT_MAX_MEMORIES_PER_SESSION)
        self.min_session_messages = cfg.get("min_session_messages", DEFAULT_MIN_SESSION_MESSAGES)
        self.session_window_hours = cfg.get("session_window_hours", DEFAULT_SESSION_WINDOW_HOURS)
        self.backfill_window_hours = cfg.get("backfill_window_hours", DEFAULT_BACKFILL_WINDOW_HOURS)
        self.min_session_size_bytes = cfg.get("min_session_size_bytes", DEFAULT_MIN_SESSION_SIZE_BYTES)
        self.max_chunk_chars = cfg.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)
        self.max_chunks_per_session = cfg.get("max_chunks_per_session", DEFAULT_MAX_CHUNKS_PER_SESSION)
        self.llm_retries = cfg.get("llm_retries", DEFAULT_LLM_RETRIES)
        self.encode_timeout = cfg.get("encode_timeout", DEFAULT_ENCODE_TIMEOUT)

        # State file
        self._state_file = Path.home() / ".mnemos" / f"{self.agent_id}_indexing_state.json"

        # Prompt template (loaded lazily)
        self._extractor_prompt: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, window_hours: int | None = None) -> dict[str, Any]:
        """Index recent sessions. Returns summary stats."""
        window = window_hours or self.session_window_hours
        return self._index(window)

    def backfill(self) -> dict[str, Any]:
        """Index sessions from a wider window (default 24h)."""
        logger.info("Running backfill (%dh window)", self.backfill_window_hours)
        return self._index(self.backfill_window_hours)

    def status(self) -> dict[str, Any]:
        """Return indexing state summary."""
        state = self._load_state()
        total_sessions = len(state.get("indexed_sessions", {}))
        total_memories = state.get("total_memories_encoded", 0)
        last_run = state.get("last_run", None)

        recent = sorted(
            state.get("indexed_sessions", {}).items(),
            key=lambda x: x[1].get("indexed_at", ""),
            reverse=True,
        )[:5]

        return {
            "last_run": last_run,
            "sessions_indexed": total_sessions,
            "total_memories_encoded": total_memories,
            "recent": [
                {"session": k, "memories": v.get("memories_encoded", 0), "at": v.get("indexed_at")}
                for k, v in recent
            ],
        }

    # ------------------------------------------------------------------
    # Core indexing loop
    # ------------------------------------------------------------------

    def _index(self, window_hours: int) -> dict[str, Any]:
        """Main indexing loop."""
        state = self._load_state()
        session_files = self._find_session_files(window_hours)

        if not session_files:
            logger.info("No recent sessions to index")
            return {"sessions_processed": 0, "memories_encoded": 0}

        logger.info("Found %d recent session files (window: %dh)", len(session_files), window_hours)

        total_encoded = 0
        sessions_processed = 0

        for path in session_files:
            session_key = path.stem

            # Check if already indexed at this file size
            last_indexed = state["indexed_sessions"].get(session_key, {})
            last_size = last_indexed.get("size", 0)
            current_size = path.stat().st_size

            if current_size == last_size:
                continue  # No new content

            logger.info("Indexing: %s", session_key)

            # Skip very small files
            if current_size < self.min_session_size_bytes:
                logger.debug("Skipping %s — too small (%d bytes)", session_key, current_size)
                state["indexed_sessions"][session_key] = {
                    "size": current_size,
                    "indexed_at": _now_iso(),
                    "memories_encoded": 0,
                    "skipped": "too_small",
                }
                continue

            messages = self._read_session_transcript(path)
            if len(messages) < self.min_session_messages:
                logger.debug(
                    "Skipping %s — only %d messages (need %d)",
                    session_key, len(messages), self.min_session_messages,
                )
                state["indexed_sessions"][session_key] = {
                    "size": current_size,
                    "indexed_at": _now_iso(),
                    "memories_encoded": 0,
                    "skipped": "too_few_messages",
                }
                continue

            transcript = self._format_transcript(messages)

            # Extract memories via LLM
            raw_memories = self._extract_memories(transcript)
            if not raw_memories:
                logger.info("No memories extracted from %s", session_key)
                state["indexed_sessions"][session_key] = {
                    "size": current_size,
                    "indexed_at": _now_iso(),
                    "memories_encoded": 0,
                }
                continue

            logger.info("Extracted %d memories, encoding to Mnemos...", len(raw_memories))

            encoded = self._encode_to_mnemos(raw_memories, session_key)
            total_encoded += encoded
            sessions_processed += 1

            state["indexed_sessions"][session_key] = {
                "size": current_size,
                "indexed_at": _now_iso(),
                "memories_encoded": encoded,
            }

        state["last_run"] = _now_iso()
        state["total_memories_encoded"] = state.get("total_memories_encoded", 0) + total_encoded
        self._save_state(state)

        logger.info(
            "Indexing complete: %d memories encoded from %d sessions",
            total_encoded, sessions_processed,
        )
        return {"sessions_processed": sessions_processed, "memories_encoded": total_encoded}

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"indexed_sessions": {}, "last_run": None, "total_memories_encoded": 0}

    def _save_state(self, state: dict) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2))

    # ------------------------------------------------------------------
    # Session discovery
    # ------------------------------------------------------------------

    def _find_session_files(self, window_hours: int) -> list[Path]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        files: list[Path] = []

        for sessions_dir in self._sessions_dirs:
            if not sessions_dir.exists():
                continue
            for f in sessions_dir.rglob("*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                    if mtime > cutoff:
                        files.append(f)
                except OSError:
                    continue

        if not files:
            logger.debug("No sessions directory found or no recent files")

        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def _read_session_transcript(self, path: Path, max_messages: int = 100) -> list[dict]:
        messages: list[dict] = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)

                        # OpenClaw v3 format
                        if entry.get("type") == "message":
                            msg = entry.get("message", {})
                        elif entry.get("role") in ("user", "assistant"):
                            msg = entry
                        else:
                            continue

                        role = msg.get("role")
                        if role not in ("user", "assistant"):
                            continue

                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                p.get("text", "")
                                for p in content
                                if isinstance(p, dict) and p.get("type") == "text"
                            )

                        if content and len(content) > 10:
                            messages.append({
                                "role": role,
                                "content": content[:3000],
                            })
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning("Error reading %s: %s", path, e)

        return messages[-max_messages:]

    def _format_transcript(self, messages: list[dict]) -> str:
        lines = []
        for msg in messages:
            name = self.user_name if msg["role"] == "user" else self.agent_name
            lines.append(f"**{name}**: {msg['content']}")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # LLM extraction
    # ------------------------------------------------------------------

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        # Fallback: read from openclaw config
        config_path = Path.home() / ".openclaw" / "openclaw.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                key = cfg.get("openRouterApiKey", "")
                if key:
                    self._api_key = key
                    return key
            except Exception:
                pass
        return ""

    def _load_extraction_prompt(self) -> str:
        if self._extractor_prompt is not None:
            return self._extractor_prompt

        # Try package-bundled prompt first
        prompt_file = Path(__file__).resolve().parent / "prompts" / "extractor.md"
        if prompt_file.exists():
            template = prompt_file.read_text()
            self._extractor_prompt = template.replace("{agent_name}", self.agent_name)
            return self._extractor_prompt

        # Inline fallback
        self._extractor_prompt = (
            f"You are {self.agent_name}'s memory extraction system. "
            "Extract structured memories from the conversation. Return a JSON array."
        )
        return self._extractor_prompt

    def _call_extraction_llm(self, prompt: str, system: str) -> Optional[str]:
        import urllib.request

        # Prefer an injected client (e.g. subscription ClaudeCLIClient) so
        # indexing does not depend on a live OpenRouter key. Falls through to
        # the OpenRouter path below only when no client is configured.
        if self._llm_client is not None:
            for attempt in range(self.llm_retries + 1):
                if attempt > 0:
                    time.sleep(2 ** (attempt - 1))
                try:
                    out = self._llm_client.structured_complete(
                        system, prompt, temperature=0.2, max_tokens=4000
                    )
                    if out and out.strip():
                        return out.strip()
                except Exception as e:
                    logger.warning(
                        "extraction client call failed (attempt %d): %s", attempt + 1, e
                    )
            logger.error(
                "extraction client returned no output after %d attempts",
                self.llm_retries + 1,
            )
            return None

        key = self._get_api_key()
        if not key:
            logger.warning("No OpenRouter API key found")
            return None

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(self.llm_retries + 1):
            model = self.extraction_model if attempt < self.llm_retries else self.fallback_model

            if attempt > 0:
                wait = 2 ** (attempt - 1)
                if model != self.extraction_model:
                    logger.info("Attempt %d: falling back to %s", attempt + 1, model)
                else:
                    logger.info("Attempt %d: retrying in %ds...", attempt + 1, wait)
                time.sleep(wait)

            body = json.dumps({
                "model": model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 4000,
            }).encode()

            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )

            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                if attempt < self.llm_retries:
                    logger.warning("LLM call failed (attempt %d): %s", attempt + 1, e)
                    continue
                logger.error("LLM call failed after %d attempts: %s", self.llm_retries + 1, e)
                return None

    def _chunk_transcript(self, transcript: str) -> list[str]:
        if len(transcript) <= self.max_chunk_chars:
            return [transcript]

        parts = transcript.split("\n\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for part in parts:
            if current_len + len(part) > self.max_chunk_chars and current:
                chunks.append("\n\n".join(current))
                current = [part]
                current_len = len(part)
            else:
                current.append(part)
                current_len += len(part) + 2

        if current:
            chunks.append("\n\n".join(current))

        return chunks[:self.max_chunks_per_session]

    def _extract_memories(self, transcript: str) -> list[dict]:
        system = self._load_extraction_prompt()

        chunks = self._chunk_transcript(transcript)
        if len(chunks) > 1:
            logger.info("Chunking: %d chars -> %d chunks", len(transcript), len(chunks))

        all_memories: list[dict] = []
        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                logger.info("Extracting chunk %d/%d...", i + 1, len(chunks))

            known = ", ".join(self.known_projects) if self.known_projects else "(none configured)"
            active = ", ".join(self.active_projects) if self.active_projects else "(none configured)"
            prompt = (
                f"Extract memories from this conversation transcript.\n\n"
                f"Known projects: {known}\n"
                f"Active projects: {active}\n\n"
                f"Transcript:\n{chunk}"
            )

            response = self._call_extraction_llm(prompt, system)
            if not response:
                continue

            try:
                json_match = re.search(r"\[[\s\S]*\]", response)
                if json_match:
                    raw_memories = json.loads(json_match.group())
                    all_memories.extend(raw_memories)
                else:
                    logger.warning("No JSON array found in chunk %d response", i + 1)
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse chunk %d response: %s", i + 1, e)

        # Validate and normalize
        valid: list[dict] = []
        for raw in all_memories:
            if not isinstance(raw, dict) or not raw.get("content"):
                continue
            mem_type = raw.get("type", "fact")
            if mem_type not in TYPE_TO_KIND:
                mem_type = "fact"
            raw["type"] = mem_type
            valid.append(raw)

        return valid[:self.max_memories_per_session]

    # ------------------------------------------------------------------
    # Mnemos encoding
    # ------------------------------------------------------------------

    def _encode_to_mnemos(self, memories: list[dict], session_key: str) -> int:
        from mnemos.store.sqlite_store import EngramStore
        from mnemos.core.types import SourceType
        from mnemos.encoding.encoder import Encoder

        store = EngramStore(self.db_path)

        # Optional: embedding index
        embedding_index = None
        try:
            from mnemos.store.embedding_index import EmbeddingIndex
            embedding_index = EmbeddingIndex(db_path=self.db_path)
        except Exception:
            logger.debug("Embedding index not available, proceeding without it")

        # Optional: LLM client for connection classification.
        # When extraction runs through an injected client (subscription CLI),
        # skip LLM classification: per-connection CLI calls are too slow and the
        # CLI's prose output does not satisfy the classifier's strict JSON parse,
        # so connection inference falls back to embeddings. Only build the
        # OpenRouter classifier when no client was injected and a key exists.
        llm_client = None
        if self._llm_client is None:
            api_key = self._get_api_key()
            if api_key:
                try:
                    from mnemos.llm import OpenRouterClient
                    llm_client = OpenRouterClient(
                        api_key=api_key,
                        model=self.classification_model,
                        max_tokens=2000,
                    )
                except Exception:
                    logger.debug("LLM client not available for connection classification")

        # Optional: shared pool
        shared_pool = None
        try:
            from mnemos.multiagent.shared_pool import SharedPool
            shared_pool = SharedPool()
        except Exception:
            logger.debug("Shared pool not available")

        encoder = Encoder(
            store,
            embedding_index=embedding_index,
            llm_client=llm_client,
            shared_pool=shared_pool,
        )

        encoded = 0
        for raw in memories:
            mem_type = raw["type"]
            content = raw["content"]
            kind = TYPE_TO_KIND.get(mem_type, "semantic")

            # Build tags
            tags = list(raw.get("tags", []))
            tags.extend(TYPE_TAGS.get(mem_type, []))
            tags.append(f"trace-type:{mem_type}")
            tags.append("session-indexed")

            projects = raw.get("projects", [])
            for p in projects:
                if p not in tags:
                    tags.append(p)

            # Build impact
            impact = ""
            if mem_type == "decision" and raw.get("reasoning"):
                impact = raw["reasoning"]
                alternatives = raw.get("alternatives", [])
                if alternatives:
                    impact += f" Alternatives considered: {', '.join(alternatives)}"
            elif mem_type == "lesson":
                impact = content

            # Map salience to confidence
            salience = raw.get("salience", 0.5)
            confidence = max(0.40, min(0.90, salience))

            try:
                class _EncodeTimeout(Exception):
                    pass

                def _timeout_handler(signum, frame):
                    raise _EncodeTimeout()

                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(self.encode_timeout)

                try:
                    engram = encoder.encode(
                        content=content,
                        impact=impact,
                        kind=kind,
                        tags=tags,
                        source=SourceType.SESSION,
                        session_id=session_key,
                        agent_id=self.agent_id,
                        override_confidence=confidence,
                        override_confidence_source="trace_extraction",
                    )
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

                encoded += 1
                conn_count = len(engram.connections)
                logger.info(
                    "  [%s->%s] %s%s",
                    mem_type, kind, content[:60],
                    f" ({conn_count} connections)" if conn_count else "",
                )
            except _EncodeTimeout:
                logger.warning("Timeout encoding: %s (skipped)", content[:60])
                continue
            except Exception as e:
                logger.warning("Failed to encode: %s", str(e)[:80])
                continue

        return encoded


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_list(var: str) -> list[str]:
    """Read a comma-separated env var into a list."""
    val = os.environ.get(var, "").strip()
    if not val:
        return []
    return [s.strip() for s in val.split(",") if s.strip()]
