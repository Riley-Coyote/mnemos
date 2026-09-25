"""A belief ask names what a mind keeps returning to, not the words it keeps using.

Maintenance notices a theme recurring across four or more memories and asks
the agent whether it has become a belief. Themes were counted over every word
of three letters or more, less sixteen stopwords, so the ask went to filler.
On a live store on 2026-09-25 it asked about "only" (65 memories), "asked"
(48), "first" (54) and "into" (47). Themes are now counted over each memory's
distinctive words: the same notion links and lessons use.
"""

from __future__ import annotations

import re

import pytest

from mnemos.simple_runtime import MnemosRuntime


def _runtime(tmp_path):
    return MnemosRuntime(
        db_path=str(tmp_path / "themes.db"), agent_id="t", person_id="p", project_scope="g"
    )


def _asked_themes(rt):
    rows = rt._store._get_conn().execute(
        "SELECT prompt FROM reflection_queue WHERE kind = 'belief' ORDER BY created_at"
    ).fetchall()
    return [m.group(1) for (prompt,) in rows if (m := re.search(r"\[theme:([^\]]+)\]", prompt))]


# Six notes share "asked", "every" and "from". Four of them are about glaze,
# and no other word recurs.
_POTTERY_NOTES = [
    "She asked every question from memory, and the glaze held.",
    "He asked every visitor from town to touch the glaze.",
    "They asked every buyer from abroad whether the glaze would crack.",
    "I asked every kiln log from spring; the glaze darkened each firing.",
    "We asked every supplier from the coast for cheaper clay.",
    "You asked every neighbour from the lane to keep quiet.",
]


def test_the_ask_names_the_theme_not_the_filler(tmp_path):
    rt = _runtime(tmp_path)
    for note in _POTTERY_NOTES:
        rt.capture(content=note, impact="kept for the test")
    rt.maintain()

    assert _asked_themes(rt) == ["glaze"], (
        f"the belief ask went to filler instead of the theme: {_asked_themes(rt)}"
    )


# Filler seen winning themes on real stores. The first four were already
# common words that the old count ignored; the rest are newly common.
@pytest.mark.parametrize("filler", [
    "from", "into", "only", "every",
    "asked", "said", "told", "wants", "because", "first", "three", "nothing",
    "currently", "https",
])
def test_filler_in_more_notes_than_the_theme_is_not_asked(tmp_path, filler):
    rt = _runtime(tmp_path)
    notes = [
        f"kiln door {filler} cracked overnight",
        f"kiln shelves {filler} restacked by hand",
        f"kiln cone bent {filler} early",
        f"kiln vent {filler} jammed again",
        f"garden beans {filler} watered",
        f"harbour ferry {filler} delayed",
    ]
    for note in notes:
        rt.capture(content=note, impact="kept for the test")
    rt.maintain()

    assert _asked_themes(rt) == ["kiln"]


# Common is not the same as empty: a writer's pages and a morning routine are
# themes, and must stay countable however often they come up.
@pytest.mark.parametrize("theme, notes", [
    ("page", [
        "rewrote the opening page twice",
        "the last page felt thin until the dialogue landed",
        "read the page aloud and cut two lines",
        "one page kept its secret intact",
    ]),
    ("morning", [
        "walked before work on a cold morning",
        "the morning light made the draft easier",
        "wrote best in the quiet morning hours",
        "a slow morning with tea and no messages",
    ]),
])
def test_an_everyday_word_that_means_something_is_still_asked(tmp_path, theme, notes):
    rt = _runtime(tmp_path)
    for note in notes:
        rt.capture(content=note, impact="kept for the test")
    rt.maintain()

    assert _asked_themes(rt) == [theme]
