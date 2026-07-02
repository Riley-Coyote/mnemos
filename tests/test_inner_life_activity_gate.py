from datetime import datetime, timedelta, timezone

from mnemos.inner_life.activity_gate import evaluate_activity_gate
from mnemos.store.sqlite_store import EngramStore


def _now():
    return datetime.now(timezone.utc)


def _assert_no_memory_writes(store: EngramStore) -> None:
    assert store.count_engrams(agent_id="oliver") == 0
    assert store.get_beliefs(agent_id="oliver") == []
    assert store.search_hypomnema(
        "activity gate",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    ) == []


def _write_turn_signal(store: EngramStore) -> None:
    store.upsert_inner_life_event(
        idempotency_key="turn:session-1:turn-1",
        event_type="turn_finalized",
        process_name="turn-finalizer",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        session_id="session-1",
        turn_id="turn-1",
        role="exchange",
        content_hash="hash",
        content_excerpt="USER: prove it\nASSISTANT: verified",
        event_tags=["u6.6", "turn-event"],
        rollout_tag="u6.6-test",
        gate_decision="ledger_only",
        metadata={"writes_memory": False},
    )


def test_activity_gate_allows_recent_turn_signal_and_records_run_without_memory(tmp_path):
    store = EngramStore(tmp_path / "activity-run.db")
    try:
        _write_turn_signal(store)

        decision = evaluate_activity_gate(
            store,
            process_name="reflect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now() + timedelta(seconds=1),
            rollout_tag="u6.6-test",
        )

        assert decision["allowed"] is True
        assert decision["reason"] == "activity_detected"
        assert decision["gate_decision"] == "run"
        assert decision["signal_count"] == 1
        assert decision["writes_memory"] is False
        assert decision["generated_memory_writes"] == 0

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "activity-gate"
        assert rows[0]["gate_decision"] == "run"
        assert rows[0]["metadata"]["target_process"] == "reflect"
        assert rows[0]["metadata"]["generated_memory_writes"] == 0
        _assert_no_memory_writes(store)
    finally:
        store.close()


def test_activity_gate_skips_without_recent_signal_and_records_reason(tmp_path):
    store = EngramStore(tmp_path / "activity-skip.db")
    try:
        decision = evaluate_activity_gate(
            store,
            process_name="wander",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=_now(),
            rollout_tag="u6.6-test",
        )

        assert decision["allowed"] is False
        assert decision["reason"] == "no_recent_activity"
        assert decision["gate_decision"] == "skip:no_recent_activity"
        assert decision["signal_count"] == 0

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "activity-gate"
        assert rows[0]["metadata"]["reason"] == "no_recent_activity"
        assert rows[0]["metadata"]["writes_memory"] is False
        _assert_no_memory_writes(store)
    finally:
        store.close()


def test_activity_gate_enforces_process_cooldown(tmp_path):
    store = EngramStore(tmp_path / "activity-cooldown.db")
    try:
        _write_turn_signal(store)
        first_now = _now() + timedelta(seconds=1)
        first = evaluate_activity_gate(
            store,
            process_name="reflect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=first_now,
            config={"activity_gate": {"processes": {"reflect": {"cooldown_minutes": 60}}}},
            rollout_tag="u6.6-test",
        )
        second = evaluate_activity_gate(
            store,
            process_name="reflect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=first_now + timedelta(minutes=10),
            config={"activity_gate": {"processes": {"reflect": {"cooldown_minutes": 60}}}},
            rollout_tag="u6.6-test",
        )

        assert first["allowed"] is True
        assert second["allowed"] is False
        assert second["reason"] == "cooldown"
        assert second["cooldown_until"] is not None
        _assert_no_memory_writes(store)
    finally:
        store.close()


def test_activity_gate_uses_consolidation_log_as_existing_mnemos_signal(tmp_path):
    store = EngramStore(tmp_path / "activity-consolidation.db")
    try:
        completed_at = _now()
        store.log_consolidation(
            log_id="cycle-1",
            pass_name="cycle",
            started_at=(completed_at - timedelta(minutes=1)).isoformat(),
            completed_at=completed_at.isoformat(),
            stats={"passes_run": ["connection_discovery", "decay"]},
        )

        decision = evaluate_activity_gate(
            store,
            process_name="affect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            now=completed_at + timedelta(seconds=1),
            rollout_tag="u6.6-test",
        )

        assert decision["allowed"] is True
        assert decision["source_ids"] == ["consolidation:cycle-1"]
        assert decision["metadata"]["signal_types"] == ["consolidation:cycle"]
        _assert_no_memory_writes(store)
    finally:
        store.close()
