"""A correction by query acts only on what the query's words really name.

mnemos_correct can forget or rewrite a note found by a query instead of an ID.
It took whatever came closest, however far that was: on a scratch store,
"forget the zeppelin schedule" archived "The writing group will meet at the
bookshop next Thursday, as a public reading." Nothing there mentions a
zeppelin. Notes were also scored on every word of the query, so sharing "the"
or "her" counted as much as sharing what the query was about.

Now a note, memory or belief is acted on only when it holds the words that mean
something in the query: at least half of them, and two when the query has two
or more. Otherwise a forget archives nothing and says so, and an update is kept
as new continuity instead of overwriting an unrelated note.
"""

from __future__ import annotations

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime

SCOPE = dict(agent_id="t", person_id="p", project_scope="g")

ZEPPELIN = "The zeppelin schedule moved to Tuesdays."
# The last shares "forget", "the" and "schedule" with "forget the zeppelin schedule".
NOTES = [
    "The writing group will meet at the bookshop next Thursday, as a public reading.",
    "A stuck scene can wait for the noon walk.",
    "Her sister Ines lives in Porto and reads every chapter first.",
    "Don't forget: the ferry schedule changes in May.",
]


def _runtime(tmp_path):
    return MnemosRuntime(db_path=str(tmp_path / "forget.db"), **SCOPE)


def _notes(rt):
    return {note["content"] for note in rt._store.search_hypomnema("", limit=50, **SCOPE)}


def _memories(rt):
    return {engram.content for engram in rt._store.get_active_engrams(agent_id="t", limit=50)}


def test_forgetting_what_no_note_mentions_archives_nothing(tmp_path):
    rt = _runtime(tmp_path)
    for note in NOTES:
        rt.capture(content=note)
    notes, memories = _notes(rt), _memories(rt)

    out = rt.correct(correction="", query="forget the zeppelin schedule", action="forget")

    assert out.startswith("Nothing was archived"), out
    assert _notes(rt) == notes
    assert _memories(rt) == memories


def test_a_memory_holding_one_of_two_words_is_not_forgotten(tmp_path):
    """No note in scope, so the query goes to the memories themselves."""
    rt = _runtime(tmp_path)
    rt._ensure_init()
    ferry = Engram(content="The ferry schedule changes in May.", kind="semantic", **{
        "owner_agent_id": "t", "person_id": "p", "project_scope": "g"})
    rt._store.save_engram(ferry)

    out = rt.correct(correction="", query="the zeppelin schedule", action="forget")

    assert out.startswith("Nothing was archived"), out
    assert rt._store.get_engram(ferry.id).state == "active"


def test_forgetting_what_the_words_name_archives_that_note(tmp_path):
    rt = _runtime(tmp_path)
    for note in [ZEPPELIN, *NOTES]:
        rt.capture(content=note)

    out = rt.correct(correction="", query="forget the zeppelin schedule", action="forget")

    assert out.startswith("Archived"), out
    assert _notes(rt) == set(NOTES)
    assert ZEPPELIN not in _memories(rt)


def test_an_update_that_names_no_note_rewrites_none(tmp_path):
    rt = _runtime(tmp_path)
    for note in NOTES:
        rt.capture(content=note)

    out = rt.correct(correction="The zeppelin now flies on Tuesdays.", query="zeppelin schedule")

    assert out.startswith("No continuity note matched"), out
    assert set(NOTES) <= _notes(rt), "a note that never mentioned a zeppelin was rewritten"
    assert "The zeppelin now flies on Tuesdays." in _notes(rt)


def test_a_belief_is_not_retired_over_what_and_she(tmp_path):
    rt = _runtime(tmp_path)
    rt._ensure_init()
    rt._store.save_belief(Belief(agent_id="t", content="She says what she means.",
                                 confidence=0.7, source="agent"))

    out = rt.correct(correction="", query="forget what she said", action="forget")

    assert "Retired" not in out, out
    assert rt._store.get_beliefs("t", active_only=True)


def test_recall_shows_the_notes_the_question_is_about(tmp_path):
    about_the_reading = [
        "She asked me to keep every morning clear this week, until the reading.",
        "The writing group will meet at the bookshop next Thursday, as a public reading.",
    ]
    rt = _runtime(tmp_path)
    for note in [
        "A stuck scene can wait for the noon walk.",
        "Jansson's quiet is the tone she's reaching for.",
        "Guard her writing hours this week.",
        *about_the_reading,
    ]:
        rt.capture(content=note)

    out = rt.recall("getting ready for her reading")
    notes = out.split("Continuity notes:", 1)[-1].split("Durable memories:", 1)[0]
    shown = {line.split("] ", 1)[1] for line in notes.splitlines() if line.startswith("- [")}

    assert shown == set(about_the_reading), shown
