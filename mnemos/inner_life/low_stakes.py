"""Low-stakes private writer for gated U6.6 generated records."""

from __future__ import annotations

import hashlib
from typing import Any

from ..core.engram import EncodingContext, Engram, Lineage, MemorySource
from ..core.types import (
    ConfidenceSource,
    EncodingDepth,
    EngramKind,
    SourceAuthority,
    SourceType,
    Visibility,
)
from ..store.read_visibility import READ_VISIBILITY_AUDIT
from ..store.sqlite_store import EngramStore
from .turn_finalizer import _excerpt


_SOURCE_BY_KIND = {
    "dream": SourceType.DREAM,
    "observe": SourceType.OBSERVER,
    "observer": SourceType.OBSERVER,
    "reflect": SourceType.REFLECTION,
    "reflection": SourceType.REFLECTION,
    "wander": SourceType.REFLECTION,
    "wandering": SourceType.REFLECTION,
}


def write_low_stakes_record(
    store: EngramStore,
    *,
    gate_result: dict[str, Any],
    candidate_kind: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u6.6",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist a generated candidate as private, low-confidence audit-only memory."""
    if not gate_result.get("allowed"):
        reason = gate_result.get("reason") or "gate_not_passed"
        _record_skip(
            store,
            gate_result=gate_result,
            candidate_kind=candidate_kind,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            reason=reason,
        )
        return _summary(written=0, skipped=1, reason=reason)

    content = " ".join(str(gate_result.get("content") or "").split())
    source_ids = _source_ids(gate_result.get("source_ids"))
    if not content:
        _record_skip(
            store,
            gate_result=gate_result,
            candidate_kind=candidate_kind,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            reason="empty_content",
        )
        return _summary(written=0, skipped=1, reason="empty_content")
    if not source_ids:
        _record_skip(
            store,
            gate_result=gate_result,
            candidate_kind=candidate_kind,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            reason="missing_source_ids",
        )
        return _summary(written=0, skipped=1, reason="missing_source_ids")

    key = idempotency_key or _record_key(candidate_kind, content, source_ids)
    if _ledger_key_exists(store, key):
        return _summary(written=0, duplicates=1, reason="duplicate")

    source_type = _source_type(candidate_kind)
    tags = _tags(candidate_kind, rollout_tag)
    engram = Engram(
        content=content,
        impact="",
        kind=EngramKind.EPISODIC,
        tags=tags,
        strength=0.25,
        stability=0.05,
        accessibility=0.20,
        encoding_context=EncodingContext(
            wm_snapshot=source_ids,
            encoding_depth=EncodingDepth.SHALLOW,
            session_id=source_ids[0],
            surprise_level=0.0,
        ),
        source=MemorySource(
            type=source_type,
            session_id=source_ids[0],
            confidence=0.35,
            confidence_source=ConfidenceSource.SPECULATIVE,
            # Inner-life low-stakes record is autonomous producer output:
            # generated, never observed (T3 finding A).
            authority=SourceAuthority.GENERATED,
        ),
        lineage=Lineage(parents=source_ids),
        owner_agent_id=agent_id,
        visibility=Visibility.PRIVATE,
        # Finding A (DAVID-1): the membrane's read_visibility must be stamped at
        # write time. PRIVATE inner-life output maps to audit_only so it never
        # enters operational reads; the handlers' quota/dedup queries filter on
        # this private class (see dreaming.py / wandering.py).
        read_visibility=READ_VISIBILITY_AUDIT,
        voice_exemplar_eligible=False,
        softening_protected=False,
        consolidation_authorized=False,
        decay_protected=False,
    )
    # Atomicity (finding 4): persist the engram and its idempotency-guard ledger
    # row in one transaction. A crash between the two must not leave a committed
    # engram without its guard, which a retry would re-mint as a duplicate.
    outcome = store.save_engram_with_inner_life_event(
        engram,
        **_ledger_event_kwargs(
            engram,
            idempotency_key=key,
            candidate_kind=candidate_kind,
            source_ids=source_ids,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
        ),
    )
    if outcome.get("duplicate"):
        # a concurrent writer won the race for this idempotency key; the staged
        # engram was rolled back, so no duplicate memory was minted.
        return _summary(written=0, duplicates=1, reason="duplicate")
    return _summary(
        written=1,
        reason="written",
        engram_id=engram.id,
        generated_memory_writes=1,
    )


def _summary(
    *,
    written: int,
    reason: str,
    skipped: int = 0,
    duplicates: int = 0,
    engram_id: str | None = None,
    generated_memory_writes: int = 0,
) -> dict[str, Any]:
    return {
        "written": written,
        "skipped": skipped,
        "duplicates": duplicates,
        "reason": reason,
        "engram_id": engram_id,
        "generated_memory_writes": generated_memory_writes,
        "belief_writes": 0,
        "identity_patches": 0,
        "shared_pool_writes": 0,
    }


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


def _source_type(candidate_kind: str) -> str:
    return _SOURCE_BY_KIND.get(candidate_kind.lower(), SourceType.REFLECTION)


def _tags(candidate_kind: str, rollout_tag: str) -> list[str]:
    return [
        "internal",
        "low-stakes",
        "generated",
        "u6.6",
        f"rollout:{rollout_tag}",
        candidate_kind.lower(),
    ]


def _record_key(candidate_kind: str, content: str, source_ids: list[str]) -> str:
    digest = hashlib.sha256(
        "|".join([candidate_kind.lower(), content, *source_ids]).encode("utf-8")
    ).hexdigest()[:16]
    return f"low-stakes:{digest}"


def _ledger_key_exists(
    store: EngramStore,
    idempotency_key: str,
) -> bool:
    conn = store._get_conn()
    row = conn.execute(
        "SELECT 1 FROM inner_life_events WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    return row is not None


def _ledger_event_kwargs(
    engram: Engram,
    *,
    idempotency_key: str,
    candidate_kind: str,
    source_ids: list[str],
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
) -> dict[str, Any]:
    """Build the inner_life_events ledger-row kwargs for a written low-stakes
    engram. Returned rather than written so the caller persists the engram and
    this ledger row in one transaction (idempotency-guard atomicity, finding 4)."""
    excerpt, truncated = _excerpt(engram.content, 480)
    return dict(
        idempotency_key=idempotency_key,
        event_type="tool_event",
        process_name="low-stakes-writer",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        source_message_id=source_ids[0],
        content_hash=hashlib.sha256(engram.content.encode("utf-8")).hexdigest(),
        content_excerpt=excerpt,
        event_tags=["u6.6", "low-stakes", candidate_kind.lower()],
        source_ids=[*source_ids, engram.id],
        metadata={
            "writes_memory": True,
            "generated_memory_writes": 1,
            "belief_writes": 0,
            "identity_patches": 0,
            "shared_pool_writes": 0,
            "voice_exemplar_eligible": False,
            "visibility": Visibility.PRIVATE,
            "engram_id": engram.id,
            "source_type": engram.source.type,
            "excerpt_truncated": truncated,
        },
        rollout_tag=rollout_tag,
        gate_decision="written:low_stakes",
    )


def _record_skip(
    store: EngramStore,
    *,
    gate_result: dict[str, Any],
    candidate_kind: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    reason: str,
) -> None:
    source_ids = _source_ids(gate_result.get("source_ids"))
    key = _record_key(candidate_kind, f"skip:{reason}", source_ids)
    store.upsert_inner_life_event(
        idempotency_key=key,
        event_type="skip",
        process_name="low-stakes-writer",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        source_message_id=source_ids[0] if source_ids else None,
        content_hash="",
        content_excerpt=f"low-stakes writer skipped: {reason}",
        event_tags=["u6.6", "low-stakes", "skip", candidate_kind.lower()],
        source_ids=source_ids,
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "belief_writes": 0,
            "identity_patches": 0,
            "shared_pool_writes": 0,
            "reason": reason,
        },
        rollout_tag=rollout_tag,
        gate_decision=f"skip:{reason}",
    )
