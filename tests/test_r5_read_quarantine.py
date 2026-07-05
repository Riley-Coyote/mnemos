"""R5 read-quarantine tests (T3): the four previously-unfiltered readers now
exclude quarantined rows, and the default-None admin readers fail closed to
operational with an explicit ``read_visibility=None`` opt-in (D8-A).

Each NEG assertion below has a matching mutation proof recorded in the T3
implementation report: reverting the filter makes the NEG test go red.
"""

from __future__ import annotations

from mnemos.core.engram import Connection, Engram, VersionRef
from mnemos.core.types import ConnectionRelation
from mnemos.store.sqlite_store import EngramStore


def _store(tmp_path):
    return EngramStore(tmp_path / "r5.db")


def test_get_engram_default_is_operational_not_admin(tmp_path):
    store = _store(tmp_path)
    review = Engram(content="review-only engram", read_visibility="review_only")
    store.save_engram(review)
    # Bare default is fail-closed: a quarantined row is not returned.
    assert store.get_engram(review.id) is None


def test_get_engram_explicit_none_is_admin(tmp_path):
    store = _store(tmp_path)
    review = Engram(content="review-only engram", read_visibility="review_only")
    store.save_engram(review)
    # Explicit unfiltered opt-in still returns the row (grep-auditable admin).
    loaded = store.get_engram(review.id, read_visibility=None)
    assert loaded is not None
    assert loaded.read_visibility == "review_only"


def test_get_connected_engram_ids_excludes_quarantined(tmp_path):
    store = _store(tmp_path)
    source = Engram(content="operational source")
    visible = Engram(content="operational neighbour")
    review = Engram(content="review-only neighbour", read_visibility="review_only")
    audit = Engram(content="audit-only neighbour", read_visibility="audit_only")
    for e in (source, visible, review, audit):
        store.save_engram(e)
    for target in (visible, review, audit):
        store.save_connection(
            source.id,
            Connection(
                target_id=target.id, relation=ConnectionRelation.SUPPORTS, strength=0.7
            ),
        )
    # Default (operational) traversal excludes the quarantined neighbours.
    assert store.get_connected_engram_ids(source.id) == {visible.id}
    # Admin opt-in traverses everything.
    assert store.get_connected_engram_ids(source.id, read_visibility=None) == {
        visible.id,
        review.id,
        audit.id,
    }


def test_get_connected_engram_ids_quarantined_root_returns_empty(tmp_path):
    """Review r5-connected-root-visibility-not-checked: a quarantined root under
    the operational filter must not expand its edges — even to operational
    neighbours."""
    store = _store(tmp_path)
    review_root = Engram(content="review-only root", read_visibility="review_only")
    operational_neighbour = Engram(content="operational neighbour")
    store.save_engram(review_root)
    store.save_engram(operational_neighbour)
    store.save_connection(
        review_root.id,
        Connection(
            target_id=operational_neighbour.id,
            relation=ConnectionRelation.SUPPORTS,
            strength=0.7,
        ),
    )
    # Operational-default traversal from a quarantined root yields nothing.
    assert store.get_connected_engram_ids(review_root.id) == set()
    # Admin opt-in still traverses.
    assert operational_neighbour.id in store.get_connected_engram_ids(
        review_root.id, read_visibility=None
    )


def test_search_archive_excludes_quarantined(tmp_path):
    store = _store(tmp_path)
    operational = Engram(content="archivable operational marker text")
    review = Engram(
        content="archivable review-only marker text", read_visibility="review_only"
    )
    store.save_engram(operational)
    store.save_engram(review)
    store.archive_engram(operational)
    store.archive_engram(review)
    # Default (operational) search recovers visibility via the persistent engram
    # row and excludes the quarantined archived content.
    hits = {row["id"] for row in store.search_archive("marker")}
    assert hits == {operational.id}
    # Admin opt-in returns both.
    admin_hits = {
        row["id"] for row in store.search_archive("marker", read_visibility=None)
    }
    assert admin_hits == {operational.id, review.id}


def test_get_functional_memory_excludes_quarantined(tmp_path):
    store = _store(tmp_path)
    record = store.write_functional_memory(
        "review-only functional record",
        memory_id="r5-func",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        read_visibility="review_only",
    )
    fid = record["id"]
    # Bare default excludes the quarantined row; admin opt-in returns it.
    assert store.get_functional_memory(fid) is None
    assert store.get_functional_memory(fid, read_visibility=None) is not None


def test_get_active_engrams_admin_read_keeps_quarantined_versions(tmp_path):
    """Admin list read (read_visibility=None) returns quarantined engrams WITH
    their version histories — the engram already cleared the caller's filter,
    so its versions must not be silently emptied (review finding
    r5-active-engram-version-visibility)."""
    store = _store(tmp_path)
    review = Engram(
        content="review-only versioned engram", read_visibility="review_only"
    )
    store.save_engram(review)
    store._save_version(
        review.id,
        VersionRef(
            version_num=1, content_snapshot="v1 snapshot", resolution_at_version=1.0
        ),
    )
    engrams = store.get_active_engrams(agent_id="default", read_visibility=None)
    match = next((e for e in engrams if e.id == review.id), None)
    assert match is not None
    assert len(match.versions) >= 1


