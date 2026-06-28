import hashlib
from datetime import datetime, timedelta, timezone

from mnemos.inner_life.emotional_driver import update_event_grounded_affect
from mnemos.store.sqlite_store import EngramStore


def _now():
    return datetime.now(timezone.utc)


def _write_event(store: EngramStore, *, event_type: str, excerpt: str, gate_decision="ledger_only"):
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16]
    store.upsert_inner_life_event(
        idempotency_key=f"{event_type}:{digest}",
        event_type=event_type,
        process_name="test-fixture",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        content_hash="hash",
        content_excerpt=excerpt,
        event_tags=["u6.6-test"],
        source_ids=["turn-1"],
        metadata={"writes_memory": False},
        rollout_tag="u6.6-test",
        gate_decision=gate_decision,
    )


def _assert_no_generated_memory(store: EngramStore) -> None:
    assert store.count_engrams(agent_id="oliver") == 0
    assert store.get_beliefs(agent_id="oliver") == []
    assert store.search_hypomnema(
        "affect",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    ) == []


def test_emotional_driver_updates_from_real_turn_and_verification_events(tmp_path):
    store = EngramStore(tmp_path / "affect-update.db")
    try:
        _write_event(store, event_type="turn_finalized", excerpt="completed exchange")
        _write_event(store, event_type="test_outcome", excerpt="pytest passed GREEN")

        result = update_event_grounded_affect(
            store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now() + timedelta(seconds=1),
            min_movement=0.01,
            rollout_tag="u6.6-test",
        )

        assert result["updated"] is True
        assert result["event_count"] == 2
        state = store.get_latest_emotional_state("oliver")
        assert state is not None
        assert state.warmth > 0.5
        assert state.isolation < 0.2
        assert state.clarity > 0.5

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert rows[-1]["process_name"] == "emotional-driver"
        assert rows[-1]["gate_decision"] == "affect_updated"
        assert rows[-1]["metadata"]["generated_memory_writes"] == 0
        assert rows[-1]["metadata"]["identity_patches"] == 0
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_emotional_driver_skips_without_recent_events(tmp_path):
    store = EngramStore(tmp_path / "affect-no-events.db")
    try:
        result = update_event_grounded_affect(
            store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now(),
            rollout_tag="u6.6-test",
        )

        assert result["updated"] is False
        assert result["reason"] == "no_recent_events"
        assert store.get_latest_emotional_state("oliver") is None
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert rows[0]["gate_decision"] == "skip:no_recent_events"
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_emotional_driver_skips_below_meaningful_movement_threshold(tmp_path):
    store = EngramStore(tmp_path / "affect-threshold.db")
    try:
        _write_event(store, event_type="turn_finalized", excerpt="completed exchange")

        result = update_event_grounded_affect(
            store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now() + timedelta(seconds=1),
            min_movement=1.0,
            rollout_tag="u6.6-test",
        )

        assert result["updated"] is False
        assert result["reason"] == "below_movement_threshold"
        assert store.get_latest_emotional_state("oliver") is None
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert rows[-1]["metadata"]["movement"] < 1.0
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_emotional_driver_error_event_increases_restlessness(tmp_path):
    store = EngramStore(tmp_path / "affect-error.db")
    try:
        _write_event(
            store,
            event_type="error",
            excerpt="reviewer failed",
            gate_decision="error:reviewer_failed",
        )

        result = update_event_grounded_affect(
            store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now() + timedelta(seconds=1),
            min_movement=0.01,
            rollout_tag="u6.6-test",
        )

        assert result["updated"] is True
        assert "retrieval_failed" in result["applied_events"]
        state = store.get_latest_emotional_state("oliver")
        assert state is not None
        assert state.restlessness > 0.3
        _assert_no_generated_memory(store)
    finally:
        store.close()
