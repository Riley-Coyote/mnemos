from __future__ import annotations


def test_advanced_server_is_blocked_by_default(monkeypatch, capsys):
    from mnemos.cli import main

    monkeypatch.delenv("MNEMOS_ENABLE_EXPERIMENTAL", raising=False)
    assert main(["serve", "--mode", "advanced"]) == 1
    assert "experimental" in capsys.readouterr().err.lower()
