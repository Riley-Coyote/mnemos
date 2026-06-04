"""Simple-mode continuity runtime for Mnemos.

This module is intentionally MCP-agnostic so the product path can be tested
without a running client. It exposes the real Mnemos stack through five simple
operations: context, capture, recall, correct, and maintain.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config.loader import load_config
from .consolidation.daemon import ConsolidationDaemon
from .core.types import SourceType
from .encoding.encoder import Encoder
from .retrieval.reactive import ReactiveRetriever
from .store.embedding_index import EmbeddingIndex
from .store.sqlite_store import EngramStore


SIMPLE_TOOL_NAMES = (
    "mnemos_context",
    "mnemos_capture",
    "mnemos_recall",
    "mnemos_correct",
    "mnemos_maintain",
)


def _slugify(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return clean or fallback


def _default_project_scope() -> str:
    cwd = Path.cwd()
    if cwd.name:
        return _slugify(cwd.name, "global")
    return "global"


@dataclass(frozen=True)
class MnemosScope:
    """Resolved identity and storage scope for simple mode."""

    agent_id: str
    person_id: str
    project_scope: str
    db_path: str


def resolve_scope(
    *,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> MnemosScope:
    """Resolve Mnemos identity from explicit args, env, config, then defaults."""

    try:
        config = load_config()
    except Exception:
        config = {}

    resolved_agent = _slugify(
        agent_id
        or os.environ.get("MNEMOS_AGENT_ID", "")
        or str(config.get("agent_id", ""))
        or "mnemos-agent",
        "mnemos-agent",
    )
    resolved_person = _slugify(
        person_id
        or os.environ.get("MNEMOS_PERSON_ID", "")
        or str(config.get("person_id", ""))
        or str(config.get("user_name", ""))
        or "user",
        "user",
    )
    resolved_project = _slugify(
        project_scope
        or os.environ.get("MNEMOS_PROJECT_SCOPE", "")
        or str(config.get("project_scope", ""))
        or _default_project_scope(),
        "global",
    )

    explicit_db = db_path or os.environ.get("MNEMOS_DB_PATH")
    if explicit_db:
        resolved_db = explicit_db
    else:
        store_config = config.get("store", {}) if isinstance(config.get("store"), dict) else {}
        configured = store_config.get("db_path")
        if configured and configured != "~/.mnemos/memory.db":
            resolved_db = str(configured)
        else:
            resolved_db = f"~/.mnemos/{resolved_agent}.db"

    return MnemosScope(
        agent_id=resolved_agent,
        person_id=resolved_person,
        project_scope=resolved_project,
        db_path=resolved_db,
    )


def _dedicated_model_requested() -> bool:
    """Return true only when simple mode has explicit model configuration."""

    explicit_env = (
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_MODEL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    )
    if any(os.environ.get(key) for key in explicit_env):
        return True
    try:
        config = load_config()
    except Exception:
        return False
    llm_config = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    return bool(llm_config.get("provider") or llm_config.get("model"))


def _classify_kind(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("how to", "process", "workflow", "steps", "procedure")):
        return "procedural"
    if any(marker in text for marker in ("todo", "remember to", "next time", "follow up", "should do")):
        return "prospective"
    if any(marker in text for marker in ("decided", "built", "debugged", "met", "changed", "fixed")):
        return "episodic"
    return "semantic"


def _classify_domain(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("identity", "who i am", "who you are", "selfhood")):
        return "identity"
    if any(marker in text for marker in ("always", "preference", "prefers", "principle", "boundary")):
        return "foundational"
    if any(marker in text for marker in ("again", "recurring", "pattern", "usually", "often")):
        return "recurring"
    if any(marker in text for marker in ("roadmap", "long term", "long-term", "arc", "future")):
        return "long-arc"
    if any(marker in text for marker in ("current", "today", "now", "temporary", "session")):
        return "situational"
    return "topical"


def _simple_tags(content: str, context: str = "") -> list[str]:
    text = f"{content} {context}".lower()
    tags = ["continuity"]
    for label, markers in {
        "preference": ("prefer", "preference", "likes", "wants"),
        "decision": ("decided", "decision", "chosen", "agreed"),
        "project": ("project", "repo", "workspace", "build"),
        "identity": ("identity", "agent", "user", "self"),
        "correction": ("correction", "wrong", "update", "forget"),
    }.items():
        if any(marker in text for marker in markers):
            tags.append(label)
    return sorted(set(tags))


_STOPWORDS = {
    "about",
    "after",
    "agent",
    "before",
    "continuity",
    "context",
    "durable",
    "memory",
    "mnemos",
    "note",
    "notes",
    "should",
    "that",
    "this",
    "when",
    "with",
}


def _query_terms(query: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-zA-Z0-9]+", query.lower())
        if len(term) >= 3 and term not in _STOPWORDS
    }


def _has_query_overlap(query: str, text: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    text_terms = _query_terms(text)
    return bool(terms & text_terms)


def _filter_continuity(query: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not query.strip():
        return entries
    return [
        entry for entry in entries
        if _has_query_overlap(query, entry.get("content", ""))
        or float(entry.get("score", 0.0)) >= 0.55
    ]


def _filter_memories(query: str, results: list[Any]) -> list[Any]:
    if not query.strip():
        return results
    filtered = []
    for result in results:
        engram = result.engram
        searchable = " ".join([
            engram.content or "",
            engram.impact or "",
            " ".join(engram.tags or []),
        ])
        if _has_query_overlap(query, searchable) or float(result.score) >= 1.35:
            filtered.append(result)
    return filtered


class MnemosRuntime:
    """High-level continuity interface used by simple MCP mode and tests."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        agent_id: str | None = None,
        person_id: str | None = None,
        project_scope: str | None = None,
        use_dedicated_model: bool = True,
    ) -> None:
        self.scope = resolve_scope(
            db_path=db_path,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )
        self._store: EngramStore | None = None
        self._encoder: Encoder | None = None
        self._retriever: ReactiveRetriever | None = None
        self._embedding_index: EmbeddingIndex | None = None
        self._llm_client: Any | None = None
        self._use_dedicated_model = use_dedicated_model

    @property
    def db_path(self) -> Path:
        return Path(self.scope.db_path).expanduser()

    @property
    def has_dedicated_model(self) -> bool:
        self._ensure_init()
        return self._llm_client is not None

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
        self._store = None
        self._encoder = None
        self._retriever = None
        self._embedding_index = None
        self._llm_client = None

    def _ensure_init(self) -> None:
        if self._store is not None:
            return

        self._store = EngramStore(self.scope.db_path)
        self._embedding_index = EmbeddingIndex(db_path=self.scope.db_path)
        try:
            from .llm import create_client

            self._llm_client = (
                create_client()
                if self._use_dedicated_model and _dedicated_model_requested()
                else None
            )
        except Exception:
            self._llm_client = None

        self._encoder = Encoder(
            self._store,
            embedding_index=self._embedding_index,
            llm_client=self._llm_client,
        )
        self._retriever = ReactiveRetriever(
            self._store,
            embedding_index=self._embedding_index,
        )

    def _stats(self) -> dict[str, Any]:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_stats(self.scope.agent_id)

    def context(self, query: str = "", max_results: int = 5) -> str:
        """Return the startup continuity packet for an agent."""

        self._ensure_init()
        assert self._store is not None

        maintenance = self.maintain(auto=True)
        stats = self._stats()
        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results,
        )
        continuity = _filter_continuity(query, continuity)
        memories = self._retrieve(query, max_results=max_results) if query else []

        lines = [
            "Mnemos continuity packet",
            f"Scope: agent={self.scope.agent_id} person={self.scope.person_id} project={self.scope.project_scope}",
            "Storage: local SQLite store ready",
            (
                "Status: "
                f"{stats.get('engrams_active', 0)} memories, "
                f"{stats.get('hypomnema_active', 0)} continuity notes, "
                f"{stats.get('connections', 0)} connections"
            ),
            "",
            "Use this at the start of a session. Capture important preferences, decisions, project state, corrections, and durable context as the conversation unfolds.",
            "",
            "Maintenance:",
            _indent(maintenance),
        ]

        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        else:
            lines.extend(["", "Continuity notes: none yet. Capture durable context when the user gives it."])

        if memories:
            lines.extend(["", "Relevant memories:"])
            lines.extend(_format_memory(result) for result in memories)

        return "\n".join(lines)

    def capture(
        self,
        content: str,
        context: str = "",
        importance: str | float = "auto",
    ) -> str:
        """Capture durable continuity without exposing Mnemos internals."""

        if not content.strip():
            return "Nothing captured: content was empty."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        full_content = content.strip()
        if context.strip():
            full_content = f"{full_content}\n\nContext: {context.strip()}"

        domain = _classify_domain(full_content)
        kind = _classify_kind(full_content)
        tags = _simple_tags(content, context)
        confidence, salience = _importance_scores(importance, domain)
        impact = _impact_for(content, domain)

        engram = self._encoder.encode(
            content=full_content,
            impact=impact,
            kind=kind,
            tags=tags,
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            override_confidence=confidence,
            skip_surprise_detection=True,
        )
        note_id = self._store.write_hypomnema_entry(
            content.strip(),
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=domain in {"foundational", "identity"},
            related_engram_id=engram.id,
        )
        self._store.mark_hypomnema_promoted(note_id, engram.id)
        maintenance = self.maintain(auto=True)

        return (
            "Captured continuity.\n"
            f"Memory ID: {engram.id}\n"
            f"Continuity note ID: {note_id}\n"
            f"Scope: {self.scope.agent_id}/{self.scope.person_id}/{self.scope.project_scope}\n"
            "Maintenance:\n"
            f"{_indent(maintenance)}"
        )

    def recall(self, query: str, max_results: int = 5) -> str:
        """Recall relevant continuity and durable memories."""

        if not query.strip():
            return "Recall needs a query."

        self._ensure_init()
        assert self._store is not None

        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results,
        )
        continuity = _filter_continuity(query, continuity)
        memories = self._retrieve(query, max_results=max_results)

        if not continuity and not memories:
            return "No relevant continuity found."

        lines = [f"Mnemos recall for: {query.strip()}"]
        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        if memories:
            lines.extend(["", "Durable memories:"])
            lines.extend(_format_memory(result) for result in memories)
        return "\n".join(lines)

    def correct(
        self,
        correction: str,
        target_id: str = "",
        query: str = "",
        action: str = "update",
    ) -> str:
        """Correct, supersede, or archive stale memory."""

        if not correction.strip() and action not in {"forget", "archive", "remove", "delete"}:
            return "Correction needs replacement text or a forget/archive action."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        action = action.strip().lower() or "update"
        target = target_id.strip()

        if target:
            hypo = self._store.get_hypomnema_entry(
                target,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if hypo is not None:
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        target,
                        reason=f"simple correction action={action}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = hypo.get("related_engram_id") or hypo.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    return f"Archived continuity note {target}."

                self._store.revise_hypomnema_entry(
                    target,
                    correction,
                    reason="simple correction",
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    confidence=0.92,
                    salience=0.75,
                )
                return f"Updated continuity note {target}."

            engram = self._store.get_engram(target)
            if engram is not None:
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                if action in {"forget", "archive", "remove", "delete"} and not correction.strip():
                    return f"Archived memory {target}."
                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact="Correction to earlier continuity.",
                    kind=_classify_kind(correction),
                    tags=["continuity", "correction"],
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                )
                return (
                    f"Archived memory {target} and captured correction {replacement.id}.\n"
                    f"Correction: {correction.strip()}"
                )

        search_text = query.strip() or correction.strip()
        query_text = query.strip()
        if query_text:
            matches = self._store.search_hypomnema(
                query_text,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                limit=1,
            )
            if matches:
                match = matches[0]
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        match["id"],
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    maintenance = self.maintain(auto=True)
                    return (
                        f"Archived closest continuity note {match['id']}.\n"
                        "Maintenance:\n"
                        f"{_indent(maintenance)}"
                    )

                note_id = match["id"]
                if action in {"supersede", "replace"}:
                    note_id = self._store.supersede_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                else:
                    self._store.revise_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        confidence=0.92,
                        salience=0.75,
                    )

                related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                if related_engram_id:
                    related = self._store.get_engram(related_engram_id)
                    if related is not None:
                        self._store.archive_engram(related, reason=f"simple_correction_{action}")

                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact="Corrected continuity for future interactions.",
                    kind=_classify_kind(correction),
                    tags=sorted(set(["continuity", "correction", *_simple_tags(correction)])),
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                )
                self._store.mark_hypomnema_promoted(note_id, replacement.id)
                maintenance = self.maintain(auto=True)
                return (
                    f"Updated closest continuity note {note_id}.\n"
                    f"Memory ID: {replacement.id}\n"
                    "Maintenance:\n"
                    f"{_indent(maintenance)}"
                )

        if action in {"forget", "archive", "remove", "delete"} and search_text:
            matches = self._retrieve(search_text, max_results=1)
            if matches:
                engram = matches[0].engram
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                return f"Archived closest matching memory {engram.id}."

        return self.capture(
            correction.strip(),
            context=f"Correction supplied through mnemos_correct. Prior query: {query.strip()}",
            importance="high",
        )

    def maintain(self, deep: bool = False, auto: bool = False) -> str:
        """Run the best available maintenance without requiring setup."""

        self._ensure_init()
        assert self._store is not None

        requested_deep = bool(deep)
        can_run_deep = requested_deep and self._llm_client is not None
        daemon = ConsolidationDaemon(
            store=self._store,
            config={},
            llm_client=self._llm_client if can_run_deep else None,
            embedding_index=self._embedding_index,
        )
        stats = daemon.run_cycle(deep=can_run_deep, agent_id=self.scope.agent_id)
        promoted = self._promote_candidates(limit=3)
        if can_run_deep:
            completed = "model-assisted deep maintenance completed"
        elif requested_deep:
            completed = "local deterministic maintenance completed; model-assisted deep pass unavailable"
        else:
            completed = "local deterministic maintenance completed"

        model_note = "dedicated model available" if self._llm_client else "no dedicated model configured"
        if requested_deep and not can_run_deep:
            model_note += "; deep requested, ran local deterministic maintenance"
        elif not requested_deep:
            model_note += "; ran local deterministic maintenance"
        if auto:
            model_note += " during normal use"

        lines = [
            f"Requested: {'deep' if requested_deep else 'standard'}",
            f"Cycle: {stats.get('cycle_type', 'shallow')}",
            f"Completed: {completed}",
            f"Passes: {', '.join(stats.get('passes_run', [])) or 'none'}",
            f"Promoted continuity notes: {promoted}",
            f"Model path: {model_note}",
        ]
        errors = [key for key in stats if key.endswith("_error")]
        for key in errors:
            lines.append(f"{key}: {stats[key]}")
        return "\n".join(lines)

    def _retrieve(self, query: str, max_results: int = 5) -> list[Any]:
        assert self._store is not None
        assert self._retriever is not None
        emotional_state = self._store.get_latest_emotional_state(self.scope.agent_id)
        return _filter_memories(query, self._retriever.retrieve(
            cue=query,
            agent_id=self.scope.agent_id,
            max_results=max(1, max_results),
            emotional_state=emotional_state,
        ))

    def _promote_candidates(self, limit: int = 3) -> int:
        assert self._store is not None
        assert self._encoder is not None
        candidates = self._store.get_hypomnema_promotion_candidates(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
        )
        promoted = 0
        for entry in candidates:
            if entry.get("related_engram_id"):
                self._store.mark_hypomnema_promoted(entry["id"], entry["related_engram_id"])
                promoted += 1
                continue
            engram = self._encoder.encode(
                content=entry["content"],
                impact="Stable continuity promoted during simple maintenance.",
                kind="semantic",
                tags=["continuity", "promoted", *entry.get("tags", [])],
                source=SourceType.BACKGROUND,
                agent_id=self.scope.agent_id,
                override_confidence=float(entry["confidence"]),
                skip_surprise_detection=True,
            )
            self._store.mark_hypomnema_promoted(entry["id"], engram.id)
            promoted += 1
        return promoted


