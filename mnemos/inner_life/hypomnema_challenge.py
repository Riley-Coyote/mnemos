"""Apply bounded hypomnema challenge results with private telemetry."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..store.sqlite_store import EngramStore
from .turn_finalizer import _excerpt


VALID_CHALLENGE_DECISIONS = {"hold", "revise_up", "revise_down", "retire"}
_DEFAULT_CONFIDENCE_DELTAS = {
    "revise_up": 0.05,
    "revise_down": -0.10,
}


def apply_hypomnema_challenge(
    store: EngramStore,
    *,
    entry_id: str,
    challenge: Mapping[str, Any] | str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    reviewer_id: str = "reviewer",
    rollout_tag: str = "u6.6",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply one challenge verdict to a scoped hypomnema entry.

    The challenge payload is expected to come from a bounded reviewer/gate. This
    function validates the verdict, applies only hypomnema-safe actions, and
    records a private event-ledger row. It never deletes rows and never creates
    engrams, beliefs, identity patches, candidates, or shared-pool records.
    """
    if not entry_id.strip():
        raise ValueError("entry_id is required")

    parsed, parse_error = _parse_challenge(challenge)
    key = idempotency_key or _challenge_key(entry_id, parsed if parsed else challenge)
    if _ledger_key_exists(
        store,
        key,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
    ):
        return {
            "applied": 0,
            "duplicates": 1,
            "decision": "duplicate",
            "generated_memory_writes": 0,
        }

    # The challenge is an inner-life review process: it examines and revises
    # hypomnema at any tier (review_only entries are exactly the ones needing
    # challenge), so it opts into unfiltered access rather than the operational
    # default (R5, T3/D8-A). It writes nothing to an operational read surface.
    before = store.get_hypomnema_entry(
        entry_id,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=None,
    )
    if before is None:
        _record_challenge_event(
            store,
            idempotency_key=key,
            event_type="error",
            entry_id=entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            reviewer_id=reviewer_id,
            rollout_tag=rollout_tag,
            gate_decision="error:missing_hypomnema",
            decision="error",
            rationale="hypomnema entry not found",
            source_ids=[entry_id],
            metadata={"error": "missing_hypomnema"},
        )
        return {
            "applied": 0,
            "duplicates": 0,
            "decision": "error",
            "reason": "missing_hypomnema",
            "generated_memory_writes": 0,
        }

    if parse_error is not None:
        _record_challenge_event(
            store,
            idempotency_key=key,
            event_type="error",
            entry_id=entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            reviewer_id=reviewer_id,
            rollout_tag=rollout_tag,
            gate_decision="error:malformed_critic_output",
            decision="error",
            rationale=parse_error,
            source_ids=[entry_id],
            metadata=_base_metadata(before, None, reviewer_id)
            | {"error": "malformed_critic_output"},
        )
        return {
            "applied": 0,
            "duplicates": 0,
            "decision": "error",
            "reason": "malformed_critic_output",
            "generated_memory_writes": 0,
        }

    decision = str(parsed.get("decision", "")).strip().lower()
    rationale = str(parsed.get("rationale", "")).strip()
    source_ids = [entry_id, *_source_ids(parsed.get("source_ids"))]
    if decision not in VALID_CHALLENGE_DECISIONS or not rationale:
        _record_challenge_event(
            store,
            idempotency_key=key,
            event_type="error",
            entry_id=entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            reviewer_id=reviewer_id,
            rollout_tag=rollout_tag,
            gate_decision="error:malformed_critic_output",
            decision="error",
            rationale=rationale or "missing rationale or unsupported decision",
            source_ids=source_ids,
            metadata=_base_metadata(before, None, reviewer_id)
            | {
                "error": "malformed_critic_output",
                "critic_decision": decision,
            },
        )
        return {
            "applied": 0,
            "duplicates": 0,
            "decision": "error",
            "reason": "malformed_critic_output",
            "generated_memory_writes": 0,
        }

    action_writes = 0
    if decision == "hold":
        after = before
    elif decision == "retire":
        store.archive_hypomnema_entry(
            entry_id,
            reason=f"u6.6 challenge retire: {rationale}",
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )
        after = store.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            read_visibility=None,  # review process: read back at any tier (R5/D8-A)
        )
        action_writes = 1
    else:
        revised_content = str(parsed.get("revised_content") or before["content"])
        delta = _confidence_delta(parsed, decision)
        store.revise_hypomnema_entry(
            entry_id,
            revised_content,
            reason=f"u6.6 challenge {decision}: {rationale}",
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            confidence=_clamp01(float(before["confidence"]) + delta),
            salience=before["salience"],
        )
        after = store.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            read_visibility=None,  # review process: read back at any tier (R5/D8-A)
        )
        action_writes = 1

    metadata = _base_metadata(before, after, reviewer_id) | {
        "decision": decision,
        "hypomnema_writes": action_writes,
        "generated_memory_writes": 0,
    }
    _record_challenge_event(
        store,
        idempotency_key=key,
        event_type="tool_event",
        entry_id=entry_id,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        reviewer_id=reviewer_id,
        rollout_tag=rollout_tag,
        gate_decision=decision,
        decision=decision,
        rationale=rationale,
        source_ids=source_ids,
        metadata=metadata,
    )
    return {
        "applied": action_writes,
        "duplicates": 0,
        "decision": decision,
        "hypomnema_writes": action_writes,
        "generated_memory_writes": 0,
        "entry_id": entry_id,
    }


