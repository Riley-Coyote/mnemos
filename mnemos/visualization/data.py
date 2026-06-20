"""
Data extraction for the Mnemos visualization dashboard.

Reads engrams, connections, beliefs, and consolidation history from the
SQLite database. Computes timeline, project groupings, and session mappings.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def extract_all(db_path: str, agent_id: str = "default") -> dict[str, Any]:
    """Extract all data needed for the dashboard from the Mnemos database."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    engrams = _extract_engrams(db)
    connections = _extract_connections(db, {e["id"] for e in engrams})
    beliefs = _extract_beliefs(db)
    consolidation_log = _extract_consolidation_log(db)

    db.close()

    # Indexing state
    state_path = Path(db_path).parent / f"{agent_id}_indexing_state.json"
    indexing_state = _load_indexing_state(state_path)

    # Computed data
    timeline = _compute_timeline(engrams)
    projects = _compute_projects(engrams)
    sessions = _compute_sessions(engrams, indexing_state)
    stats = _compute_stats(engrams, connections, beliefs, indexing_state)

    return {
        "engrams": engrams,
        "connections": connections,
        "beliefs": beliefs,
        "consolidation_log": consolidation_log,
        "timeline": timeline,
        "projects": projects,
        "sessions": sessions,
        "indexing_state": indexing_state,
        "stats": stats,
    }


def _extract_engrams(db: sqlite3.Connection) -> list[dict]:
    engrams = []
    for r in db.execute(
        "SELECT id, content, impact, kind, tags, strength, stability, accessibility, "
        "source, created_at, last_accessed, access_count, encoding_context, "
        "reconsolidation_count, state FROM engrams ORDER BY created_at DESC"
    ).fetchall():
        source = _parse_json(r["source"], {})
        tags = _parse_json(r["tags"], [])
        enc_ctx = _parse_json(r["encoding_context"], {})

        # Clean tags
        clean_tags = [t for t in tags if not t.startswith("trace-type:") and t != "session-indexed"]

        engrams.append({
            "id": r["id"],
            "content": r["content"] or "",
            "impact": r["impact"] or "",
            "kind": r["kind"] or "semantic",
            "tags": clean_tags,
            "all_tags": tags,  # Keep originals for project detection
            "strength": r["strength"] or 0.5,
            "stability": r["stability"] or 0.1,
            "accessibility": r["accessibility"] or 0.5,
            "created_at": (r["created_at"] or "")[:19],
            "last_accessed": (r["last_accessed"] or "")[:19],
            "access_count": r["access_count"] or 0,
            "reconsolidation_count": r["reconsolidation_count"] or 0,
            "state": r["state"] or "active",
            "source_type": source.get("type", source.get("source_type", "unknown")),
            "confidence": source.get("confidence", 0.5),
            "session_id": source.get("session_id") or enc_ctx.get("session_id", ""),
            "encoding_depth": enc_ctx.get("encoding_depth", ""),
            "surprise_level": enc_ctx.get("surprise_level", 0),
            "attention_level": enc_ctx.get("attention_level", 0.5),
        })

    return engrams


def _extract_connections(db: sqlite3.Connection, engram_ids: set[str]) -> list[dict]:
    connections = []
    for r in db.execute(
        "SELECT source_id, target_id, relation, strength, formed_at, formed_by FROM connections"
    ).fetchall():
        if r["source_id"] in engram_ids and r["target_id"] in engram_ids:
            connections.append({
                "source": r["source_id"],
                "target": r["target_id"],
                "relation": r["relation"] or "supports",
                "strength": r["strength"] or 0.5,
                "formed_at": (r["formed_at"] or "")[:19],
                "formed_by": r["formed_by"] or "",
            })
    return connections


def _extract_beliefs(db: sqlite3.Connection) -> list[dict]:
    beliefs = []
    try:
        for r in db.execute(
            "SELECT id, content, confidence, domain, created_at, last_revised, "
            "revision_history FROM beliefs WHERE superseded_by IS NULL ORDER BY confidence DESC"
        ).fetchall():
            beliefs.append({
                "id": r["id"],
                "content": r["content"] or "",
                "confidence": r["confidence"] or 0.5,
                "domain": r["domain"] or "",
                "created_at": (r["created_at"] or "")[:19],
                "last_revised": (r["last_revised"] or "")[:19],
            })
    except sqlite3.OperationalError:
        pass  # Table might not exist
    return beliefs


def _extract_consolidation_log(db: sqlite3.Connection) -> list[dict]:
    logs = []
    try:
        for r in db.execute(
            "SELECT pass_name, started_at, completed_at, stats FROM consolidation_log "
            "ORDER BY started_at DESC LIMIT 20"
        ).fetchall():
            logs.append({
                "pass_name": r["pass_name"] or "",
                "started_at": (r["started_at"] or "")[:19],
                "completed_at": (r["completed_at"] or "")[:19],
                "stats": _parse_json(r["stats"], {}),
            })
    except sqlite3.OperationalError:
        pass
    return logs


