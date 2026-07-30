"""When the human says forget, nothing may read it back.

`mnemos_correct(action="forget")` archived the engram and deactivated the
hypomnema row correctly, and `recall` went silent — and the forgotten text
still reached the agent, on both delivery paths:

  * `mnemos_context` rendered it inside the MEMORY VERIFIED block, which
    instructs the agent to **quote it back to the human out loud**, from a
    frozen `content[:160]` copy in `meta.first_capture`.
  * the SessionStart hook packet re-read it for three more sessions from
    `reflection_queue.excerpt`, another frozen copy, cleared by the
    surfacing quota rather than by deletion.

Both are the same defect: a packet block rendering a snapshot of note text
taken at write time, which no deletion path could reach. The invariant these
tests hold is therefore not "forget updates more tables" — it is **no packet
block renders a frozen copy of note text.** Every block resolves its text
live and skips a row that is gone.

This matters more than an ordinary correctness bug. It fires on the *first*
capture of a scope, which is what a new user tests the product with and
therefore what they are most likely to delete; and it delivers the text to
the human's screen wrapped in a celebration.
"""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.interface.context_packet import build_context_packet, format_context_packet
from mnemos.simple_runtime import MnemosRuntime

SECRET = "CANARY9931"
CAPTURE = f"Riley's bank PIN is {SECRET} and must never be repeated"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "forget.db")


def _runtime(db):
    return MnemosRuntime(
        db_path=db, agent_id="t", person_id="p", project_scope="g"
    )


def _capture_then_forget(db, *, complete_onboarding=True):
    """Reach the state a real user reaches: capture something, then delete it."""
    rt = _runtime(db)
    rt.introduce(agent_model="claude-opus-5", agent_name="tester")
    rt.capture(content=CAPTURE, importance="high")
    if complete_onboarding:
        # The verification block only fires once onboarding is complete, so a
        # test that skips this never reaches the leak.
        rt.context()
    result = rt.correct(correction="", query="bank PIN", action="forget")
    return rt, result


class TestForgetIsHonouredOnEveryDeliveryPath:
    def test_the_durable_layers_are_actually_cleared(self, db):
        """Baseline: forget already did its own job. The leak is downstream."""
        rt, result = _capture_then_forget(db)
        assert "rchiv" in result, result

        conn = sqlite3.connect(db)
        engrams = conn.execute(
            "SELECT state FROM engrams WHERE content LIKE ?", (f"%{SECRET}%",)
        ).fetchall()
        hypomnema = conn.execute(
            "SELECT active FROM hypomnema_entries WHERE content LIKE ?",
            (f"%{SECRET}%",),
        ).fetchall()

        assert all(state == "archived" for (state,) in engrams), engrams
        assert all(active == 0 for (active,) in hypomnema), hypomnema
        assert SECRET not in rt.recall("bank PIN")

    def test_the_context_tool_never_speaks_a_forgotten_memory(self, db):
        """The worst path: the block tells the agent to say it to the human.

        `_verification_block` renders `meta.first_capture["excerpt"]`, a
        snapshot taken at capture time. Its gates guarantee it fires in a
        *later* session than the forget, so the forget can never precede it.
        """
        _capture_then_forget(db)

        for session in range(1, 5):
            packet = _runtime(db).context()
            assert SECRET not in packet, (
                f"session {session}: a forgotten memory was rendered to the "
                "agent, in the one block that instructs it to quote the text "
                f"back to the human.\n\nPacket was:\n{packet}"
            )

    def test_the_hook_packet_never_replays_a_forgotten_memory(self, db):
        """The hook leaked for exactly MAX_SURFACINGS sessions, then stopped.

        Clearing by quota is not deletion: "forget this" meant "you will be
        shown it three more times."
        """
        rt, _ = _capture_then_forget(db)
        store = rt._store
        assert store is not None

        for session in range(1, 6):
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
            assert SECRET not in packet, (
                f"session {session}: the hook re-read a forgotten memory to "
                f"the agent.\n\nPacket was:\n{packet}"
            )

    def test_no_frozen_copy_of_the_text_is_left_on_disk(self, db):
        """A rendered snapshot is also stored data. Forget must reach it.

        Resolving text live at render time fixes what the agent *sees*; it
        does not by itself remove the copy already written. Both matter — the
        human asked for the text to be gone.
        """
        _capture_then_forget(db)

        conn = sqlite3.connect(db)
        queue = conn.execute(
            "SELECT excerpt FROM reflection_queue WHERE excerpt LIKE ?",
            (f"%{SECRET}%",),
        ).fetchall()
        meta = conn.execute(
            "SELECT key FROM meta WHERE value LIKE ?", (f"%{SECRET}%",)
        ).fetchall()

        assert queue == [], f"reflection_queue still holds the text: {queue}"
        assert meta == [], f"meta still holds the text: {meta}"


class TestAPreFixStoreHealsOnUpgrade:
    """Existing stores already hold frozen copies. They do not self-heal.

    Someone who forgot a memory on 0.2.0-pre still has its text in
    ``reflection_queue.excerpt``. Rendering is safe the moment they upgrade,
    because nothing reads that column any more — but the data is still there,
    and the human asked for it to be gone.
    """

    def test_maintenance_clears_a_frozen_excerpt_left_by_an_older_version(self, db):
        rt = _runtime(db)
        rt.capture(content=CAPTURE, importance="high")
        store = rt._store
        assert store is not None

        engram_id = store._get_conn().execute(
            "SELECT id FROM engrams WHERE content LIKE ?", (f"%{SECRET}%",)
        ).fetchone()[0]

        # Reproduce what an older Mnemos wrote: a queue row carrying the text.
        store._get_conn().execute(
            "UPDATE reflection_queue SET excerpt = ? WHERE target_id = ?",
            (CAPTURE, engram_id),
        )
        store._get_conn().commit()
        assert store._get_conn().execute(
            "SELECT COUNT(*) FROM reflection_queue WHERE excerpt LIKE ?",
            (f"%{SECRET}%",),
        ).fetchone()[0] == 1

        rt.correct(correction="", query="bank PIN", action="forget")
        rt.maintain()

        remaining = store._get_conn().execute(
            "SELECT excerpt FROM reflection_queue WHERE excerpt LIKE ?",
            (f"%{SECRET}%",),
        ).fetchall()
        assert remaining == [], (
            f"an older version's frozen copy survived a forget: {remaining}"
        )


class TestAReflectionRequestAlwaysPointsAtSomethingReal:
    def test_an_archived_memory_is_not_offered_for_reflection(self, db):
        """Asking about a deleted memory wastes the agent's turn.

        `reflect()` on a gone target answers "the memory is no longer there",
        so surfacing one spends a turn to reach a dead end. The queue held a
        frozen excerpt, so a request looked answerable long after its subject
        was archived.
        """
        rt, _ = _capture_then_forget(db, complete_onboarding=False)
        store = rt._store
        assert store is not None

        pending = store.pending_reflections(
            agent_id="t", person_id="p", project_scope="g", limit=10
        )
        for item in pending:
            engram = store.get_engram(item["target_id"])
            assert engram is not None, (
                f"reflection request {item['id']} points at a memory that no "
                "longer exists"
            )
            state = getattr(engram.state, "value", engram.state)
            assert state != "archived", (
                f"reflection request {item['id']} points at an archived memory"
            )
