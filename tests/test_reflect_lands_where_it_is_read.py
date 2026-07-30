"""The agent's own words must reach the agent.

Mnemos never calls a model to maintain memory. Work needing judgement is
proposed by the server and answered by the agent in its own voice, through
`mnemos_reflect`. That one sentence — what a memory *changed* — is the only
thing in the system nothing else can write.

`reflect()` set `engram.impact` and stopped there. The session packet is
built from hypomnema, and the engram layer is excluded from it by default
(`include_engrams=False` on the hook path, and a query the hook never
supplies). So the answer was not merely on the copy that decays: it was
unreachable from the automatic path in principle. Only a manual `recall`
with a matching cue could return it.

The system replied "Reflection recorded." every time.

These tests hold the invariant on **both** delivery paths. Fixing only
`context()` and not `build_context_packet` is a mistake already made once in
this codebase — the reflection loop was shipped into simple mode while the
SessionStart hook, which is what actually runs before an agent's first turn,
never saw it.
"""

from __future__ import annotations

import re

import pytest

from mnemos.interface.context_packet import build_context_packet, format_context_packet
from mnemos.simple_runtime import MnemosRuntime

ANSWER = "I now propose maintenance rather than perform it myself."


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "reflect.db")


def _runtime(db):
    return MnemosRuntime(
        db_path=db, agent_id="t", person_id="p", project_scope="g"
    )


def _capture_then_reflect(db):
    """Reach the state the packet asks for: a pending request, answered."""
    rt = _runtime(db)
    rt.capture(content="Riley rejected MCP sampling for Mnemos maintenance")
    packet = rt.context()

    match = re.search(r"(engram_[A-Z0-9]+)", packet)
    assert match, f"no reflection request was surfaced to answer:\n{packet}"

    result = rt.reflect(target_id=match.group(1), text=ANSWER)
    assert "ecorded" in result, result
    return rt


class TestAnAnsweredReflectionReachesTheNextSession:
    def test_the_context_tool_carries_it(self, db):
        _capture_then_reflect(db)
        packet = _runtime(db).context()
        assert ANSWER in packet, (
            "the agent's own reflection did not survive into the next "
            f"session's packet.\n\nPacket was:\n{packet}"
        )

    def test_the_hook_packet_carries_it(self, db):
        """The path that runs before an agent's first turn.

        This is the one that matters most and the one most easily missed: it
        excludes engrams by default, so anything written only to
        `engram.impact` cannot appear here at all.
        """
        rt = _capture_then_reflect(db)
        store = rt._store
        assert store is not None

        packet = format_context_packet(
            build_context_packet(
                store,
                query="",
                agent_id="t",
                person_id="p",
                project_scope="g",
                include_engrams=False,
            )
        )
        assert ANSWER in packet, (
            "the SessionStart hook — the mechanism that delivers continuity "
            f"automatically — did not carry the reflection.\n\nPacket was:\n{packet}"
        )

    def test_the_original_capture_is_not_destroyed(self, db):
        """A reflection adds to a memory; it must not overwrite it."""
        rt = _capture_then_reflect(db)
        packet = _runtime(db).context()
        assert "MCP sampling" in packet, (
            f"the reflection replaced the memory it was about:\n{packet}"
        )

    def test_the_engram_impact_is_still_written(self, db):
        """Reaching hypomnema must not cost the trace on the engram.

        Shift 1 lives on `engram.impact` — it is what survives when softening
        blurs the detail. Both writes are required.
        """
        rt = _capture_then_reflect(db)
        store = rt._store
        assert store is not None

        row = store._get_conn().execute(
            "SELECT impact FROM engrams WHERE content LIKE '%MCP sampling%'"
        ).fetchone()
        assert row is not None and row[0] == ANSWER, row


class TestReflectingIsIdempotentAndHonest:
    def test_answering_twice_does_not_duplicate_the_sentence(self, db):
        rt = _capture_then_reflect(db)
        packet = _runtime(db).context()
        assert packet.count(ANSWER) == 1, (
            f"the reflection appears {packet.count(ANSWER)} times in one packet"
        )

    def test_a_reflection_on_a_memory_with_no_note_still_records(self, db):
        """Not every engram has a continuity note. That must not be an error.

        Engrams encoded outside the simple capture path have no hypomnema row
        to revise. The impact write must still land, and the caller must not
        see a failure for something that worked.
        """
        rt = _runtime(db)
        rt.capture(content="Riley prefers concise answers with no preamble")
        store = rt._store
        assert store is not None

        engram_id = store._get_conn().execute(
            "SELECT id FROM engrams WHERE content LIKE '%concise%'"
        ).fetchone()[0]
        store._get_conn().execute(
            "UPDATE hypomnema_entries SET related_engram_id = NULL WHERE related_engram_id = ?",
            (engram_id,),
        )
        store._get_conn().commit()

        result = rt.reflect(target_id=engram_id, text="Brevity is the respect.")
        assert "ecorded" in result, result
        row = store._get_conn().execute(
            "SELECT impact FROM engrams WHERE id = ?", (engram_id,)
        ).fetchone()
        assert row[0] == "Brevity is the respect.", row
