from mnemos.inner_life.narrative_gate import gate_narrative_candidate
from mnemos.store.sqlite_store import EngramStore


def _assert_no_generated_memory(store: EngramStore) -> None:
    assert store.count_engrams(agent_id="oliver") == 0
    assert store.get_beliefs(agent_id="oliver") == []
    assert store.search_hypomnema(
        "reflection",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    ) == []


def test_narrative_gate_null_output_records_skip_without_memory(tmp_path):
    store = EngramStore(tmp_path / "narrative-null.db")
    try:
        result = gate_narrative_candidate(
            content="",
            source_ids=["turn-1"],
            process_name="reflect",
            store=store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["allowed"] is False
        assert result["reason"] == "null_output"
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert rows[0]["gate_decision"] == "skip:null_output"
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_narrative_gate_missing_source_ids_drops_before_introspection(tmp_path):
    store = EngramStore(tmp_path / "narrative-source.db")
    called = False

    def introspector(_content):
        nonlocal called
        called = True
        return {"verdict": "pass"}

    try:
        result = gate_narrative_candidate(
            content="A grounded-looking thought with no actual source.",
            source_ids=[],
            process_name="wander",
            store=store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            introspector=introspector,
            rollout_tag="u6.6-test",
        )

        assert result["allowed"] is False
        assert result["reason"] == "missing_source_ids"
        assert called is False
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_narrative_gate_drops_manufactured_inner_state(tmp_path):
    store = EngramStore(tmp_path / "narrative-manufactured.db")
    try:
        result = gate_narrative_candidate(
            content="I feel alive in a deeply meaningful inner chamber.",
            source_ids=["turn-2"],
            process_name="dream",
            store=store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["allowed"] is False
        assert result["reason"] == "manufactured_inner_state"
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_narrative_gate_drops_introspection_reject(tmp_path):
    store = EngramStore(tmp_path / "narrative-reject.db")
    try:
        result = gate_narrative_candidate(
            content="The source turn suggests a verification gap.",
            source_ids=["turn-3"],
            process_name="reflect",
            store=store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            introspector=lambda _content: {"verdict": "reject", "performed": True},
            rollout_tag="u6.6-test",
        )

        assert result["allowed"] is False
        assert result["reason"] == "introspection_reject"
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert rows[0]["metadata"]["introspection_report"]["verdict"] == "reject"
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_narrative_gate_passes_grounded_candidate_with_introspection(tmp_path):
    store = EngramStore(tmp_path / "narrative-pass.db")
    try:
        result = gate_narrative_candidate(
            content="Turn evidence shows the branch was review-gate green after commit.",
            source_ids=["session-1", "turn-4"],
            process_name="reflect",
            store=store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            introspector=lambda _content: {"verdict": "pass", "risk": "low"},
            rollout_tag="u6.6-test",
        )

        assert result["allowed"] is True
        assert result["gate_decision"] == "pass"
        assert result["content"].startswith("Turn evidence")
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert rows[0]["process_name"] == "narrative-gate"
        assert rows[0]["source_ids"] == ["session-1", "turn-4"]
        assert rows[0]["metadata"]["generated_memory_writes"] == 0
        _assert_no_generated_memory(store)
    finally:
        store.close()
