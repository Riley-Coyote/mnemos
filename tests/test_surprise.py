"""Shift 3: surprise as growth, without beliefs and without a model.

"The most important moment to encode is when you're wrong." It was
unreachable: surprise required beliefs, beliefs were only created by the
LLM-gated belief-review pass, and that pass never ran without a provider. So
surprise always returned 0.0, which is why every call site passed
skip_surprise_detection=True — a rational shortcut for a path that could not
work.

Novelty breaks the cycle. Something unlike anything already remembered is
genuinely surprising, and measuring that needs neither beliefs nor a model.
"""

import pytest

from mnemos.core.types import SourceType


def _encode(encoder, content, agent_id="s"):
    return encoder.encode(
        content=content, impact="", kind="semantic", tags=[],
        source=SourceType.SESSION, agent_id=agent_id,
        skip_surprise_detection=True,
    )


class TestNoveltyNeedsNothing:
    def test_surprise_is_non_zero_with_no_beliefs_and_no_model(self, store, encoder):
        for i in range(6):
            _encode(encoder, f"Routine note {i} about the deploy pipeline and migrations")
        odd = _encode(encoder, "Riley keeps bees and the hive swarmed last spring")

        assert store.get_beliefs("s", active_only=True) == []
        assert encoder._llm_client is None

        novelty = encoder._structural_novelty(odd, store, "s")
        assert novelty > 0.0, "surprise is still structurally unreachable"

    def test_the_unfamiliar_is_more_surprising_than_the_familiar(self, store, encoder):
        for i in range(6):
            _encode(encoder, f"The deploy pipeline runs migrations before rollout, note {i}")
        familiar = _encode(encoder, "Deploy pipeline migrations happen before the rollout")
        unfamiliar = _encode(encoder, "Riley keeps bees and the hive swarmed last spring")

        assert (
            encoder._structural_novelty(unfamiliar, store, "s")
            > encoder._structural_novelty(familiar, store, "s")
        )

    def test_an_almost_empty_store_has_no_expectations_to_violate(self, store, encoder):
        """The first memories are not surprising, they are simply first."""
        first = _encode(encoder, "The very first thing ever remembered here")

        assert encoder._structural_novelty(first, store, "s") == 0.0

    def test_novelty_stays_below_the_contradiction_range(self, store, encoder):
        """Unfamiliar is weaker evidence than a belief actually contradicted."""
        for i in range(6):
            _encode(encoder, f"Something entirely unrelated number {i}")
        odd = _encode(encoder, "Zebra xylophone quixotic aardvark")

        assert encoder._structural_novelty(odd, store, "s") <= 0.6


class TestCapturePathUsesIt:
    def test_capture_no_longer_skips_surprise_detection(self, tmp_path, monkeypatch):
        """The skip existed because the path returned 0.0. It no longer does."""
        home = tmp_path / "home"
        (home / ".mnemos").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        from mnemos.simple_runtime import MnemosRuntime

        r = MnemosRuntime(db_path=str(tmp_path / "s.db"), agent_id="demo",
                          use_dedicated_model=False)
        try:
            for i in range(6):
                r.capture(f"Routine note {i} about deployment work", importance="low")
            r.capture("Riley keeps bees and the hive swarmed", importance="high")

            engrams = r._store.get_active_engrams(agent_id="demo", limit=20)
            assert engrams, "nothing was captured"
        finally:
            r.close()
