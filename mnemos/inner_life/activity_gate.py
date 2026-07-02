"""Zero-LLM activity gate for gated inner-life processes.

The gate decides whether a scheduled inner-life process has enough recent,
grounded activity to run. It writes telemetry only to the private
``inner_life_events`` ledger and never encodes durable memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..store.sqlite_store import EngramStore


DEFAULT_ACTIVITY_GATE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "default_cooldown_minutes": 240,
    "default_signal_window_minutes": 24 * 60,
    "default_min_signals": 1,
    "max_event_scan": 500,
    "max_consolidation_scan": 20,
    "consolidation_passes": ["cycle"],
    "processes": {
        "challenge": {"cooldown_minutes": 12 * 60},
        "observe": {"cooldown_minutes": 12 * 60},
        "affect": {"cooldown_minutes": 6 * 60},
        "reflect": {"cooldown_minutes": 4 * 60},
        "wander": {"cooldown_minutes": 8 * 60},
        "dream": {"cooldown_minutes": 24 * 60},
    },
}

_SIGNAL_EVENT_TYPES = {
    "session_finalized",
    "turn_finalized",
    "turn_message",
    "tool_event",
    "file_event",
    "test_outcome",
}


@dataclass(frozen=True)
class ActivitySignal:
    source_id: str
    source_type: str
    occurred_at: datetime


@dataclass(frozen=True)
class ActivityGateDecision:
    allowed: bool
    process_name: str
    reason: str
    gate_decision: str
    signal_count: int
    source_ids: list[str]
    latest_signal_at: str | None
    cooldown_until: str | None
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "process_name": self.process_name,
            "reason": self.reason,
            "gate_decision": self.gate_decision,
            "signal_count": self.signal_count,
            "source_ids": list(self.source_ids),
            "latest_signal_at": self.latest_signal_at,
            "cooldown_until": self.cooldown_until,
            "metadata": dict(self.metadata),
            "writes_memory": False,
            "generated_memory_writes": 0,
        }


def evaluate_activity_gate(
    store: EngramStore,
    *,
    process_name: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str | None = None,
    thread_id: str | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | str | None = None,
    rollout_tag: str = "u6.6",
    record_decision: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Evaluate whether an inner-life process should run before LLM work."""
    resolved_process = process_name.strip()
    if not resolved_process:
        raise ValueError("process_name is required")

    gate_config = _resolve_gate_config(config)
    process_config = _resolve_process_config(gate_config, resolved_process)
    now_dt = _coerce_datetime(now)
    signal_window_minutes = _positive_int(
        process_config.get(
            "signal_window_minutes",
            gate_config["default_signal_window_minutes"],
        ),
        gate_config["default_signal_window_minutes"],
    )
    cooldown_minutes = _positive_int(
        process_config.get(
            "cooldown_minutes",
            gate_config["default_cooldown_minutes"],
        ),
        gate_config["default_cooldown_minutes"],
    )
    min_signals = _positive_int(
        process_config.get("min_signals", gate_config["default_min_signals"]),
        gate_config["default_min_signals"],
    )

    if not gate_config.get("enabled", True):
        decision = _decision(
            allowed=False,
            process_name=resolved_process,
            reason="gate_disabled",
            signals=[],
            cooldown_until=None,
            config_metadata={
                "cooldown_minutes": cooldown_minutes,
                "signal_window_minutes": signal_window_minutes,
                "min_signals": min_signals,
            },
        )
    elif not process_config.get("enabled", True):
        decision = _decision(
            allowed=False,
            process_name=resolved_process,
            reason="process_disabled",
            signals=[],
            cooldown_until=None,
            config_metadata={
                "cooldown_minutes": cooldown_minutes,
                "signal_window_minutes": signal_window_minutes,
                "min_signals": min_signals,
            },
        )
    else:
        cooldown_until = _cooldown_until(
            store,
            process_name=resolved_process,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            cooldown_minutes=cooldown_minutes,
            now=now_dt,
            limit=_positive_int(gate_config.get("max_event_scan"), 500),
        )
        if cooldown_until is not None:
            decision = _decision(
                allowed=False,
                process_name=resolved_process,
                reason="cooldown",
                signals=[],
                cooldown_until=cooldown_until,
                config_metadata={
                    "cooldown_minutes": cooldown_minutes,
                    "signal_window_minutes": signal_window_minutes,
                    "min_signals": min_signals,
                },
            )
        else:
            since = now_dt - timedelta(minutes=signal_window_minutes)
            signals = _collect_activity_signals(
                store,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                since=since,
                now=now_dt,
                event_limit=_positive_int(gate_config.get("max_event_scan"), 500),
                consolidation_limit=_positive_int(
                    gate_config.get("max_consolidation_scan"),
                    20,
                ),
                consolidation_passes=gate_config.get(
                    "consolidation_passes",
                    ["cycle"],
                ),
            )
            allowed = len(signals) >= min_signals
            decision = _decision(
                allowed=allowed,
                process_name=resolved_process,
                reason="activity_detected" if allowed else "no_recent_activity",
                signals=signals,
                cooldown_until=None,
                config_metadata={
                    "cooldown_minutes": cooldown_minutes,
                    "signal_window_minutes": signal_window_minutes,
                    "min_signals": min_signals,
                },
            )

    if record_decision:
        _record_gate_decision(
            store,
            decision=decision,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            session_id=session_id,
            thread_id=thread_id,
            now=now_dt,
            rollout_tag=rollout_tag,
            idempotency_key=idempotency_key,
        )
    return decision.as_dict()


