"""CLI smoke tests for simple-mode setup helpers."""

from mnemos.cli import main


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
