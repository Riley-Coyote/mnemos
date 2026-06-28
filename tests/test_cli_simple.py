"""CLI smoke tests for simple-mode setup helpers."""

import json
from pathlib import Path

from mnemos.cli import main
from mnemos.store.sqlite_store import EngramStore


def test_doctor_smoke_with_temp_db(tmp_path, capsys):
    result = main(
        [
            "doctor",
            "--db-path",
            str(tmp_path / "doctor.db"),
            "--agent-id",
            "nova",
            "--person-id",
            "riley",
            "--project-scope",
            "demo",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Mnemos Doctor" in out
    assert "Agent:       nova" in out
    assert "Simple tools:" in out


def test_remember_captures_in_scope(tmp_path, capsys):
    """`mnemos remember` writes through the same path as mnemos_capture."""
    db = str(tmp_path / "remember.db")
    result = main(
        [
            "remember",
            "Decided to keep the consolidation passes scope-strict",
            "--context", "audit follow-up",
            "--db-path", db,
            "--agent-id", "nova",
            "--person-id", "riley",
            "--project-scope", "demo",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Captured continuity." in out
    assert "Scope: nova/riley/demo" in out

    from mnemos.store.sqlite_store import EngramStore

    store = EngramStore(db)
    try:
        entries = store.search_hypomnema(
            "consolidation scope",
            agent_id="nova", person_id="riley", project_scope="demo",
        )
        assert any("scope-strict" in e["content"] for e in entries)
    finally:
        store.close()


def test_mcp_install_generic_prints_simple_config(capsys):
    result = main(["mcp", "install", "generic", "--agent-id", "nova"])
    out = capsys.readouterr().out

    assert result == 0
    assert '"mcpServers"' in out
    assert '"args": [' in out
    assert '"simple"' in out
    assert '"MNEMOS_AGENT_ID": "nova"' in out


def test_mcp_install_codex_prints_command(capsys):
    result = main(["mcp", "install", "codex", "--name", "mnemos"])
    out = capsys.readouterr().out

    assert result == 0
    assert "codex mcp add mnemos --" in out
    assert "serve --mode simple" in out


def test_inner_life_session_finalize_cli_writes_private_ledger(tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"id": "u1", "role": "user", "content": "verify on a copy"}) + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "inner-life.db"

    result = main(
        [
            "inner-life",
            "session-finalize",
            "--transcript",
            str(transcript),
            "--session-id",
            "session-cli",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--rollout-tag",
            "u6.6-test",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Inner-life session finalize" in out
    assert "Turn events:   1" in out
    assert "Memory writes: 0" in out

    from mnemos.store.sqlite_store import EngramStore

    store = EngramStore(db)
    try:
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-cli",
        )
        assert len(rows) == 2
        assert store.count_engrams(agent_id="oliver") == 0
    finally:
        store.close()


def test_inner_life_cli_requires_representative_db(capsys):
    result = main(
        [
            "inner-life",
            "turn-finalize",
            "--session-id",
            "session-cli",
            "--user-text",
            "hello",
        ]
    )
    err = capsys.readouterr().err

    assert result == 1
    assert "requires --db-path" in err


def test_inner_life_cli_refuses_default_live_db_without_override(capsys):
    result = main(
        [
            "inner-life",
            "turn-finalize",
            "--session-id",
            "session-cli",
            "--user-text",
            "hello",
            "--db-path",
            str(Path("~/.mnemos/memory.db").expanduser()),
        ]
    )
    err = capsys.readouterr().err

    assert result == 1
    assert "refuses live Mnemos databases" in err


def test_inner_life_activity_gate_cli_records_preflight_without_memory(tmp_path, capsys):
    db = tmp_path / "inner-life-activity.db"
    store = EngramStore(db)
    try:
        store.upsert_inner_life_event(
            idempotency_key="turn:session-cli:1",
            event_type="turn_finalized",
            process_name="turn-finalizer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-cli",
            turn_id="1",
            content_hash="hash",
            content_excerpt="USER: go\nASSISTANT: verified",
            event_tags=["u6.6", "turn-event"],
            rollout_tag="u6.6-test",
            gate_decision="ledger_only",
            metadata={"writes_memory": False},
        )
    finally:
        store.close()

    result = main(
        [
            "inner-life",
            "activity-gate",
            "--process",
            "reflect",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--rollout-tag",
            "u6.6-test",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Inner-life activity gate" in out
    assert "Decision:      run" in out
    assert "Memory writes: 0" in out

    store = EngramStore(db)
    try:
        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
            rollout_tag="u6.6-test",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "activity-gate"
        assert rows[0]["metadata"]["target_process"] == "reflect"
        assert store.count_engrams(agent_id="oliver") == 0
    finally:
        store.close()


def test_inner_life_status_cli_summarizes_rollout_telemetry(tmp_path, capsys):
    db = tmp_path / "inner-life-status.db"
    store = EngramStore(db)
    try:
        store.upsert_inner_life_event(
            idempotency_key="gate:reflect:1",
            event_type="tool_event",
            process_name="narrative-gate",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            content_hash="hash",
            content_excerpt="passed",
            rollout_tag="u6.6-test",
            gate_decision="pass",
            metadata={"generated_memory_writes": 0},
        )
        store.upsert_inner_life_event(
            idempotency_key="write:reflect:1",
            event_type="tool_event",
            process_name="low-stakes-writer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            content_hash="hash",
            content_excerpt="written",
            rollout_tag="u6.6-test",
            gate_decision="written:low_stakes",
            metadata={
                "generated_memory_writes": 1,
                "belief_writes": 0,
                "identity_patches": 0,
                "shared_pool_writes": 0,
            },
        )
    finally:
        store.close()

    result = main(
        [
            "inner-life",
            "status",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--rollout-tag",
            "u6.6-test",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Inner-life status" in out
    assert "Rows:                  2" in out
    assert "Generated memory writes: 1" in out
    assert "Process low-stakes-writer: 1" in out
    assert "Decision written:low_stakes: 1" in out


def test_inner_life_preflight_cli_blocks_default_schedules(tmp_path, capsys):
    db = tmp_path / "inner-life-preflight.db"
    store = EngramStore(db)
    store.close()

    result = main(
        [
            "inner-life",
            "preflight",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
        ]
    )
    out = capsys.readouterr().out

    assert result == 2
    assert "Inner-life preflight" in out
    assert "Full scheduled activation: blocked" in out
    assert "Blocker: inner_life_schedules_disabled" in out
    assert "Process reflect: scheduled=False activity_gate=True" in out


def test_inner_life_preflight_cli_does_not_create_missing_db(tmp_path, capsys):
    db = tmp_path / "missing-preflight.db"

    result = main(
        [
            "inner-life",
            "preflight",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
        ]
    )
    out = capsys.readouterr().out

    assert result == 2
    assert "DB exists:             False" in out
    assert "Blocker: db_missing" in out
    assert not db.exists()
