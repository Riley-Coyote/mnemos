"""Tests for dashboard data extraction."""

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.visualization.data import extract_all


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
