"""
Post-retrieval memory reconsolidation.

Every time a memory is retrieved, it is reconsolidated — its access metadata,
stability, accessibility, and connections are updated based on the current
context. The stored strength column is S0 and remains fixed after encoding.
This models the neuroscience finding that memories become labile (modifiable)
upon retrieval and are then re-stored in an updated form.

This is what makes Mnemos memories "living" — they change every time
they are touched, rather than being static records.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ..core.engram import Engram
from ..core.types import ConnectionRelation

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def reconsolidate(
    engram: Engram,
    current_context: str,
    co_retrieved_ids: list[str],
    store: EngramStore,
    strength_delta: float = 0.05,
    stability_delta: float = 0.01,
    accessibility_floor: float = 0.8,
    *,
    config: dict[str, Any] | None = None,
) -> Engram:
    """Update a memory after retrieval (reconsolidation).

    Models the reconsolidation window: when a memory is retrieved, it
    becomes temporarily labile and is re-stored with updates based on
    the current retrieval context. S0 strength is not changed.

    Effects:
    1. Access metadata updated (count, timestamp)
    2. Stability increased slowly (spaced repetition effect)
    3. Accessibility boosted (just accessed = highly retrievable)
    4. Connections to co-retrieved engrams created/strengthened
    5. Version snapshot saved for audit trail
    6. Persisted to store

    Args:
        engram: The engram being reconsolidated.
        current_context: The retrieval cue / context (for audit trail).
        co_retrieved_ids: IDs of other engrams retrieved alongside this one.
        store: The storage backend for persisting updates.
        strength_delta: Deprecated; engram strength is frozen S0 after encoding.
        stability_delta: How much to increase stability per retrieval.
        accessibility_floor: Minimum accessibility after retrieval.

    Returns:
        The updated engram after reconsolidation.
    """
    # 1. Access metadata
    engram.record_access()
    engram.reconsolidation_count += 1

    # 2. Stability increases — scaled by retrieval history (spaced repetition)
    cfg = config or {}
    spacing_factor = cfg.get("reconsolidation_spacing_factor", 0.5)
    max_delta = cfg.get("reconsolidation_max_stability_delta", 0.03)
    scaled_delta = min(
        max_delta,
        stability_delta
        * (1 + math.log1p(engram.reconsolidation_count) * spacing_factor),
    )

    # Connection bonus: well-connected memories stabilize faster on retrieval
    conn_bonus_rate = cfg.get("reconsolidation_connection_bonus", 0.002)
    n_conns = len(engram.connections)
    conn_bonus = min(0.01, conn_bonus_rate * n_conns)

    engram.stability = min(1.0, engram.stability + scaled_delta + conn_bonus)

    # 4. Accessibility boost — just accessed, very retrievable now
    engram.accessibility = min(1.0, max(engram.accessibility, accessibility_floor))

    # 5. Co-retrieval connections — memories retrieved together become
    # linked. Co-activation is correlation, not evidence: the edge is
    # CO_ACTIVATED (structural), not SUPPORTS, so the discovery pass can
    # classify it semantically later instead of inheriting a monoculture.
    connection_updates = []
    for co_id in co_retrieved_ids:
        if co_id != engram.id:
            connection_updates.append(
                engram.add_connection(
                    target_id=co_id,
                    relation=ConnectionRelation.CO_ACTIVATED,
                    strength=0.3,
                    formed_by="retrieval",
                )
            )

    # 6. Version snapshot for audit trail
    engram.add_version(reason="reconsolidation")

    # 7. Persist
    if connection_updates:
        store.save_engram_with_connection_updates(engram, connection_updates)
    else:
        store.save_engram(engram)

    return engram
