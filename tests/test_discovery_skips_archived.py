"""Maintenance links only live memories; a retired one gets no new links.

``correct()`` retires the memory it replaces (archives it) and captures the
correction beside it. Archiving drops the old memory from keyword search, but
its vector stays in the embedding index, and connection discovery took every
meaning match it could load without asking whether that memory was still
live. A correction says nearly what the old memory said, so the next
maintenance cycle linked the two, and recall then passed light from the
correction into the retired memory and on to its neighbours. A memory that
decay had made dormant could be linked the same way.

Keyword candidates were already limited to active memories, and so are
recall's meaning seeds. Discovery's meaning candidates now follow that rule.
"""

from __future__ import annotations

import pytest

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


def _links_touching(store, engram: Engram) -> list[tuple[str, str]]:
    rows = store._get_conn().execute(
        "SELECT source_id, target_id FROM connections "
        "WHERE source_id = ? OR target_id = ?",
        (engram.id, engram.id),
    ).fetchall()
    return [(row["source_id"], row["target_id"]) for row in rows]


def _corrected(store, engram: Engram) -> None:
    """What ``correct()`` does to the memory it replaces."""
    store.archive_engram(engram, reason="simple_correction_update")


def _gone_dormant(store, engram: Engram) -> None:
    """What decay does to a memory that has faded below the dormant line."""
    engram.state = "dormant"
    store.save_engram(engram)


class _Meaning:
    """An embedding index that finds these memories at these similarities."""

    available = True

    def __init__(self, similarities: dict[str, float]):
        self.similarities = similarities

    def search(self, text, k=10, exclude_ids=None):
        exclude = exclude_ids or set()
        hits = [(eid, s) for eid, s in self.similarities.items() if eid not in exclude]
        return sorted(hits, key=lambda hit: hit[1], reverse=True)[:k]


@pytest.mark.parametrize("retire", [_corrected, _gone_dormant], ids=["corrected", "dormant"])
def test_a_retired_memory_gets_no_new_link_but_a_live_one_does(store, retire):
    old = _memory(store, "the standup is at nine on mondays")
    retire(store, old)
    new = _memory(store, "the standup is at ten on mondays")
    # Shares no words with the correction, so only meaning can link the two.
    live = _memory(store, "weekly team sync opens the week")

    run_connection_discovery(
        store,
        embedding_index=_Meaning({old.id: 0.97, live.id: 0.8}),
        config={},
        **PASS,
    )

    assert _links_touching(store, old) == [], "a retired memory was given a new link"
    assert live.id in _links(store, new), "a live memory found by meaning is still linked"
