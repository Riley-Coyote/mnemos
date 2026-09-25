"""Words with punctuation attached are searched, not dropped.

Queries were built from whitespace-split words that had to pass
``str.isalnum()``, so "alive?", "residents'", "house:" and "decline." were
dropped whole: in a question, usually the last word, and often the one that
mattered. The index itself (FTS5, unicode61) splits on punctuation, so the
queries now split the same way.
"""

from __future__ import annotations

from mnemos.core.engram import Engram
from mnemos.retrieval.reactive import ReactiveRetriever, _to_fts_query
from mnemos.store.fts import fts_words

SCOPE = dict(agent_id="default", person_id="user", project_scope="global")


def _memory(content: str) -> Engram:
    return Engram(content=content, kind="semantic", owner_agent_id="default",
                  person_id="user", project_scope="global")


def test_words_are_split_the_way_the_index_splits_them():
    assert fts_words("Is it really clean?") == ["really", "clean"]
    assert fts_words("the residents' house: a rule.") == ["the", "residents", "house", "rule"]
    assert fts_words("don't e-mail 2026-09-24") == ["don", "mail", "2026"]
    assert fts_words("The the THE") == ["The"]


def test_a_question_mark_does_not_hide_the_word_it_follows(store):
    clean = _memory("a clean room, swept and aired")
    other = _memory("a really long walk by the sea")
    for engram in (clean, other):
        store.save_engram(engram)

    assert '"clean"' in _to_fts_query("is it really clean?")
    found = [r.engram.id for r in ReactiveRetriever(store, reconsolidation_enabled=False)
             .retrieve("is it really clean?", **SCOPE)]
    assert clean.id in found


def test_a_new_memory_links_through_its_last_word(store, encoder):
    """Encoding links a new memory to what shares its words; the word before a
    full stop counts too. A link needs two shared words, and here one of the two
    is that last word."""
    earlier = encoder.encode(content="Residents may decline an invitation", kind="semantic",
                             person_id="user", project_scope="global")
    new = encoder.encode(content="Every invitation lets a visitor decline.", kind="semantic",
                         person_id="user", project_scope="global")
    assert earlier.id in {c.target_id for c in new.connections}
