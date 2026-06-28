import pytest

from mnemos.inner_life.hypomnema_challenge import apply_hypomnema_challenge
from mnemos.store.sqlite_store import EngramStore


def _write_entry(store: EngramStore, *, confidence: float = 0.8) -> str:
    return store.write_hypomnema_entry(
        "David prefers proof-bearing implementation over narrative polish.",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        source="observed",
        domain="situational",
        tags=["u6.6-test", "continuity"],
        confidence=confidence,
        salience=0.7,
    )


def _assert_no_generated_memory(store: EngramStore) -> None:
    assert store.count_engrams(agent_id="oliver") == 0
    assert store.get_beliefs(agent_id="oliver") == []


def test_hypomnema_challenge_revise_down_lowers_confidence_and_preserves_history(tmp_path):
    store = EngramStore(tmp_path / "challenge-revise.db")
    try:
        entry_id = _write_entry(store, confidence=0.8)

        result = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge={
                "decision": "revise_down",
                "rationale": "The entry overstates the preference scope.",
                "confidence_delta": -0.2,
                "source_ids": ["turn-1"],
            },
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            reviewer_id="critic-a",
            rollout_tag="u6.6-test",
        )

        assert result["decision"] == "revise_down"
        assert result["hypomnema_writes"] == 1
        revised = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert revised["active"] is True
        assert revised["confidence"] == pytest.approx(0.6)
        assert revised["revision_count"] == 1
        assert "u6.6 challenge revise_down" in revised["revisions"][0]["reason"]

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "hypomnema-challenge"
        assert rows[0]["gate_decision"] == "revise_down"
        assert rows[0]["source_ids"] == [entry_id, "turn-1"]
        assert rows[0]["metadata"]["confidence_before"] == pytest.approx(0.8)
        assert rows[0]["metadata"]["confidence_after"] == pytest.approx(0.6)
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_hypomnema_challenge_retire_archives_without_deleting(tmp_path):
    store = EngramStore(tmp_path / "challenge-retire.db")
    try:
        entry_id = _write_entry(store)

        result = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge={
                "decision": "retire",
                "rationale": "Superseded by later explicit correction.",
                "source_ids": ["session-1", "turn-2"],
            },
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            reviewer_id="critic-a",
            rollout_tag="u6.6-test",
        )

        assert result["decision"] == "retire"
        assert store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            active_only=True,
        ) is None
        archived = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert archived["active"] is False
        assert archived["revision_count"] == 1
        assert "archived: u6.6 challenge retire" in archived["revisions"][0]["reason"]
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_hypomnema_challenge_hold_writes_telemetry_without_revision(tmp_path):
    store = EngramStore(tmp_path / "challenge-hold.db")
    try:
        entry_id = _write_entry(store)

        result = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge={
                "decision": "hold",
                "rationale": "Still grounded by recent turn evidence.",
                "source_ids": ["turn-3"],
            },
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        unchanged = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert result["decision"] == "hold"
        assert result["hypomnema_writes"] == 0
        assert unchanged["active"] is True
        assert unchanged["revision_count"] == 0
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert rows[0]["gate_decision"] == "hold"
        assert rows[0]["metadata"]["hypomnema_writes"] == 0
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_hypomnema_challenge_malformed_output_records_error_without_change(tmp_path):
    store = EngramStore(tmp_path / "challenge-error.db")
    try:
        entry_id = _write_entry(store, confidence=0.75)

        result = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge={"decision": "amplify", "rationale": ""},
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["decision"] == "error"
        assert result["reason"] == "malformed_critic_output"
        unchanged = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert unchanged["active"] is True
        assert unchanged["confidence"] == pytest.approx(0.75)
        assert unchanged["revision_count"] == 0
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="error",
        )
        assert len(rows) == 1
        assert rows[0]["gate_decision"] == "error:malformed_critic_output"
        assert rows[0]["metadata"]["error"] == "malformed_critic_output"
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_hypomnema_challenge_duplicate_key_does_not_reapply_revision(tmp_path):
    store = EngramStore(tmp_path / "challenge-duplicate.db")
    try:
        entry_id = _write_entry(store, confidence=0.8)
        challenge = {
            "decision": "revise_down",
            "rationale": "Scope should be narrower.",
            "source_ids": ["turn-4"],
        }

        first = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge=challenge,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            idempotency_key="challenge-once",
        )
        second = apply_hypomnema_challenge(
            store,
            entry_id=entry_id,
            challenge=challenge,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            idempotency_key="challenge-once",
        )

        entry = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert first["hypomnema_writes"] == 1
        assert second["duplicates"] == 1
        assert entry["revision_count"] == 1
    finally:
        store.close()
