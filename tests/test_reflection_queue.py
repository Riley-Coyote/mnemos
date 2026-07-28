"""The agent maintains its own memory, in its own words.

Mnemos never calls a model. Consolidation that needs judgement is proposed
by maintenance and performed by the agent through mnemos_reflect. Two
properties matter more than any other here:

- the server must never invent an answer, because a phrase it picked from a
  list is exactly the boilerplate that left 76% of a live store holding
  records rather than traces; and
- it must not nag. A request that reappears every session becomes a chore
  list appended to every conversation, which is the failure mode that would
  make an agent ignore its own memory.
"""

import pytest

from mnemos.simple_runtime import MnemosRuntime


def _seed_home(tmp_path):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
    r = MnemosRuntime(
        db_path=str(tmp_path / "reflect.db"), agent_id="demo",
        use_dedicated_model=False,
    )
    yield r
    r.close()


class TestTheLoop:
    def test_a_capture_without_a_trace_is_asked_about(self, runtime):
        runtime.capture(
            "Spent three hours debugging what turned out to be a guard clause",
            importance="high",
        )
        runtime.maintain()

        pending = runtime.pending_reflections()
        assert pending, "nothing was proposed for a capture carrying no trace"
        assert pending[0]["kind"] == "impact"
        assert "guard clause" in pending[0]["excerpt"]

    def test_the_agents_answer_becomes_the_memory_trace(self, runtime):
        """The whole shift, in one assertion.

        "Three hours debugging a guard clause" becoming "patience with small
        things pays off" is the example the original design used for what
        should survive when the details fade.
        """
        runtime.capture(
            "Spent three hours debugging what turned out to be a guard clause",
            importance="high",
        )
        runtime.maintain()
        target = runtime.pending_reflections()[0]["target_id"]

        runtime.reflect(target, "Patience with small things pays off.")

        engram = runtime._store.get_engram(target)
        assert engram.impact == "Patience with small things pays off."

    def test_an_answered_reflection_is_never_asked_again(self, runtime):
        runtime.capture("Something worth remembering happened today", importance="high")
        runtime.maintain()
        target = runtime.pending_reflections()[0]["target_id"]

        runtime.reflect(target, "It taught me to check the obvious first.")

        remaining = [i["target_id"] for i in runtime.pending_reflections()]
        assert target not in remaining

    def test_an_ignored_request_eventually_stops_asking(self, runtime):
        """Declining three times is an answer. Stop asking."""
        runtime.capture("A capture the agent will never reflect on", importance="high")
        runtime.maintain()
        assert runtime.pending_reflections(), "nothing to ignore"

        for _ in range(runtime._store.MAX_SURFACINGS):
            runtime._reflection_block()

        assert runtime.pending_reflections() == [], (
            "the same request kept resurfacing; a packet that nags becomes a "
            "chore list appended to every conversation"
        )


class TestTheServerNeverAnswers:
    def test_a_templated_impact_still_counts_as_missing(self, runtime):
        """The server's own phrases are not traces.

        `_impact_for` fills the column with a fixed phrase, which reads as
        complete while carrying nothing about how understanding changed.
        """
        runtime.capture("Riley prefers cold brew", importance="high")
        runtime.maintain()

        engram_id = runtime.pending_reflections()[0]["target_id"]
        engram = runtime._store.get_engram(engram_id)
        from mnemos.simple_runtime import _TEMPLATED_IMPACTS

        assert engram.impact in _TEMPLATED_IMPACTS, (
            "test premise wrong: this capture did not get a templated impact"
        )

    def test_an_empty_reflection_records_nothing(self, runtime):
        runtime.capture("A thing happened", importance="high")
        runtime.maintain()
        target = runtime.pending_reflections()[0]["target_id"]

        result = runtime.reflect(target, "   ")

        assert "Nothing recorded" in result
        assert runtime.pending_reflections(), "the request was consumed by an empty answer"

    def test_reflecting_on_an_unknown_id_is_survivable(self, runtime):
        result = runtime.reflect("engram_does_not_exist", "a thought")
        assert "Nothing was pending" in result


