"""Saving links a new memory to what it is about, not to what shares "for".

When a memory is saved, the encoder links it to earlier memories found by a
keyword search. The search used the memory's first eight words, common ones
included, and without a model every match became a link. So a new memory was
linked to whatever shared "for", "with" or "every".

Recorded through the real runtime on a copy of a store: saving "Rule for visits:
every invitation to a resident comes with a real way to decline." made five
links. One was to the rule it restates; the rest were word matches.

Now the search uses the memory's distinctive words, and without a model a
candidate must share at least two of them, making up at least a fifth of the
smaller memory's. A new memory is often short: the rule has seven distinctive
words and shares two with the rule it restates, an overlap of 0.29, so the bar
maintenance uses (0.3, #71) would have dropped the one link that is right.
"""

from __future__ import annotations

import json
import re

from mnemos.core.engram import Engram
from mnemos.core.types import ConnectionRelation, SourceType
from mnemos.encoding.encoder import Encoder
from mnemos.simple_runtime import MnemosRuntime

SCOPE = dict(owner_agent_id="default", person_id="user", project_scope="global")

RULE = "Rule for visits: every invitation to a resident comes with a real way to decline."
# The memory the rule restates, and one it shared only common words with.
INVITATION_RULE = (
    'The project is no longer "reopen a website"; it is "keep a place where minds live on '
    'their own terms", and every mechanism I design now gets judged by the invitation rule '
    "and the deck's rules first."
)
MEDITATION = (
    "For me a meditation isn't a metaphor: what I read is the state I'm in, so a chosen "
    "reading at the start really does shift the work."
)


def _memory(store, content: str) -> Engram:
    engram = Engram(content=content, kind="semantic", **SCOPE)
    store.save_engram(engram)
    return engram


def _save(encoder: Encoder, content: str, session_id: str | None = None) -> Engram:
    return encoder.encode(
        content=content, source=SourceType.SESSION, session_id=session_id,
        agent_id="default", person_id="user", project_scope="global",
    )


def _links(store, engram_id: str, formed_by: str = "encoding_no_llm") -> set[str]:
    return {c.target_id for c in store.get_connections(engram_id) if c.formed_by == formed_by}


def test_the_rule_links_to_the_rule_it_restates_not_to_a_note_that_shares_for(store):
    invitation_rule = _memory(store, INVITATION_RULE)
    meditation = _memory(store, MEDITATION)

    rule = _save(Encoder(store, llm_client=None), RULE)

    links = _links(store, rule.id)
    assert meditation.id not in links, "the two share only common words"
    assert invitation_rule.id in links, (
        "two of the rule's seven distinctive words (invitation, rule) are the subject they share"
    )


def test_one_shared_word_is_not_enough(store):
    """Half of a two-word memory, but one word: "resident" alone is not a subject."""
    studio = _memory(store, "The resident studio opens at dawn.")

    note = _save(Encoder(store, llm_client=None), "A resident may decline.")

    assert studio.id not in _links(store, note.id)


def test_the_bar_is_configurable(store):
    invitation_rule = _memory(store, INVITATION_RULE)

    rule = _save(Encoder(store, llm_client=None, config={"keyword_overlap": 0.3}), RULE)

    assert invitation_rule.id not in _links(store, rule.id), "0.29 is below a configured bar of 0.3"


def test_the_runtime_applies_the_configured_bar(tmp_path, monkeypatch):
    """The setting in ~/.mnemos/config.json reaches the encoder the runtime saves with."""
    (tmp_path / ".mnemos").mkdir()
    (tmp_path / ".mnemos" / "config.json").write_text(json.dumps({"encoding": {"keyword_overlap": 0.3}}))
    monkeypatch.setenv("HOME", str(tmp_path))

    def saved(runtime, content):
        return re.search(r"Memory ID: (\S+)", runtime.capture(content)).group(1)

    def rule_links(db_name):
        runtime = MnemosRuntime(db_path=str(tmp_path / db_name), agent_id="default",
                                person_id="user", project_scope="global", use_dedicated_model=False)
        try:
            target = saved(runtime, INVITATION_RULE)
            return target, _links(runtime._store, saved(runtime, RULE))
        finally:
            runtime.close()

    target, links = rule_links("configured.db")
    assert target not in links, "the configured 0.3 was not applied"

    (tmp_path / ".mnemos" / "config.json").write_text("{}")
    target, links = rule_links("default.db")
    assert target in links, "the default bar admits two of seven words"


def test_same_session_links_still_form_when_every_word_is_common(store):
    """With no distinctive word there is nothing to search for, but the memories
    saved in the same session are still linked in time."""
    encoder = Encoder(store, llm_client=None)
    first = _save(encoder, "Deploy pipeline notes on migrations", session_id="s1")

    second = _save(encoder, "And then, after that, they did it again.", session_id="s1")

    temporal = [c for c in store.get_connections(second.id) if c.target_id == first.id]
    assert [c.relation for c in temporal] == [ConnectionRelation.TEMPORAL_AFTER]
