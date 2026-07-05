"""Tests for dashboard data extraction."""

import sqlite3

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.visualization.data import extract_all


LEGACY_TIMESTAMP = "2026-01-01T00:00:00+00:00"


LEGACY_DASHBOARD_SCHEMA = """
CREATE TABLE engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
    resolution REAL NOT NULL DEFAULT 1.0,
    kind TEXT NOT NULL DEFAULT 'episodic',
    tags TEXT NOT NULL DEFAULT '[]',
    schema_refs TEXT NOT NULL DEFAULT '[]',
    strength REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.1,
    accessibility REAL NOT NULL DEFAULT 0.5,
    encoding_context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '{}',
    lineage TEXT NOT NULL DEFAULT '{}',
    owner_agent_id TEXT NOT NULL DEFAULT 'default',
    visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE beliefs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.3,
    domain TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    last_revised TEXT NOT NULL,
    last_challenged TEXT NOT NULL,
    revision_history TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    supporting_engram_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta(key, value) VALUES ('schema_version', '3');
"""


def _create_legacy_dashboard_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_DASHBOARD_SCHEMA)
        conn.execute(
            """
            INSERT INTO engrams (
                id, content, content_at_encoding, impact, created_at, last_accessed
            ) VALUES (
                'legacy-dashboard-engram', 'legacy dashboard content',
                'legacy dashboard content', '', ?, ?
            )
            """,
            (LEGACY_TIMESTAMP, LEGACY_TIMESTAMP),
        )
        conn.execute(
            """
            INSERT INTO beliefs (
                id, agent_id, content, confidence, domain,
                created_at, last_revised, last_challenged
            ) VALUES (
                'legacy-dashboard-belief', 'default', 'legacy dashboard belief',
                0.8, 'general', ?, ?, ?
            )
            """,
            (LEGACY_TIMESTAMP, LEGACY_TIMESTAMP, LEGACY_TIMESTAMP),
        )
        conn.commit()
    finally:
        conn.close()


def test_dashboard_extracts_operational_visibility_by_default(store, tmp_db):
    operational_engram = Engram(
        id="dashboard-operational-engram",
        content="Visible dashboard engram",
        read_visibility="operational_context",
    )
    review_engram = Engram(
        id="dashboard-review-engram",
        content="Hidden review dashboard engram",
        read_visibility="review_only",
    )
    audit_engram = Engram(
        id="dashboard-audit-engram",
        content="Hidden audit dashboard engram",
        read_visibility="audit_only",
    )
    for engram in (operational_engram, review_engram, audit_engram):
        store.save_engram(engram)

    operational_belief = Belief(
        id="dashboard-operational-belief",
        content="Visible dashboard belief",
        confidence=0.7,
        read_visibility="operational_context",
    )
    review_belief = Belief(
        id="dashboard-review-belief",
        content="Hidden review dashboard belief",
        confidence=0.9,
        read_visibility="review_only",
        confidence_pending_review=True,
    )
    audit_belief = Belief(
        id="dashboard-audit-belief",
        content="Hidden audit dashboard belief",
        confidence=0.95,
        read_visibility="audit_only",
        confidence_pending_review=True,
    )
    for belief in (operational_belief, review_belief, audit_belief):
        store.save_belief(belief)

    data = extract_all(tmp_db)

    assert {row["id"] for row in data["engrams"]} == {operational_engram.id}
    assert {row["id"] for row in data["beliefs"]} == {operational_belief.id}
    assert data["stats"]["total_active"] == 1


def test_dashboard_audit_mode_extracts_non_operational_rows(store, tmp_db):
    for read_visibility in (
        "operational_context",
        "review_only",
        "audit_only",
    ):
        store.save_engram(
            Engram(
                id=f"dashboard-{read_visibility}-engram",
                content=f"{read_visibility} dashboard engram",
                read_visibility=read_visibility,
            )
        )
        store.save_belief(
            Belief(
                id=f"dashboard-{read_visibility}-belief",
                content=f"{read_visibility} dashboard belief",
                confidence=0.5,
                read_visibility=read_visibility,
                confidence_pending_review=read_visibility != "operational_context",
            )
        )

    data = extract_all(tmp_db, include_non_operational=True)

    assert {row["id"] for row in data["engrams"]} == {
        "dashboard-operational_context-engram",
        "dashboard-review_only-engram",
        "dashboard-audit_only-engram",
    }
    assert {row["id"] for row in data["beliefs"]} == {
        "dashboard-operational_context-belief",
        "dashboard-review_only-belief",
        "dashboard-audit_only-belief",
    }
    assert data["stats"]["total_active"] == 3


def test_dashboard_extracts_legacy_db_after_store_migration(tmp_path):
    db_path = tmp_path / "legacy-dashboard.db"
    _create_legacy_dashboard_db(db_path)

    data = extract_all(str(db_path))

    assert {row["id"] for row in data["engrams"]} == {"legacy-dashboard-engram"}
    assert {row["id"] for row in data["beliefs"]} == {"legacy-dashboard-belief"}
    assert data["stats"]["total_active"] == 1

    conn = sqlite3.connect(db_path)
    try:
        engram_columns = {row[1] for row in conn.execute("PRAGMA table_info(engrams)")}
        belief_columns = {row[1] for row in conn.execute("PRAGMA table_info(beliefs)")}
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "read_visibility" in engram_columns
    assert "read_visibility" in belief_columns
    assert "confidence_pending_review" in belief_columns
    assert schema_version == "10"
