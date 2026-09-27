"""
Post-retrieval memory reconsolidation.

When a memory is returned to the one reading it, it is reconsolidated — its
strength, stability, and connections are updated based on the current context.
This models the neuroscience finding that memories become labile (modifiable)
upon retrieval and are then re-stored in an updated form.

What a return may change, and how often:

- Only a memory actually shown to the reader is reconsolidated. The caller
  decides what that is (see ``ReactiveRetriever.reinforce``), after all of its
  own filtering.
- A memory is reinforced at most once per session: the Claude Code session
  (``CLAUDE_CODE_SESSION_ID``) when one is named, otherwise this process. A
  second return in the same session changes nothing at all. One session, or
  an automated loop, used to reinforce the same memory thousands of times.
- A return writes no version. A version records a change of content, impact
  or resolution, and a return changes none of them, so the access record and
  the trace dynamics are updated in place instead.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..authorship import harness_session
from ..core.engram import Connection, Engram
from ..core.types import ConnectionRelation

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

# Memories this process has reinforced while no harness session named it: the
# process is then the session. Held in memory, because it ends with the
# process; a named session can span several processes, so it is recorded in
# the store (``EngramStore.claim_return``).
_REINFORCED_IN_PROCESS: set[tuple[str, str]] = set()


def _process_key(store: Any, engram_id: str) -> tuple[str, str]:
    try:
        where = str(Path(store.db_path).resolve())
    except (AttributeError, TypeError, OSError):
        where = f"store:{id(store)}"
    return where, engram_id


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
    session: str | None = None,
) -> Engram:
    """Update a memory after it was returned to the reader (reconsolidation).

    Models the reconsolidation window: when a memory is retrieved, it
    becomes temporarily labile and is re-stored with updates based on
    the current retrieval context.

    The update is applied to the engram as ``store`` holds it, reloaded by
    ID inside the write transaction, not to the object passed in: callers may
    hold a partial engram (FTS results carry no connections or versions), and
    another process may have reinforced it since it was read.

    Effects, the first time this session returns the memory:
    1. Access metadata updated (count, timestamp)
    2. Strength increased (retrieval strengthens storage)
    3. Stability increased slowly (spaced repetition effect)
    4. Accessibility boosted (just accessed = highly retrievable)
    5. Connections to co-retrieved engrams created/strengthened
    6. Persisted in place: no version is written, since nothing a version
       records (content, impact, resolution) has changed

    Any later return in the same session changes nothing.

    Args:
        engram: The engram being reconsolidated.
        current_context: The retrieval cue / context.
        co_retrieved_ids: IDs of other engrams returned alongside this one.
        store: The storage backend for persisting updates.
        strength_delta: How much to increase strength per retrieval.
        stability_delta: How much to increase stability per retrieval.
        accessibility_floor: Minimum accessibility after retrieval.
        session: The session this return belongs to. None reads
            ``CLAUDE_CODE_SESSION_ID``; an empty string means this process.

    Returns:
        The engram as stored after reconsolidation (reloaded from the store),
        or the engram passed in, unchanged, when this session has already
        reinforced it or the store no longer holds it.
    """
    session = harness_session() if session is None else session
    in_process = _process_key(store, engram.id)
    if not session and in_process in _REINFORCED_IN_PROCESS:
        return engram

    with store.transaction():
        # Claimed first, in the same immediate transaction as the write, so
        # two processes of one session cannot both reinforce the memory.
        if session and not store.claim_return(engram.id, session):
            return engram

        # Reload. An FTS seed arrives without connections or versions, and
        # re-storing it reset reinforced links to 0.3 and overwrote version 1.
        stored = store.get_engram(engram.id)
        if stored is None:
            return engram  # gone from the store since it was found
        engram = stored

        # 1. Access metadata
        engram.record_access()
        engram.reconsolidation_count += 1

        # 2. Strength increases — retrieval is rehearsal
        engram.strength = min(1.0, engram.strength + strength_delta)

        # 3. Stability increases — scaled by retrieval history (spaced repetition)
        cfg = config or {}
        spacing_factor = cfg.get("reconsolidation_spacing_factor", 0.5)
        max_delta = cfg.get("reconsolidation_max_stability_delta", 0.03)
        scaled_delta = min(
            max_delta,
            stability_delta * (1 + math.log1p(engram.reconsolidation_count) * spacing_factor),
        )

        # Connection bonus: well-connected memories stabilize faster on retrieval
        conn_bonus_rate = cfg.get("reconsolidation_connection_bonus", 0.002)
        n_conns = len(engram.connections)
        conn_bonus = min(0.01, conn_bonus_rate * n_conns)

        engram.stability = min(1.0, engram.stability + scaled_delta + conn_bonus)

        # 4. Accessibility boost — just accessed, very retrievable now
        engram.accessibility = min(1.0, max(engram.accessibility, accessibility_floor))

        # 5. Co-retrieval connections — memories returned together become
        # linked. Co-activation is correlation, not evidence: the edge is
        # CO_ACTIVATED (structural), not SUPPORTS, so the discovery pass can
        # classify it semantically later instead of inheriting a monoculture.
        links: list[Connection] = []
        for co_id in co_retrieved_ids:
            if co_id != engram.id:
                engram.add_connection(
                    target_id=co_id,
                    relation=ConnectionRelation.CO_ACTIVATED,
                    strength=0.3,
                    formed_by="retrieval",
                )
                links.extend(
                    c for c in engram.connections
                    if c.target_id == co_id
                    and c.relation == ConnectionRelation.CO_ACTIVATED
                )

        # 6. Persist in place. No version: a return's snapshot was a copy of a
        # memory that had not changed (124,493 of the 125,431 version rows on
        # a copy of one real store were written this way).
        store.record_return(engram, links)

    if not session:
        _REINFORCED_IN_PROCESS.add(in_process)
    return engram