def _parse_challenge(
    challenge: Mapping[str, Any] | str,
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(challenge, Mapping):
        return dict(challenge), None
    try:
        parsed = json.loads(challenge)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "challenge output must be a JSON object"
    return parsed, None


def _challenge_key(entry_id: str, challenge: Any) -> str:
    payload = json.dumps(challenge, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"hypomnema-challenge:{entry_id}:{digest}"


def _ledger_key_exists(
    store: EngramStore,
    idempotency_key: str,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
) -> bool:
    rows = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=1000,
    )
    return any(row["idempotency_key"] == idempotency_key for row in rows)


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


def _confidence_delta(challenge: dict[str, Any], decision: str) -> float:
    if "confidence_delta" not in challenge:
        return _DEFAULT_CONFIDENCE_DELTAS[decision]
    try:
        value = float(challenge["confidence_delta"])
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE_DELTAS[decision]
    if decision == "revise_down":
        return min(0.0, max(value, -0.35))
    return max(0.0, min(value, 0.20))


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _base_metadata(
    before: dict[str, Any],
    after: dict[str, Any] | None,
    reviewer_id: str,
) -> dict[str, Any]:
    return {
        "writes_memory": False,
        "generated_memory_writes": 0,
        "reviewer_id": reviewer_id,
        "confidence_before": before.get("confidence"),
        "confidence_after": None if after is None else after.get("confidence"),
        "active_before": before.get("active"),
        "active_after": None if after is None else after.get("active"),
        "revision_count_before": before.get("revision_count"),
        "revision_count_after": None if after is None else after.get("revision_count"),
    }


def _record_challenge_event(
    store: EngramStore,
    *,
    idempotency_key: str,
    event_type: str,
    entry_id: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    reviewer_id: str,
    rollout_tag: str,
    gate_decision: str,
    decision: str,
    rationale: str,
    source_ids: list[str],
    metadata: dict[str, Any],
) -> None:
    excerpt, _ = _excerpt(f"{entry_id}: {decision}: {rationale}", 480)
    store.upsert_inner_life_event(
        idempotency_key=idempotency_key,
        event_type=event_type,
        process_name="hypomnema-challenge",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        role="critic",
        source_message_id=source_ids[1] if len(source_ids) > 1 else None,
        content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        content_excerpt=excerpt,
        event_tags=["u6.6", "hypomnema-challenge", f"decision:{decision}"],
        source_ids=source_ids,
        metadata=metadata | {"reviewer_id": reviewer_id},
        rollout_tag=rollout_tag,
        gate_decision=gate_decision,
    )
