"""Tests for turnkey context packets and visual snapshots."""

from mnemos.core.engram import Engram
from mnemos.core.belief import Belief
from mnemos.interface.context_packet import build_context_packet, format_context_packet
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


def test_review_context_packet_can_include_review_only_rows(store):
    functional = store.write_functional_memory(
        "Explicit review functional prose may enter a review packet.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
        read_visibility="review_only",
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Explicit review hypomnema prose may enter a review packet.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        read_visibility="review_only",
    )

    operational = build_context_packet(
        store,
        "explicit review prose",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )
    review = build_context_packet(
        store,
        "explicit review prose",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
    )

    assert "Explicit review functional prose" not in str(operational)
    assert "Explicit review hypomnema prose" not in str(operational)
    assert functional["id"] in operational["prompt"]
    assert hypomnema_id in operational["prompt"]

    review_prompt = review["prompt"]
    assert "Explicit review functional prose" in review_prompt
    assert "Explicit review hypomnema prose" in review_prompt
    assert review["review_queue"]["functional_needs_confirmation"][0]["read_visibility"] == "review_only"
    assert review["review_queue"]["hypomnema_promotion_candidates"][0]["read_visibility"] == "review_only"


def test_review_context_packet_includes_low_salience_identity_review_rows(store):
    identity_id = store.write_hypomnema_entry(
        "Low-salience identity continuity still needs explicit review.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        domain="identity",
        confidence=0.45,
        salience=0.35,
        foundational=False,
    )

    operational = build_context_packet(
        store,
        "low-salience identity",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )
    review = build_context_packet(
        store,
        "low-salience identity",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
    )

    assert "Low-salience identity continuity" not in str(operational)
    assert identity_id in operational["prompt"]
    assert f"source_id={identity_id}" in operational["prompt"]
    assert operational["review_queue"]["hypomnema_promotion_candidate_count"] == 1
    assert "Low-salience identity continuity" in review["prompt"]
    assert review["review_queue"]["hypomnema_promotion_candidates"][0]["id"] == identity_id


def test_review_context_packet_excludes_audit_only_hypomnema_candidates(store):
    store.write_hypomnema_entry(
        "Review packet may disclose review-only hypomnema candidate.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        read_visibility="review_only",
    )
    store.write_hypomnema_entry(
        "Review packet must not disclose audit-only hypomnema candidate.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.99,
        salience=0.95,
        foundational=True,
        read_visibility="audit_only",
    )

    packet = build_context_packet(
        store,
        "hypomnema candidate",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
    )

    assert "Review packet may disclose review-only hypomnema candidate." in packet["prompt"]
    assert "Review packet must not disclose audit-only" not in packet["prompt"]
    assert packet["review_queue"]["hypomnema_promotion_candidate_count"] == 1


def test_review_context_packet_excludes_audit_only_functional_confirmations(store):
    review = store.write_functional_memory(
        "Review packet may disclose review-only functional confirmation.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        read_visibility="review_only",
    )
    audit = store.write_functional_memory(
        "Review packet must not disclose audit-only functional confirmation.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        read_visibility="audit_only",
    )

    packet = build_context_packet(
        store,
        "functional confirmation",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
    )

    review_ids = {
        item["id"] for item in packet["review_queue"]["functional_needs_confirmation"]
    }

    assert (
        "Review packet may disclose review-only functional confirmation."
        in packet["prompt"]
    )
    assert "Review packet must not disclose audit-only" not in packet["prompt"]
    assert review_ids == {review["id"]}
    assert audit["id"] not in str(packet)
    assert packet["review_queue"]["functional_needs_confirmation_count"] == 1


