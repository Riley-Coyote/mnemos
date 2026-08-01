from __future__ import annotations

import pytest

from mnemos.experimental import ExperimentalFeatureUnavailable


def test_unfinished_prototypes_fail_clearly_instead_of_faking_success():
    from mnemos.advanced.dreaming import run_dream_cycle
    from mnemos.multiagent.federation import FederationClient

    with pytest.raises(ExperimentalFeatureUnavailable, match="experimental"):
        run_dream_cycle(None, None, {})
    with pytest.raises(ExperimentalFeatureUnavailable, match="experimental"):
        FederationClient("https://example.invalid", None).sync()


def test_advanced_server_is_blocked_by_default(monkeypatch, capsys):
    from mnemos.cli import main

    monkeypatch.delenv("MNEMOS_ENABLE_EXPERIMENTAL", raising=False)
    assert main(["serve", "--mode", "advanced"]) == 1
    assert "experimental" in capsys.readouterr().err.lower()
