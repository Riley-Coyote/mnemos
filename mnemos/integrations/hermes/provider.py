"""Hermes MemoryProvider adapter backed by the Mnemos runtime."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...simple_runtime import MnemosRuntime
from ...store.sqlite_store import READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW
from .scope import (
    HermesMnemosConfig,
    HermesScope,
    derive_hermes_scope,
    save_hermes_mnemos_config,
)

logger = logging.getLogger(__name__)

_TRIVIAL_RE = re.compile(
    r"^(ok|okay|thanks|thank you|got it|sure|yes|no|yep|nope|k|ty|thx|np)\.?$",
    re.IGNORECASE,
)
_CONTEXT_TAG_RE = re.compile(r"<[^>]*memory-context[^>]*>[\s\S]*?</[^>]*memory-context>", re.IGNORECASE)
_MNEMOS_CONTEXT_RE = re.compile(r"#?\s*Mnemos (Recall|Identity Continuity|continuity packet)[\s\S]*", re.IGNORECASE)


IDENTITY_CAPTURE_SCHEMA = {
    "name": "mnemos_identity_capture",
    "description": (
        "Persist scoped identity-continuity notes in Mnemos. Use for stable preferences, "
        "self-model facts, corrections, session handoffs, and project decisions that should "
        "survive future Hermes sessions. Set review=true for uncertain claims."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The durable memory or continuity note to persist."},
            "context": {"type": "string", "description": "Optional provenance or session context."},
            "importance": {
                "description": "Importance as low, auto, high, critical, or a 0-1 numeric score.",
                "anyOf": [{"type": "string"}, {"type": "number"}],
            },
            "review": {
                "type": "boolean",
                "description": "Queue as uncertain instead of promoting as high-confidence continuity.",
            },
        },
        "required": ["content"],
    },
}

IDENTITY_RECALL_SCHEMA = {
    "name": "mnemos_identity_recall",
    "description": (
        "Recall scoped Mnemos identity continuity for the active Hermes agent, person, and project. "
        "Use when an answer depends on prior preferences, corrections, self-model, project decisions, "
        "or handoff context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Recall query."},
            "max_results": {"type": "integer", "description": "Maximum results to return."},
            "include_graph": {"type": "boolean", "description": "Include structured identity graph summary data."},
        },
        "required": ["query"],
    },
}

IDENTITY_CORRECT_SCHEMA = {
    "name": "mnemos_identity_correct",
    "description": (
        "Correct, supersede, archive, or forget stale Mnemos identity continuity. Use when the user "
        "updates a remembered preference, self-model fact, decision, or correction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "correction": {"type": "string", "description": "Replacement continuity text."},
            "target_id": {"type": "string", "description": "Known Mnemos memory or continuity note ID."},
            "query": {"type": "string", "description": "Search phrase for the stale memory if no ID is known."},
            "action": {
                "type": "string",
                "enum": ["update", "revise", "supersede", "replace", "archive", "forget", "remove"],
            },
        },
    },
}

IDENTITY_REPORT_SCHEMA = {
    "name": "mnemos_identity_report",
    "description": (
        "Inspect scoped Mnemos identity continuity. Produces a compact context packet, identity graph, "
        "review inbox, status, or maintenance report for the active scope."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["context", "graph", "inbox", "status", "maintain"],
                "description": "Report kind.",
            },
            "query": {"type": "string", "description": "Optional query for context reports."},
            "max_results": {"type": "integer", "description": "Maximum results or graph nodes."},
            "deep": {"type": "boolean", "description": "Request optional model-assisted maintenance if configured."},
        },
        "required": ["kind"],
    },
}


def _json_ok(**payload: Any) -> str:
    payload.setdefault("ok", True)
    return json.dumps(payload, ensure_ascii=True, default=str)


def _json_error(message: str, **payload: Any) -> str:
    payload.update({"ok": False, "error": message})
    return json.dumps(payload, ensure_ascii=True, default=str)


def _clean_text(text: Any) -> str:
    value = str(text or "")
    value = _CONTEXT_TAG_RE.sub("", value)
    value = _MNEMOS_CONTEXT_RE.sub("", value)
    return value.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n[Mnemos context truncated to fit configured budget]\n\n"
    keep = max(0, limit - len(marker))
    head = int(keep * 0.72)
    tail = keep - head
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _is_trivial(text: str) -> bool:
    return not text.strip() or bool(_TRIVIAL_RE.match(text.strip()))


def _importance_value(value: Any, fallback: str | float = "auto") -> str | float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip().lower()
    if raw in {"low", "minor", "auto", "high", "important", "critical"}:
        return raw
    return fallback


def _domain_for(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("identity", "who i am", "who you are", "selfhood", "soul.md")):
        return "identity"
    if any(marker in lowered for marker in ("always", "preference", "prefers", "principle", "boundary")):
        return "foundational"
    if any(marker in lowered for marker in ("again", "recurring", "usually", "often")):
        return "recurring"
    if any(marker in lowered for marker in ("roadmap", "long term", "long-term", "future", "arc")):
        return "long-arc"
    if any(marker in lowered for marker in ("current", "today", "temporary", "session")):
        return "situational"
    return "topical"


def _tags_for(text: str, *extra: str) -> list[str]:
    lowered = text.lower()
    tags = {"hermes", "continuity", *[tag for tag in extra if tag]}
    markers = {
        "preference": ("prefer", "preference", "likes", "wants"),
        "decision": ("decided", "decision", "going with", "will use"),
        "project": ("project", "repo", "workspace", "build"),
        "identity": ("identity", "agent", "self", "soul.md", "who i am"),
        "correction": ("correction", "wrong", "forget", "stale"),
        "handoff": ("handoff", "delegated", "subagent"),
    }
    for label, terms in markers.items():
        if any(term in lowered for term in terms):
            tags.add(label)
    return sorted(tags)


class MnemosMemoryProviderCore:
    """Hermes-compatible provider implementation without importing Hermes."""

    def __init__(self, config: HermesMnemosConfig | None = None):
        self._config = config
        self._scope: HermesScope | None = None
        self._runtime: MnemosRuntime | None = None
        self._session_id = ""
        self._prefetch_cache: dict[str, tuple[str, str]] = {}
        self._writes_enabled = True
        self._turn_count = 0

    @property
    def name(self) -> str:
        return "mnemos"

    @property
    def scope(self) -> HermesScope:
        self._ensure_runtime()
        assert self._scope is not None
        return self._scope

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home")
        config = self._config or HermesMnemosConfig.load(hermes_home)
        self._scope = derive_hermes_scope(
            session_id=session_id,
            hermes_home=hermes_home,
            config=config,
            runtime_context=kwargs,
        )
        self._config = config
        self._session_id = self._scope.session_id
        self._writes_enabled = self._scope.agent_context in {"", "primary"}
        self._runtime = MnemosRuntime(
            db_path=self._scope.db_path,
            agent_id=self._scope.agent_id,
            person_id=self._scope.person_id,
            project_scope=self._scope.project_scope,
            use_dedicated_model=config.deep_maintenance,
        )
        Path(self._scope.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._runtime.context(max_results=1)
        if config.auto_bootstrap and self._writes_enabled:
            self._bootstrap_identity(kwargs)

    def system_prompt_block(self) -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        assert self._config is not None
        packet = self._runtime.context(max_results=min(4, self._config.max_recall_results))
        body = (
            "# Mnemos Identity Continuity\n"
            "Mnemos is active in Provider Mode as Hermes' external memory provider for "
            "identity continuity. Hermes built-in memory remains active. Mnemos is local-first, "
            "scoped by agent, person, and project, and never overwrites SOUL.md, AGENTS.md, "
            "MEMORY.md, or USER.md.\n\n"
            f"Scope: agent={self.scope.agent_id} person={self.scope.person_id} "
            f"project={self.scope.project_scope}\n\n"
            "Use Mnemos tools only when identity continuity matters: durable preferences, "
            "corrections, self-model updates, project decisions, session persistence, "
            "delegation handoffs, or continuity reports.\n\n"
            f"{packet}"
        )
        return _truncate(body, self._config.max_context_chars)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        assert self._config is not None
        if not self._config.auto_recall:
            return ""
        clean = _clean_text(query)
        if not clean:
            return ""
        cache_key = session_id or self._session_id or self.scope.session_id
        cached = self._prefetch_cache.get(cache_key)
        if cached and cached[0] == clean:
            return cached[1]
        recalled = self._runtime.recall(clean, max_results=self._config.max_recall_results)
        if "No relevant continuity found" in recalled:
            return ""
        context = _truncate("# Mnemos Recall\n" + recalled, self._config.max_context_chars)
        self._prefetch_cache[cache_key] = (clean, context)
        return context

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        context = self.prefetch(query, session_id=session_id)
        if not context:
            return
        cache_key = session_id or self._session_id or self.scope.session_id
        self._prefetch_cache[cache_key] = (_clean_text(query), context)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._writes_enabled:
            return
        self._ensure_runtime()
        assert self._config is not None
        if not self._config.auto_capture:
            return
        candidates = self._extract_turn_candidates(user_content, assistant_content, session_id=session_id)
        if messages and not candidates:
            candidates = self._distill_messages(messages[-6:], reason="turn-sync")
        for candidate in candidates[:3]:
            if candidate.get("review", False) and self._config.capture_uncertain:
                self._queue_review_item(
                    candidate["content"],
                    reason=candidate.get("reason", "turn sync"),
                    context=candidate.get("context", ""),
                )
            elif not candidate.get("review", False):
                self._capture(candidate["content"], candidate.get("context", ""), candidate.get("importance", "auto"))

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            IDENTITY_CAPTURE_SCHEMA,
            IDENTITY_RECALL_SCHEMA,
            IDENTITY_CORRECT_SCHEMA,
            IDENTITY_REPORT_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "mnemos_identity_capture":
                return self._tool_capture(args)
            if tool_name == "mnemos_identity_recall":
                return self._tool_recall(args)
            if tool_name == "mnemos_identity_correct":
                return self._tool_correct(args)
            if tool_name == "mnemos_identity_report":
                return self._tool_report(args)
            return _json_error(f"Unknown Mnemos tool: {tool_name}")
        except Exception as exc:
            logger.debug("Mnemos tool %s failed", tool_name, exc_info=True)
            return _json_error(str(exc), tool=tool_name)

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._turn_count = int(turn_number or self._turn_count + 1)
        if not self._writes_enabled:
            return
        self._ensure_runtime()
        assert self._config is not None
        assert self._runtime is not None
        interval = self._config.maintenance_interval
        if interval and self._turn_count > 0 and self._turn_count % interval == 0:
            self._runtime.maintain(auto=True)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not self._writes_enabled:
            return
        self._ensure_runtime()
        assert self._config is not None
        assert self._runtime is not None
        if self._config.auto_session_distill:
            for item in self._distill_messages(messages, reason="session-end")[:5]:
                self._capture(item["content"], item["context"], item["importance"])
        self._runtime.maintain(auto=True)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        self._session_id = str(new_session_id or "")
        if reset:
            self._prefetch_cache.clear()
        if parent_session_id and self._writes_enabled:
            self._capture(
                f"Hermes session continuity moved from {parent_session_id} to {new_session_id}.",
                context=f"Session switch metadata: {json.dumps(kwargs, sort_keys=True, default=str)}",
                importance=0.6,
            )

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        if not self._writes_enabled:
            return ""
        distilled = self._distill_messages(messages, reason="pre-compression")
        saved: list[str] = []
        for item in distilled[:5]:
            self._capture(item["content"], item["context"], item["importance"])
            saved.append(item["content"])
        if not saved:
            return ""
        return "Mnemos preserved these identity-critical facts before compression:\n" + "\n".join(
            f"- {item}" for item in saved
        )

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any) -> None:
        if not self._writes_enabled:
            return
        clean_task = _clean_text(task)
        clean_result = _clean_text(result)
        if not clean_task and not clean_result:
            return
        self._capture(
            "Hermes delegation handoff recorded: "
            f"task={clean_task[:500]} result={clean_result[:700]}",
            context=f"child_session_id={child_session_id}; metadata={json.dumps(kwargs, sort_keys=True, default=str)}",
            importance=0.78,
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_runtime()
        assert self._config is not None
        assert self._runtime is not None
        if not self._writes_enabled or not self._config.mirror_builtin_memory:
            return
        clean = _clean_text(content)
        if not clean:
            return
        action = (action or "").lower()
        context = (
            f"Mirrored from Hermes built-in {target} memory. "
            f"Metadata: {json.dumps(metadata or {}, sort_keys=True, default=str)}"
        )
        if action in {"add", "replace"}:
            self._capture(clean, context=context, importance="high" if target == "user" else "auto")
        elif action in {"remove", "delete", "forget"}:
            self._runtime.correct("", query=clean, action="forget")

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "db_path", "description": "Mnemos identity-continuity SQLite path", "default": "$HERMES_HOME/mnemos/mnemos.db"},
            {"key": "agent_id", "description": "Optional fixed Mnemos agent identity scope"},
            {"key": "person_id", "description": "Optional fixed person/user identity scope"},
            {"key": "project_scope", "description": "Optional fixed project continuity scope"},
            {"key": "auto_recall", "description": "Auto-inject scoped identity continuity", "default": "true", "choices": ["true", "false"]},
            {"key": "auto_capture", "description": "Auto-capture durable identity-continuity facts", "default": "true", "choices": ["true", "false"]},
            {"key": "auto_bootstrap", "description": "Seed identity from SOUL.md/context files without editing them", "default": "true", "choices": ["true", "false"]},
            {"key": "deep_maintenance", "description": "Enable optional model-assisted Mnemos maintenance when model keys exist", "default": "false", "choices": ["true", "false"]},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        save_hermes_mnemos_config(hermes_home, values)

    def shutdown(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
        self._runtime = None
        self._scope = None
        self._prefetch_cache.clear()

    def _tool_capture(self, args: dict[str, Any]) -> str:
        content = _clean_text(args.get("content"))
        if not content:
            return _json_error("content is required")
        context = _clean_text(args.get("context"))
        importance = _importance_value(args.get("importance"), "auto")
        if bool(args.get("review")):
            note_id = self._queue_review_item(content, reason="tool review=true", context=context)
            return _json_ok(status="queued_for_review", continuity_note_id=note_id, scope=asdict(self.scope))
        result = self._capture(content, context, importance)
        return _json_ok(status="captured", result=result, scope=asdict(self.scope))

    def _tool_recall(self, args: dict[str, Any]) -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        query = _clean_text(args.get("query"))
        if not query:
            return _json_error("query is required")
        max_results = self._bounded_int(args.get("max_results"), default=self._config.max_recall_results if self._config else 6)
        recall = self._runtime.recall(query, max_results=max_results)
        payload: dict[str, Any] = {"query": query, "result": recall, "scope": asdict(self.scope)}
        if bool(args.get("include_graph")):
            graph = self._runtime.identity_graph(max_nodes=max_results)
            payload["graph"] = {
                "summary": graph["summary"],
                "stats": graph["stats"],
                "nodes": graph["nodes"],
                "edges": graph["edges"],
            }
        return _json_ok(**payload)

    def _tool_correct(self, args: dict[str, Any]) -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        result = self._runtime.correct(
            correction=_clean_text(args.get("correction")),
            target_id=_clean_text(args.get("target_id")),
            query=_clean_text(args.get("query")),
            action=str(args.get("action") or "update"),
        )
        return _json_ok(status="corrected", result=result, scope=asdict(self.scope))

    def _tool_report(self, args: dict[str, Any]) -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        kind = str(args.get("kind") or "status").strip().lower()
        max_results = self._bounded_int(args.get("max_results"), default=self._config.max_recall_results if self._config else 6)
        if kind == "context":
            return _json_ok(kind=kind, result=self._runtime.context(_clean_text(args.get("query")), max_results=max_results), scope=asdict(self.scope))
        if kind == "graph":
            return _json_ok(kind=kind, graph=self._runtime.identity_graph(max_nodes=max_results), scope=asdict(self.scope))
        if kind == "inbox":
            return _json_ok(kind=kind, inbox=self._review_inbox(limit=max_results), scope=asdict(self.scope))
        if kind == "maintain":
            return _json_ok(kind=kind, result=self._runtime.maintain(deep=bool(args.get("deep")), auto=False), scope=asdict(self.scope))
        if kind == "status":
            return _json_ok(kind=kind, scope=asdict(self.scope), config=asdict(self._config) if self._config else {})
        return _json_error(f"Unsupported report kind: {kind}")

    def _ensure_runtime(self) -> None:
        if self._runtime is not None:
            return
        self.initialize(self._session_id or "mnemos-hermes")

    def _capture(self, content: str, context: str = "", importance: str | float = "auto") -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        return self._runtime.capture(content, context=context, importance=importance)

    def _queue_review_item(self, content: str, *, reason: str, context: str = "") -> str:
        self._ensure_runtime()
        assert self._runtime is not None
        self._runtime._ensure_init()
        assert self._runtime._store is not None
        note = content.strip()
        if context.strip():
            note = f"{note}\n\nContext: {context.strip()}"
        return self._runtime._store.write_hypomnema_entry(
            note,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            domain=_domain_for(note),
            tags=_tags_for(note, "review", "inbox", "uncertain"),
            confidence=0.46,
            salience=0.42,
            foundational=False,
            related_session_id=self._session_id,
            read_visibility=READ_VISIBILITY_REVIEW,
        )

    def _review_inbox(self, limit: int = 10) -> list[dict[str, Any]]:
        self._ensure_runtime()
        assert self._runtime is not None
        self._runtime._ensure_init()
        assert self._runtime._store is not None
        entries = self._runtime._store.search_hypomnema(
            "",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max(1, min(50, limit * 3)),
            read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
        )
        review = [
            entry for entry in entries
            if float(entry.get("confidence", 1.0)) < 0.62
            or {"review", "inbox", "uncertain"} & set(entry.get("tags", []))
        ]
        return review[:limit]

    def _bootstrap_identity(self, runtime_context: dict[str, Any]) -> None:
        self._ensure_runtime()
        assert self._runtime is not None
        self._runtime._ensure_init()
        assert self._runtime._store is not None
        existing = self._runtime._store.search_hypomnema(
            "Hermes identity bootstrap",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=3,
        )
        if any("bootstrap" in entry.get("tags", []) for entry in existing):
            return
        seeds = self._identity_seed_texts(runtime_context)
        for seed in seeds[:4]:
            self._runtime._store.write_hypomnema_entry(
                seed["content"],
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                source="co-formed",
                domain=seed["domain"],
                tags=_tags_for(seed["content"], "bootstrap"),
                confidence=seed["confidence"],
                salience=seed["salience"],
                foundational=seed["domain"] in {"identity", "foundational"},
                related_session_id=self._session_id,
            )

    def _identity_seed_texts(self, runtime_context: dict[str, Any]) -> list[dict[str, Any]]:
        seeds: list[dict[str, Any]] = []
        soul = self.scope.hermes_home / "SOUL.md"
        if soul.exists():
            text = _clean_text(soul.read_text(encoding="utf-8", errors="replace"))[:1200]
            if text:
                seeds.append({
                    "content": f"Hermes identity bootstrap from SOUL.md: {text}",
                    "domain": "identity",
                    "confidence": 0.72,
                    "salience": 0.74,
                })

        for name in (".hermes.md", "HERMES.md", "AGENTS.md"):
            path = Path.cwd() / name
            if path.exists():
                text = _clean_text(path.read_text(encoding="utf-8", errors="replace"))[:900]
                if text:
                    seeds.append({
                        "content": f"Hermes project-context bootstrap from {name}: {text}",
                        "domain": "foundational",
                        "confidence": 0.68,
                        "salience": 0.65,
                    })
                break

        profile = runtime_context.get("agent_identity")
        platform = runtime_context.get("platform")
        if profile or platform:
            seeds.append({
                "content": (
                    "Hermes runtime identity bootstrap: "
                    f"profile={profile or 'default'} platform={platform or 'cli'} "
                    f"scope={self.scope.agent_id}/{self.scope.person_id}/{self.scope.project_scope}."
                ),
                "domain": "identity",
                "confidence": 0.66,
                "salience": 0.58,
            })
        return seeds

    def _extract_turn_candidates(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        user = _clean_text(user_content)
        assistant = _clean_text(assistant_content)
        if _is_trivial(user) and _is_trivial(assistant):
            return []

        candidates: list[dict[str, Any]] = []
        context = f"Hermes completed turn. session_id={session_id or self._session_id}"
        user_lower = user.lower()
        assistant_lower = assistant.lower()

        durable_user_markers = (
            "remember",
            "i prefer",
            "i want you to",
            "please always",
            "don't ",
            "do not ",
            "call me",
            "my name is",
            "we decided",
            "let's use",
            "going with",
            "correction",
            "actually,",
            "current project",
        )
        if any(marker in user_lower for marker in durable_user_markers):
            candidates.append({
                "content": f"User continuity from Hermes turn: {user[:1400]}",
                "context": context,
                "importance": "high",
                "review": False,
                "reason": "durable user marker",
            })

        assistant_markers = (
            "implemented",
            "added",
            "fixed",
            "verified",
            "changed",
            "created",
            "configured",
            "completed",
        )
        if any(marker in assistant_lower for marker in assistant_markers) and len(assistant) > 80:
            candidates.append({
                "content": f"Hermes project outcome: {assistant[:1400]}",
                "context": context,
                "importance": 0.72,
                "review": False,
                "reason": "assistant project outcome",
            })

        uncertain_markers = ("maybe remember", "might be useful", "not sure", "possibly", "could matter")
        if any(marker in user_lower for marker in uncertain_markers):
            candidates.append({
                "content": f"Uncertain Hermes continuity candidate: {user[:900]}",
                "context": context,
                "importance": 0.42,
                "review": True,
                "reason": "uncertain user claim",
            })
        return candidates

    def _distill_messages(self, messages: list[dict[str, Any]], *, reason: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for message in messages[-30:]:
            role = str(message.get("role") or "")
            content = _clean_text(message.get("content"))
            if not content or _is_trivial(content):
                continue
            lowered = content.lower()
            if role == "user" and any(
                marker in lowered
                for marker in (
                    "remember",
                    "prefer",
                    "decided",
                    "correction",
                    "actually",
                    "current project",
                    "always",
                    "do not",
                )
            ):
                candidates.append({
                    "content": f"Hermes {reason} user continuity: {content[:1200]}",
                    "context": f"Distilled during {reason}; session_id={self._session_id}",
                    "importance": "high",
                })
            elif role == "assistant" and any(
                marker in lowered for marker in ("implemented", "verified", "fixed", "added", "completed")
            ):
                candidates.append({
                    "content": f"Hermes {reason} outcome: {content[:1200]}",
                    "context": f"Distilled during {reason}; session_id={self._session_id}",
                    "importance": 0.7,
                })
        return candidates

    @staticmethod
    def _bounded_int(value: Any, *, default: int) -> int:
        try:
            return max(1, min(48, int(value)))
        except Exception:
            return default


def build_memory_provider_class(memory_provider_base: type) -> type:
    """Build a Hermes ``MemoryProvider`` subclass without hard-importing Hermes."""

    class MnemosMemoryProvider(MnemosMemoryProviderCore, memory_provider_base):  # type: ignore[misc, valid-type]
        pass

    MnemosMemoryProvider.__name__ = "MnemosMemoryProvider"
    MnemosMemoryProvider.__qualname__ = "MnemosMemoryProvider"
    MnemosMemoryProvider.__module__ = __name__
    return MnemosMemoryProvider