def test_proposal_rows_are_counts_only_operational_and_labeled_in_review(store):
    review = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        domain="identity",
        target_surface="beliefs",
        transition="semantic_to_identity",
        blast_radius="identity",
        read_visibility="review_only",
        reason="Needs David review.",
        provenance_ids=["source-hypomnema"],
        payload={"content": "Review proposal prose must stay out of operational packets."},
    )

    operational = build_context_packet(
        store,
        "proposal prose",
        agent_id="default",
        person_id="user",
        project_scope="global",
        packet_mode="operational",
    )
    review_packet = build_context_packet(
        store,
        "proposal prose",
        agent_id="default",
        person_id="user",
        project_scope="global",
        packet_mode="review",
    )

    assert "Review proposal prose" not in str(operational)
    assert "semantic_to_identity" not in str(operational)
    assert review["id"] in operational["prompt"]
    assert f"source_id={review['id']}" in operational["prompt"]
    assert operational["review_queue"]["proposal_candidate_count"] == 1
    assert operational["review_queue"]["proposal_candidates"] == [
        {
            "id": review["id"],
            "source_id": review["id"],
            "read_visibility": "review_only",
        }
    ]

    assert "Review proposal prose must stay out" in review_packet["prompt"]
    proposal = review_packet["review_queue"]["proposal_candidates"][0]
    assert proposal["id"] == review["id"]
    assert proposal["source_authority"] == "generated"
    assert proposal["kind"] == "semantic"
    assert proposal["blast_radius"] == "identity"
    assert proposal["provenance_ids"] == ["source-hypomnema"]


def test_operational_proposal_count_uses_total_not_limited_reference_sample(store):
    for index in range(8):
        store.write_proposal(
            source_authority="generated",
            kind="semantic",
            domain="identity",
            target_surface="beliefs",
            transition=f"proposal_{index}",
            blast_radius="identity",
            read_visibility="review_only",
            payload={"content": f"Review proposal prose {index} must stay withheld."},
        )

    operational = build_context_packet(
        store,
        "proposal prose",
        packet_mode="operational",
    )

    assert operational["review_queue"]["proposal_candidate_count"] == 8
    assert len(operational["review_queue"]["proposal_candidates"]) == 6
    assert "Review proposal prose" not in operational["prompt"]
    assert operational["prompt"].count("source_id=") >= 6


def test_active_review_packets_exclude_terminal_proposals(store):
    pending = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="pending_candidate",
        read_visibility="review_only",
        payload={"content": "Pending proposal prose may show in review packet."},
    )
    deferred = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="deferred_candidate",
        read_visibility="review_only",
        status="deferred",
        payload={"content": "Deferred proposal prose must not be active."},
    )
    rejected = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="rejected_candidate",
        read_visibility="review_only",
        status="rejected",
        payload={"content": "Rejected proposal prose must not be active."},
    )

    operational = build_context_packet(store, "proposal", packet_mode="operational")
    review_packet = build_context_packet(store, "proposal", packet_mode="review")
    snapshot = build_memory_visual_snapshot(store)

    assert operational["review_queue"]["proposal_candidate_count"] == 1
    assert review_packet["review_queue"]["proposal_candidate_count"] == 1
    assert review_packet["review_queue"]["proposal_candidates"][0]["id"] == pending["id"]
    assert pending["id"] in snapshot
    for terminal in (deferred, rejected):
        assert terminal["id"] not in str(operational)
        assert terminal["id"] not in str(review_packet)
        assert terminal["id"] not in snapshot
    assert "Deferred proposal prose" not in str(review_packet)
    assert "Rejected proposal prose" not in str(review_packet)
    assert "Deferred proposal prose" not in snapshot
    assert "Rejected proposal prose" not in snapshot


def test_audit_only_proposal_and_hypomnema_are_absent_from_review_packet(store):
    audit_proposal = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="audit_only_candidate",
        payload={"content": "Audit-only proposal prose must be absent."},
    )
    audit_hypomnema = store.write_hypomnema_entry(
        "Audit-only hypomnema prose must be absent from review packet.",
        confidence=0.99,
        salience=0.95,
        foundational=True,
        read_visibility="audit_only",
    )

    packet = build_context_packet(store, "audit-only", packet_mode="review")

    assert audit_proposal["id"] not in str(packet)
    assert "Audit-only proposal prose" not in str(packet)
    assert audit_hypomnema not in str(packet)
    assert "Audit-only hypomnema prose" not in str(packet)


