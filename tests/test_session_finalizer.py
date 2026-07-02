import json

from mnemos.inner_life.session_finalizer import finalize_session_transcript
from mnemos.store.sqlite_store import EngramStore


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_session_finalizer_writes_bounded_provenance_below_memory(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"id": "u1", "timestamp": "2026-06-28T20:00:00Z", "role": "user", "content": "Start the Mnemos preflight."},
            {"id": "a1", "timestamp": "2026-06-28T20:00:01Z", "role": "assistant", "content": "I verified the live DB was not touched."},
            {"id": "t1", "timestamp": "2026-06-28T20:00:02Z", "type": "tool_call", "name": "pytest", "content": "uv run pytest tests/test_turn_finalizer.py"},
            {"id": "a2", "timestamp": "2026-06-28T20:00:03Z", "role": "assistant", "content": "Tests passed and the generated rows stayed private."},
        ],
    )
    store = EngramStore(tmp_path / "session.db")
    try:
        result = finalize_session_transcript(
            store,
            transcript,
            session_id="session-1",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            max_turn_events=10,
            max_excerpt_chars=80,
        )

        assert result["session_written"] == 1
        assert result["turn_events_written"] == 4
        assert result["full_transcript_sent_to_llm"] is False

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-1",
            limit=10,
        )
        assert [row["event_type"] for row in rows].count("session_finalized") == 1
        turn_rows = [row for row in rows if row["event_type"] != "session_finalized"]
        assert len(turn_rows) == 4
        assert all(len(row["content_excerpt"]) <= 80 for row in turn_rows)
        assert any(row["event_type"] == "tool_event" for row in turn_rows)
        assert all(row["metadata"]["writes_memory"] is False for row in rows)

        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
        assert store.search_hypomnema(
            "generated rows stayed private",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        ) == []
    finally:
        store.close()


def test_session_finalizer_prefilters_long_transcript_deterministically(tmp_path):
    transcript = tmp_path / "long-session.jsonl"
    rows = [
        {"id": f"u{i}", "role": "user", "content": f"ordinary turn {i}"}
        for i in range(20)
    ]
    rows[7]["content"] = "important verification: tests passed for the copy DB"
    rows[13]["content"] = "error path: malformed critic output skipped safely"
    _write_jsonl(transcript, rows)
    store = EngramStore(tmp_path / "long-session.db")
    try:
        result = finalize_session_transcript(
            store,
            transcript,
            session_id="session-long",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            max_turn_events=3,
        )

        assert result["events_seen"] == 20
        assert result["turn_events_written"] == 3
        assert result["events_dropped"] == 17
        assert result["selected_source_ids"][:2] == ["u7", "u13"]
        assert result["full_transcript_sent_to_llm"] is False

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-long",
            limit=10,
        )
        assert len(rows) == 4
        assert any("tests passed" in row["content_excerpt"] for row in rows)
        assert any("malformed critic" in row["content_excerpt"] for row in rows)
    finally:
        store.close()


def test_session_finalizer_reports_malformed_json_without_memory_writes(tmp_path):
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text('{"role": "user", "content": "ok"}\n{broken json\n', encoding="utf-8")
    store = EngramStore(tmp_path / "broken.db")
    try:
        result = finalize_session_transcript(
            store,
            transcript,
            session_id="session-broken",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["malformed_lines"] == 1
        assert result["session_written"] == 1
        assert result["generated_memory_writes"] == 0
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()
