"""Step 1 runtime receipt and origin-stamp tests."""

from __future__ import annotations

import pytest

from mnemos.core.engram import Engram, MemorySource
from mnemos.core.types import SourceAuthority, SourceType
from mnemos.encoding.encoder import Encoder
from mnemos.instrumentation.receipts import (
    IMMEDIACY_REMEMBERED,
    ORIGIN_IMPORT,
    ORIGIN_INFERENCE,
    ORIGIN_USER_WITNESSED,
    ReceiptValidationError,
)
from mnemos.store.sqlite_store import EngramStore


def test_append_receipt_round_trips_full_envelope(tmp_path):
    store = EngramStore(tmp_path / "receipts.db")
    try:
        receipt = store.append_receipt(
            kind="retrieval-why",
            actor="oliver",
            runtime="test",
            session_id="s1",
            engram_refs=["e1"],
            immediacy=IMMEDIACY_REMEMBERED,
            payload={"why": {"path": "fts"}},
        )

        rows = store.get_runtime_receipts(kind="retrieval-why")
        assert rows[0]["receipt_id"] == receipt["receipt_id"]
        assert rows[0]["actor"] == "oliver"
        assert rows[0]["engram_refs"] == ["e1"]
        assert rows[0]["payload"] == {"why": {"path": "fts"}}
    finally:
        store.close()


def test_receipts_fail_closed_for_unknown_kind_and_bad_refs(tmp_path):
    store = EngramStore(tmp_path / "receipts.db")
    try:
        with pytest.raises(ReceiptValidationError):
            store.append_receipt(
                kind="unknown-kind",
                actor="oliver",
                runtime="test",
                session_id="s1",
                engram_refs=[],
                immediacy=IMMEDIACY_REMEMBERED,
                payload={},
            )
        with pytest.raises(ReceiptValidationError):
            store.append_receipt(
                kind="retrieval-why",
                actor="oliver",
                runtime="test",
                session_id="s1",
                engram_refs="not-a-list",  # type: ignore[arg-type]
                immediacy=IMMEDIACY_REMEMBERED,
                payload={},
            )
        with pytest.raises(ReceiptValidationError):
            store.append_receipt(
                kind="retrieval-why",
                actor="oliver",
                runtime="",
                session_id="s1",
                engram_refs=[],
                immediacy=IMMEDIACY_REMEMBERED,
                payload={},
            )
        with pytest.raises(ReceiptValidationError):
            store.append_receipt(
                kind="retrieval-why",
                actor="oliver",
                runtime="test",
                session_id="s1",
                engram_refs=[],
                immediacy="surface-now",
                payload={},
            )

        assert store.get_runtime_receipts() == []
        assert not hasattr(store, "update_receipt")
        assert not hasattr(store, "delete_receipt")
    finally:
        store.close()


def test_origin_stamp_round_trips_and_stays_separate_from_authority(tmp_path):
    store = EngramStore(tmp_path / "origin.db")
    try:
        encoder = Encoder(store, llm_client=None)
        witnessed = encoder.encode(
            content="David said to keep Step 1 record-only.",
            kind="semantic",
            source=SourceType.SESSION,
            source_authority=SourceAuthority.OBSERVED,
            skip_surprise_detection=True,
        )

        imported = Engram(
            content="Imported source row",
            source=MemorySource(
                type=SourceType.EXTERNAL,
                authority=SourceAuthority.IMPORTED,
            ),
            origin_stamp=ORIGIN_IMPORT,
        )
        inferred = Engram(
            content="Generated synthesis",
            source=MemorySource(
                type=SourceType.REFLECTION,
                authority=SourceAuthority.GENERATED,
            ),
            origin_stamp=ORIGIN_INFERENCE,
        )
        legacy_unstamped = Engram(
            content="Legacy pre-instrumentation row",
            source=MemorySource(authority=SourceAuthority.OBSERVED),
        )
        store.save_engram(imported)
        store.save_engram(inferred)
        store.save_engram(legacy_unstamped)

        assert store.get_engram(witnessed.id).origin_stamp == ORIGIN_USER_WITNESSED
        assert store.get_engram(witnessed.id).source.authority == "observed"
        assert store.get_engram(imported.id).origin_stamp == ORIGIN_IMPORT
        assert store.get_engram(imported.id).source.authority == "imported"
        assert store.get_engram(inferred.id).origin_stamp == ORIGIN_INFERENCE
        assert store.get_engram(legacy_unstamped.id).origin_stamp is None

        with pytest.raises(ValueError):
            Engram(content="bad", origin_stamp="experienced")
    finally:
        store.close()


def test_origin_stamp_upsert_preserves_existing_when_incoming_stamp_is_null(tmp_path):
    store = EngramStore(tmp_path / "origin.db")
    try:
        stamped = Engram(content="Imported source row", origin_stamp=ORIGIN_IMPORT)
        store.save_engram(stamped)

        unstamped_update = Engram(
            id=stamped.id,
            content="Updated source row",
            origin_stamp=None,
        )
        store.save_engram(unstamped_update)

        assert store.get_engram(stamped.id).content == "Updated source row"
        assert store.get_engram(stamped.id).origin_stamp == ORIGIN_IMPORT

        legacy = Engram(content="Legacy row")
        store.save_engram(legacy)
        stamped_update = Engram(
            id=legacy.id,
            content="Measured later",
            origin_stamp=ORIGIN_INFERENCE,
        )
        store.save_engram(stamped_update)

        assert store.get_engram(legacy.id).origin_stamp == ORIGIN_INFERENCE
    finally:
        store.close()


def test_instrumentation_failure_counts_are_persisted_and_db_scoped(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    store_a = EngramStore(db_a)
    store_b = EngramStore(db_b)
    try:
        assert store_a.record_instrumentation_failure("retrieval_events") == 1
        assert store_a.record_instrumentation_failure("retrieval_events") == 2

        assert store_a.instrumentation_failure_counts() == {"retrieval_events": 2}
        assert store_b.instrumentation_failure_counts() == {}
        assert store_a.get_stats()["instrumentation_failures"] == 2
        assert store_b.get_stats()["instrumentation_failures"] == 0

        event = store_a.record_retrieval_event(
            actor="oliver",
            runtime="test",
            session_id="s1",
            agent_id="default",
            cue="failure visibility",
            read_visibility=None,
            max_results=5,
            surfaced_engram_ids=[],
            why={},
        )
        assert event["failure_count"] == 2
    finally:
        store_a.close()
        store_b.close()

    reopened = EngramStore(db_a)
    try:
        assert reopened.instrumentation_failure_counts() == {"retrieval_events": 2}
        assert reopened.get_stats()["instrumentation_failures"] == 2
    finally:
        reopened.close()
