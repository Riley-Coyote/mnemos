"""CLI smoke tests for simple-mode setup helpers."""

import json
from pathlib import Path
import plistlib

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


def test_inspect_defaults_to_operational_visibility(tmp_path, capsys):
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    db = str(tmp_path / "inspect.db")
    store = EngramStore(db)
    operational = Engram(content="Operational engram prose may be inspected.")
    review = Engram(
        content="Review-only engram prose must require explicit review mode.",
        read_visibility="review_only",
    )
    audit = Engram(
        content="Audit-only engram prose must require explicit audit mode.",
        read_visibility="audit_only",
    )
    try:
        store.save_engram(operational)
        store.save_engram(review)
        store.save_engram(audit)
    finally:
        store.close()

    result = main(["--db-path", db, "inspect", operational.id])
    captured = capsys.readouterr()
    assert result == 0
    assert "Operational engram prose may be inspected." in captured.out

    result = main(["--db-path", db, "inspect", review.id])
    captured = capsys.readouterr()
    assert result == 1
    assert "Engram not found" in captured.err
    assert "Review-only engram prose" not in captured.out
    assert "Review-only engram prose" not in captured.err

    result = main(["--db-path", db, "inspect", audit.id])
    captured = capsys.readouterr()
    assert result == 1
    assert "Engram not found" in captured.err
    assert "Audit-only engram prose" not in captured.out
    assert "Audit-only engram prose" not in captured.err

    result = main(["--db-path", db, "inspect", "--review", review.id])
    captured = capsys.readouterr()
    assert result == 0
    assert "Review-only engram prose must require explicit review mode." in captured.out

    result = main(["--db-path", db, "inspect", "--audit", audit.id])
    captured = capsys.readouterr()
    assert result == 0
    assert "Audit-only engram prose must require explicit audit mode." in captured.out

    result = main(["--db-path", db, "inspect", "--admin", audit.id])
    captured = capsys.readouterr()
    assert result == 0
    assert "Audit-only engram prose must require explicit audit mode." in captured.out
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


def test_inner_life_run_cli_executes_scheduled_affect_without_memory(tmp_path, capsys):
    db = tmp_path / "inner-life-run.db"
    store = EngramStore(db)
    try:
        store.upsert_inner_life_event(
            idempotency_key="turn:session-cli:run",
            event_type="turn_finalized",
            process_name="turn-finalizer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-cli",
            turn_id="run",
            content_hash="hash",
            content_excerpt="USER: continue\nASSISTANT: verified",
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
            "run",
            "--process",
            "affect",
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
            "--run-id",
            "cli-affect",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Inner-life scheduled run" in out
    assert "Status:        ran" in out
    assert "Gate:          run" in out
    assert "Memory writes: 0" in out

    store = EngramStore(db)
    try:
        assert store.get_latest_emotional_state("oliver") is not None
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_inner_life_plist_cli_writes_without_loading(tmp_path, capsys):
    db = tmp_path / "inner-life-plist.db"
    EngramStore(db).close()
    plist = tmp_path / "com.davidef.mnemos.innerlife.affect.plist"
    artifact_dir = tmp_path / "artifacts"

    result = main(
        [
            "inner-life",
            "plist",
            "--process",
            "affect",
            "--plist",
            str(plist),
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
            "--interval-seconds",
            "3600",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Inner-life launchd plist" in out
    assert "Loaded:        false" in out
    assert plist.exists()
    payload = plistlib.loads(plist.read_bytes())
    args = payload["ProgramArguments"]
    assert args[:5] == [
        str(Path.cwd() / ".venv" / "bin" / "python3"),
        "-m",
        "mnemos.cli",
        "inner-life",
        "run",
    ]
    assert "--allow-live-db" not in args
    assert payload["StartInterval"] == 3600
    assert payload["RunAtLoad"] is True


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


def test_soak_tick_cli_requires_representative_db(capsys):
    result = main(["soak", "tick", "--agent-id", "oliver"])
    err = capsys.readouterr().err

    assert result == 1
    assert "mnemos soak requires --db-path" in err


def test_soak_tick_cli_refuses_default_live_db_without_override(capsys):
    result = main(
        [
            "soak",
            "tick",
            "--db-path",
            str(Path("~/.mnemos/memory.db").expanduser()),
            "--agent-id",
            "oliver",
        ]
    )
    err = capsys.readouterr().err

    assert result == 1
    assert "mnemos soak refuses live Mnemos databases" in err


def test_soak_tick_cli_reports_disabled_tick_without_memory(tmp_path, capsys):
    db = tmp_path / "soak-cli.db"
    EngramStore(db).close()

    result = main(
        [
            "soak",
            "tick",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--rollout-tag",
            "u7-test",
            "--run-id",
            "cli-disabled",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Soak scheduled tick" in out
    assert "Status:        skipped" in out
    assert "Reason:        soak_tick_disabled" in out
    assert "Families ran:  0" in out

    store = EngramStore(db)
    try:
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_soak_plist_cli_writes_orchestrator_without_loading(tmp_path, capsys):
    db = tmp_path / "soak-plist-cli.db"
    EngramStore(db).close()
    plist = tmp_path / "com.davidef.mnemos.soak.tick.plist"
    artifact_dir = tmp_path / "artifacts"

    result = main(
        [
            "soak",
            "plist",
            "--plist",
            str(plist),
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--rollout-tag",
            "u7-test",
            "--interval-seconds",
            "900",
            "--artifact-dir",
            str(artifact_dir),
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Soak tick launchd plist" in out
    assert "Loaded:        false" in out
    assert plist.exists()
    payload = plistlib.loads(plist.read_bytes())
    args = payload["ProgramArguments"]
    assert args[:5] == [
        str(Path.cwd() / ".venv" / "bin" / "python3"),
        "-m",
        "mnemos.cli",
        "soak",
        "tick",
    ]
    assert "--allow-live-db" not in args
    assert payload["StartInterval"] == 900


def test_soak_preflight_cli_writes_artifact_without_live_activation(tmp_path, capsys):
    db = tmp_path / "soak-preflight-cli.db"
    EngramStore(db).close()
    artifact = tmp_path / "u7-preflight.json"

    result = main(
        [
            "soak",
            "preflight",
            "--db-path",
            str(db),
            "--agent-id",
            "oliver",
            "--person-id",
            "david",
            "--project-scope",
            "pai",
            "--artifact",
            str(artifact),
        ]
    )
    out = capsys.readouterr().out

    assert result == 2
    assert "Soak activation preflight" in out
    assert "U7 activation:      blocked" in out
    assert "Blocker: watch_doctor_missing" in out
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema"] == "mnemos.u7_soak_preflight.v1"
    assert payload["db"]["exists"] is True
    assert payload["ready_for_u7_activation"] is False
    assert "soak_tick_disabled" in payload["blockers"]
