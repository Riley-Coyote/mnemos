"""Step 3 S2 generic-save connection firewall tests."""

from __future__ import annotations

import pytest

from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation


CONNECTION_COLUMNS = (
    "source_id",
    "target_id",
    "relation",
    "strength",
    "formed_at",
    "formed_by",
    "valid_at",
    "invalid_at",
    "confidence",
    "runner_up_label",
    "runner_up_confidence",
    "classifier_version",
)


def _connection_rows(store) -> list[tuple]:
    selected = ", ".join(CONNECTION_COLUMNS)
    rows = store._get_conn().execute(
        f"SELECT {selected} FROM connections ORDER BY source_id, target_id, relation"
    )
    return [tuple(row) for row in rows.fetchall()]


def _seed_rights_row(store, source: Engram, target: Engram) -> tuple:
    row = (
        source.id,
        target.id,
        ConnectionRelation.SUPPORTS.value,
        0.81,
        "2026-07-10T12:00:00+00:00",
        "classifier",
        "2026-07-10T12:01:00+00:00",
        None,
        0.93,
        ConnectionRelation.EXTENDS.value,
        0.61,
        "classifier-v1",
    )
    placeholders = ", ".join("?" for _ in row)
    store._get_conn().execute(
        f"INSERT INTO connections ({', '.join(CONNECTION_COLUMNS)}) "
        f"VALUES ({placeholders})",
        row,
    )
    store._get_conn().commit()
    return row