class TestPacketPresence:
    def test_a_quiet_scope_shows_nothing(self, runtime):
        """Most sessions should carry no request at all."""
        assert runtime._reflection_block() is None

        packet = runtime.context()
        assert "Something of yours is waiting" not in packet

    def test_the_request_appears_with_the_id_needed_to_answer(self, runtime):
        runtime.capture("Spent the afternoon rewriting the deploy script", importance="high")
        runtime.maintain()

        packet = runtime.context()

        assert "Something of yours is waiting" in packet
        assert "mnemos_reflect(target_id=" in packet
        # It must read as an invitation, not an obligation.
        assert "leave it" in packet

    def test_at_most_two_requests_are_shown(self, runtime):
        for i in range(6):
            runtime.capture(f"Capture number {i} about the ongoing work", importance="high")
            runtime.maintain()

        assert len(runtime.pending_reflections(limit=10)) >= 3, "not enough queued to test"
        assert len(runtime.pending_reflections()) <= 2


class TestScopeIsolation:
    def test_reflections_do_not_leak_across_agents(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        db = str(tmp_path / "shared.db")
        a = MnemosRuntime(db_path=db, agent_id="agent-a", use_dedicated_model=False)
        try:
            a.capture("Something only agent A experienced", importance="high")
            a.maintain()
            assert a.pending_reflections()
        finally:
            a.close()

        b = MnemosRuntime(db_path=db, agent_id="agent-b", use_dedicated_model=False)
        try:
            assert b.pending_reflections() == [], (
                "one agent was asked to reflect on another agent's memory"
            )
        finally:
            b.close()


class TestTheHookPacketCarriesThem:
    """The SessionStart hook is the path most agents actually receive.

    Phase 2 built the loop into simple mode's context() only, so on the
    hook path — the one Mnemos installs by default — a reflection request
    existed in the queue and never reached the agent at all.
    """

    def test_the_session_start_packet_includes_pending_reflections(self, runtime):
        from mnemos.interface.context_packet import build_context_packet

        runtime.capture("Spent three hours on a guard clause", importance="high")
        runtime.maintain()
        assert runtime.pending_reflections(), "nothing queued to test with"

        packet = build_context_packet(
            runtime._store, "",
            agent_id=runtime.scope.agent_id,
            person_id=runtime.scope.person_id,
            project_scope=runtime.scope.project_scope,
            include_engrams=False,
        )

        assert packet["reflections"], "the packet carried no reflection request"
        assert "Waiting On You" in packet["prompt"]
        assert "mnemos_reflect(target_id=" in packet["prompt"]

    def test_a_quiet_scope_adds_no_section(self, runtime):
        from mnemos.interface.context_packet import build_context_packet

        runtime._ensure_init()
        packet = build_context_packet(
            runtime._store, "",
            agent_id=runtime.scope.agent_id,
            person_id=runtime.scope.person_id,
            project_scope=runtime.scope.project_scope,
            include_engrams=False,
        )
        assert packet["reflections"] == []
        assert "Waiting On You" not in packet["prompt"]

    def test_a_read_only_caller_can_decline_to_consume_a_surfacing(self, runtime):
        """Inspecting the packet must not use up an agent's chances to answer."""
        from mnemos.interface.context_packet import build_context_packet

        runtime.capture("Something worth reflecting on", importance="high")
        runtime.maintain()

        for _ in range(5):
            build_context_packet(
                runtime._store, "",
                agent_id=runtime.scope.agent_id,
                person_id=runtime.scope.person_id,
                project_scope=runtime.scope.project_scope,
                include_engrams=False, mark_surfaced=False,
            )

        assert runtime.pending_reflections(), (
            "read-only inspection burned the request's surfacings"
        )
