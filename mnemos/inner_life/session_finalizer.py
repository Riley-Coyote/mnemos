"""Session-end transcript finalization for U6.6.

This module reconstructs bounded private provenance from JSONL/checkpoint-like
session material. It deliberately avoids LLM calls and never persists generated
memory. Later gates may use these rows as source-grounding evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..store.sqlite_store import EngramStore
from .turn_finalizer import _excerpt, _hash_text


HIGH_SIGNAL_TERMS = (
    "verified",
    "verification",
    "test",
    "tests",
    "passed",
    "failed",
    "error",
    "fix",
    "commit",
    "snapshot",
    "rollback",
    "live db",
    "~/.mnemos",
    "davidauth",
    "david-auth",
    "invariant",
)


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(_content_to_text(item.get("text") or item.get("content")))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return _content_to_text(value.get("text") or value.get("content"))
    return str(value)


def _extract_candidate(record: dict[str, Any], line_no: int) -> dict[str, Any] | None:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    role = record.get("role") or message.get("role")
    raw_type = record.get("type") or record.get("event_type") or message.get("type")
    content = _content_to_text(
        record.get("content")
        or record.get("text")
        or message.get("content")
        or record.get("result")
    ).strip()
    name = record.get("name") or record.get("tool_name") or message.get("name")
    if not content and name:
        content = str(name)
    if not content:
        return None

    event_type = "turn_message"
    tags = ["u6.6", "session-finalizer"]
    if raw_type in {"tool_call", "tool_result", "function_call"} or role == "tool":
        event_type = "tool_event"
        tags.append("tool-event")
    elif raw_type in {"file_event", "file"}:
        event_type = "file_event"
        tags.append("file-event")
    elif raw_type in {"test_outcome", "test"}:
        event_type = "test_outcome"
        tags.append("test-outcome")

    source_message_id = (
        record.get("id")
        or record.get("uuid")
        or record.get("message_id")
        or message.get("id")
        or f"line-{line_no}"
    )
    timestamp = (
        record.get("timestamp")
        or record.get("created_at")
        or message.get("timestamp")
        or message.get("created_at")
    )
    return {
        "event_type": event_type,
        "role": role or raw_type or "event",
        "content": content,
        "source_message_id": str(source_message_id),
        "source_timestamp": str(timestamp) if timestamp else None,
        "tags": tags,
        "line_no": line_no,
        "high_signal": _is_high_signal(content),
    }


def _is_high_signal(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in HIGH_SIGNAL_TERMS)


def _select_candidates(
    candidates: list[dict[str, Any]],
    max_turn_events: int,
) -> list[dict[str, Any]]:
    high_signal = [item for item in candidates if item["high_signal"]]
    ordinary = [item for item in candidates if not item["high_signal"]]
    return (high_signal + ordinary)[: max(0, max_turn_events)]


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_session_transcript(
    store: EngramStore,
    transcript_path: str | Path,
    *,
    session_id: str,
    thread_id: str | None = None,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u6.6",
    max_turn_events: int = 25,
    max_excerpt_chars: int = 480,
) -> dict[str, Any]:
    """Finalize a JSONL transcript/checkpoint into bounded provenance rows."""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return {
            "session_written": 0,
            "turn_events_written": 0,
            "skipped": 1,
            "reason": "missing_transcript",
            "generated_memory_writes": 0,
            "full_transcript_sent_to_llm": False,
        }

    candidates: list[dict[str, Any]] = []
    malformed_lines = 0
    total_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(record, dict):
                continue
            candidate = _extract_candidate(record, line_no)
            if candidate is not None:
                candidates.append(candidate)

    selected = _select_candidates(candidates, max_turn_events)
    source_hash = _source_hash(path)
    session_key = f"session:{session_id}:{source_hash[:24]}"
    before_session = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        event_type="session_finalized",
        limit=1000,
    )
    session_existed = any(
        row["idempotency_key"] == session_key for row in before_session
    )
    store.upsert_inner_life_event(
        idempotency_key=session_key,
        event_type="session_finalized",
        process_name="session-finalizer",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        source_path=str(path),
        content_hash=source_hash,
        content_excerpt=f"session transcript: {path.name}",
        event_tags=["u6.6", "session-finalizer"],
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "total_lines": total_lines,
            "events_seen": len(candidates),
            "events_selected": len(selected),
            "events_dropped": max(0, len(candidates) - len(selected)),
            "malformed_lines": malformed_lines,
            "full_transcript_sent_to_llm": False,
        },
        rollout_tag=rollout_tag,
        gate_decision="ledger_only",
    )

    written = 0
    duplicates = 0
    selected_ids: list[str] = []
    for item in selected:
        selected_ids.append(item["source_message_id"])
        event_hash = _hash_text(item["content"])
        idempotency_key = (
            f"session-event:{session_id}:{item['source_message_id']}:{event_hash[:16]}"
        )
        before = store.get_inner_life_events(
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            limit=1000,
        )
        existed = any(row["idempotency_key"] == idempotency_key for row in before)
        excerpt, truncated = _excerpt(item["content"], max_excerpt_chars)
        store.upsert_inner_life_event(
            idempotency_key=idempotency_key,
            event_type=item["event_type"],
            process_name="session-finalizer",
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=str(item["line_no"]),
            role=item["role"],
            source_message_id=item["source_message_id"],
            source_path=str(path),
            source_timestamp=item["source_timestamp"],
            content_hash=event_hash,
            content_excerpt=excerpt,
            event_tags=item["tags"],
            source_ids=[item["source_message_id"]],
            metadata={
                "writes_memory": False,
                "generated_memory_writes": 0,
                "line_no": item["line_no"],
                "high_signal": item["high_signal"],
                "excerpt_truncated": truncated,
                "full_transcript_sent_to_llm": False,
            },
            rollout_tag=rollout_tag,
            gate_decision="ledger_only",
        )
        if existed:
            duplicates += 1
        else:
            written += 1

    return {
        "session_written": 0 if session_existed else 1,
        "turn_events_written": written,
        "duplicates": duplicates,
        "events_seen": len(candidates),
        "events_dropped": max(0, len(candidates) - len(selected)),
        "selected_source_ids": selected_ids,
        "malformed_lines": malformed_lines,
        "generated_memory_writes": 0,
        "full_transcript_sent_to_llm": False,
    }
