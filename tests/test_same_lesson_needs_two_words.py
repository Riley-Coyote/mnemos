"""One shared word does not make two lessons the same.

When a memory fades, softening looks for a lesson that already says what it
taught and files the memory under it instead of making a new one. Two lessons
counted as the same when they shared half of the smaller one's distinctive
words. #75 made "everything", "else", "first" and others common words, which
left many lessons with two distinctive words, and then one shared word is half.
Recording a three-week history through the runtime, lessons fell from 38 to 29
after #75: each pair below was filed under a lesson it shares one word with,
where before #75 it became its own.

They must now also share two distinctive words, the rule #74 set for links made
without a model. A false merge loses a lesson; a missed one only leaves a
near-duplicate.
"""

from __future__ import annotations

import pytest

from mnemos.consolidation.softening import run_softening_pass
from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.simple_runtime import MnemosRuntime

SCOPE = dict(owner_agent_id="default", person_id="user", project_scope="global")

BOOK = "This book is what we're here for; everything else bends around it."
READER = "Ines is her first reader, not me."
CHAPTERS = "Short chapters; flag any that run long."
VERIFY = "Always verify."


def _fade(store, content, impact):
    """A memory old and faint enough for softening to take its lesson."""
    engram = Engram(content=content, impact=impact, impact_source="agent", kind="episodic",
                    accessibility=0.02, resolution=1.0, **SCOPE)
    store.save_engram(engram)
    conn = store._get_conn()
    conn.execute("UPDATE engrams SET created_at = ? WHERE id = ?",
                 ("2020-01-01T00:00:00+00:00", engram.id))
    conn.commit()
    return engram


def _soften(store):
    return run_softening_pass(store, {}, None, agent_id="default", person_id="user",
                              project_scope="global")


def _lessons(store):
    return [e for e in store.get_active_engrams(agent_id="default", limit=500)
            if "lesson" in e.tags]


def _filed_under(store, engram):
    return {c.target_id for c in store.get_connections(engram.id)
            if c.relation == ConnectionRelation.DISTILLED_INTO}


@pytest.mark.parametrize(
    ("first", "then"),
    [
        (BOOK, "Weather is the book's clock."),
        (BOOK, "The tides in this book are true; get them right."),
        (READER, "On a sentence's rhythm, Ines is the one to trust."),
        # A lesson with one distinctive word shares it with anything using it.
        (VERIFY, "Verify the kiln's temperature before a firing."),
    ],
    ids=["weather-under-book", "tides-under-book", "rhythm-under-reader", "one-word-lesson"],
)
def test_one_shared_word_does_not_make_it_the_same_lesson(store, first, then):
    _fade(store, "what the first memory was about", first)
    _soften(store)
    (existing,) = _lessons(store)

    later = _fade(store, "what a later memory was about", then)
    _soften(store)

    lessons = {e.content: e for e in _lessons(store)}
    assert set(lessons) == {first, then}, (
        f"{then!r} was filed under {first!r}, a lesson it shares one word with"
    )
    assert _filed_under(store, later) == {lessons[then].id}
    assert lessons[first].strength == pytest.approx(existing.strength), (
        "a lesson that says something else was reinforced"
    )


def test_the_same_lesson_in_other_words_is_still_reinforced(store):
    _fade(store, "her pacing notes on part one", CHAPTERS)
    _soften(store)
    (lesson,) = _lessons(store)

    again = _fade(store, "the chapter nine draft ran to forty pages",
                  "Keep her chapters short and flag the long ones.")
    _soften(store)

    (after,) = _lessons(store)
    assert after.id == lesson.id
    assert after.strength == pytest.approx(lesson.strength + 0.1)
    assert _filed_under(store, again) == {lesson.id}


def test_a_one_word_lesson_is_still_its_own(store):
    """A lesson with one distinctive word shares only one with anything, itself
    included. The memory it came from must still recognise it, cycle after
    cycle, or it distils a copy each time."""
    memory = _fade(store, "shipped a fix that was never run", VERIFY)
    for _ in range(3):
        _soften(store)

    lessons = _lessons(store)
    assert [e.content for e in lessons] == [VERIFY]
    assert _filed_under(store, memory) == {lessons[0].id}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = MnemosRuntime(db_path=str(tmp_path / "r.db"), agent_id="demo", use_dedicated_model=False)
    r._ensure_init()
    yield r
    r.close()


def test_repair_lists_links_filed_on_one_shared_word(runtime):
    """Links #75 filed on one shared word are listed like any other misfiled
    link. A memory's link to its own one-word lesson holds."""
    scope = dict(owner_agent_id="demo", person_id=runtime.scope.person_id,
                 project_scope=runtime.scope.project_scope)

    def lesson(text):
        engram = Engram(content=text, impact=text, kind="procedural",
                        tags=["lesson", "distilled"], **scope)
        runtime._store.save_engram(engram)
        return engram

    def filed(content, impact, under):
        engram = Engram(content=content, impact=impact, impact_source="agent",
                        kind="episodic", **scope)
        engram.connections.append(Connection(
            target_id=under.id, relation=ConnectionRelation.DISTILLED_INTO,
            strength=0.9, formed_by="consolidation"))
        runtime._store.save_engram(engram)
        return engram

    book, verify, chapters = lesson(BOOK), lesson(VERIFY), lesson(CHAPTERS)
    weather = filed("the rain chapter", "Weather is the book's clock.", book)
    filed("why we are here", BOOK, book)
    filed("shipped a fix that was never run", VERIFY, verify)
    filed("chapter nine ran long", "Keep her chapters short and flag the long ones.", chapters)

    plan = runtime.repair_lessons()

    assert plan["links"] == 4
    assert [(m["source_id"], m["reason"]) for m in plan["misfiled"]] == [
        (weather.id, "unrelated")
    ]