def test_generic_save_never_dispatches_a_connection_write(store, monkeypatch):
    source = Engram(content="firewall source")
    target = Engram(content="firewall target")
    store.save_engram(target)
    source.add_connection(
        target.id,
        ConnectionRelation.SUPPORTS,
        strength=0.72,
        formed_by="tampered_collection",
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("generic save reached the connection writer")

    monkeypatch.setattr(store, "_save_connection_no_commit", fail_if_called)
    store.save_engram(source)

    assert store.get_engram(source.id) is not None
    assert _connection_rows(store) == []


def test_generic_save_leaves_every_connection_column_byte_identical(store):
    source = Engram(content="original source")
    target = Engram(content="classified target")
    injected = Engram(content="injected target")
    for engram in (source, target, injected):
        store.save_engram(engram)
    expected = _seed_rights_row(store, source, target)

    loaded = store.get_engram(source.id, read_visibility=None)
    assert loaded is not None
    assert len(loaded.connections) == 1
    loaded.content = "ordinary content update"
    loaded.connections[0].strength = 0.02
    loaded.connections[0].formed_by = "tampered"
    loaded.connections.append(
        Connection(
            target_id=injected.id,
            relation=ConnectionRelation.CONTRADICTS,
            strength=0.99,
            formed_by="injected",
        )
    )

    store.save_engram(loaded)

    assert _connection_rows(store) == [expected]
    assert store.search_fts("ordinary content update")[0].id == source.id


def test_inner_life_and_no_commit_routes_inherit_the_firewall(store):
    target = Engram(content="route target")
    store.save_engram(target)
    inner = Engram(content="inner-life source")
    inner.add_connection(target.id, ConnectionRelation.SUPPORTS)

    result = store.save_engram_with_inner_life_event(
        inner,
        idempotency_key="s2-inner-life-firewall",
        event_type="tool_event",
        process_name="s2-test",
    )
    direct = Engram(content="PAI no-commit source")
    direct.add_connection(target.id, ConnectionRelation.CONTRADICTS)
    conn = store._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    store._save_engram_no_commit(conn, direct)
    conn.commit()

    assert result["inserted"] is True
    assert store.get_engram(direct.id) is not None
    assert _connection_rows(store) == []


def test_add_connection_returns_the_exact_created_or_reinforced_delta():
    engram = Engram(content="delta source")

    created = engram.add_connection(
        "target",
        ConnectionRelation.CO_ACTIVATED,
        strength=0.3,
        formed_by="first",
    )
    reinforced = engram.add_connection(
        "target",
        ConnectionRelation.CO_ACTIVATED,
        strength=0.9,
        formed_by="second",
    )

    assert created is engram.connections[0]
    assert reinforced is created
    assert reinforced.strength == pytest.approx(0.4)
    assert reinforced.formed_by == "second"


def test_explicit_edge_batch_deduplicates_one_final_row_per_key(store, monkeypatch):
    source = Engram(content="explicit source")
    target = Engram(content="explicit target")
    for engram in (source, target):
        store.save_engram(engram)
    first = Connection(
        target_id=target.id,
        relation=ConnectionRelation.SUPPORTS,
        strength=0.2,
        formed_by="first",
    )
    final = Connection(
        target_id=target.id,
        relation=ConnectionRelation.SUPPORTS.value,
        strength=0.8,
        formed_by="final",
    )
    real_save = store._save_connection_no_commit
    calls = []

    def capture(conn, source_id, conn_obj, *, receipt_context=None):
        calls.append((source_id, conn_obj, receipt_context))
        return real_save(
            conn,
            source_id,
            conn_obj,
            receipt_context=receipt_context,
        )

    monkeypatch.setattr(store, "_save_connection_no_commit", capture)
    store.save_connections(source.id, [first, final])

    assert calls == [(source.id, final, None)]
    [row] = _connection_rows(store)
    assert row[3] == pytest.approx(0.8)
    assert row[5] == "final"


def test_reserved_receipt_context_is_keyword_only_and_fails_closed(store):
    source = Engram(content="receipt-context source")
    target = Engram(content="receipt-context target")
    store.save_engram(target)
    edge = Connection(target.id, ConnectionRelation.SUPPORTS)
    before = _connection_rows(store)

    with pytest.raises(ValueError, match="not active in Step 3 S2"):
        store.save_engram_with_connection_updates(
            source,
            [],
            receipt_context=object(),
        )
    with pytest.raises(ValueError, match="not active in Step 3 S2"):
        store.save_connections(source.id, [edge], receipt_context=object())
    with pytest.raises(ValueError, match="not active in Step 3 S2"):
        store.save_connections(source.id, [], receipt_context=object())
    with pytest.raises(TypeError):
        store.save_engram_with_connection_updates(source, [], object())
    with pytest.raises(ValueError, match="not active in Step 3 S2"):
        store._save_connection_no_commit(
            store._get_conn(),
            source.id,
            edge,
            receipt_context=object(),
        )

    assert store.get_engram(source.id) is None
    assert _connection_rows(store) == before


def test_explicit_edge_batch_rolls_back_after_mid_batch_failure(store, monkeypatch):
    source = Engram(content="batch source")
    first_target = Engram(content="batch target one")
    second_target = Engram(content="batch target two")
    for engram in (source, first_target, second_target):
        store.save_engram(engram)
    updates = [
        Connection(first_target.id, ConnectionRelation.SUPPORTS),
        Connection(second_target.id, ConnectionRelation.EXTENDS),
    ]
    real_save = store._save_connection_no_commit

    def fail_second(conn, source_id, conn_obj, *, receipt_context=None):
        if conn_obj.target_id == second_target.id:
            raise RuntimeError("injected second-edge failure")
        return real_save(
            conn,
            source_id,
            conn_obj,
            receipt_context=receipt_context,
        )

    monkeypatch.setattr(store, "_save_connection_no_commit", fail_second)
    with pytest.raises(RuntimeError, match="second-edge"):
        store.save_connections(source.id, updates)

    assert _connection_rows(store) == []


@pytest.mark.parametrize("combined", [False, True])
def test_explicit_seams_leave_caller_transaction_recoverable(store, combined):
    source = Engram(content="nested explicit source")
    target = Engram(content="nested explicit target")
    store.save_engram(target)
    conn = store._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO engrams_fts (id, content) VALUES (?, ?)",
        ("outer-unrelated", "outer transaction marker"),
    )
    edge = Connection(target.id, ConnectionRelation.SUPPORTS)

    with pytest.raises(RuntimeError, match="idle store transaction"):
        if combined:
            store.save_engram_with_connection_updates(source, [edge])
        else:
            store.save_connections(source.id, [edge])

    assert conn.in_transaction is True
    conn.commit()
    assert (
        conn.execute(
            "SELECT content FROM engrams_fts WHERE id = 'outer-unrelated'"
        ).fetchone()[0]
        == "outer transaction marker"
    )
    assert _connection_rows(store) == []


