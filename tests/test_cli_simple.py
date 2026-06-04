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