def _load_indexing_state(path: Path) -> dict:
    if not path.exists():
        return {"indexed_sessions": {}, "total_memories_encoded": 0}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"indexed_sessions": {}, "total_memories_encoded": 0}


def _compute_timeline(engrams: list[dict]) -> dict[str, dict[str, int]]:
    """Engrams per day, broken down by kind."""
    timeline: dict[str, dict[str, int]] = {}
    for e in engrams:
        if e["state"] != "active" or not e["created_at"]:
            continue
        day = e["created_at"][:10]
        kind = e["kind"]
        if day not in timeline:
            timeline[day] = {}
        timeline[day][kind] = timeline[day].get(kind, 0) + 1
    return dict(sorted(timeline.items()))


def _compute_projects(engrams: list[dict]) -> dict[str, list[str]]:
    """Group engram IDs by project tag."""
    # Count tag frequency to identify project-like tags
    tag_counts: Counter = Counter()
    for e in engrams:
        if e["state"] != "active":
            continue
        for t in e["all_tags"]:
            if not t.startswith("trace-type:") and t != "session-indexed":
                tag_counts[t] += 1

    # Project tags: appear on 3+ engrams, not generic type tags
    generic_tags = {"decision", "lesson", "pattern", "relationship", "context",
                    "foundational", "active_project"}
    project_tags = {t for t, c in tag_counts.items()
                    if c >= 3 and t not in generic_tags}

    projects: dict[str, list[str]] = {}
    for tag in sorted(project_tags):
        ids = [e["id"] for e in engrams
               if e["state"] == "active" and tag in e["all_tags"]]
        if ids:
            projects[tag] = ids

    return projects


def _compute_sessions(engrams: list[dict], state: dict) -> dict[str, dict]:
    """Map sessions to their extracted memories."""
    # Build session → engram_ids mapping from encoding context
    session_engrams: dict[str, list[str]] = {}
    for e in engrams:
        sid = e.get("session_id", "")
        if sid:
            session_engrams.setdefault(sid, []).append(e["id"])

    # Merge with indexing state
    sessions = {}
    for session_key, info in state.get("indexed_sessions", {}).items():
        sessions[session_key] = {
            **info,
            "engram_ids": session_engrams.get(session_key, []),
        }

    # Add any sessions from engrams not in state
    for sid, eids in session_engrams.items():
        if sid not in sessions:
            sessions[sid] = {
                "memories_encoded": len(eids),
                "engram_ids": eids,
            }

    return sessions


def _compute_stats(engrams: list, connections: list, beliefs: list, state: dict) -> dict:
    active = [e for e in engrams if e["state"] == "active"]
    dormant = [e for e in engrams if e["state"] == "dormant"]

    kind_counts = Counter(e["kind"] for e in active)
    source_counts = Counter(e["source_type"] for e in active)
    conn_type_counts = Counter(c["relation"] for c in connections)

    # Tag counts (excluding internal)
    tag_counts: Counter = Counter()
    for e in active:
        for t in e["tags"]:
            tag_counts[t] += 1

    # Accessibility distribution
    acc_buckets = {"high (>0.7)": 0, "medium (0.3-0.7)": 0, "low (<0.3)": 0}
    for e in active:
        a = e["accessibility"]
        if a > 0.7: acc_buckets["high (>0.7)"] += 1
        elif a >= 0.3: acc_buckets["medium (0.3-0.7)"] += 1
        else: acc_buckets["low (<0.3)"] += 1

    # Strength distribution
    str_buckets = {"strong (>0.7)": 0, "moderate (0.4-0.7)": 0, "weak (<0.4)": 0}
    for e in active:
        s = e["strength"]
        if s > 0.7: str_buckets["strong (>0.7)"] += 1
        elif s >= 0.4: str_buckets["moderate (0.4-0.7)"] += 1
        else: str_buckets["weak (<0.4)"] += 1

    # Avg connections
    conn_per: Counter = Counter()
    for c in connections:
        conn_per[c["source"]] += 1
        conn_per[c["target"]] += 1
    avg_conn = round(sum(conn_per.values()) / max(len(conn_per), 1), 1)

    indexed = state.get("indexed_sessions", {})

    return {
        "total_active": len(active),
        "total_dormant": len(dormant),
        "total_connections": len(connections),
        "total_beliefs": len(beliefs),
        "avg_connections": avg_conn,
        "sessions_indexed": len(indexed),
        "total_encoded": state.get("total_memories_encoded", 0),
        "kind_counts": dict(kind_counts),
        "source_counts": dict(source_counts),
        "conn_type_counts": dict(conn_type_counts),
        "tag_counts": dict(tag_counts.most_common(15)),
        "acc_buckets": acc_buckets,
        "str_buckets": str_buckets,
    }


def _parse_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
