"""A fading memory is filed under the lesson it taught, and only that one.

When a memory fades, softening looks for a lesson that already says what it
taught, reinforces it, and links the memory to it (distilled_into). Three
things went wrong on a real store:

  * The search ORed the impact's first six words, common ones included, and the
    first lesson found was taken as the same lesson. "When I looked for how I
    actually exist..." was filed under a lesson about checking a clean grep,
    through "when", "how" and "actually". Of 631 distilled_into links, 451
    joined memories to lessons they share almost no words with.
  * With no model, a fading memory's wording is never compressed, so it
    qualifies again every cycle, and its lesson was reinforced every cycle:
    143 reinforcements a cycle, the same few lessons growing without end.
  * Placeholder impacts the server wrote ("Correction to earlier continuity.")
    became lessons, and every memory carrying one reinforced it.
"""

from __future__ import annotations

import pytest

from mnemos.consolidation.softening import _create_or_reinforce_lesson
from mnemos.core.engram import Engram
from mnemos.core.types import ConnectionRelation
from mnemos.simple_runtime import MnemosRuntime

SCOPE = dict(owner_agent_id="default", person_id="user", project_scope="global")


def _lesson(store, content: str) -> Engram:
    lesson = Engram(content=content, impact=content, kind="procedural",
                    tags=["lesson", "distilled"], strength=0.8, stability=0.8, **SCOPE)
    store.save_engram(lesson)
    return lesson


def _fading(store, content: str, impact: str, impact_source: str = "agent") -> Engram:
    engram = Engram(content=content, impact=impact, impact_source=impact_source,
                    kind="episodic", **SCOPE)
    store.save_engram(engram)
    return store.get_engram(engram.id)


def test_sharing_common_words_does_not_make_it_the_same_lesson(store):
    grep = _lesson(store, "When I claim something is clean, the verification must "
                          "match how the thing actually varies.")
    exist = _fading(store, "a long conversation about what I am",
                    "When I looked for how I actually exist, what was true was relational.")

    lesson_id = _create_or_reinforce_lesson(exist, store, {})

    assert lesson_id != grep.id
    assert store.get_engram(lesson_id).content == exist.impact
    assert store.get_engram(grep.id).strength == pytest.approx(0.8), "an unrelated lesson was reinforced"


def test_the_same_lesson_said_again_is_reinforced(store):
    rendered = _lesson(store, "Verify the rendered result, not the declaration: "
                              "computed styles prove nothing.")
    again = _fading(store, "fonts looked right in the inspector and wrong on screen",
                    "Verify the rendered result, not the declaration.")

    assert _create_or_reinforce_lesson(again, store, {}) == rendered.id
    assert store.get_engram(rendered.id).strength == pytest.approx(0.9)
    links = store.get_engram(again.id).connections
    assert any(c.target_id == rendered.id and c.relation == ConnectionRelation.DISTILLED_INTO for c in links)


def test_a_placeholder_impact_is_not_a_lesson(store):
    placeholder = _fading(store, "the note was corrected", "Correction to earlier continuity.",
                          impact_source="template")
    stats: dict = {}

    assert _create_or_reinforce_lesson(placeholder, store, stats) is None
    assert not stats


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = MnemosRuntime(db_path=str(tmp_path / "l.db"), agent_id="demo", use_dedicated_model=False)
    yield r
    r.close()


def test_a_memory_reinforces_its_lesson_once_not_every_cycle(runtime):
    """Without a model the memory stays fading, cycle after cycle; its lesson is
    drawn from it once."""
    runtime.capture("Spent three hours on a misplaced guard clause in the handler",
                    impact="Small guard clauses deserve the same care as big designs.")
    engram = runtime._store.get_active_engrams(agent_id="demo", limit=5)[0]
    engram.accessibility, engram.resolution = 0.02, 1.0
    runtime._store.save_engram(engram)
    conn = runtime._store._get_conn()
    conn.execute("UPDATE engrams SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", engram.id))
    conn.commit()

    def lesson_strength():
        lessons = [e for e in runtime._store.get_active_engrams(agent_id="demo", limit=50)
                   if "lesson" in e.tags]
        assert len(lessons) == 1, [e.content for e in lessons]
        return lessons[0].strength

    runtime.maintain()
    first = lesson_strength()
    runtime.maintain()
    runtime.maintain()

    assert lesson_strength() == pytest.approx(first)
