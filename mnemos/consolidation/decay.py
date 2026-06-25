"""
Decay pass: recalculate strength, stability, and accessibility for all active engrams.

Models the natural forgetting curve. The dual-trace model:
- Strength: how well stored (slow to change)
- Stability: resistance to interference (builds with repeated access, resists decay)
- Accessibility: how retrievable RIGHT NOW (fluctuates with recency + connections)

Accessibility decays exponentially, modulated by stability. Higher stability
means slower forgetting. Strength decays much more slowly (10x slower).

Ported from Anima's salience.py and adapted for the dual-trace model.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def run_decay_pass(
    store: EngramStore,
    config: dict[str, Any],
    agent_id: str | None = "default",
) -> dict[str, Any]:
    """Recalculate strength, stability, and accessibility for all active engrams.

    Args:
        store: The engram store containing active engrams.
        config: Configuration dict with decay parameters.
        agent_id: Which agent's engrams to decay. None = all agents
            (used for shared DB consolidation).

    Returns:
        Statistics dict with counts and accessibility changes.
    """
    decay_rate = config.get("decay_rate", 0.01)
    dormant_threshold = config.get("dormant_threshold", 0.05)
    archive_threshold = config.get("archive_threshold", 0.01)

    # load_connections=True because decay uses connection count for decay resistance.
    # U3a: decay_protected and consolidation_authorized filter at the candidate
    # query layer so protected engrams are never considered for mutation.
    engrams = store.get_active_engrams(
        agent_id=agent_id,
        limit=10000,
        load_connections=True,
        include_decay_protected=False,
        require_consolidation_authorized=True,
    )

    stats = {
        "engrams_processed": 0,
        "engrams_decayed": 0,
        "engrams_dormant": 0,
        "engrams_archived": 0,
        "avg_accessibility_before": 0.0,
        "avg_accessibility_after": 0.0,
    }

    if not engrams:
        return stats

    total_before = 0.0
    total_after = 0.0

    for engram in engrams:
        stats["engrams_processed"] += 1
        total_before += engram.accessibility

        hours = _hours_since(engram.last_accessed)

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
        new_accessibility = engram.accessibility * math.exp(-effective_decay * hours)
        new_accessibility = min(1.0, max(0.0, new_accessibility))

        # 2. STRENGTH DECAY (10x slower than accessibility decay)
        # Uses same effective_decay but reduced by factor of 10
        strength_loss = engram.strength * (1.0 - math.exp(-effective_decay * 0.1 * hours))
        new_strength = max(0.0, engram.strength - strength_loss)

        # 3. ANTI-DECAY FLOORS
        if "foundational" in engram.tags:
            new_accessibility = max(0.5, new_accessibility)
            new_strength = max(0.5, new_strength)

        if "active_project" in engram.tags:
            new_accessibility = max(0.6, new_accessibility)

        if hours < 72:
            new_accessibility = max(0.4, new_accessibility)

        # Track if anything changed
        changed = (
            abs(new_accessibility - engram.accessibility) > 0.001
            or abs(new_strength - engram.strength) > 0.001
        )

        if changed:
            stats["engrams_decayed"] += 1

        engram.accessibility = round(new_accessibility, 4)
        engram.strength = round(new_strength, 4)

        # 4. STATE TRANSITIONS
        if new_accessibility < archive_threshold:
            store.archive_engram(engram, reason="decay_below_threshold")
            stats["engrams_archived"] += 1
            continue
        elif new_accessibility < dormant_threshold:
            engram.state = "dormant"
            stats["engrams_dormant"] += 1

        total_after += engram.accessibility

        # 5. PERSIST
        store.save_engram(engram)

    n = max(1, stats["engrams_processed"])
    stats["avg_accessibility_before"] = round(total_before / n, 4)
    # Use same denominator for fair comparison (archived engrams count as 0.0 accessibility)
    stats["avg_accessibility_after"] = round(total_after / n, 4)

    return stats


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