def test_operational_stats_exclude_audit_only_memory_counts(store):
    store.write_functional_memory(
        "Audit-only functional memory must not affect operational counts.",
        read_visibility="audit_only",
    )
    store.write_hypomnema_entry(
        "Audit-only hypomnema must not affect operational counts.",
        read_visibility="audit_only",
    )
    store.save_engram(
        Engram(
            content="Audit-only engram must not affect operational counts.",
            read_visibility="audit_only",
        )
    )

    packet = build_context_packet(
        store,
        "audit-only counts",
        packet_mode="operational",
    )
    snapshot = build_memory_visual_snapshot(store)

    assert packet["stats"]["engrams_active"] == 0
    assert packet["stats"]["functional_active"] == 0
    assert packet["stats"]["hypomnema_active"] == 0
    assert 'Functional memory<br/>0 active' in snapshot
    assert 'Hypomnema<br/>0 scoped entries' in snapshot
    assert 'Mnemos graph<br/>0 engrams' in snapshot


def test_context_packet_stats_are_visibility_scoped(store):
    for visibility in ("operational_context", "review_only", "audit_only"):
        store.write_functional_memory(
            f"{visibility} functional stats row",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            read_visibility=visibility,
        )
        store.write_hypomnema_entry(
            f"{visibility} hypomnema stats row",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            confidence=0.2,
            salience=0.2,
            read_visibility=visibility,
        )
        store.save_engram(
            Engram(
                content=f"{visibility} engram stats row",
                owner_agent_id="vektor",
                read_visibility=visibility,
            )
        )
        store.save_belief(
            Belief(
                content=f"{visibility} belief stats row",
                agent_id="vektor",
                read_visibility=visibility,
            )
        )

    store.write_functional_memory(
        "Other scoped operational functional stats row",
        agent_id="vektor",
        person_id="other",
        project_scope="elsewhere",
        read_visibility="operational_context",
    )
    store.write_hypomnema_entry(
        "Other scoped operational hypomnema stats row",
        agent_id="vektor",
        person_id="other",
        project_scope="elsewhere",
        confidence=0.2,
        salience=0.2,
        read_visibility="operational_context",
    )

    operational = build_context_packet(
        store,
        "stats boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
        include_prompt=False,
    )
    review = build_context_packet(
        store,
        "stats boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
        include_prompt=False,
    )

    assert operational["stats"]["functional_active"] == 1
    assert operational["stats"]["hypomnema_active"] == 1
    assert operational["stats"]["engrams_active"] == 1
    assert operational["stats"]["beliefs_active"] == 1
    assert review["stats"]["functional_active"] == 2
    assert review["stats"]["hypomnema_active"] == 2
    assert review["stats"]["engrams_active"] == 2
    assert review["stats"]["beliefs_active"] == 2