def _resolve_gate_config(config: dict[str, Any] | None) -> dict[str, Any]:
    gate_config = _deep_merge(
        DEFAULT_ACTIVITY_GATE_CONFIG,
        _activity_gate_config_fragment(config or {}),
    )
    gate_config["processes"] = _deep_merge(
        DEFAULT_ACTIVITY_GATE_CONFIG["processes"],
        gate_config.get("processes", {}),
    )
    return gate_config


def _activity_gate_config_fragment(config: dict[str, Any]) -> dict[str, Any]:
    if "inner_life" in config:
        return config.get("inner_life", {}).get("activity_gate", {})
    if "activity_gate" in config:
        return config.get("activity_gate", {})
    return config


def _resolve_process_config(
    gate_config: dict[str, Any],
    process_name: str,
) -> dict[str, Any]:
    processes = gate_config.get("processes", {})
    process_config = processes.get(process_name, {})
    return process_config if isinstance(process_config, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        key: _deep_merge(value, {}) if isinstance(value, dict) else value
        for key, value in base.items()
    }
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


def _collect_activity_signals(
    store: EngramStore,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    since: datetime,
    now: datetime,
    event_limit: int,
    consolidation_limit: int,
    consolidation_passes: list[str] | tuple[str, ...],
) -> list[ActivitySignal]:
    signals: list[ActivitySignal] = []
    events = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=event_limit,
    )
    for row in events:
        if row.get("process_name") == "activity-gate":
            continue
        if row.get("event_type") not in _SIGNAL_EVENT_TYPES:
            continue
        occurred_at = _parse_datetime(row.get("created_at"))
        if occurred_at is None or not since <= occurred_at <= now:
            continue
        signals.append(
            ActivitySignal(
                source_id=str(row.get("id") or row.get("idempotency_key")),
                source_type=f"inner_life:{row.get('event_type')}",
                occurred_at=occurred_at,
            )
        )

    for pass_name in consolidation_passes:
        for row in store.get_consolidation_runs(
            str(pass_name), limit=consolidation_limit
        ):
            occurred_at = _parse_datetime(
                row.get("completed_at") or row.get("started_at")
            )
            if occurred_at is None or not since <= occurred_at <= now:
                continue
            signals.append(
                ActivitySignal(
                    source_id=f"consolidation:{row.get('id')}",
                    source_type=f"consolidation:{pass_name}",
                    occurred_at=occurred_at,
                )
            )

    return sorted(signals, key=lambda signal: signal.occurred_at, reverse=True)


def _cooldown_until(
    store: EngramStore,
    *,
    process_name: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    cooldown_minutes: int,
    now: datetime,
    limit: int,
) -> datetime | None:
    rows = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        event_type="tool_event",
        limit=limit,
    )
    for row in reversed(rows):
        if row.get("process_name") != "activity-gate":
            continue
        metadata = row.get("metadata") or {}
        if metadata.get("target_process") != process_name:
            continue
        if row.get("gate_decision") != "run":
            continue
        created_at = _parse_datetime(row.get("created_at"))
        if created_at is None:
            continue
        until = created_at + timedelta(minutes=cooldown_minutes)
        if now < until:
            return until
        return None
    return None


def _decision(
    *,
    allowed: bool,
    process_name: str,
    reason: str,
    signals: list[ActivitySignal],
    cooldown_until: datetime | None,
    config_metadata: dict[str, Any],
) -> ActivityGateDecision:
    latest = signals[0].occurred_at.isoformat() if signals else None
    source_ids = [signal.source_id for signal in signals[:20]]
    metadata = {
        "writes_memory": False,
        "generated_memory_writes": 0,
        "target_process": process_name,
        "reason": reason,
        "signal_count": len(signals),
        "signal_types": sorted({signal.source_type for signal in signals}),
        **config_metadata,
    }
    return ActivityGateDecision(
        allowed=allowed,
        process_name=process_name,
        reason=reason,
        gate_decision="run" if allowed else f"skip:{reason}",
        signal_count=len(signals),
        source_ids=source_ids,
        latest_signal_at=latest,
        cooldown_until=cooldown_until.isoformat() if cooldown_until else None,
        metadata=metadata,
    )


def _record_gate_decision(
    store: EngramStore,
    *,
    decision: ActivityGateDecision,
    agent_id: str,
    person_id: str,
    project_scope: str,
    session_id: str | None,
    thread_id: str | None,
    now: datetime,
    rollout_tag: str,
    idempotency_key: str | None,
) -> None:
    key = idempotency_key or (
        f"activity-gate:{decision.process_name}:{agent_id}:"
        f"{person_id}:{project_scope}:{now.isoformat()}"
    )
    store.upsert_inner_life_event(
        idempotency_key=key,
        event_type="tool_event" if decision.allowed else "skip",
        process_name="activity-gate",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        session_id=session_id,
        thread_id=thread_id,
        role="gate",
        source_timestamp=now.isoformat(),
        content_hash="",
        content_excerpt=(
            f"{decision.process_name}: {decision.gate_decision}; "
            f"signals={decision.signal_count}"
        ),
        event_tags=["u6.6", "activity-gate"],
        source_ids=decision.source_ids,
        metadata=decision.metadata,
        rollout_tag=rollout_tag,
        gate_decision=decision.gate_decision,
    )
