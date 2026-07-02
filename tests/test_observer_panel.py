from mnemos.inner_life.observer_panel import run_observer_panel
from mnemos.store.sqlite_store import EngramStore


class GoodReviewer:
    reviewer_id = "good-reviewer"

    def __init__(self, findings=None):
        self.findings = findings or [
            {
                "finding": "The turn shows verification before claim.",
                "rationale": "It cites a concrete command result.",
                "confidence": 0.72,
                "tags": ["verification"],
            }
        ]
        self.called = False

    def observe(self, *, context, source_ids):
        self.called = True
        assert context
        assert source_ids
        return {"findings": self.findings}


class FailingReviewer:
    reviewer_id = "failed-reviewer"

    def observe(self, *, context, source_ids):
        raise RuntimeError("provider unavailable")


def _assert_no_generated_memory(store: EngramStore) -> None:
    assert store.count_engrams(agent_id="oliver") == 0
    assert store.get_beliefs(agent_id="oliver") == []
    assert (
        store.search_hypomnema(
            "observer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        == []
    )


def test_observer_panel_no_reviewers_records_clean_skip(tmp_path):
    store = EngramStore(tmp_path / "observer-none.db")
    try:
        result = run_observer_panel(
            store,
            reviewer_clients=[],
            context="bounded turn context",
            source_ids=["session-1", "turn-1"],
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["skipped"] == 1
        assert result["reason"] == "no_reviewers"
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
        )
        assert len(rows) == 1
        assert rows[0]["gate_decision"] == "skip:no_reviewers"
        assert rows[0]["metadata"]["generated_memory_writes"] == 0
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_observer_panel_missing_source_ids_skips_before_client_call(tmp_path):
    store = EngramStore(tmp_path / "observer-missing-source.db")
    reviewer = GoodReviewer()
    try:
        result = run_observer_panel(
            store,
            reviewer_clients=[reviewer],
            context="bounded turn context",
            source_ids=[],
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["skipped"] == 1
        assert result["reason"] == "missing_source_ids"
        assert reviewer.called is False
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_observer_panel_failed_reviewer_does_not_discard_successful_finding(tmp_path):
    store = EngramStore(tmp_path / "observer-partial.db")
    try:
        result = run_observer_panel(
            store,
            reviewer_clients=[FailingReviewer(), GoodReviewer()],
            context="bounded turn context",
            source_ids=["session-1", "turn-2"],
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["written"] == 1
        assert result["errors"] == 1
        events = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert [row["event_type"] for row in events] == ["error", "tool_event"]
        finding = events[1]
        assert finding["process_name"] == "observer-panel"
        assert finding["gate_decision"] == "observer_signal"
        assert finding["source_ids"] == ["session-1", "turn-2"]
        assert finding["metadata"]["identity_patches"] == 0
        assert "source:observer" in finding["event_tags"]
        _assert_no_generated_memory(store)
    finally:
        store.close()


def test_observer_panel_bounds_findings_and_excerpts(tmp_path):
    store = EngramStore(tmp_path / "observer-bounds.db")
    long_text = "signal " * 80
    reviewer = GoodReviewer(
        findings=[
            {"finding": f"one {long_text}", "rationale": "first", "confidence": 0.5},
            {"finding": f"two {long_text}", "rationale": "second", "confidence": 0.5},
            {"finding": f"three {long_text}", "rationale": "third", "confidence": 0.5},
        ]
    )
    try:
        result = run_observer_panel(
            store,
            reviewer_clients=[reviewer],
            context="bounded turn context",
            source_ids=["session-1", "turn-3"],
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            max_findings_per_reviewer=2,
            max_excerpt_chars=80,
        )

        assert result["written"] == 2
        assert result["dropped"] == 1
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
        )
        assert len(rows) == 2
        assert all(len(row["content_excerpt"]) <= 80 for row in rows)
        assert all(row["content_excerpt"].endswith("...") for row in rows)
        _assert_no_generated_memory(store)
    finally:
        store.close()