def _importance_scores(importance: str | float, domain: str) -> tuple[float, float]:
    if isinstance(importance, (float, int)):
        normalized_score = min(max(float(importance), 0.0), 1.0)
        confidence = min(max(0.55 + (normalized_score * 0.4), 0.55), 0.95)
        salience = min(max(0.35 + (normalized_score * 0.55), 0.35), 0.9)
        return confidence, salience

    normalized = str(importance).strip().lower()
    if normalized in {"low", "minor"}:
        return 0.72, 0.45
    if normalized in {"high", "important", "critical"}:
        return 0.92, 0.82
    if domain in {"foundational", "identity"}:
        return 0.9, 0.8
    if domain in {"recurring", "long-arc"}:
        return 0.86, 0.72
    return 0.82, 0.66


def _impact_for(content: str, domain: str) -> str:
    if domain in {"foundational", "identity"}:
        return "Foundational continuity for future interactions."
    if domain == "recurring":
        return "Recurring pattern worth carrying across sessions."
    if domain == "long-arc":
        return "Long-arc context that should shape future work."
    if domain == "situational":
        return "Current working context for continuity."
    if "prefer" in content.lower() or "wants" in content.lower():
        return "Preference to respect in future decisions."
    return "Durable continuity captured from the session."


def _format_continuity(entry: dict[str, Any]) -> str:
    score = entry.get("score", 0.0)
    content = entry["content"].replace("\n", " ")
    if len(content) > 180:
        content = content[:177] + "..."
    return (
        f"- [{score:.2f}] {content}\n"
        f"  id={entry['id']} domain={entry['domain']} confidence={entry['confidence']:.2f}"
    )


def _format_memory(result: Any) -> str:
    engram = result.engram
    display = engram.impact or engram.content
    display = display.replace("\n", " ")
    if len(display) > 180:
        display = display[:177] + "..."
    return (
        f"- [{result.score:.2f}] {display}\n"
        f"  id={engram.id} kind={engram.kind} confidence={engram.source.confidence:.2f}"
    )


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())
