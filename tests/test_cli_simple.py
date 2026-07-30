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


def test_doctor_does_not_create_a_store(tmp_path, capsys):
    """A diagnostic must not materialize the thing it inspects.

    `doctor` used to call `context()`, which opens the store (building its
    schema) and runs maintenance. Against a fresh or mistyped scope that mints
    a phantom empty database and then reports it — the same hazard `health()`
    and the SessionStart hook already guard against.
    """
    missing = tmp_path / "typo" / "memory.db"
    result = main([
        "doctor",
        "--db-path", str(missing),
        "--agent-id", "nova",
        "--person-id", "riley",
        "--project-scope", "demo",
    ])
    out = capsys.readouterr().out

    assert result == 0
    assert "DB exists:    no" in out
    assert not missing.exists(), "doctor created a store just by inspecting"
    # Store-free readiness fields still report.
    assert "Agent:       nova" in out
    assert "Simple tools:" in out


def test_doctor_reports_a_seeded_store(tmp_path, capsys):
    """With a real store, doctor shows its continuity status."""
    db = str(tmp_path / "seeded.db")
    from mnemos.simple_runtime import MnemosRuntime

    rt = MnemosRuntime(db_path=db, agent_id="nova", person_id="riley",
                       project_scope="demo")
    rt.capture("Riley prefers concise answers", importance="high")
    rt.close()

    result = main([
        "doctor",
        "--db-path", db,
        "--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo",
    ])
    out = capsys.readouterr().out
    assert result == 0
    assert "DB exists:    yes" in out
    assert "Continuity:" in out


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
