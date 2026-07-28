"""Memory that cannot notice its own amnesia will always fail silently.

Every failure this system has had looked identical from outside: a layer
reporting success while carrying nothing. A scope that did not match, a
config never applied, a scheduled job maintaining a phantom database —
each logged a clean, healthy cycle.

The one thing none of them could fake is that the packet came back empty,
session after session. These tests cover the signals that make that
visible, because a health card that only ever says "fine" is worth
nothing on the day it isn't.
"""

import pytest

from mnemos.simple_runtime import MnemosRuntime, format_health_card


def _seed_home(tmp_path):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
    r = MnemosRuntime(db_path=str(tmp_path / "signals.db"), agent_id="demo")
    yield r
    r.close()


class TestAbsenceIsReported:
    def test_an_empty_scope_says_so_rather_than_looking_healthy(self, runtime):
        runtime.context()
        signals = runtime.continuity_signals()

        assert signals["notes_active"] == 0
        assert any("Nothing has been captured" in w for w in signals["warnings"]), (
            "a scope carrying nothing reported no warning at all"
        )

    def test_repeated_empty_packets_accumulate_into_a_warning(self, runtime):
        for _ in range(3):
            runtime.context()

        signals = runtime.continuity_signals()
        assert signals["empty_context_streak"] == 3
        assert any("carried no continuity" in w for w in signals["warnings"])

    def test_a_capture_clears_the_alarm(self, runtime):
        for _ in range(4):
            runtime.context()
        assert runtime.continuity_signals()["empty_context_streak"] == 4

        runtime.capture("Riley prefers cold brew", importance="high")
        runtime.context()

        signals = runtime.continuity_signals()
        assert signals["empty_context_streak"] == 0
        assert signals["notes_active"] >= 1
        assert signals["warnings"] == [], signals["warnings"]

    def test_the_streak_resets_only_when_something_is_actually_returned(self, runtime):
        """A packet that carried nothing must not be counted as a success."""
        runtime.capture("Riley prefers cold brew", importance="high")
        runtime.context()
        assert runtime.continuity_signals()["empty_context_streak"] == 0

        # A query that matches nothing still returns an empty packet.
        runtime.context(query="zzzzz-nothing-matches-this-zzzzz")
        assert runtime.continuity_signals()["empty_context_streak"] == 1


class TestHealthCardSpeaksPlainly:
    def test_the_card_leads_with_the_warning_when_memory_is_empty(self, runtime):
        for _ in range(3):
            runtime.context()

        card = format_health_card(runtime.health())

        assert "ATTENTION" in card, "the card did not surface the problem at all"
        assert "Nothing has been captured" in card
        # It must stay relayable to a human without jargon.
        assert "hypomnema" not in card.lower()
        assert "engram" not in card.lower()

    def test_a_working_scope_states_that_it_is_working(self, runtime):
        runtime.capture("Riley ships at 3am", importance="high")
        runtime.context()

        card = format_health_card(runtime.health())

        assert "ATTENTION" not in card
        assert "Continuity check: carrying continuity" in card


class TestHealthStaysReadOnly:
    def test_reading_health_does_not_change_the_signals(self, runtime):
        for _ in range(2):
            runtime.context()
        before = runtime.continuity_signals()

        for _ in range(3):
            runtime.health()

        after = runtime.continuity_signals()
        assert before == after, (
            "health() mutated the very signals it reports, so the numbers "
            "would drift every time anyone looked at them"
        )
