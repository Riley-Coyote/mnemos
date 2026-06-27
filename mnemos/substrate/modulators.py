"""
Substrate modulators.

Modulators shape the character of the substrate's response to events.
They don't decide what happens — they influence HOW handlers behave.

Approach: weight recent events (last 24h) more heavily than historical
averages. This makes modulators responsive to current state.

Four modulators:
  arousal     — how active/reactive the system is (high = more handler triggers)
  openness    — willingness to form new connections and explore (affects LLM temp)
  resolution  — how much detail the system attends to (affects handler thoroughness)
  selection   — threshold for what's worth processing (affects event filtering)
"""

from dataclasses import dataclass
import sqlite3
import os
from datetime import datetime, timedelta, timezone


@dataclass
class ModulatorState:
    """Current modulator values. All 0.0 - 1.0."""
    arousal: float = 0.5
    openness: float = 0.5
    resolution: float = 0.5
    selection_threshold: float = 0.5

    @property
    def temperature(self) -> float:
        """LLM temperature derived from openness. More open = higher temp."""
        # Range: 0.4 (closed) to 1.0 (open)
        return 0.4 + (self.openness * 0.6)


def compute_modulators(
    db_path: str,
    recent_window_hours: int = 24,
    agent_id: str | None = None,
    require_consolidation_authorized: bool = False,
) -> ModulatorState:
    """Compute modulator values from the memory graph.

    Weights recent activity (last N hours) heavily to make modulators
    responsive to current state rather than historical averages.
    """
    db_path = os.path.expanduser(db_path)
    conn = sqlite3.connect(db_path)

    now = datetime.now(timezone.utc)
    recent_cutoff = (now - timedelta(hours=recent_window_hours)).isoformat()

    predicates = ["state='active'"]
    params: list[str] = []
    if agent_id is not None:
        predicates.append("owner_agent_id = ?")
        params.append(agent_id)
    if require_consolidation_authorized:
        predicates.append("consolidation_authorized = 1")
    engram_where = " AND ".join(predicates)

    def count_connections(formed_after: str | None = None) -> int:
        if agent_id is None and not require_consolidation_authorized:
            if formed_after is None:
                return conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM connections WHERE formed_at > ?",
                (formed_after,),
            ).fetchone()[0]

        src_predicates = ["src.state='active'"]
        dst_predicates = ["dst.state='active'"]
        connection_params: list[str] = []
        if agent_id is not None:
            src_predicates.append("src.owner_agent_id = ?")
            connection_params.append(agent_id)
            dst_predicates.append("dst.owner_agent_id = ?")
            connection_params.append(agent_id)
        if require_consolidation_authorized:
            src_predicates.append("src.consolidation_authorized = 1")
            dst_predicates.append("dst.consolidation_authorized = 1")

        connection_predicates = [
            *src_predicates,
            *dst_predicates,
        ]
        if formed_after is not None:
            connection_predicates.append("c.formed_at > ?")
            connection_params.append(formed_after)

        return conn.execute(
            f"""
            SELECT COUNT(*)
            FROM connections c
            JOIN engrams src ON src.id = c.source_id
            JOIN engrams dst ON dst.id = c.target_id
            WHERE {" AND ".join(connection_predicates)}
            """,
            connection_params,
        ).fetchone()[0]

    # ── Total counts ──
    total_engrams = conn.execute(
        f"SELECT COUNT(*) FROM engrams WHERE {engram_where}",
        params,
    ).fetchone()[0]
    total_connections = count_connections()

    # ── Recent activity ──
    recent_engrams = conn.execute(
        f"SELECT COUNT(*) FROM engrams WHERE {engram_where} AND created_at > ?",
        (*params, recent_cutoff),
    ).fetchone()[0]
    # ── Average vividness (accessibility * strength) ──
    avg_vividness = conn.execute(
        f"SELECT AVG(accessibility * strength) FROM engrams WHERE {engram_where}",
        params,
    ).fetchone()[0] or 0.25

    # ── Belief stability ──
    belief_predicates = ["superseded_by IS NULL", "confidence_pending_review = 0"]
    belief_params: list[str] = []
    if agent_id is not None:
        belief_predicates.append("agent_id = ?")
        belief_params.append(agent_id)
    belief_count = conn.execute(
        f"SELECT COUNT(*) FROM beliefs WHERE {' AND '.join(belief_predicates)}",
        belief_params,
    ).fetchone()[0]

    conn.close()

    # ── Compute modulators ──

    # Arousal: how active has memory formation been recently?
    if total_engrams > 0:
        recent_ratio = recent_engrams / max(total_engrams * 0.1, 1)
        arousal = min(0.9, max(0.1, recent_ratio))
    else:
        arousal = 0.3

    # Openness: inversely related to belief count and connection density
    if total_engrams > 0:
        connection_density = total_connections / total_engrams
        belief_settlement = min(belief_count / 10.0, 1.0)
        openness = max(0.2, 0.8 - (connection_density * 0.1) - (belief_settlement * 0.2))
    else:
        openness = 0.7

    # Resolution: based on average vividness of recent memories
    resolution = min(0.9, max(0.2, avg_vividness * 2))

    # Selection threshold: derived from arousal
    selection_threshold = max(0.2, 0.7 - (arousal * 0.3))

    return ModulatorState(
        arousal=round(arousal, 3),
        openness=round(openness, 3),
        resolution=round(resolution, 3),
        selection_threshold=round(selection_threshold, 3),
    )