def test_operational_context_packet_redacts_candidates_outside_review_queue(store):
    for index in range(6):
        store.write_hypomnema_entry(
            f"High priority review candidate {index}",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            confidence=0.95,
            salience=0.9,
            foundational=True,
        )
    hidden_id = store.write_hypomnema_entry(
        "Seventh review candidate carries the unique throttle boundary.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.83,
        salience=0.66,
        foundational=True,
    )

    packet = build_context_packet(
        store,
        "unique throttle boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )

    serialized_packet = str(packet)
    hypomnema_ids = {entry["id"] for entry in packet["hypomnema"]}

    assert hidden_id not in hypomnema_ids
    assert "Seventh review candidate" not in serialized_packet
    assert packet["review_queue"]["hypomnema_promotion_candidate_count"] == 7


def test_operational_context_packet_omits_functional_review_source(store):
    store.write_functional_memory(
        "Pending functional body is review-only.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        source="free-form source carries leaked pending prose",
        confidence=0.9,
        salience=0.9,
    )

    packet = build_context_packet(
        store,
        "review source boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )

    reference = packet["review_queue"]["functional_needs_confirmation"][0]

    assert "source" not in reference
    assert "free-form source carries leaked pending prose" not in str(packet)


def test_operational_context_packet_omits_hypomnema_related_ids(store):
    related_engram_id = "related-engram-id-carries-review-prose"
    store.save_engram(Engram(id=related_engram_id, content="Existing related engram."))
    store.write_hypomnema_entry(
        "Pending hypomnema body is review-only.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        related_session_id="related session carries leaked pending prose",
        related_engram_id=related_engram_id,
    )

    packet = build_context_packet(
        store,
        "hypomnema boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
    )

    reference = packet["review_queue"]["hypomnema_promotion_candidates"][0]
    serialized_packet = str(packet)

    assert "related_session_id" not in reference
    assert "related_engram_id" not in reference
    assert "graduated_to_engram_id" not in reference
    assert "related session carries leaked pending prose" not in serialized_packet
    assert related_engram_id not in serialized_packet


def test_operational_functional_filter_runs_before_limit(store):
    for index in range(5):
        store.write_functional_memory(
            f"Dominant pending functional hit {index} critical anchor.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            memory_type="open_question",
            needs_confirmation=True,
            pinned=True,
            confidence=1.0,
            salience=1.0,
        )
    active = store.write_functional_memory(
        "Active operational functional memory critical anchor.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.35,
        salience=0.35,
    )

    packet = build_context_packet(
        store,
        "critical anchor",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
        max_functional=1,
    )

    assert [item["id"] for item in packet["functional_memory"]] == [active["id"]]
    assert "Active operational functional memory" in packet["prompt"]
    assert "Dominant pending functional hit" not in str(packet)


def test_formatter_operational_override_redacts_existing_review_packet(store):
    functional = store.write_functional_memory(
        "Formatter functional prose must not survive operational rendering.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Formatter hypomnema prose must not survive operational rendering.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
    )
    packet = build_context_packet(
        store,
        "formatter boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="review",
        include_prompt=False,
    )

    prompt = format_context_packet(packet, packet_mode="operational")

    assert "Formatter functional prose" not in prompt
    assert "Formatter hypomnema prose" not in prompt
    assert functional["id"] in prompt
    assert hypomnema_id in prompt


def test_formatter_review_override_rejects_redacted_operational_packet(store):
    store.write_functional_memory(
        "Redacted functional prose cannot be recovered by formatter override.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
    )
    packet = build_context_packet(
        store,
        "redacted formatter escalation",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
        include_prompt=False,
    )

    try:
        format_context_packet(packet, packet_mode="review")
    except ValueError as exc:
        assert "redacted operational references" in str(exc)
    else:
        raise AssertionError("redacted operational packet should not escalate to review")


def test_formatter_review_override_rejects_redacted_operational_proposal_packet(store):
    store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="redacted_proposal_escalation",
        read_visibility="review_only",
        payload={"content": "Redacted proposal prose cannot be recovered."},
    )
    packet = build_context_packet(
        store,
        "redacted proposal escalation",
        packet_mode="operational",
        include_prompt=False,
    )

    try:
        format_context_packet(packet, packet_mode="review")
    except ValueError as exc:
        assert "redacted operational proposal references" in str(exc)
    else:
        raise AssertionError("redacted operational proposal should not escalate to review")


def test_operational_review_cues_survive_low_token_budget(store):
    functional = store.write_functional_memory(
        "Pending low-budget functional prose must stay withheld.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Pending low-budget hypomnema prose must stay withheld.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
    )
    store.write_functional_memory(
        "Operational filler " + ("x" * 5000),
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="working",
        needs_confirmation=False,
        confidence=0.8,
        salience=0.8,
    )

    packet = build_context_packet(
        store,
        "low-budget cue boundary",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        packet_mode="operational",
        token_budget=20,
    )

    prompt = packet["prompt"]

    assert "[context packet truncated to token budget]" in prompt
    assert "1 functional memory item(s) need confirmation" in prompt
    assert "1 hypomnema promotion candidate(s) need review" in prompt
    assert functional["id"] in prompt
    assert hypomnema_id in prompt
    assert "Pending low-budget functional prose" not in prompt
    assert "Pending low-budget hypomnema prose" not in prompt


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


def test_visual_snapshot_counts_are_operational_scoped(store):
    store.write_functional_memory(
        "Visual snapshot operational functional row.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        read_visibility="operational_context",
    )
    store.write_functional_memory(
        "Visual snapshot review-only functional row.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        read_visibility="review_only",
    )
    store.write_hypomnema_entry(
        "Visual snapshot operational hypomnema row.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.2,
        salience=0.2,
        read_visibility="operational_context",
    )
    store.write_hypomnema_entry(
        "Visual snapshot review-only hypomnema row.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.2,
        salience=0.2,
        read_visibility="review_only",
    )

    snapshot = build_memory_visual_snapshot(
        store,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Functional memory<br/>1 active" in snapshot
    assert "Hypomnema<br/>1 scoped entries" in snapshot


def test_visual_snapshot_redacts_review_queue_prose(store):
    functional = store.write_functional_memory(
        "Visual snapshot pending functional prose must be withheld.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        confidence=0.9,
        salience=0.9,
        read_visibility="operational_context",
    )
    hypomnema_id = store.write_hypomnema_entry(
        "Visual snapshot promotion candidate prose must be withheld.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        source="synthesized",
    )
    store._get_conn().execute(
        """
        UPDATE hypomnema_entries
        SET domain = 'foundational',
            confidence = 0.95,
            salience = 0.9,
            foundational = 1,
            read_visibility = 'operational_context'
        WHERE id = ?
        """,
        (hypomnema_id,),
    )
    store._get_conn().commit()

    snapshot = build_memory_visual_snapshot(
        store,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Visual snapshot pending functional prose" not in snapshot
    assert "Visual snapshot promotion candidate prose" not in snapshot
    assert functional["id"] in snapshot
    assert hypomnema_id in snapshot
    assert (
        "functional memory item(s) need confirmation (review-only; prose withheld)"
        in snapshot
    )
    assert (
        "hypomnema promotion candidate(s) need review (review-only; prose withheld)"
        in snapshot
    )


def test_visual_snapshot_excludes_audit_only_functional_review_ids(store):
    audit = store.write_functional_memory(
        "Visual snapshot audit-only functional prose must be absent.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        read_visibility="audit_only",
    )

    snapshot = build_memory_visual_snapshot(
        store,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Visual snapshot audit-only functional prose" not in snapshot
    assert audit["id"] not in snapshot


def test_visual_snapshot_redacts_proposal_review_prose(store):
    proposal = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="visual_snapshot_candidate",
        read_visibility="review_only",
        payload={"content": "Visual snapshot proposal prose must be withheld."},
    )
    audit = store.write_proposal(
        source_authority="generated",
        kind="semantic",
        target_surface="beliefs",
        transition="visual_snapshot_audit",
        payload={"content": "Visual snapshot audit proposal must be absent."},
    )

    snapshot = build_memory_visual_snapshot(store)

    assert "Visual snapshot proposal prose" not in snapshot
    assert "Visual snapshot audit proposal" not in snapshot
    assert "visual_snapshot_candidate" not in snapshot
    assert proposal["id"] in snapshot
    assert audit["id"] not in snapshot
    assert "proposal candidate(s) need review" in snapshot