def test_reactive_retrieve_forwards_read_visibility_to_get_connections(
    tmp_path, monkeypatch
):
    """The retriever forwards its read_visibility into get_connections so admin/
    review retrieval keeps graph expansion (review finding
    r5-retriever-connection-visibility)."""
    from mnemos.retrieval.reactive import ReactiveRetriever

    store = _store(tmp_path)
    seed = Engram(content="alpha bridge seed memory")
    target = Engram(content="alpha bridge review target", read_visibility="review_only")
    store.save_engram(seed)
    store.save_engram(target)
    store.save_connection(
        seed.id,
        Connection(
            target_id=target.id, relation=ConnectionRelation.SUPPORTS, strength=0.8
        ),
    )

    seen = []
    real = store.get_connections

    def spy(engram_id, *, read_visibility="operational_context"):
        seen.append(read_visibility)
        return real(engram_id, read_visibility=read_visibility)

    monkeypatch.setattr(store, "get_connections", spy)
    ReactiveRetriever(store).retrieve("alpha bridge", read_visibility=None)
    # The admin read visibility reached get_connections (fix threads it through);
    # without the fix the retriever would call get_connections with the
    # operational default and quarantined connections would be dropped.
    assert None in seen


def test_get_versions_inherit_parent_visibility(tmp_path):
    store = _store(tmp_path)
    review = Engram(
        content="review-only versioned engram", read_visibility="review_only"
    )
    store.save_engram(review)
    store._save_version(
        review.id,
        VersionRef(
            version_num=1, content_snapshot="v1 snapshot", resolution_at_version=1.0
        ),
    )
    # Versions of a quarantined parent are not returned under the operational
    # default (fail-closed JOIN), but are under an explicit admin opt-in.
    assert store._get_versions(review.id) == []
    assert len(store._get_versions(review.id, read_visibility=None)) >= 1


# ── 007 §5.1 (T5): _get_versions / _normalize_read_visibility sequence-and-None-
# aware caller-visibility threading. get_engram gates versions by the ENGRAM's
# OWN tier (like get_active_engrams), and _get_versions accepts a sequence. ──


def test_t5_get_engram_admin_gets_versions_gated_by_engram_own_tier(tmp_path):
    """007 §5.1: get_engram(id, read_visibility=None) on a review_only engram
    returns the engram WITH its versions, gated by the engram's OWN visibility
    (not the raw None). The engram already cleared the caller's filter, so its
    history inherits its own tier. Mutation proof: threading the raw caller
    filter instead of engram.read_visibility changes which rows the JOIN gates."""
    store = _store(tmp_path)
    review = Engram(
        content="review-only versioned engram", read_visibility="review_only"
    )
    store.save_engram(review)
    store._save_version(
        review.id,
        VersionRef(
            version_num=1, content_snapshot="v1 snapshot", resolution_at_version=1.0
        ),
    )
    loaded = store.get_engram(review.id, read_visibility=None)
    assert loaded is not None
    assert len(loaded.versions) >= 1, (
        "007 §5.1: admin get_engram on a review_only engram dropped its versions"
    )


def test_t5_get_engram_operational_default_gets_own_versions(tmp_path):
    """007 §5.1: an operational engram loaded with the default filter returns its
    versions (gated by its own operational tier). Regression guard that the
    engram-own-tier gating did not break the common case."""
    store = _store(tmp_path)
    ops = Engram(content="operational versioned engram")
    store.save_engram(ops)
    store._save_version(
        ops.id,
        VersionRef(version_num=1, content_snapshot="v1", resolution_at_version=1.0),
    )
    loaded = store.get_engram(ops.id)  # default operational
    assert loaded is not None
    assert len(loaded.versions) >= 1


def test_t5_get_versions_accepts_a_sequence(tmp_path):
    """007 §5.1: _get_versions is sequence-aware — a review/admin list read may
    pass (operational, review_only) and must NOT raise (previously
    _normalize_read_visibility rejected sequences). The JOIN gates to parents
    whose visibility is IN the set."""
    store = _store(tmp_path)
    review = Engram(content="review versioned", read_visibility="review_only")
    store.save_engram(review)
    store._save_version(
        review.id,
        VersionRef(version_num=1, content_snapshot="v1", resolution_at_version=1.0),
    )
    # Sequence including the parent's tier → versions returned (no raise).
    got = store._get_versions(
        review.id, read_visibility=("operational_context", "review_only")
    )
    assert len(got) >= 1
    # Sequence excluding the parent's tier → fail-closed empty (no raise).
    empty = store._get_versions(review.id, read_visibility=("operational_context",))
    assert empty == []
