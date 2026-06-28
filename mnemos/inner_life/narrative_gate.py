"""Hard gate for generated inner-life narrative candidates."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from ..store.sqlite_store import EngramStore
from .turn_finalizer import _excerpt


_MANUFACTURE_PATTERNS = (
    "i feel alive",
    "i felt alive",
    "my soul",
    "deeply meaningful",
    "profound inner",
    "as if i had",
    "it feels like i",
)


def gate_narrative_candidate(
    *,
    content: str | None,
    source_ids: list[str] | tuple[str, ...] | None,
    process_name: str,
    store: EngramStore | None = None,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    candidate_kind: str = "reflection",
    introspector: Callable[[str], Mapping[str, Any] | None] | None = None,
    rollout_tag: str = "u6.6",
    max_excerpt_chars: int = 480,
) -> dict[str, Any]:
    """Evaluate a generated candidate before low-stakes persistence."""
    clean = " ".join((content or "").split())
    sources = _source_ids(source_ids)
    if not clean:
        decision = _decision(False, "null_output", "skip:null_output", clean, sources)
    elif not sources:
        decision = _decision(False, "missing_source_ids", "drop:missing_source_ids", clean, sources)
    elif _looks_manufactured(clean):
        decision = _decision(False, "manufactured_inner_state", "drop:manufactured_inner_state", clean, sources)
    else:
        report, error = _run_introspection(clean, introspector)
        if error:
            decision = _decision(
                False,
                "introspection_failed",
                "drop:introspection_failed",
                clean,
                sources,
                introspection_error=error,
            )
        elif _introspection_rejects(report):
            decision = _decision(
                False,
                "introspection_reject",
                "drop:introspection_reject",
                clean,
                sources,
                introspection_report=dict(report or {}),
            )
        else:
            decision = _decision(
                True,
                "passed",
                "pass",
                clean,
                sources,
                introspection_report=dict(report or {}),
            )

    if store is not None:
        _record_gate_decision(
            store,
            decision=decision,
            process_name=process_name,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            candidate_kind=candidate_kind,
            rollout_tag=rollout_tag,
            max_excerpt_chars=max_excerpt_chars,
        )
    return decision


def _decision(
    allowed: bool,
    reason: str,
    gate_decision: str,
    content: str,
    source_ids: list[str],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "reason": reason,
        "gate_decision": gate_decision,
        "content": content if allowed else "",
        "source_ids": source_ids,
        "writes_memory": False,
        "generated_memory_writes": 0,
        "identity_patches": 0,
        **extra,
    }


def _source_ids(value: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _looks_manufactured(content: str) -> bool:
    lowered = content.lower()
    return any(pattern in lowered for pattern in _MANUFACTURE_PATTERNS)


def _run_introspection(
    content: str,
    introspector: Callable[[str], Mapping[str, Any] | None] | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if introspector is None:
        return {"verdict": "pass", "mode": "not_configured"}, None
    try:
        return introspector(content), None
    except Exception as exc:
        return None, str(exc)


def _introspection_rejects(report: Mapping[str, Any] | None) -> bool:
    if not report:
        return False
    verdict = str(report.get("verdict") or report.get("decision") or "").lower()
    if verdict in {"reject", "fail", "failed", "drop"}:
        return True
    if bool(report.get("performed")) is True:
        return True
    risk = str(report.get("risk") or report.get("risk_level") or "").lower()
    return risk in {"high", "severe"}


def _record_gate_decision(
    store: EngramStore,
    *,
    decision: dict[str, Any],
    process_name: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    candidate_kind: str,
    rollout_tag: str,
    max_excerpt_chars: int,
) -> None:
    excerpt, truncated = _excerpt(
        decision["content"] or f"{candidate_kind}: {decision['reason']}",
        max_excerpt_chars,
    )
    source_ids = decision["source_ids"]
    digest = hashlib.sha256(
        "|".join([process_name, candidate_kind, decision["reason"], excerpt, *source_ids]).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    metadata = {
        "writes_memory": False,
        "generated_memory_writes": 0,
        "identity_patches": 0,
        "target_process": process_name,
        "candidate_kind": candidate_kind,
        "reason": decision["reason"],
        "excerpt_truncated": truncated,
        "introspection_report": decision.get("introspection_report", {}),
        "introspection_error": decision.get("introspection_error"),
    }
    store.upsert_inner_life_event(
        idempotency_key=f"narrative-gate:{digest}",
        event_type="tool_event" if decision["allowed"] else "skip",
        process_name="narrative-gate",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        role="gate",
        source_message_id=source_ids[0] if source_ids else None,
        content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        content_excerpt=excerpt,
        event_tags=["u6.6", "narrative-gate", candidate_kind],
        source_ids=source_ids,
        metadata=metadata,
        rollout_tag=rollout_tag,
        gate_decision=decision["gate_decision"],
    )
