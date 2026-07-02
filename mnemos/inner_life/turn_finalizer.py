"""Post-turn finalization below durable memory.

The turn finalizer writes private provenance rows only. It does not encode
engrams, hypomnema, beliefs, identity patches, candidates, or shared records.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..store.sqlite_store import EngramStore


DEFAULT_EXCERPT_CHARS = 480


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _excerpt(text: str, limit: int = DEFAULT_EXCERPT_CHARS) -> tuple[str, bool]:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[: max(0, limit - 3)].rstrip() + "...", True


def _source_id_list(source_message_ids: list[str] | tuple[str, ...] | None) -> list[str]:
    if not source_message_ids:
        return []
    return [str(item).strip() for item in source_message_ids if str(item).strip()]


def finalize_turn_event(
    store: EngramStore,
    *,
    session_id: str,
    turn_id: str | None = None,
    thread_id: str | None = None,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    user_text: str = "",
    assistant_text: str = "",
    source_message_ids: list[str] | tuple[str, ...] | None = None,
    source_timestamp: str | None = None,
    rollout_tag: str = "u6.6",
    max_excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize one completed exchange as an idempotent event-ledger row."""
    user_text = user_text.strip()
    assistant_text = assistant_text.strip()
    if not user_text and not assistant_text:
        return {
            "written": 0,
            "duplicates": 0,
            "skipped": 1,
            "reason": "empty_exchange",
            "generated_memory_writes": 0,
        }

    source_ids = _source_id_list(source_message_ids)
    combined = f"USER:\n{user_text}\n\nASSISTANT:\n{assistant_text}".strip()
    content_hash = _hash_text(combined)
    resolved_turn_id = (turn_id or content_hash[:16]).strip()
    excerpt, truncated = _excerpt(
        f"USER: {user_text}\nASSISTANT: {assistant_text}",
        max_excerpt_chars,
    )
    idempotency_key = f"turn:{session_id}:{resolved_turn_id}"
    before = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        event_type="turn_finalized",
        limit=1000,
    )
    existed = any(row["idempotency_key"] == idempotency_key for row in before)
    event_metadata = {
        "writes_memory": False,
        "generated_memory_writes": 0,
        "user_hash": _hash_text(user_text) if user_text else "",
        "assistant_hash": _hash_text(assistant_text) if assistant_text else "",
        "excerpt_truncated": truncated,
        "queued_continuity": False,
        "skip_reason": "ledger_only",
    }
    event_metadata.update(metadata or {})
    store.upsert_inner_life_event(
        idempotency_key=idempotency_key,
        event_type="turn_finalized",
        process_name="turn-finalizer",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=resolved_turn_id,
        role="exchange",
        source_message_id=source_ids[0] if source_ids else None,
        source_timestamp=source_timestamp,
        content_hash=content_hash,
        content_excerpt=excerpt,
        event_tags=["u6.6", "turn-event"],
        source_ids=source_ids,
        metadata=event_metadata,
        rollout_tag=rollout_tag,
        gate_decision="ledger_only",
    )
    return {
        "written": 0 if existed else 1,
        "duplicates": 1 if existed else 0,
        "skipped": 0,
        "reason": "ledger_only",
        "generated_memory_writes": 0,
        "idempotency_key": idempotency_key,
    }
