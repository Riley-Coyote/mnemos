import pytest

from mnemos.bridge import MnemosBridge
from mnemos.core.belief import Belief, BeliefRevision
from mnemos.interface.belief_render import (
    belief_challenge_state,
    format_belief_challenge_line,
)
from mnemos.interface.context_packet import build_context_packet
from mnemos.interface.prompt_builder import PromptBuilder
from mnemos.interface.visual_snapshot import build_memory_visual_snapshot


def test_challenge_state_derivation_excludes_annulled_false_events():
    false_down = BeliefRevision(
        timestamp="2026-07-05T01:00:00+00:00",
        old_confidence=0.4,
        new_confidence=0.35,
        reason="Contradicted by new evidence: false event",
    )
    restore = BeliefRevision(
        timestamp="2026-07-06T01:00:00+00:00",
        old_confidence=0.35,
        new_confidence=0.4,
        reason="restore",
        _extra_fields={"annuls": ["2026-07-05T01:00:00+00:00"]},
    )
    belief = Belief(id="b", agent_id="oliver", content="Belief", confidence=0.4)
    belief.revision_history = [false_down, restore]

    assert format_belief_challenge_line(belief) == "challenge: never-challenged"


def test_challenge_state_derivation_emits_three_launch_states():
    pending = Belief(id="pending", agent_id="oliver", needs_review=True)
    confidence_pending = Belief(
        id="confidence-pending",
        agent_id="oliver",
        confidence_pending_review=True,
    )
    down = Belief(id="down", agent_id="oliver")
    down.revision_history = [
        BeliefRevision(
            timestamp="2026-07-05T04:05:06+00:00",
            old_confidence=0.7,
            new_confidence=0.6,
            reason="real decrease",
        )
    ]
    never = Belief(id="never", agent_id="oliver")

    assert belief_challenge_state(pending).line == "challenge: under-challenge"
    assert (
        belief_challenge_state(confidence_pending).line == "challenge: under-challenge"
    )
    assert belief_challenge_state(down).line == "challenge: revised-down (2026-07-05)"
    assert belief_challenge_state(never).line == "challenge: never-challenged"


def test_post_restore_down_event_renders_revised_down_again():
    false_down = BeliefRevision(
        timestamp="2026-07-05T01:00:00+00:00",
        old_confidence=0.4,
        new_confidence=0.35,
        reason="Contradicted by new evidence: false event",
    )
    restore = BeliefRevision(
        timestamp="2026-07-06T01:00:00+00:00",
        old_confidence=0.35,
        new_confidence=0.4,
        reason="restore",
        _extra_fields={"annuls": ["2026-07-05T01:00:00+00:00"]},
    )
    later_down = BeliefRevision(
        timestamp="2026-07-07T01:00:00+00:00",
        old_confidence=0.4,
        new_confidence=0.3,
        reason="review queue decrease",
    )
    belief = Belief(id="b", agent_id="oliver", content="Belief", confidence=0.3)
    belief.revision_history = [false_down, restore, later_down]

    assert (
        format_belief_challenge_line(belief) == "challenge: revised-down (2026-07-07)"
    )


def test_context_packet_renders_challenge_line_and_render_metadata(store):
    belief = Belief(
        id="context-belief",
        agent_id="oliver",
        content="David wants challenge state visible.",
        domain="memory",
        confidence=0.7,
    )
    store.save_belief(belief)

    packet = build_context_packet(store, "", agent_id="oliver")

    rendered = packet["beliefs"][0]
    assert rendered["challenge_line"] == "challenge: never-challenged"
    assert rendered["render_metadata"] == {
        "tier": "rendered",
        "fitting_eligible": False,
        "citation_role": "belief-render",
    }
    assert "challenge: never-challenged" in packet["prompt"]


def test_operational_context_packet_renders_operational_pending_beliefs(store):
    pending = Belief(
        id="operational-pending-belief",
        agent_id="oliver",
        content="Operational pending belief remains visible while challenged.",
        domain="memory",
        confidence=0.67,
    )
    pending.revision_history = [
        BeliefRevision(
            timestamp="2026-07-05T04:05:06+00:00",
            old_confidence=0.67,
            new_confidence=0.99,
            reason="pending proposed confidence",
        )
    ]
    review_only = Belief(
        id="review-only-pending-belief",
        agent_id="oliver",
        content="Review-only pending belief must stay out of operational packets.",
        domain="memory",
        confidence=0.99,
        needs_review=True,
        confidence_pending_review=True,
        read_visibility="review_only",
    )
    store.save_belief(pending)
    store.save_belief(review_only)
    conn = store._get_conn()
    conn.execute(
        """
        UPDATE beliefs
        SET needs_review = 1,
            confidence_pending_review = 1,
            read_visibility = 'operational_context'
        WHERE id = ?
        """,
        (pending.id,),
    )
    conn.commit()

    packet = build_context_packet(
        store,
        "",
        agent_id="oliver",
        packet_mode="operational",
    )
    prompt = packet["prompt"]

    assert "Operational pending belief remains visible while challenged." in prompt
    assert "challenge: under-challenge" in prompt
    assert "[memory, 67%]" in prompt
    assert "[memory, 99%]" not in prompt
    assert "Review-only pending belief must stay out" not in str(packet)
    assert packet["beliefs"][0]["needs_review"] is True
    assert packet["beliefs"][0]["confidence_pending_review"] is True


def test_prompt_builder_bridge_and_mcp_beliefs_render_challenge_line(
    store, monkeypatch
):
    mcp_server = pytest.importorskip("mnemos.mcp_server")

    belief = Belief(
        id="surface-belief",
        agent_id="oliver",
        content="David wants challenge state visible.",
        domain="memory",
        confidence=0.7,
    )
    store.save_belief(belief)

    prompt = PromptBuilder(store).build("", agent_id="oliver")
    assert "challenge: never-challenged" in prompt

    bridge = MnemosBridge(agent_id="oliver", db_path=str(store.db_path))
    assert "challenge: never-challenged" in bridge.beliefs()

    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    assert "challenge: never-challenged" in mcp_server.mnemos_beliefs(agent_id="oliver")


def test_visual_snapshot_renders_belief_challenge_line(store):
    belief = Belief(
        id="visual-belief",
        agent_id="oliver",
        content="David wants challenge state visible.",
        domain="memory",
        confidence=0.7,
    )
    store.save_belief(belief)

    snapshot = build_memory_visual_snapshot(store, agent_id="oliver")

    assert "### Identity Signals" in snapshot
    assert "challenge: never-challenged" in snapshot
