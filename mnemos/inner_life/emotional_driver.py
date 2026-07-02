"""Event-grounded affect driver for U6.6 inner-life gates."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.emotional_state import EmotionalState
from ..store.sqlite_store import EngramStore


DEFAULT_WINDOW_HOURS = 24
DEFAULT_MAX_EVENTS = 80
DEFAULT_MAGNITUDE = 0.05
DEFAULT_MIN_MOVEMENT = 0.03


def update_event_grounded_affect(
    store: EngramStore,
    *,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_events: int = DEFAULT_MAX_EVENTS,
    magnitude: float = DEFAULT_MAGNITUDE,
    min_movement: float = DEFAULT_MIN_MOVEMENT,
    now: datetime | str | None = None,
    rollout_tag: str = "u6.6",
    record_decision: bool = True,
) -> dict[str, Any]:
    """Compute affect from real Mnemos events and save only meaningful movement."""
    now_dt = _coerce_datetime(now)
    since = now_dt - timedelta(hours=max(1, int(window_hours)))
    events = _recent_signal_events(
        store,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        since=since,
        now=now_dt,
        limit=max(1, int(max_events)),
    )
    if not events:
        if record_decision:
            _record_affect_event(
                store,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                rollout_tag=rollout_tag,
                gate_decision="skip:no_recent_events",
                event_type="skip",
                reason="no_recent_events",
                source_ids=[],
                metadata={"event_count": 0},
            )
        return _summary(updated=False, reason="no_recent_events", event_count=0)

    before = store.get_latest_emotional_state(agent_id) or EmotionalState()
    after = EmotionalState.from_dict(before.to_dict())
    applied: list[str] = []
    for row in events:
        for event_name in _event_influences(row):
            if after.apply_cognitive_event(event_name, magnitude=magnitude):
                applied.append(event_name)

    movement = _movement(before, after)
    source_ids = [str(row.get("id") or row.get("idempotency_key")) for row in events[:20]]
    if movement < min_movement or not applied:
        if record_decision:
            _record_affect_event(
                store,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                rollout_tag=rollout_tag,
                gate_decision="skip:below_movement_threshold",
                event_type="skip",
                reason="below_movement_threshold",
                source_ids=source_ids,
                metadata={
                    "event_count": len(events),
                    "applied_events": applied,
                    "movement": movement,
                    "min_movement": min_movement,
                },
            )
        return _summary(
            updated=False,
            reason="below_movement_threshold",
            event_count=len(events),
            movement=movement,
            applied_events=applied,
        )

    store.save_emotional_state(after, agent_id=agent_id)
    if record_decision:
        _record_affect_event(
            store,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            gate_decision="affect_updated",
            event_type="tool_event",
            reason="affect_updated",
            source_ids=source_ids,
            metadata={
                "event_count": len(events),
                "applied_events": applied,
                "movement": movement,
                "before": before.to_dict(),
                "after": after.to_dict(),
            },
        )
    return _summary(
        updated=True,
        reason="affect_updated",
        event_count=len(events),
        movement=movement,
        applied_events=applied,
        state=after.to_dict(),
    )


def _summary(
    *,
    updated: bool,
    reason: str,
    event_count: int,
    movement: float = 0.0,
    applied_events: list[str] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "updated": updated,
        "reason": reason,
        "event_count": event_count,
        "movement": round(movement, 4),
        "applied_events": applied_events or [],
        "state": state,
        "generated_memory_writes": 0,
        "identity_patches": 0,
    }


def _recent_signal_events(
    store: EngramStore,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    since: datetime,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    rows = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=limit,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("process_name") == "emotional-driver":
            continue
        created_at = _parse_datetime(row.get("created_at"))
        if created_at is None or not since <= created_at <= now:
            continue
        if _event_influences(row):
            out.append(row)
    return out


def _event_influences(row: dict[str, Any]) -> list[str]:
    event_type = row.get("event_type")
    process_name = row.get("process_name")
    gate_decision = str(row.get("gate_decision") or "")
    tags = set(row.get("event_tags") or [])
    text = " ".join(
        [
            str(row.get("content_excerpt") or ""),
            gate_decision,
            " ".join(tags),
        ]
    ).lower()
    influences: list[str] = []
    if event_type in {"turn_finalized", "session_finalized", "turn_message"}:
        influences.append("user_interaction")
    if "verified" in text or "passed" in text or "green" in text:
        influences.append("schema_slots_filled")
    if event_type == "test_outcome" and "failed" not in text:
        influences.append("schema_slots_filled")
    if event_type == "error" or "error:" in gate_decision or "failed" in text:
        influences.append("retrieval_failed")
    if "contradiction" in text or "revise_down" in gate_decision:
        influences.append("contradiction_detected")
    if process_name == "observer-panel" and gate_decision == "observer_signal":
        influences.append("relationship_memory_accessed")
    if process_name == "hypomnema-challenge" and gate_decision == "retire":
        influences.append("stagnant_belief_found")
    return influences


def _movement(before: EmotionalState, after: EmotionalState) -> float:
    dims = (
        "curiosity",
        "restlessness",
        "warmth",
        "clarity",
        "creative_flow",
        "isolation",
    )
    return round(
        sum(abs(float(getattr(after, dim)) - float(getattr(before, dim))) for dim in dims),
        4,
    )


def _record_affect_event(
    store: EngramStore,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    gate_decision: str,
    event_type: str,
    reason: str,
    source_ids: list[str],
    metadata: dict[str, Any],
) -> None:
    store.upsert_inner_life_event(
        idempotency_key=f"emotional-driver:{gate_decision}:{_source_key(source_ids)}",
        event_type=event_type,
        process_name="emotional-driver",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        role="affect",
        source_message_id=source_ids[0] if source_ids else None,
        content_hash="",
        content_excerpt=f"emotional-driver {reason}; events={metadata.get('event_count', 0)}",
        event_tags=["u6.6", "emotional-driver", "affect"],
        source_ids=source_ids,
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "identity_patches": 0,
            "reason": reason,
            **metadata,
        },
        rollout_tag=rollout_tag,
        gate_decision=gate_decision,
    )


def _source_key(source_ids: list[str]) -> str:
    if not source_ids:
        return "none"
    return hashlib.sha256("|".join(source_ids).encode("utf-8")).hexdigest()[:16]


def _coerce_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid datetime: {value}")
    return parsed


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
