"""Tests for turnkey context packets and visual snapshots."""

from mnemos.interface.context_packet import build_context_packet
from mnemos.interface.visual_snapshot import build_memory_visual_snapshot


def test_context_packet_orders_memory_layers(store):
    session = store.start_memory_session(
        session_id="ctx-session",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        title="Context packet test",
    )
    store.write_functional_memory(
        "Current task is building the turnkey single-agent memory system.",
        session_id=session["id"],
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="working",
        confidence=0.9,
        salience=0.9,
    )
    store.write_hypomnema_entry(
        "Hypomnema carries scoped continuity before promotion into Mnemos.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.88,
        salience=0.75,
        foundational=True,
    )

    packet = build_context_packet(
        store,
        "turnkey memory system",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        session_id="ctx-session",
    )

    prompt = packet["prompt"]
    assert "### Functional Memory" in prompt
    assert "### Hypomnema" in prompt
    assert "### Mnemos Graph" in prompt
    assert "turnkey single-agent memory system" in prompt
    assert "scoped continuity" in prompt


def test_operational_context_packet_quarantines_review_prose(store):
    functional = store.write_functional_memory(
        "Pending functional prose must not enter the operational packet.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Pending hypomnema prose must not enter the operational packet.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
    )

    packet = build_context_packet(
        store,
        "operational boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )

    prompt = packet["prompt"]
    review_queue = packet["review_queue"]
    serialized_review = str(review_queue)

    assert packet["packet_mode"] == "operational"
    assert "Pending functional prose" not in prompt
    assert "Pending hypomnema prose" not in prompt
    assert "Pending functional prose" not in serialized_review
    assert "Pending hypomnema prose" not in serialized_review
    assert functional["id"] in prompt
    assert hypomnema_id in prompt
    assert review_queue["functional_needs_confirmation_count"] == 1
    assert review_queue["hypomnema_promotion_candidate_count"] == 1


def test_review_context_packet_exposes_review_prose_with_labels(store):
    functional = store.write_functional_memory(
        "Review functional prose should be visible only in review mode.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Review hypomnema prose should be visible only in review mode.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        source="synthesized",
        domain="foundational",
        confidence=0.95,
        salience=0.9,
        foundational=True,
    )

    packet = build_context_packet(
        store,
        "review boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
    )

    prompt = packet["prompt"]

    assert packet["packet_mode"] == "review"
    assert "Review functional prose should be visible" in prompt
    assert "Review hypomnema prose should be visible" in prompt
    assert "review-only" in prompt
    assert functional["id"] in prompt
    assert hypomnema_id in prompt


def test_context_packet_rejects_invalid_packet_mode(store):
    try:
        build_context_packet(store, "bad mode", packet_mode="operator")
    except ValueError as exc:
        assert "packet_mode" in str(exc)
    else:
        raise AssertionError("invalid packet_mode should fail closed")


def test_visual_snapshot_returns_mermaid(store):
    store.write_functional_memory(
        "A visible memory map should be renderable inline.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    snapshot = build_memory_visual_snapshot(
        store,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "```mermaid" in snapshot
    assert "Functional memory" in snapshot
    assert "Hypomnema" in snapshot
