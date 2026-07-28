"""Shift 5: identity is measured from the graph, and must not need a model.

`compute_identity_profile` contains zero LLM references — its own docstring
says "computed from graph topology — not narrated, measured." It nevertheless
lived inside the reflection pass, which is deep-only and skipped entirely when
no model is configured.

The consequence on the install Mnemos actually ships was zero identity rows:
an agent whose sense of self was never computed once. These tests pin the
property that matters — it runs with nothing configured.
"""

import pytest

from mnemos.consolidation.daemon import ConsolidationDaemon
from mnemos.consolidation.reflection import run_identity_pass
from mnemos.core.types import SourceType


def _fill(encoder, agent_id="identity-test"):
    for content in [
        "Riley prefers cold brew and works best at 3am",
        "The design language is monochrome, colour only from the pixel world",
        "Deploys always go to staging before production",
        "Never force-push to main; everything lands through a pull request",
    ]:
        encoder.encode(
            content=content, impact="", kind="semantic", tags=["continuity"],
            source=SourceType.SESSION, agent_id=agent_id,
            skip_surprise_detection=True,
        )


class TestIdentityNeedsNoModel:
    def test_identity_is_computed_with_no_llm_client(self, store, encoder):
        _fill(encoder)

        stats = run_identity_pass(store, agent_id="identity-test")

        assert stats["identity_computed"] is True
        identity = store.get_identity("identity-test")
        assert identity is not None, "identity was never persisted"
        assert identity.epoch_state.self_summary.strip(), (
            "identity was computed but says nothing about the agent"
        )

    def test_the_daemon_runs_it_on_a_shallow_cycle(self, store, encoder):
        """Not deep, no model — the configuration Mnemos actually ships."""
        _fill(encoder)
        daemon = ConsolidationDaemon(store=store, config={}, llm_client=None)

        stats = daemon.run_cycle(deep=False, agent_id="identity-test")

        assert "identity" in stats["passes_run"], (
            f"identity did not run on a shallow cycle: {stats['passes_run']}"
        )
        assert store.get_identity("identity-test") is not None

    def test_identity_reflects_what_the_agent_keeps_returning_to(self, store, encoder):
        """The summary is measured, not narrated, so it must track content."""
        for _ in range(4):
            encoder.encode(
                content="Another decision about deployment and staging pipelines",
                impact="", kind="semantic", tags=["deployment"],
                source=SourceType.SESSION, agent_id="identity-test",
                skip_surprise_detection=True,
            )

        run_identity_pass(store, agent_id="identity-test")

        summary = store.get_identity("identity-test").epoch_state.self_summary
        assert summary.strip()


class TestIdentityIsNotGatedOnRecentActivity:
    def test_a_quiet_period_does_not_erase_the_self(self, store, encoder):
        """Identity is not a property of the last 24 hours.

        The reflection pass returned early unless three engrams had been
        created within the lookback window, so a quiet week produced no
        identity even with a model configured. A quiet week is not an
        absence of self.
        """
        _fill(encoder)
        # Backdate everything well outside any lookback window.
        store._get_conn().execute(
            "UPDATE engrams SET created_at = '2020-01-01T00:00:00+00:00'"
        )
        store._get_conn().commit()

        stats = run_identity_pass(store, agent_id="identity-test")

        assert stats["identity_computed"] is True, (
            "identity vanished because nothing was captured recently"
        )


class TestEmptyStore:
    def test_an_empty_store_is_not_an_error(self, store):
        """No memories yet is not a failure, and must not raise."""
        stats = run_identity_pass(store, agent_id="nobody")

        assert stats["identity_computed"] is False
        assert "reason" in stats
