"""Reconsolidation must rewrite the memory it was given, not a husk of it.

Every recall reconsolidates its top results: strength and stability rise, links
between memories retrieved together are reinforced, and a version is appended
to the audit trail. That is a read-modify-write, and for the memories FTS found
directly — the seeds, usually the most relevant results of all — the read was
incomplete. ``search_fts`` builds engrams from the ``engrams`` row alone, so a
seed arrived with no connections and no versions, and reconsolidation wrote
that husk back:

  * ``add_connection`` found no co_activated link in the empty list and
    appended a fresh one at 0.3, which ``INSERT OR REPLACE`` wrote over the
    stored link — a link reinforced to 1.0 came back at 0.3, newly formed;
  * the connection bonus counted ``len(engram.connections) == 0``, so a seed
    never stabilized faster for being well connected;
  * ``add_version`` numbered every snapshot ``len(versions) + 1 == 1``, so
    each recall overwrote version 1 and the history never grew.

A memory reached through the graph instead (a resonance result) was loaded
whole and reinforced correctly, so the same link strengthened or reset
depending on how the memory was found. On a copy of a real store, one recall
left a seed's co_activated links at 0.3 and a resonance result's at 1.0.
"""

from __future__ import annotations

import pytest

from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.retrieval.reactive import ReactiveRetriever, RetrievalResult
from mnemos.store.sqlite_store import EngramStore

CUE = "lighthouse keeper"
LONG_AGO = "2026-01-01T00:00:00+00:00"


def _link(target: Engram, strength: float) -> Connection:
    return Connection(
        target_id=target.id,
        relation=ConnectionRelation.CO_ACTIVATED,
        strength=strength,
        formed_at=LONG_AGO,
        formed_by="retrieval",
    )


def _save(db: str, *engrams: Engram) -> None:
    store = EngramStore(db)
    try:
        for engram in engrams:
            store.save_engram(engram)
    finally:
        store.close()


def _recall(db: str) -> dict[str, RetrievalResult]:
    """One recall as a session makes it: a fresh store, a default retriever."""
    store = EngramStore(db)
    try:
        results = ReactiveRetriever(store).retrieve(CUE)
    finally:
        store.close()
    return {result.engram.id: result for result in results}


def _stored(db: str, engram_id: str) -> Engram:
    """Read an engram back the way the next session will: from disk."""
    store = EngramStore(db)
    try:
        engram = store.get_engram(engram_id)
    finally:
        store.close()
    assert engram is not None
    return engram


def _link_to(engram: Engram, target_id: str) -> Connection:
    [link] = [c for c in engram.connections if c.target_id == target_id]
    return link


def test_seed_reinforces_its_existing_co_activated_link(tmp_path):
    db = str(tmp_path / "recall.db")
    seed = Engram(content="The lighthouse keeper logs every storm in the green ledger.")
    peer = Engram(content="Tide tables are pinned beside the radio.")
    seed.connections.append(_link(peer, 0.7))
    peer.connections.append(_link(seed, 0.7))
    _save(db, seed, peer)

    results = _recall(db)

    # The premise: one memory found by FTS, the other only through the link.
    assert results[seed.id].retrieval_path == "fts"
    assert results[peer.id].retrieval_path == "resonance"

    seed_link = _link_to(_stored(db, seed.id), peer.id)
    peer_link = _link_to(_stored(db, peer.id), seed.id)
    # Co-retrieval reinforces a link by 0.1 however the memory was found.
    assert seed_link.strength == pytest.approx(0.8)
    assert peer_link.strength == pytest.approx(0.8)
    # Reinforced in place, not replaced by a newly formed link.
    assert seed_link.formed_at == LONG_AGO


def test_seed_gets_the_connection_bonus_for_its_stored_links(tmp_path):
    db = str(tmp_path / "recall.db")
    linked = Engram(content="Lighthouse keeper rota for the winter months.", stability=0.3)
    bare = Engram(content="Lighthouse keeper rota for the summer months.", stability=0.3)
    notes = [Engram(content=f"Harbour note number {n}.") for n in ("one", "two", "three")]
    linked.connections.extend(_link(note, 0.5) for note in notes)
    _save(db, linked, bare, *notes)

    results = _recall(db)

    assert results[linked.id].retrieval_path == "fts"
    assert results[bare.id].retrieval_path == "fts"

    gain_linked = _stored(db, linked.id).stability - 0.3
    gain_bare = _stored(db, bare.id).stability - 0.3
    # Two seeds alike but for three stored links. The default bonus is 0.002
    # stability per link a memory already has when it is recalled.
    assert gain_linked - gain_bare == pytest.approx(3 * 0.002)


def test_each_recall_appends_a_version_instead_of_overwriting_the_first(tmp_path):
    db = str(tmp_path / "recall.db")
    seed = Engram(content="The lighthouse keeper logs every storm in the green ledger.")
    _save(db, seed)

    assert _recall(db)[seed.id].retrieval_path == "fts"
    first = _stored(db, seed.id).versions
    _recall(db)
    second = _stored(db, seed.id).versions

    assert [v.version_num for v in first] == [1]
    assert [v.version_num for v in second] == [1, 2]
    # Version 1 is history once written; a later recall must not rewrite it.
    assert second[0].changed_at == first[0].changed_at
