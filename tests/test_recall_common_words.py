"""Recall searches the words that mean something in a cue, not "for" and "her".

Recall seeds from a keyword search, and every word of the cue went into it,
ORed together. In a small store bm25 barely discounts a common word, so a short
memory holding "for" can be the best match there is. On a store recorded
through the real runtime, "getting ready for her reading" made 29 keyword
seeds, and recall led with "A stuck scene can wait for the noon walk." and
"Jansson's quiet is the tone she's reaching for.", which share only "for" with
it. Common words are now left out of the search, unless a cue has nothing else.
"""

from __future__ import annotations

from mnemos.core.engram import Engram
from mnemos.retrieval.reactive import ReactiveRetriever, _to_fts_query
from mnemos.simple_runtime import MnemosRuntime

SCOPE = dict(agent_id="default", person_id="user", project_scope="global")

_CUE = "getting ready for her reading"
# From that store. These share only "for" or "her" with the cue...
_FILLER = [
    "A stuck scene can wait for the noon walk.",
    "Jansson's quiet is the tone she's reaching for.",
    "Guard her writing hours this week.",
]
# ...and these hold the word it is about.
_ABOUT_THE_READING = [
    "She asked me to keep every morning clear this week, until the reading.",
    "The writing group will meet at the bookshop on Harbour Street next Thursday, as a public reading.",
]


def _memory(content: str) -> Engram:
    return Engram(content=content, kind="semantic", owner_agent_id="default",
                  person_id="user", project_scope="global")


def test_a_memory_sharing_only_for_or_her_is_not_recalled(store):
    for text in _FILLER + _ABOUT_THE_READING:
        store.save_engram(_memory(text))

    results = ReactiveRetriever(store, reconsolidation_enabled=False).retrieve(_CUE, **SCOPE)
    returned = [r.engram.content for r in results]

    assert returned, "recall returned nothing"
    assert returned[0] in _ABOUT_THE_READING, f"recall led with {returned[0]!r}"
    assert not set(returned) & set(_FILLER), f"filler matches came back: {returned}"


def test_the_answer_leads_recall_through_the_runtime(tmp_path):
    """The path an agent calls: captured with the defaults, recalled by mnemos_recall."""
    rt = MnemosRuntime(db_path=str(tmp_path / "reading.db"), agent_id="t", person_id="p",
                       project_scope="g")
    for text in _FILLER + _ABOUT_THE_READING:
        rt.capture(content=text)

    durable = rt.recall(_CUE).split("Durable memories:", 1)[-1]
    shown = [line.split("] ", 1)[1] for line in durable.splitlines() if line.startswith("- [")]

    assert shown and shown[0] in _ABOUT_THE_READING, f"recall led with {shown[:1]}"
    last_answer = max(shown.index(text) for text in _ABOUT_THE_READING if text in shown)
    assert not [t for t in shown[:last_answer] if t in _FILLER], (
        f"a memory sharing only 'for' or 'her' outranked one about the reading: {shown}"
    )


def test_the_search_keeps_the_meaningful_words_in_order():
    assert _to_fts_query(_CUE) == '"getting" OR "ready" OR "reading"'
    assert _to_fts_query("What did we decide about the deadline?") == '"decide" OR "deadline"'


def test_a_cue_of_only_common_words_is_still_searched(store):
    """With nothing else to go on, the common words are the search."""
    store.save_engram(_memory("what she did next surprised everyone"))

    assert _to_fts_query("what did she") == '"what" OR "did" OR "she"'
    assert ReactiveRetriever(store, reconsolidation_enabled=False).retrieve("what did she", **SCOPE)
