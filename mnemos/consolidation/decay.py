"""
Decay pass: recalculate strength, stability, and accessibility for active and
dormant engrams.

Models the natural forgetting curve. The dual-trace model:
- Strength: how well stored (slow to change)
- Stability: resistance to interference (builds with repeated access, resists decay)
- Accessibility: how retrievable RIGHT NOW (fluctuates with recency + connections)

Accessibility decays exponentially, modulated by stability. Higher stability
means slower forgetting. Strength decays much more slowly (10x slower).

A memory whose accessibility falls below the dormant threshold goes dormant,
and below the archive threshold it moves to the archive. A dormant memory keeps
fading by the same curve until it wakes or reaches the archive; the pass never
raises one, because only a return wakes it (see retrieval/reactive.py). This
pass used to read active memories only, so a dormant one was never touched
again: it could neither fade on nor, since recall did not look either, come
back.

A memory the agent marked standing (how the human wants it to work in every
session) is exempt while it is marked: the pass never reads it, so it neither
fades nor moves. A standing rule is obeyed, not recalled, and fading it for
disuse would take it away exactly because it is followed. Unmarked, it is
read again and fades from where it stands, by the pass's usual elapsed time.

Ported from Anima's salience.py and adapted for the dual-trace model.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..code_version import MAINTENANCE_CODE_VERSION

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

# At most this many active memories are decayed in one pass, the most
# accessible first.
ACTIVE_LIMIT = 10000
# Dormant memories are read this many at a time, and all of them are decayed.
DORMANT_PAGE = 500


def run_decay_pass(
    store: EngramStore,
    config: dict[str, Any],
    agent_id: str | None = "default",
    person_id: str | None = None,
    project_scope: str | None = None,
    max_elapsed_hours: float | None = None,
) -> dict[str, Any]:
    """Recalculate strength, stability, and accessibility for active and
    dormant engrams.

    Args:
        store: The engram store containing the engrams.
        config: Configuration dict with decay parameters.
        agent_id: Which agent's engrams to decay. None = all agents
            (used for shared DB consolidation).
        max_elapsed_hours: Ceiling on how many hours of decay a single pass
            may apply, normally the time since the previous cycle. Each engram
            ages from its own ``last_accessed``, which this pass never writes,
            so without a ceiling every pass re-applies the full age-since-access
            decay and repeated passes compound. None means no ceiling — correct
            only for the first pass, which is genuine catch-up.

    Returns:
        Statistics dict with counts and accessibility changes. The counts and
        averages that were there before describe active memories only;
        ``engrams_dormant`` counts the ones that went dormant in this pass, and
        ``engrams_archived`` every memory the pass archived, from either state.
        ``dormant_processed`` and ``dormant_decayed`` count the dormant ones.
    """
    stats = {
        "engrams_processed": 0,
        "engrams_decayed": 0,
        "engrams_dormant": 0,
        "engrams_archived": 0,
        "avg_accessibility_before": 0.0,
        "avg_accessibility_after": 0.0,
        "dormant_processed": 0,
        "dormant_decayed": 0,
    }

    # Dormant memories keep fading, by rules this code version holds: code
    # older than the store leaves them as they are, whoever calls the pass
    # (the simple runtime runs no pass at all then). They are walked on their
    # own, every one of them, before any active memory goes dormant in this
    # pass (so none is decayed twice). Read together with the active ones,
    # the most accessible first under one limit, they were the first to be
    # left out once a scope held that many active memories.
    if not _older_than(store):
        for engram in _dormant_engrams(store, agent_id, person_id, project_scope):
            stats["dormant_processed"] += 1
            _decay_engram(store, engram, config, max_elapsed_hours, stats)

    # load_connections=True because decay uses connection count for decay resistance
    engrams = store.get_engrams_in_states(
        ("active",), agent_id=agent_id, person_id=person_id,
        project_scope=project_scope, limit=ACTIVE_LIMIT, load_connections=True,
        standing=False,
    )

    if not engrams:
        return stats

    total_before = 0.0
    total_after = 0.0

    for engram in engrams:
        stats["engrams_processed"] += 1
        total_before += engram.accessibility
        after = _decay_engram(store, engram, config, max_elapsed_hours, stats)
        if after is not None:
            total_after += after

    n = max(1, stats["engrams_processed"])
    stats["avg_accessibility_before"] = round(total_before / n, 4)
    # Use same denominator for fair comparison (archived engrams count as 0.0 accessibility)
    stats["avg_accessibility_after"] = round(total_after / n, 4)

    return stats


def _dormant_engrams(
    store: Any,
    agent_id: str | None,
    person_id: str | None,
    project_scope: str | None,
) -> Iterator[Any]:
    """Every dormant memory in the scope not marked standing, a page at a
    time, in id order: a walk the pass's own changes cannot make skip or
    repeat one."""
    after = ""
    while True:
        page = store.get_engrams_in_states(
            ("dormant",), agent_id=agent_id, person_id=person_id,
            project_scope=project_scope, limit=DORMANT_PAGE,
            load_connections=True, after_id=after, standing=False,
        )
        if not page:
            return
        yield from page
        after = page[-1].id


def _decay_engram(
    store: Any,
    engram: Any,
    config: dict[str, Any],
    max_elapsed_hours: float | None,
    stats: dict[str, Any],
) -> float | None:
    """Decay one memory, move it on if it faded far enough, and save it.
    Returns its accessibility after the pass, or None when it was archived."""
    decay_rate = config.get("decay_rate", 0.01)
    dormant_threshold = config.get("dormant_threshold", 0.05)
    archive_threshold = config.get("archive_threshold", 0.01)
    was_dormant = engram.state == "dormant"

    # age_hours is how long since this memory was last touched; it drives
    # the recency floor below. decay_hours is how much decay this pass is
    # entitled to apply — capped at the elapsed window since the previous
    # cycle, because accessibility is already the decayed value and would
    # otherwise be re-decayed by its full age on every single pass.
    age_hours = _hours_since(engram.last_accessed)
    decay_hours = age_hours
    if max_elapsed_hours is not None:
        decay_hours = min(age_hours, max_elapsed_hours)

    # 1. ACCESSIBILITY DECAY
    # Stability resists decay exponentially: high stability → near-zero decay
    stability_factor = config.get("stability_decay_factor", 3.0)
    effective_decay = decay_rate * math.exp(-stability_factor * engram.stability)

    # Connection factor: well-connected memories decay slower (multiplicative)
    n_connections = len(engram.connections)
    if n_connections > 0:
        connection_factor = min(1.0, 0.2 + 0.2 * math.log1p(n_connections))
        effective_decay *= (1.0 - connection_factor * 0.5)
        # At 5 connections: decay slowed by ~16%. At 20: slowed by ~30%.

    # Connection-driven stability growth: structurally important memories
    # gain stability each cycle — the graph topology determines persistence
    stability_conn_threshold = config.get("stability_connection_threshold", 3)
    stability_growth_rate = config.get("stability_growth_rate", 0.002)
    stability_growth_cap = config.get("stability_growth_cap", 0.005)

    if n_connections >= stability_conn_threshold:
        growth = min(stability_growth_cap, stability_growth_rate * math.log1p(n_connections))
        engram.stability = min(1.0, round(engram.stability + growth, 4))

    # Exponential decay
    new_accessibility = engram.accessibility * math.exp(-effective_decay * decay_hours)
    new_accessibility = min(1.0, max(0.0, new_accessibility))

    # 2. STRENGTH DECAY (10x slower than accessibility decay)
    # Uses same effective_decay but reduced by factor of 10
    strength_loss = engram.strength * (1.0 - math.exp(-effective_decay * 0.1 * decay_hours))
    new_strength = max(0.0, engram.strength - strength_loss)

    # 3. ANTI-DECAY FLOORS
    # They hold an active memory up. A dormant one only fades: the pass
    # never raises it, since only a return wakes it.
    if not was_dormant:
        if "foundational" in engram.tags:
            new_accessibility = max(0.5, new_accessibility)
            new_strength = max(0.5, new_strength)

        if "active_project" in engram.tags:
            new_accessibility = max(0.6, new_accessibility)

        if age_hours < 72:
            new_accessibility = max(0.4, new_accessibility)

    # Track if anything changed
    changed = (
        abs(new_accessibility - engram.accessibility) > 0.001
        or abs(new_strength - engram.strength) > 0.001
    )

    if changed:
        stats["dormant_decayed" if was_dormant else "engrams_decayed"] += 1

    engram.accessibility = round(new_accessibility, 4)
    engram.strength = round(new_strength, 4)

    # 4. STATE TRANSITIONS
    if new_accessibility < archive_threshold:
        store.archive_engram(engram, reason="decay_below_threshold")
        stats["engrams_archived"] += 1
        return None
    elif new_accessibility < dormant_threshold and not was_dormant:
        engram.state = "dormant"
        stats["engrams_dormant"] += 1

    # 5. PERSIST
    store.save_engram(engram)
    return engram.accessibility


def _older_than(store: Any) -> bool:
    """Whether code newer than this has opened ``store`` (see code_version)."""
    minimum = store.min_code_version()
    return minimum is not None and minimum > MAINTENANCE_CODE_VERSION


def _hours_since(iso_timestamp: str) -> float:
    """Calculate hours elapsed since an ISO 8601 timestamp."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - then
        return max(0.0, delta.total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0
