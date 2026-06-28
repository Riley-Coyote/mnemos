from mnemos.inner_life.turn_finalizer import finalize_turn_event
from mnemos.store.sqlite_store import EngramStore


def test_turn_finalizer_writes_one_idempotent_provenance_row_only(tmp_path):
    store = EngramStore(tmp_path / "turn.db")
    try:
        result = finalize_turn_event(
            store,
            session_id="session-1",
            turn_id="turn-1",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            user_text="Can you verify the scope before writing?",
            assistant_text="I verified on a copy and did not touch live ~/.mnemos.",
            source_message_ids=["msg-user-1", "msg-assistant-1"],
            rollout_tag="u6.6-test",
        )
        repeated = finalize_turn_event(
            store,
            session_id="session-1",
            turn_id="turn-1",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            user_text="Can you verify the scope before writing?",
            assistant_text="I verified on a copy and did not touch live ~/.mnemos.",
            source_message_ids=["msg-user-1", "msg-assistant-1"],
            rollout_tag="u6.6-test",
        )

        assert result["written"] == 1
        assert repeated["written"] == 0
        assert repeated["duplicates"] == 1

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-1",
            event_type="turn_finalized",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "exchange"
        assert row["content_hash"]
        assert "verify the scope" in row["content_excerpt"]
        assert "live ~/.mnemos" in row["content_excerpt"]
        assert row["source_ids"] == ["msg-user-1", "msg-assistant-1"]
        assert row["gate_decision"] == "ledger_only"
        assert row["metadata"]["writes_memory"] is False

        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
        assert store.search_hypomnema(
            "verify the scope",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        ) == []
    finally:
        store.close()


def test_turn_finalizer_skips_empty_exchange_without_writing(tmp_path):
    store = EngramStore(tmp_path / "turn-empty.db")
    try:
        result = finalize_turn_event(
            store,
            session_id="session-1",
            turn_id="turn-empty",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            user_text="",
            assistant_text="",
            rollout_tag="u6.6-test",
        )

        assert result["written"] == 0
        assert result["skipped"] == 1
        assert result["reason"] == "empty_exchange"
        assert store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        ) == []
    finally:
        store.close()
