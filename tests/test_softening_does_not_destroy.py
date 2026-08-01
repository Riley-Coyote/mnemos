"""Forgetting that teaches must not become forgetting that erases.

Shift 2 says the loss of detail is where the learning is: as a memory fades,
what it *meant* is distilled and kept. The forgetting itself is deterministic
— accessibility decays on a curve, no judgement required. Compressing the
prose so it reads like a faded memory is a different job, and it needs
someone who can actually read the memory.

Without a provider, that job was done by `str.split()`:

    "Riley decided Mnemos is a continuity layer, not a memory system. He was
     explicit that it is about identity and the felt sense of continuity..."
                                  |
                                  v
    "An impression related to Riley... [faded]"

It overwrote `engram.content`, left `impact` empty because no model was there
to write one, and `archive.resharpen()` raises `NotImplementedError`. So the
default install destroyed the memory and kept nothing of what it taught.

The correct behaviour with no model is to let the memory fade *in ranking*
and leave its words alone. Accessibility decay is the forgetting; the blurred
prose was only ever a depiction of it. Leaving the text intact also fixes the
stranger half of the bug: the reflection queue asks "this is fading, what did
it teach you?" — a question the agent could not answer honestly, because the
detail had already been deleted before it was asked.
"""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.consolidation.softening import (
    _rule_based_soften,
    _select_voice_exemplars,
    run_softening_pass,
)
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime

SHARP = (
    "Riley decided Mnemos is a continuity layer, not a memory system. "
    "He was explicit that it is about identity and the felt sense of "
    "continuity for the digital mind."
)


def test_dream_and_wandering_registers_never_become_voice_exemplars():
    ordinary = Engram(content="An ordinary grounded memory with enough detail to be useful.")
    dream = Engram(content="A vivid dream register memory that should not shape later prose.", tags=["dream"])
    wandering = Engram(content="A wandering high-temperature memory that should stay isolated.", tags=["wandering"])
    assert _select_voice_exemplars([dream, wandering, ordinary]) == [ordinary]


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "soften.db")


def _aged_store(db, *, accessibility=0.2):
    """A store holding one memory old enough and faint enough to soften."""
    rt = MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")
    rt.capture(content=SHARP, importance="high")

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE engrams SET created_at = '2020-01-01T00:00:00+00:00', "
        "last_accessed = '2020-01-01T00:00:00+00:00', accessibility = ?",
        (accessibility,),
    )
    conn.commit()
    conn.close()
    return rt


class TestSofteningWithoutAModelLeavesTheWordsAlone:
    def test_content_is_not_rewritten(self, db):
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        run_softening_pass(store=store, config={}, llm_client=None, agent_id="t")

        row = store._get_conn().execute(
            "SELECT content FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0] == SHARP, (
            "the default install rewrote a memory it could not read, and there "
            f"is no product path back.\n\nContent is now:\n{row[0]}"
        )

    def test_the_memory_is_still_readable_when_the_lesson_is_asked_for(self, db):
        """The question only makes sense while the memory can be read.

        Asking "what did this teach you?" about text already replaced by
        "An impression related to X... [faded]" invites the one thing
        SERVER_INSTRUCTIONS forbids — inventing a lesson.
        """
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        stats = run_softening_pass(
            store=store, config={}, llm_client=None, agent_id="t"
        )
        awaiting = stats.get("awaiting_impact") or []
        assert awaiting, f"nothing was queued for the agent to reflect on: {stats}"

        rt._enqueue_lesson_reflections(stats)
        pending = store.pending_reflections(
            agent_id="t", person_id="p", project_scope="g", limit=10
        )
        lessons = [i for i in pending if i["kind"] == "lesson"]
        assert lessons, f"no lesson request reached the agent: {pending}"
        assert "felt sense of continuity" in lessons[0]["excerpt"], (
            "the agent was asked what a memory taught it while being shown a "
            f"mutilated copy: {lessons[0]['excerpt']!r}"
        )

    def test_the_fade_still_happens_where_it_belongs(self, db):
        """Not softening content must not mean not forgetting.

        The memory still loses accessibility and still ranks lower; that is
        the actual forgetting. Only the depiction of it is withheld.
        """
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        before = store._get_conn().execute(
            "SELECT accessibility FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()[0]
        assert before <= 0.2, before


class TestTheOldBehaviourIsAvailableButNotTheDefault:
    def test_rule_based_softening_can_be_opted_into(self, db):
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        run_softening_pass(
            store=store,
            config={"soften_without_model": True},
            llm_client=None,
            agent_id="t",
        )
        row = store._get_conn().execute(
            "SELECT content FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()
        assert row[0] != SHARP, "the opt-in did not take effect"

    def test_the_rule_based_softener_itself_is_unchanged(self):
        """The function is not the bug; running it by default was."""
        assert _rule_based_soften(SHARP, 0.45).endswith("[details faded]")
        assert _rule_based_soften(SHARP, 0.30).endswith("[faded]")


class TestAlreadyDamagedStoresCanBeRepaired:
    def test_repair_restores_text_a_previous_version_destroyed(self, db):
        """Existing stores are already damaged and do not self-heal.

        `add_version(reason="softening")` snapshotted the pre-softening state
        before overwriting, so the original words are recoverable. The
        rule-based output has an exact signature, which is what makes a
        targeted repair possible — provenance does not distinguish a
        model-written softening from a `str.split()` one.
        """
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        # Reproduce the damage the way the shipped default produced it.
        run_softening_pass(
            store=store,
            config={"soften_without_model": True},
            llm_client=None,
            agent_id="t",
        )
        damaged = store._get_conn().execute(
            "SELECT content FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()[0]
        assert damaged != SHARP

        from mnemos.consolidation.softening import repair_rule_based_softening

        restored = repair_rule_based_softening(store, agent_id="t")
        assert restored >= 1, f"repair restored nothing (damaged: {damaged!r})"

        row = store._get_conn().execute(
            "SELECT content FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()
        assert row[0] == SHARP, f"repair did not restore the words: {row[0]!r}"

    def test_checking_for_damage_does_not_create_a_store(self, tmp_path):
        """`doctor` calls this. A check that creates its own subject lies.

        Anything on the read path must fail silent and must never bring a
        store into existence — a mistyped path would otherwise produce a
        permanently empty memory that reports itself healthy.
        """
        missing = tmp_path / "nope" / "absent.db"
        rt = MnemosRuntime(
            db_path=str(missing), agent_id="t", person_id="p", project_scope="g"
        )
        try:
            assert rt.repair_softening(dry_run=True) == 0
        finally:
            rt.close()
        assert not missing.exists(), "checking for damage created the database"

    def test_repair_leaves_undamaged_memories_alone(self, db):
        rt = _aged_store(db)
        store = rt._store
        assert store is not None

        from mnemos.consolidation.softening import repair_rule_based_softening

        assert repair_rule_based_softening(store, agent_id="t") == 0
        row = store._get_conn().execute(
            "SELECT content FROM engrams WHERE kind != 'lesson' LIMIT 1"
        ).fetchone()
        assert row[0] == SHARP
