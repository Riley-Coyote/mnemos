"""Bounded observer panel for U6.6 immune-layer signals."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from ..store.sqlite_store import EngramStore
from .turn_finalizer import _excerpt


DEFAULT_MAX_REVIEWERS = 3
DEFAULT_MAX_FINDINGS_PER_REVIEWER = 2
DEFAULT_MAX_EXCERPT_CHARS = 480


def run_observer_panel(
    store: EngramStore,
    *,
    reviewer_clients: list[Any] | tuple[Any, ...],
    context: str,
    source_ids: list[str] | tuple[str, ...],
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str | None = None,
    thread_id: str | None = None,
    rollout_tag: str = "u6.6",
    max_reviewers: int = DEFAULT_MAX_REVIEWERS,
    max_findings_per_reviewer: int = DEFAULT_MAX_FINDINGS_PER_REVIEWER,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> dict[str, Any]:
    """Run bounded observer clients and persist private observer signals only."""
    resolved_sources = _source_ids(source_ids)
    if not resolved_sources:
        _record_panel_skip(
            store,
            reason="missing_source_ids",
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            thread_id=thread_id,
            rollout_tag=rollout_tag,
        )
        return _summary(skipped=1, reason="missing_source_ids")

    clients = list(reviewer_clients or [])[: max(1, max_reviewers)]
    if not clients:
        _record_panel_skip(
            store,
            reason="no_reviewers",
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            thread_id=thread_id,
            rollout_tag=rollout_tag,
            source_ids=resolved_sources,
        )
        return _summary(skipped=1, reason="no_reviewers")

    written = 0
    dropped = 0
    errors = 0
    for index, client in enumerate(clients, start=1):
        reviewer_id = _reviewer_id(client, index)
        try:
            output = _call_reviewer(client, context, resolved_sources)
        except Exception as exc:
            errors += 1
            _record_panel_error(
                store,
                reviewer_id=reviewer_id,
                error=str(exc),
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                session_id=session_id,
                thread_id=thread_id,
                rollout_tag=rollout_tag,
                source_ids=resolved_sources,
            )
            continue

        findings = _normalize_findings(output)
        if not findings:
            dropped += 1
            _record_panel_skip(
                store,
                reason="empty_finding",
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                session_id=session_id,
                thread_id=thread_id,
                rollout_tag=rollout_tag,
                source_ids=resolved_sources,
                reviewer_id=reviewer_id,
            )
            continue

        bounded = findings[: max(1, max_findings_per_reviewer)]
        dropped += max(0, len(findings) - len(bounded))
        for finding_index, finding in enumerate(bounded, start=1):
            if _record_observer_finding(
                store,
                finding=finding,
                reviewer_id=reviewer_id,
                finding_index=finding_index,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                session_id=session_id,
                thread_id=thread_id,
                rollout_tag=rollout_tag,
                source_ids=resolved_sources,
                max_excerpt_chars=max_excerpt_chars,
            ):
                written += 1

    return _summary(
        written=written,
        dropped=dropped,
        errors=errors,
        reviewer_count=len(clients),
        reason="observer_panel_complete",
    )


def _summary(
    *,
    written: int = 0,
    dropped: int = 0,
    errors: int = 0,
    skipped: int = 0,
    reviewer_count: int = 0,
    reason: str,
) -> dict[str, Any]:
    return {
        "written": written,
        "dropped": dropped,
        "errors": errors,
        "skipped": skipped,
        "reviewer_count": reviewer_count,
        "reason": reason,
        "generated_memory_writes": 0,
        "identity_patches": 0,
    }


def _reviewer_id(client: Any, index: int) -> str:
    return str(
        getattr(client, "reviewer_id", None)
        or getattr(client, "name", None)
        or f"reviewer-{index}"
    )


def _call_reviewer(client: Any, context: str, source_ids: list[str]) -> Any:
    if hasattr(client, "observe"):
        return client.observe(context=context, source_ids=source_ids)
    if isinstance(client, Callable):
        return client(context=context, source_ids=source_ids)
    raise TypeError("reviewer client must be callable or expose observe()")


def _normalize_findings(output: Any) -> list[dict[str, Any]]:
    if output is None:
        return []
    if isinstance(output, Mapping):
        if "findings" in output:
            raw = output.get("findings")
            if isinstance(raw, list):
                return [_finding_dict(item) for item in raw if _finding_dict(item)]
            return []
        finding = _finding_dict(output)
        return [finding] if finding else []
    if isinstance(output, list):
        return [_finding_dict(item) for item in output if _finding_dict(item)]
    return []


def _finding_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    text = str(value.get("finding") or value.get("observation") or "").strip()
    if not text:
        return {}
    return {
        "finding": text,
        "rationale": str(value.get("rationale") or "").strip(),
        "confidence": _confidence(value.get("confidence")),
        "tags": _source_ids(value.get("tags")),
    }


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.4
    return min(1.0, max(0.0, parsed))


def _source_ids(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _record_observer_finding(
    store: EngramStore,
    *,
    finding: dict[str, Any],
    reviewer_id: str,
    finding_index: int,
    agent_id: str,
    person_id: str,
    project_scope: str,
    session_id: str | None,
    thread_id: str | None,
    rollout_tag: str,
    source_ids: list[str],
    max_excerpt_chars: int,
) -> bool:
    text = finding["finding"]
    rationale = finding.get("rationale") or ""
    excerpt, truncated = _excerpt(
        f"{reviewer_id}: {text}\nRATIONALE: {rationale}",
        max_excerpt_chars,
    )
    key_payload = "|".join([reviewer_id, str(finding_index), text, *source_ids])
    digest = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:16]
    store.upsert_inner_life_event(
        idempotency_key=f"observer-panel:{digest}",
        event_type="tool_event",
        process_name="observer-panel",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        role="observer",
        source_message_id=source_ids[0],
        content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        content_excerpt=excerpt,
        event_tags=["u6.6", "observer", "source:observer", *finding["tags"]],
        source_ids=source_ids,
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "identity_patches": 0,
            "reviewer_id": reviewer_id,
            "confidence": finding["confidence"],
            "excerpt_truncated": truncated,
        },
        rollout_tag=rollout_tag,
        gate_decision="observer_signal",
    )
    return True


def _record_panel_skip(
    store: EngramStore,
    *,
    reason: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    session_id: str | None,
    thread_id: str | None,
    rollout_tag: str,
    source_ids: list[str] | None = None,
    reviewer_id: str | None = None,
) -> None:
    key = _event_key("skip", reason, source_ids or [], reviewer_id or "")
    store.upsert_inner_life_event(
        idempotency_key=f"observer-panel:{key}",
        event_type="skip",
        process_name="observer-panel",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        role="observer",
        content_hash="",
        content_excerpt=f"observer-panel skipped: {reason}",
        event_tags=["u6.6", "observer", "skip"],
        source_ids=source_ids or [],
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "identity_patches": 0,
            "reason": reason,
            "reviewer_id": reviewer_id,
        },
        rollout_tag=rollout_tag,
        gate_decision=f"skip:{reason}",
    )


def _record_panel_error(
    store: EngramStore,
    *,
    reviewer_id: str,
    error: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    session_id: str | None,
    thread_id: str | None,
    rollout_tag: str,
    source_ids: list[str],
) -> None:
    excerpt, _ = _excerpt(f"{reviewer_id}: {error}", DEFAULT_MAX_EXCERPT_CHARS)
    key = _event_key("error", error, source_ids, reviewer_id)
    store.upsert_inner_life_event(
        idempotency_key=f"observer-panel:{key}",
        event_type="error",
        process_name="observer-panel",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        role="observer",
        source_message_id=source_ids[0],
        content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        content_excerpt=excerpt,
        event_tags=["u6.6", "observer", "error"],
        source_ids=source_ids,
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "identity_patches": 0,
            "reviewer_id": reviewer_id,
            "error": error,
        },
        rollout_tag=rollout_tag,
        gate_decision="error:reviewer_failed",
    )


def _event_key(kind: str, text: str, source_ids: list[str], reviewer_id: str) -> str:
    payload = "|".join([kind, text, reviewer_id, *source_ids])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
