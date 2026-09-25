"""Maintenance links memories that belong together, not ones that share "the".

Connection discovery gives every underconnected memory up to five new links a
cycle, from candidates found by meaning and by keywords. Both searches were too
loose:

  * Meaning: anything above 0.3 similarity. The configured bar,
    ``similarity_threshold`` (0.7 in the defaults and in users' config files),
    was never read.
  * Keywords: the memory's first eight words, common ones included, so "the",
    "with" and "every" matched nearly everything, and any match became a link.

Over a simulated week of nights on a copy of a real store, the pass added 1,491
links; with these bars, 265, and recall found as much or more.
"""

from __future__ import annotations

from mnemos.consolidation.connection_discovery import run_connection_discovery
from mnemos.core.engram import Engram

SCOPE = dict(owner_agent_id="default", person_id="user", project_scope="global")
PASS = dict(agent_id="default", person_id="user", project_scope="global")


def _memory(store, content: str) -> Engram:
    engram = Engram(content=content, kind="semantic", **SCOPE)
    store.save_engram(engram)
    return engram


def _links(store, engram: Engram) -> set[str]:
    return {c.target_id for c in store.get_connections(engram.id)}


class _Meaning:
    """An embedding index that finds one memory at a known similarity."""

    available = True

    def __init__(self, target: Engram, similarity: float):
        self.target, self.similarity = target, similarity

    def search(self, text, k=10, exclude_ids=None):
        return [] if self.target.id in (exclude_ids or ()) else [(self.target.id, self.similarity)]


def test_common_words_alone_do_not_link_two_memories(store):
    a = _memory(store, "about the thing that they would have done")
    b = _memory(store, "what they would have made from that")

    run_connection_discovery(store, embedding_index=None, config={}, **PASS)

    assert b.id not in _links(store, a)


def test_one_incidental_word_does_not_link_them_either(store):
    """Sharing "reviewing" is not being about the same thing."""
    a = _memory(store, "reviewing the harbour lighting budget with the council")
    b = _memory(store, "reviewing chapter drafts for the novel about orchards")

    run_connection_discovery(store, embedding_index=None, config={}, **PASS)

    assert b.id not in _links(store, a)


def test_a_shared_subject_still_links_them(store):
    a = _memory(store, "the lighthouse keeper kept a logbook")
    b = _memory(store, "a logbook of storms, kept at the lighthouse")

    run_connection_discovery(store, embedding_index=None, config={}, **PASS)

    assert b.id in _links(store, a)


def test_meaning_links_honour_the_configured_similarity(store):
    a = _memory(store, "first entry, nothing in common")
    b = _memory(store, "second passage, unrelated wording")

    run_connection_discovery(store, embedding_index=_Meaning(b, 0.5), config={}, **PASS)
    assert b.id not in _links(store, a), "0.5 is below the default bar of 0.7"

    run_connection_discovery(store, embedding_index=_Meaning(b, 0.5),
                             config={"similarity_threshold": 0.4}, **PASS)
    assert b.id in _links(store, a), "a configured bar of 0.4 admits 0.5"