def test_combined_transaction_rolls_back_every_table(store, monkeypatch):
    first_target = Engram(content="combined target one")
    second_target = Engram(content="combined target two")
    for target in (first_target, second_target):
        store.save_engram(target)
    source = Engram(content="combined transaction marker")
    source.add_version(reason="s2-test")
    updates = [
        Connection(first_target.id, ConnectionRelation.SUPPORTS),
        Connection(second_target.id, ConnectionRelation.EXTENDS),
    ]
    real_save = store._save_connection_no_commit

    def fail_second(conn, source_id, conn_obj, *, receipt_context=None):
        if conn_obj.target_id == second_target.id:
            raise RuntimeError("injected combined failure")
        return real_save(
            conn,
            source_id,
            conn_obj,
            receipt_context=receipt_context,
        )

    monkeypatch.setattr(store, "_save_connection_no_commit", fail_second)
    with pytest.raises(RuntimeError, match="combined failure"):
        store.save_engram_with_connection_updates(source, updates)

    conn = store._get_conn()
    assert store.get_engram(source.id) is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM engrams_fts WHERE id = ?", (source.id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM versions WHERE engram_id = ?", (source.id,)
        ).fetchone()[0]
        == 0
    )
    assert _connection_rows(store) == []


def test_discovery_edge_batch_is_atomic_per_source(store, monkeypatch):
    from mnemos.consolidation.connection_discovery import run_connection_discovery

    class StubEmbeddingIndex:
        available = True

        def search(self, _query, k=10, exclude_ids=None):
            excluded = exclude_ids or set()
            return [
                (target.id, 0.95)
                for target in (first_target, second_target)
                if target.id not in excluded
            ][:k]

    source = Engram(content="atomic discovery source", accessibility=1.0)
    first_target = Engram(content="first distinct target", accessibility=0.4)
    second_target = Engram(content="second distinct target", accessibility=0.3)
    for engram in (source, first_target, second_target):
        store.save_engram(engram)
    real_save = store._save_connection_no_commit

    def fail_second(conn, source_id, conn_obj, *, receipt_context=None):
        if conn_obj.target_id == second_target.id:
            raise RuntimeError("injected discovery batch failure")
        return real_save(
            conn,
            source_id,
            conn_obj,
            receipt_context=receipt_context,
        )

    monkeypatch.setattr(store, "_save_connection_no_commit", fail_second)
    with pytest.raises(RuntimeError, match="discovery batch"):
        run_connection_discovery(
            store,
            embedding_index=StubEmbeddingIndex(),
            config={"max_engrams_per_discovery_pass": 1},
            llm_client=None,
        )

    assert store.get_connections(source.id) == []


def test_reconsolidation_preserves_unrelated_rights_rows(store):
    from mnemos.retrieval.reconsolidation import reconsolidate

    unrelated = Engram(content="unrelated rights source")
    unrelated_target = Engram(content="unrelated rights target")
    active = Engram(content="retrieval reconsolidation source")
    co_retrieved = Engram(content="retrieval co-retrieved target")
    for engram in (unrelated, unrelated_target, active, co_retrieved):
        store.save_engram(engram)
    rights_row = _seed_rights_row(store, unrelated, unrelated_target)

    reconsolidate(
        active,
        "retrieval test context",
        [co_retrieved.id],
        store,
        config={},
    )

    rows = _connection_rows(store)
    assert rights_row in rows
    assert any(
        row[0] == active.id
        and row[1] == co_retrieved.id
        and row[2] == ConnectionRelation.CO_ACTIVATED.value
        for row in rows
    )


def test_shared_publish_is_generic_but_conflict_resolution_is_explicit(tmp_path):
    from mnemos.multiagent.shared_pool import SharedPool

    pool = SharedPool(str(tmp_path / "shared.db"))
    try:
        stronger = Engram(content="stronger shared memory", strength=0.9)
        weaker = Engram(content="weaker shared memory", strength=0.2)
        stronger.add_connection(
            weaker.id,
            ConnectionRelation.SUPPORTS,
            strength=0.7,
            formed_by="attached_before_publish",
        )
        pool.publish(stronger)
        pool.publish(weaker)

        assert pool._store.get_connections(stronger.id) == []
        result = pool.resolve_conflict(stronger.id, weaker.id)
        [edge] = pool._store.get_connections(result["loser_id"])
        assert edge.target_id == result["winner_id"]
        assert edge.formed_by == "conflict_resolution"
    finally:
        pool.close()
