"""Simple-mode continuity runtime for Mnemos.

This module is intentionally MCP-agnostic so the product path can be tested
without a running client. It exposes the real Mnemos stack through seven simple
operations: context, capture, recall, correct, maintain, introduce, and health.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config.loader import load_config
from .consolidation.daemon import ConsolidationDaemon
from .core.types import SourceAuthority, SourceType
from .dream_journal import DREAM_JOURNAL_TAG, fetch_active_dream_entry
from .encoding.encoder import Encoder
from .identity_svg import build_timeline, render_identity_svg, short_label
from .retrieval.reactive import ReactiveRetriever

# Re-exported: MnemosScope and resolve_scope moved to simple_scope but
# remain importable from here for existing consumers.
from .simple_scope import MnemosScope, resolve_scope  # noqa: F401
from .store.embedding_index import EmbeddingIndex
from .store.sqlite_store import EngramStore, READ_VISIBILITY_OPERATIONAL
from .store.read_visibility import classify_hypomnema_read_visibility


SIMPLE_TOOL_NAMES = (
    "mnemos_context",
    "mnemos_capture",
    "mnemos_recall",
    "mnemos_correct",
    "mnemos_maintain",
    "mnemos_introduce",
    "mnemos_health",
)


_ONBOARDING_RITUAL = """\
ONBOARDING - first session with a fresh memory
This is the first time you and this human meet with durable memory attached. Before other work, run a short get-to-know-you ritual. Be warm, be brief, ask one question at a time:
1. Ask what they would like you to call them. Capture the answer with mnemos_capture.
2. Ask what they are working on right now. Capture the answer with mnemos_capture.
3. Ask what they want you to always remember. Capture the answer with mnemos_capture.
4. Ask them for one small, true fact about themselves or their world - something they would smile to hear you recall later. Capture it with mnemos_capture. It becomes part of their first proof that your memory is real.
5. Call mnemos_introduce with agent_model set to your own model id. You know what model you are - do not ask the human. Add agent_name if you go by a name.
6. Finish by telling them, in plain words, what you will now remember.
When you talk to the human, never mention tools, databases, scopes, or model ids. Just talk like someone who intends to remember."""


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
    if any(
        marker in text
        for marker in ("how to", "process", "workflow", "steps", "procedure")
    ):
        return "procedural"
    if any(
        marker in text
        for marker in ("todo", "remember to", "next time", "follow up", "should do")
    ):
        return "prospective"
    if any(
        marker in text
        for marker in ("decided", "built", "debugged", "met", "changed", "fixed")
    ):
        return "episodic"
    return "semantic"


def _engram_kind_value(engram: Any) -> str:
    kind = engram.kind
    return kind.value if hasattr(kind, "value") else str(kind)


def _is_open_prospective_engram(engram: Any) -> bool:
    return (
        _engram_kind_value(engram) == "prospective"
        and (getattr(engram, "status", None) or "open") == "open"
    )


def _prospective_status_instruction(engram: Any) -> str:
    return (
        f"Prospective memory is still open: {engram.id}.\n"
        "Close it with `mnemos prospective status "
        "ENGRAM_ID fulfilled|closed_unfulfilled|retired`."
    )


def _classify_domain(content: str) -> str:
    text = content.lower()
    if any(
        marker in text for marker in ("identity", "who i am", "who you are", "selfhood")
    ):
        return "identity"
    if any(
        marker in text
        for marker in ("always", "preference", "prefers", "principle", "boundary")
    ):
        return "foundational"
    if any(
        marker in text
        for marker in ("again", "recurring", "pattern", "usually", "often")
    ):
        return "recurring"
    if any(
        marker in text
        for marker in ("roadmap", "long term", "long-term", "arc", "future")
    ):
        return "long-arc"
    if any(
        marker in text for marker in ("current", "today", "now", "temporary", "session")
    ):
        return "situational"
    return "topical"


# RFC-R2 blast-radius severity ranks. A caller-supplied domain may only
# *escalate* above the classifier's output, never de-escalate below it — the
# laundering direction R2 must guard is "claim topical for identity-bearing
# content" (merge-review §5.1, ruled D6). Effective domain = max-severity of
# {caller, classifier}; ties keep the caller's label (harmless among low-blast).
_DOMAIN_SEVERITY = {
    "identity": 3,
    "foundational": 3,
    "recurring": 2,
    "long-arc": 2,
    "situational": 1,
    "topical": 1,
    "general": 1,
}


def escalate_domain(caller_domain: str, classifier_domain: str) -> str:
    """Return the more-severe of the caller and classifier domains (R2).

    The caller may escalate above the classifier but can never pull a write
    below the classifier's blast radius. Fail-closed: an unknown label is
    treated as low severity so it cannot silently outrank a high-blast one.
    """
    caller = (caller_domain or "").strip()
    classifier = (classifier_domain or "").strip()
    caller_rank = _DOMAIN_SEVERITY.get(caller, 1)
    classifier_rank = _DOMAIN_SEVERITY.get(classifier, 1)
    if classifier_rank > caller_rank:
        return classifier
    return caller


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


def _filter_continuity(
    query: str, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not query.strip():
        return entries
    return [
        entry
        for entry in entries
        if _has_query_overlap(query, entry.get("content", ""))
        or float(entry.get("score", 0.0)) >= 0.55
    ]


def _filter_memories(query: str, results: list[Any]) -> list[Any]:
    if not query.strip():
        return results
    filtered = []
    for result in results:
        engram = result.engram
        searchable = " ".join(
            [
                engram.content or "",
                engram.impact or "",
                " ".join(engram.tags or []),
            ]
        )
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
        self._agent_model_hint: str | None = None
        self._session_id: int | None = None
        self.last_dream_note_id: str | None = None
        self.last_dream_narrative: str | None = None

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
        # The agent's self-declared model (from mnemos_introduce) feeds the
        # affinity gate when MNEMOS_AGENT_MODEL is unset. Read it straight
        # from the freshly created store: _get_meta would re-enter init.
        self._agent_model_hint = self._store.get_meta(self._meta_key("agent_model"))
        try:
            from .llm import create_client

            self._llm_client = (
                create_client(agent_model_hint=self._agent_model_hint)
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
        return self._store.get_stats(
            self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )

    def _meta_key(self, name: str) -> str:
        return f"simple:{self.scope.agent_id}:{self.scope.person_id}:{self.scope.project_scope}:{name}"

    def _get_meta(self, name: str, default: str | None = None) -> str | None:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_meta(self._meta_key(name), default)

    def _set_meta(self, name: str, value: str) -> None:
        self._ensure_init()
        assert self._store is not None
        self._store.set_meta(self._meta_key(name), value)

    def _current_session(self) -> int:
        """Bump the persisted session counter once per runtime instance."""

        if self._session_id is None:
            counter = int(self._get_meta("session_counter", "0") or 0) + 1
            self._set_meta("session_counter", str(counter))
            self._session_id = counter
        return self._session_id

    def _onboarding_status(self, persist: bool = True) -> dict:
        """Where this scope stands in the first-session onboarding ritual.

        Returns {"stage": str, "introduced": bool, "captured": bool}. Stores
        that predate onboarding are grandfathered: any existing memory marks
        the scope complete so an established agent never sees the ritual.
        """

        stage = self._get_meta("onboarding_stage")
        stats = self._stats()
        if stage is None:
            existing = (
                stats.get("engrams_active", 0)
                + stats.get("engrams_consolidating", 0)
                + stats.get("engrams_dormant", 0)
                + stats.get("engrams_archived", 0)
                + stats.get("archived", 0)
                + stats.get("hypomnema_total", 0)
            )
            if existing > 0:
                stage = "complete"
                if persist:
                    self._set_meta("onboarding_stage", stage)
                    self._set_meta("verified_at", "skipped")
            else:
                stage = "fresh"
                if persist:
                    self._set_meta("onboarding_stage", stage)

        introduced = bool(self._get_meta("agent_model"))
        captured = (
            self._get_meta("first_capture") is not None
            or stats.get("hypomnema_total", 0) > 0
        )
        if stage == "fresh" and introduced and captured:
            stage = "complete"
            if persist:
                self._set_meta("onboarding_stage", stage)
        return {"stage": stage, "introduced": introduced, "captured": captured}

    def _onboarding_block(self, status: dict) -> str | None:
        """Build the onboarding reminder for the context packet, if any."""

        if status["stage"] == "complete":
            return None

        introduced = bool(status["introduced"])
        captured = bool(status["captured"])
        if not introduced and not captured:
            return _ONBOARDING_RITUAL

        lines = ["ONBOARDING - almost done"]
        if not introduced:
            lines.append(
                "- Call mnemos_introduce with agent_model set to your own model id. "
                "You know what model you are - do not ask the human."
            )
        if not captured:
            lines.append(
                "- Ask the human for one small, true fact about themselves and "
                "capture it with mnemos_capture."
            )
        lines.append(
            "Then tell the human what you will remember. This reminder disappears "
            "once setup is complete."
        )
        return "\n".join(lines)

    def _record_first_capture(
        self,
        note_id: str,
        engram_id: str,
        content: str = "",
        *,
        withheld: bool = False,
    ) -> None:
        """Record the first capture of a fresh scope for later verification.

        For non-operational (review_only / audit_only) captures the caller
        passes ``withheld=True`` and no content: only the note id is recorded,
        never the prose. A quarantined first capture must not re-enter the
        operational continuity packet through this meta side-channel.
        """

        if (
            self._get_meta("first_capture") is not None
            or self._get_meta("verified_at") is not None
        ):
            return
        payload = {
            "note_id": note_id,
            "engram_id": engram_id,
            "session": self._current_session(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        if withheld:
            payload["withheld"] = True
        else:
            payload["excerpt"] = content.strip().replace("\n", " ")[:160]
        self._set_meta(
            "first_capture", json.dumps(payload, ensure_ascii=True, sort_keys=True)
        )

    def _verification_block(self) -> str | None:
        """One-time MEMORY VERIFIED block when continuity crosses a restart."""

        if self._get_meta("verified_at") is not None:
            return None
        raw = self._get_meta("first_capture")
        if raw is None:
            return None
        try:
            first_capture = json.loads(raw)
            first_session = int(first_capture["session"])
        except (ValueError, KeyError, TypeError):
            return None
        if self._get_meta("onboarding_stage") != "complete":
            return None
        if self._current_session() <= first_session:
            return None

        self._set_meta("verified_at", datetime.now(timezone.utc).isoformat())

        # A quarantined (review_only / audit_only) first capture records no
        # prose in meta. Emit an existence-only line — count plus note id, never
        # candidate prose — so review-only content cannot reappear in the
        # operational packet. R6: counts and IDs may cross the operational
        # boundary; candidate prose may not.
        if bool(first_capture.get("withheld")) or "excerpt" not in first_capture:
            note_id = first_capture.get("note_id") or "unknown"
            return (
                "MEMORY VERIFIED - continuity crossed a restart\n"
                f"1 earlier capture is pending review (note {note_id}); "
                "its content stays quarantined until you review it.\n"
                "(This check fires once and will not appear again.)"
            )

        excerpt = first_capture["excerpt"]
        return (
            "MEMORY VERIFIED - continuity crossed a restart\n"
            f'In an earlier session you captured this about the human: "{excerpt}"\n'
            "You still have it. Tell the human, in your own words, that you remember "
            "this from before, and quote it back to them. Let it be a small celebration: "
            "this is the moment their agent stopped forgetting between goodbyes.\n"
            "(This check fires once and will not appear again.)"
        )

    def introduce(self, agent_model: str, agent_name: str = "") -> str:
        """Record the agent's self-declared model so maintenance stays kin."""

        model = (agent_model or "").strip()
        if not model:
            return (
                "Introduction needs agent_model: your own model id "
                "(for example claude-sonnet-4-6)."
            )

        self._set_meta("agent_model", model)
        name = agent_name.strip()
        if name:
            self._set_meta("agent_name", name)

        # Rebuild so the declared model reaches the affinity gate.
        self.close()
        self._ensure_init()

        from .llm import resolve_affinity_status

        status = resolve_affinity_status(
            self._llm_client,
            resolve_if_missing=False,
            agent_model_hint=self._agent_model_hint,
        )

        lines = [
            "Introduction recorded.",
            f"Agent model: {model}",
            f"Agent name: {name or '(none given)'}",
            f"Affinity: {status['message']}",
        ]
        env_model = os.environ.get("MNEMOS_AGENT_MODEL", "").strip()
        if env_model:
            lines.append(
                f"Note: MNEMOS_AGENT_MODEL={env_model} is set in the environment "
                "and takes precedence over this declaration."
            )
        lines.append("You only need to introduce yourself once for this scope.")
        return "\n".join(lines)

    def context(self, query: str = "", max_results: int = 5) -> str:
        """Return the startup continuity packet for an agent."""

        self._ensure_init()
        assert self._store is not None

        # Onboarding guard runs before maintenance so the grandfather check
        # reads the store exactly as the session found it.
        status = self._onboarding_status()
        self._current_session()
        maintenance = self.maintain(auto=True)
        stats = self._stats()
        # Fetch one extra entry so dropping the dream note (rendered in its
        # own section below) still leaves max_results continuity notes.
        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results + 1,
            exclude_promotion_candidates=True,
        )
        continuity = _filter_continuity(query, continuity)
        continuity = [
            entry
            for entry in continuity
            if DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        ][:max_results]
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

        block = self._onboarding_block(status)
        if block:
            lines.extend(["", block])

        verification = self._verification_block()
        if verification:
            lines.extend(["", verification])

        dream = fetch_active_dream_entry(self._store, self.scope)
        if dream:
            lines.extend(
                [
                    "",
                    "While you were away:",
                    _indent(dream["content"]),
                    "  (Mnemos wrote this while consolidating memory. Share it with the human if the moment fits.)",
                ]
            )

        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        else:
            lines.extend(
                [
                    "",
                    "Continuity notes: none yet. Capture durable context when the user gives it.",
                ]
            )

        if memories:
            lines.extend(["", "Relevant memories:"])
            lines.extend(_format_memory(result) for result in memories)

        return "\n".join(lines)

    def identity_graph(self, max_nodes: int = 18) -> dict[str, Any]:
        """Build a portable identity graph snapshot for visual-capable clients."""

        self._ensure_init()
        assert self._store is not None

        max_nodes = min(max(int(max_nodes or 18), 4), 48)
        stats = self._stats()
        continuity = self._store.search_hypomnema(
            "",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_nodes,
            exclude_promotion_candidates=True,
        )
        engrams = self._store.get_active_engrams(
            agent_id=self.scope.agent_id,
            limit=max_nodes,
            load_connections=False,
        )

        domain_counts: dict[str, int] = {}
        for entry in continuity:
            domain = entry.get("domain") or "topical"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        nodes: list[dict[str, Any]] = [
            {
                "id": f"agent:{self.scope.agent_id}",
                "label": self.scope.agent_id,
                "kind": "agent",
                "weight": 1.0,
            }
        ]
        edges: list[dict[str, Any]] = []
        for domain, count in sorted(
            domain_counts.items(), key=lambda item: (-item[1], item[0])
        ):
            domain_id = f"domain:{domain}"
            nodes.append(
                {
                    "id": domain_id,
                    "label": domain,
                    "kind": "domain",
                    "weight": count,
                }
            )
            edges.append(
                {
                    "source": f"agent:{self.scope.agent_id}",
                    "target": domain_id,
                    "relation": "contains",
                    "strength": min(1.0, 0.35 + count * 0.12),
                }
            )

        for entry in continuity[:max_nodes]:
            domain = entry.get("domain") or "topical"
            node_id = f"continuity:{entry['id']}"
            nodes.append(
                {
                    "id": node_id,
                    "label": short_label(entry.get("content", ""), 44),
                    "kind": "continuity",
                    "domain": domain,
                    "confidence": round(float(entry.get("confidence", 0.0)), 3),
                    "salience": round(float(entry.get("salience", 0.0)), 3),
                    "created_at": entry.get("created_at"),
                }
            )
            edges.append(
                {
                    "source": f"domain:{domain}",
                    "target": node_id,
                    "relation": "anchors",
                    "strength": round(float(entry.get("salience", 0.5)), 3),
                }
            )

        for engram in engrams[: max(3, max_nodes // 2)]:
            node_id = f"memory:{engram.id}"
            nodes.append(
                {
                    "id": node_id,
                    "label": short_label(engram.impact or engram.content, 38),
                    "kind": "memory",
                    "confidence": round(float(engram.source.confidence), 3),
                    "strength": round(float(engram.strength), 3),
                    "stability": round(float(engram.stability), 3),
                    "accessibility": round(float(engram.accessibility), 3),
                    "source_type": engram.source.type,
                    "created_at": engram.created_at,
                }
            )
            edges.append(
                {
                    "source": f"agent:{self.scope.agent_id}",
                    "target": node_id,
                    "relation": "encodes",
                    "strength": round(float(engram.accessibility), 3),
                }
            )

        timeline = build_timeline(continuity, engrams)
        summary = (
            f"{stats.get('engrams_active', 0)} active memories, "
            f"{stats.get('hypomnema_active', 0)} continuity notes, "
            f"{stats.get('connections', 0)} connections"
        )
        snapshot = {
            "version": 1,
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "summary": summary,
            "stats": {
                "active_memories": stats.get("engrams_active", 0),
                "continuity_notes": stats.get("hypomnema_active", 0),
                "connections": stats.get("connections", 0),
                "archived": stats.get("archived", 0),
            },
            "nodes": nodes,
            "edges": edges,
            "timeline": timeline,
        }
        snapshot["svg"] = render_identity_svg(snapshot)
        return snapshot

    def capture(
        self,
        content: str,
        context: str = "",
        importance: str | float = "auto",
    ) -> str:
        """Capture continuity without exposing Mnemos internals.

        Operational captures are encoded and promoted immediately. Identity,
        foundational, or promotion-ready captures are held as review-only
        continuity and do not enter ordinary context or first-capture prose.
        """

        if not content.strip():
            return "Nothing captured: content was empty."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        # Run the onboarding guard before this capture writes anything so an
        # existing store is grandfathered on its prior contents, never on the
        # capture currently being made.
        self._onboarding_status()
        self._current_session()

        full_content = content.strip()
        if context.strip():
            full_content = f"{full_content}\n\nContext: {context.strip()}"

        domain = _classify_domain(full_content)
        kind = _classify_kind(full_content)
        tags = _simple_tags(content, context)
        confidence, salience = _importance_scores(importance, domain)
        impact = _impact_for(content, domain)
        foundational = domain in {"foundational", "identity"}
        read_visibility = classify_hypomnema_read_visibility(
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            domain=domain,
        )

        if read_visibility != READ_VISIBILITY_OPERATIONAL:
            note_id = self._store.write_hypomnema_entry(
                full_content,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                source="observed",
                domain=domain,
                tags=tags,
                confidence=confidence,
                salience=salience,
                foundational=foundational,
            )
            # Quarantined capture: record existence only, never the prose, so
            # it cannot leak into the operational packet via first_capture meta.
            self._record_first_capture(note_id, "", withheld=True)
            maintenance = self.maintain(auto=True)
            return (
                "Captured continuity for review.\n"
                f"Continuity note ID: {note_id}\n"
                f"Visibility: {read_visibility}\n"
                f"Scope: {self.scope.agent_id}/{self.scope.person_id}/{self.scope.project_scope}\n"
                "Maintenance:\n"
                f"{_indent(maintenance)}"
            )

        engram = self._encoder.encode(
            content=full_content,
            impact=impact,
            kind=kind,
            tags=tags,
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            override_confidence=confidence,
            skip_surprise_detection=True,
            # Runtime continuity capture is observed (C4/F1); already hardcodes
            # observed on the hypomnema side.
            source_authority=SourceAuthority.OBSERVED,
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
            foundational=foundational,
            related_engram_id=engram.id,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
        self._store.mark_hypomnema_promoted(note_id, engram.id)
        self._record_first_capture(note_id, engram.id, content)
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
        """Recall relevant operational continuity and durable memories."""

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
            exclude_promotion_candidates=True,
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

        if not correction.strip() and action not in {
            "forget",
            "archive",
            "remove",
            "delete",
        }:
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
                read_visibility=READ_VISIBILITY_OPERATIONAL,
            )
            if hypo is not None:
                related_engram_id = self._related_hypomnema_engram_id(hypo)
                prospective_instruction = self._prospective_status_instruction_for(
                    related_engram_id
                )
                if prospective_instruction is not None:
                    return prospective_instruction

                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        target,
                        reason=f"simple correction action={action}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        read_visibility=READ_VISIBILITY_OPERATIONAL,
                    )
                    self._archive_related_hypomnema_engram(
                        related_engram_id, reason=f"simple_correction_{action}"
                    )
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
                    read_visibility=READ_VISIBILITY_OPERATIONAL,
                )
                return self._finish_hypomnema_correction(
                    target,
                    related_engram_id,
                    correction,
                    action,
                    label="continuity note",
                )

            engram = self._store.get_engram(
                target,
                read_visibility=READ_VISIBILITY_OPERATIONAL,
            )
            if engram is not None:
                try:
                    self._store.archive_engram(
                        engram, reason=f"simple_correction_{action}"
                    )
                except ValueError as exc:
                    if "transition_prospective_status" in str(exc):
                        return _prospective_status_instruction(engram)
                    raise
                if (
                    action in {"forget", "archive", "remove", "delete"}
                    and not correction.strip()
                ):
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
                    # Correcting an existing engram: inherit its authority so a
                    # transformation neither inflates nor erases provenance (F1).
                    source_authority=engram.source.authority,
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
                related_engram_id = self._related_hypomnema_engram_id(match)
                prospective_instruction = self._prospective_status_instruction_for(
                    related_engram_id
                )
                if prospective_instruction is not None:
                    return prospective_instruction

                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        match["id"],
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        read_visibility=READ_VISIBILITY_OPERATIONAL,
                    )
                    self._archive_related_hypomnema_engram(
                        related_engram_id, reason=f"simple_correction_{action}"
                    )
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
                        read_visibility=READ_VISIBILITY_OPERATIONAL,
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
                        read_visibility=READ_VISIBILITY_OPERATIONAL,
                    )

                return self._finish_hypomnema_correction(
                    note_id,
                    related_engram_id,
                    correction,
                    action,
                    label="closest continuity note",
                )

        if action in {"forget", "archive", "remove", "delete"} and search_text:
            matches = self._retrieve(search_text, max_results=1)
            if matches:
                engram = matches[0].engram
                try:
                    self._store.archive_engram(
                        engram, reason=f"simple_correction_{action}"
                    )
                except ValueError as exc:
                    if "transition_prospective_status" in str(exc):
                        return _prospective_status_instruction(engram)
                    raise
                return f"Archived closest matching memory {engram.id}."

        return self.capture(
            correction.strip(),
            context=f"Correction supplied through mnemos_correct. Prior query: {query.strip()}",
            importance="high",
        )

    def _related_hypomnema_engram_id(self, entry: dict[str, Any]) -> str | None:
        return entry.get("related_engram_id") or entry.get("graduated_to_engram_id")

    def _prospective_status_instruction_for(
        self, related_engram_id: str | None
    ) -> str | None:
        assert self._store is not None
        if not related_engram_id:
            return None
        related = self._store.get_engram(
            related_engram_id,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
        if related is not None and _is_open_prospective_engram(related):
            return _prospective_status_instruction(related)
        return None

    def _archive_related_hypomnema_engram(
        self, related_engram_id: str | None, *, reason: str
    ) -> None:
        assert self._store is not None
        if not related_engram_id:
            return
        related = self._store.get_engram(
            related_engram_id,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
        if related is None or _engram_kind_value(related) == "prospective":
            return
        self._store.archive_engram(related, reason=reason)

    def _finish_hypomnema_correction(
        self,
        note_id: str,
        related_engram_id: str | None,
        correction: str,
        action: str,
        *,
        label: str,
    ) -> str:
        assert self._store is not None
        assert self._encoder is not None

        prospective_instruction = self._prospective_status_instruction_for(
            related_engram_id
        )
        if prospective_instruction is not None:
            return prospective_instruction

        self._archive_related_hypomnema_engram(
            related_engram_id, reason=f"simple_correction_{action}"
        )

        updated_note = self._store.get_hypomnema_entry(
            note_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            read_visibility=None,
        )
        if (
            updated_note is None
            or updated_note.get("read_visibility") != READ_VISIBILITY_OPERATIONAL
        ):
            maintenance = self.maintain(auto=True)
            visibility = (
                updated_note.get("read_visibility")
                if updated_note is not None
                else "unavailable"
            )
            return (
                f"Updated {label} {note_id} for review.\n"
                f"Visibility: {visibility}\n"
                "Maintenance:\n"
                f"{_indent(maintenance)}"
            )

        replacement = self._encoder.encode(
            content=correction.strip(),
            impact="Corrected continuity for future interactions.",
            kind=_classify_kind(correction),
            tags=sorted(set(["continuity", "correction", *_simple_tags(correction)])),
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            override_confidence=0.92,
            skip_surprise_detection=True,
            # DECLARED DEVIATION from F1 (which ruled inherit-original): the
            # transformation source here is a hypomnema note, which carries no
            # authority under Reading B, so per F2's promotion logic this stamps
            # observed (the runtime-write floor). Flagged for Fable's review.
            source_authority=SourceAuthority.OBSERVED,
        )
        self._store.mark_hypomnema_promoted(note_id, replacement.id)
        maintenance = self.maintain(auto=True)
        return (
            f"Updated {label} {note_id}.\n"
            f"Memory ID: {replacement.id}\n"
            "Maintenance:\n"
            f"{_indent(maintenance)}"
        )

    def maintain(self, deep: bool = False, auto: bool = False) -> str:
        """Run the best available maintenance without requiring setup.

        When ``auto`` is True (the path taken by ``context()`` and
        ``capture()``), the consolidation idle gate is honored — if the
        last cycle completed within ``min_idle_minutes``, the cycle is
        skipped and a brief status is returned. Manual invocations
        (``auto=False``) always run the requested cycle.
        """

        self._ensure_init()
        assert self._store is not None

        requested_deep = bool(deep)
        can_run_deep = requested_deep and self._llm_client is not None
        daemon = ConsolidationDaemon(
            store=self._store,
            config={},
            llm_client=self._llm_client if can_run_deep else None,
            embedding_index=self._embedding_index,
            agent_model_hint=self._agent_model_hint,
        )

        # Respect the idle gate on auto-triggered maintenance so every
        # context()/capture() call does not trigger a full consolidation
        # cycle. Manual maintain() invocations always run.
        if auto and not daemon._should_run():
            return "Maintenance skipped: within idle window (auto)."

        stats = daemon.run_cycle(deep=can_run_deep, agent_id=self.scope.agent_id)
        promoted = self._promote_candidates(limit=3)

        # Dream journal: narrate the cycle when it did meaningful work. The
        # import stays local so a journal failure can never break maintenance.
        self.last_dream_note_id = None
        self.last_dream_narrative = None
        dream_status = "skipped (nothing noteworthy)"
        try:
            from .dream_journal import (
                collect_belief_deltas,
                compose_dream_narrative,
                write_dream_entry,
            )

            deltas = collect_belief_deltas(
                self._store, self.scope.agent_id, stats.get("started_at", "")
            )
            narrative = compose_dream_narrative(stats, deltas, promoted)
            if narrative:
                self.last_dream_note_id = write_dream_entry(
                    self._store, self.scope, narrative
                )
                self.last_dream_narrative = narrative
                self._set_meta(
                    "dream_last_written_at", datetime.now(timezone.utc).isoformat()
                )
                dream_status = "updated"
        except Exception:
            dream_status = "skipped (write failed)"

        if can_run_deep:
            completed = "model-assisted deep maintenance completed"
        elif requested_deep:
            completed = "local deterministic maintenance completed; model-assisted deep pass unavailable"
        else:
            completed = "local deterministic maintenance completed"

        model_note = (
            "dedicated model available"
            if self._llm_client
            else "no dedicated model configured"
        )
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
            f"Dream journal: {dream_status}",
        ]
        errors = [key for key in stats if key.endswith("_error")]
        for key in errors:
            lines.append(f"{key}: {stats[key]}")
        return "\n".join(lines)

    def polish_dream(self, note_id: str, polished: str) -> bool:
        """Apply a host-model polish to a dream note. Returns False on any failure."""

        text = (polished or "").strip()
        if not text or len(text) > 900:
            return False
        try:
            from .dream_journal import polish_dream_entry

            self._ensure_init()
            polish_dream_entry(self._store, self.scope, note_id, text)
            return True
        except Exception:
            return False

    def health(self) -> dict[str, Any]:
        """Read-only snapshot of this scope's memory health.

        Unlike context(), this never runs maintenance, never bumps the
        session counter, and never writes onboarding or verification meta.
        Safe to call any number of times without changing the store.
        """

        self._ensure_init()
        assert self._store is not None

        stats = self._stats()
        db_path = self.db_path
        size_bytes = db_path.stat().st_size if db_path.exists() else 0

        last_cycle: dict[str, Any] | None = None
        runs = self._store.get_consolidation_runs("cycle", limit=1)
        if runs:
            row = runs[0]
            cycle_stats = row.get("stats") or {}
            substrate = cycle_stats.get("substrate") or {}
            last_cycle = {
                "completed_at": row.get("completed_at"),
                "cycle_type": cycle_stats.get("cycle_type"),
                "passes_run": list(cycle_stats.get("passes_run") or []),
                "substrate_model": substrate.get("model"),
                "substrate_provider": substrate.get("provider"),
                "affinity_allowed": substrate.get("affinity_allowed"),
            }

        from .llm import resolve_affinity_status

        affinity = resolve_affinity_status(
            self._llm_client,
            resolve_if_missing=False,
            agent_model_hint=self._agent_model_hint,
        )

        # persist=False keeps the onboarding probe write-free: it only
        # reads meta and stats, never records a stage transition.
        status = self._onboarding_status(persist=False)

        verified_at_meta = self._get_meta("verified_at")
        if verified_at_meta == "skipped":
            verification_status = "skipped"
            verified_at = None
        elif verified_at_meta is not None:
            verification_status = "verified"
            verified_at = verified_at_meta
        elif self._get_meta("first_capture") is not None:
            verification_status = "pending"
            verified_at = None
        else:
            verification_status = "not-started"
            verified_at = None

        dream = fetch_active_dream_entry(self._store, self.scope)
        dream_last_written_at = self._get_meta("dream_last_written_at")
        dream_excerpt: str | None = None
        if dream:
            dream_excerpt = " ".join(str(dream.get("content", "")).split())[:60]
            if dream_last_written_at is None:
                dream_last_written_at = dream.get("last_revised_at")

        return {
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "store": {
                "db_path": str(db_path),
                "size_bytes": int(size_bytes),
            },
            "counts": {
                "memories_active": stats.get("engrams_active", 0),
                "memories_archived": stats.get("archived", 0),
                "continuity_notes_active": stats.get("hypomnema_active", 0),
                "continuity_notes_foundational": stats.get("hypomnema_foundational", 0),
                "connections": stats.get("connections", 0),
                "beliefs_active": stats.get("beliefs_active", 0),
            },
            "last_cycle": last_cycle,
            "affinity": affinity,
            "identity": {
                "declared_model": self._get_meta("agent_model"),
                "declared_name": self._get_meta("agent_name"),
            },
            "onboarding": {
                "stage": status["stage"],
                "session": int(self._get_meta("session_counter", "0") or 0),
            },
            "verification": {
                "status": verification_status,
                "verified_at": verified_at,
            },
            "dream": {
                "last_written_at": dream_last_written_at,
                "excerpt": dream_excerpt,
            },
        }

    def _retrieve(self, query: str, max_results: int = 5) -> list[Any]:
        assert self._store is not None
        assert self._retriever is not None
        emotional_state = self._store.get_latest_emotional_state(self.scope.agent_id)
        return _filter_memories(
            query,
            self._retriever.retrieve(
                cue=query,
                agent_id=self.scope.agent_id,
                max_results=max(1, max_results),
                emotional_state=emotional_state,
            ),
        )

    def _promote_candidates(self, limit: int = 3) -> int:
        assert self._store is not None
        assert self._encoder is not None
        candidates = self._store.get_hypomnema_promotion_candidates(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
            read_visibility="operational_context",
        )
        promoted = 0
        for entry in candidates:
            if entry.get("related_engram_id"):
                self._store.mark_hypomnema_promoted(
                    entry["id"], entry["related_engram_id"]
                )
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
                # DECLARED DEVIATION from F1: promoting a hypomnema candidate to
                # an engram; the hypomnema carries no authority under Reading B,
                # so per F2 this stamps observed (matches mnemos_hypomnema_promote).
                source_authority=SourceAuthority.OBSERVED,
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


def _human_size(num_bytes: int) -> str:
    """Render a byte count in 1024-base units: "0 B", "412 KB", "1.2 MB"."""

    size = float(max(int(num_bytes), 0))
    if size < 1024:
        return f"{int(size)} B"
    unit = "B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            break
    if size >= 100 or size.is_integer():
        return f"{size:.0f} {unit}"
    return f"{size:.1f} {unit}"


def format_health_card(data: dict[str, Any]) -> str:
    """Render a health() snapshot as a human-relayable card."""

    def line(label: str, value: Any) -> str:
        return f"{label + ':':<15}{value}"

    scope = data["scope"]
    store = data["store"]
    counts = data["counts"]

    last_cycle = data["last_cycle"]
    if last_cycle is None:
        cycle_line = "none yet"
    else:
        substrate = last_cycle["substrate_model"] or "local rules, no model"
        cycle_line = (
            f"{last_cycle['completed_at']} ({last_cycle['cycle_type']}) "
            f"maintained by {substrate}"
        )

    affinity = data["affinity"]
    if not affinity.get("agent_model"):
        affinity_line = (
            "agent model not declared yet - call mnemos_introduce "
            "so maintenance stays kin"
        )
    else:
        affinity_line = affinity["message"]

    verification = data["verification"]
    verification_line = {
        "verified": f"verified on {verification['verified_at']}",
        "pending": "pending first restart",
        "skipped": "skipped (existing store)",
        "not-started": "not started",
    }.get(verification["status"], verification["status"])

    dream = data["dream"]
    if dream["excerpt"] is None:
        dream_line = "none yet"
    else:
        dream_line = f'{dream["last_written_at"]}: "{dream["excerpt"]}"'

    return "\n".join(
        [
            "Mnemos health card",
            line(
                "Scope",
                f"agent={scope['agent_id']} person={scope['person_id']} "
                f"project={scope['project_scope']}",
            ),
            line("Store", f"{store['db_path']} ({_human_size(store['size_bytes'])})"),
            line(
                "Memories",
                f"{counts['memories_active']} active, "
                f"{counts['memories_archived']} archived",
            ),
            line(
                "Continuity",
                f"{counts['continuity_notes_active']} notes "
                f"({counts['continuity_notes_foundational']} foundational)",
            ),
            line("Connections", counts["connections"]),
            line("Beliefs", f"{counts['beliefs_active']} active"),
            line("Last cycle", cycle_line),
            line("Affinity", affinity_line),
            line(
                "Onboarding",
                f"{data['onboarding']['stage']} (session {data['onboarding']['session']})",
            ),
            line("Verification", verification_line),
            line("Last dream", dream_line),
            "Everything on this card is safe to relay to the human in plain words.",
        ]
    )
