"""RM-7 — affect recency paging primitive (rulings 004/004b).

`_recent_signal_events` used to fetch the newest `limit` rows and only then
apply the content-semantic filter chain (`_event_influences` cannot move into
SQL — event-type narrowing was declined permanently, ruling 004). A burst of
more than `limit` newer non-influencing rows inside [since, now] could evict an
older in-window turn/test/error signal and make affect skip
(`emotional-driver-filter-after-limit`).

RM-7 closes the eviction with a paging primitive: newest-first pages via the
store's (created_at, id) cursor, the unchanged Python filter per page,
accumulating until `max_events` influencing rows are collected or the window is
exhausted. These tests cover the eviction regression (red on the pre-RM-7
code), the page-boundary cursor contract, and the termination conditions.
"""

from datetime import datetime, timedelta, timezone

from mnemos.inner_life.emotional_driver import (
    _recent_signal_events,
    update_event_grounded_affect,
)
from mnemos.store.sqlite_store import EngramStore

import pytest


SCOPE = {"agent_id": "oliver", "person_id": "david", "project_scope": "pai"}


def _seed_event(
    store: EngramStore,
    key: str,
    *,
    created_at: str,
    process_name: str = "turn-finalizer",
    event_type: str = "tool_event",
    gate_decision: str = "ledger_only",
    rollout_tag: str = "rm7-test",
    metadata: dict | None = None,
) -> None:
    """Insert an inner_life_events row with an explicit created_at (the API
    stamps _utc_now(), so timestamps are forced here for deterministic order)."""
    store.upsert_inner_life_event(
        idempotency_key=key,
        event_type=event_type,
        process_name=process_name,
        gate_decision=gate_decision,
        rollout_tag=rollout_tag,
        metadata=metadata or {},
        **SCOPE,
    )
    conn = store._get_conn()
    conn.execute(
        "UPDATE inner_life_events SET created_at = ? WHERE idempotency_key = ?",
        (created_at, key),
    )
    conn.commit()


def _seed_influencing(store: EngramStore, key: str, *, created_at: str) -> None:
    # event_type turn_finalized -> _event_influences() returns user_interaction
    _seed_event(store, key, created_at=created_at, event_type="turn_finalized")


def _seed_noise(store: EngramStore, key: str, *, created_at: str) -> None:
    # tool_event / ledger_only / empty excerpt: _event_influences() returns []
    _seed_event(store, key, created_at=created_at, event_type="tool_event")


class _CountingStore:
    """Pass-through store wrapper counting get_inner_life_events pages."""

    def __init__(self, store: EngramStore) -> None:
        self._store = store
        self.pages = 0

    def get_inner_life_events(self, **kwargs):
        self.pages += 1
        return self._store.get_inner_life_events(**kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


# ── The eviction regression (red on the pre-RM-7 filter-after-limit code) ────


def test_burst_of_newer_noninfluencing_rows_does_not_evict_influencing_signal(
    tmp_path,
):
    """More than max_events newer non-influencing rows inside [since, now] used
    to fill the single recency slice and evict the older in-window influencing
    signal, so affect skipped with no_recent_events. The paging primitive keeps
    filtering into older pages until the window is exhausted."""
    store = EngramStore(tmp_path / "rm7-eviction.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        signal_at = now - timedelta(hours=6)  # inside the 24h window
        _seed_influencing(store, "signal", created_at=signal_at.isoformat())
        max_events = 5
        for i in range(4 * max_events):  # burst >> max_events, all newer
            _seed_noise(
                store,
                f"noise-{i}",
                created_at=(signal_at + timedelta(minutes=i + 1)).isoformat(),
            )

        since = now - timedelta(hours=24)
        events = _recent_signal_events(
            store,
            since=since,
            now=now,
            limit=max_events,
            **SCOPE,
        )
        assert [row["idempotency_key"] for row in events] == ["signal"]

        result = update_event_grounded_affect(
            store,
            max_events=max_events,
            now=now,
            record_decision=False,
            **SCOPE,
        )
        assert result["reason"] != "no_recent_events"
        assert "user_interaction" in result["applied_events"]
        assert result["updated"] is True
    finally:
        store.close()


# ── Page-boundary cursor contract ────────────────────────────────────────────


def test_influencing_rows_at_page_boundaries_neither_skipped_nor_duplicated(
    tmp_path,
):
    """Every row is influencing; with page_size=3 the boundaries fall mid-set.
    Paging must return each row exactly once, ascending, regardless of where
    the page cuts land."""
    store = EngramStore(tmp_path / "rm7-boundary.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        base = now - timedelta(hours=6)
        keys = [f"sig-{i}" for i in range(10)]
        for i, key in enumerate(keys):
            _seed_influencing(
                store, key, created_at=(base + timedelta(minutes=i)).isoformat()
            )

        events = _recent_signal_events(
            store,
            since=now - timedelta(hours=24),
            now=now,
            limit=50,
            page_size=3,  # 10 rows -> pages of 3/3/3/1
            **SCOPE,
        )
        assert [row["idempotency_key"] for row in events] == keys  # once each, ASC
    finally:
        store.close()


def test_page_boundary_created_at_tie_is_broken_by_id_without_skip_or_duplicate(
    tmp_path,
):
    """Rows sharing one created_at span a page boundary; the (created_at, id)
    pair cursor must neither re-fetch the boundary row nor skip its same-stamp
    siblings (a created_at-only cursor does both wrongly)."""
    store = EngramStore(tmp_path / "rm7-tie.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        stamp = (now - timedelta(hours=6)).isoformat()
        keys = [f"tie-{i}" for i in range(7)]
        for key in keys:  # identical created_at; order carried by id alone
            _seed_influencing(store, key, created_at=stamp)

        events = _recent_signal_events(
            store,
            since=now - timedelta(hours=24),
            now=now,
            limit=50,
            page_size=2,  # 7 same-stamp rows -> boundary lands inside the tie
            **SCOPE,
        )
        assert sorted(row["idempotency_key"] for row in events) == keys
        assert len({row["id"] for row in events}) == len(keys)
    finally:
        store.close()


# ── Termination conditions ───────────────────────────────────────────────────


def test_window_exhaustion_stops_paging_without_scanning_older_history(tmp_path):
    """Once a page reaches rows older than `since`, paging stops — the pre-window
    history (many pages of it) is never fetched."""
    store = EngramStore(tmp_path / "rm7-window.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        since = now - timedelta(hours=24)
        for i in range(50):  # many pages of pre-window history
            _seed_influencing(
                store,
                f"ancient-{i}",
                created_at=(since - timedelta(hours=i + 1)).isoformat(),
            )
        in_window = [f"recent-{i}" for i in range(3)]
        for i, key in enumerate(in_window):
            _seed_influencing(
                store,
                key,
                created_at=(now - timedelta(minutes=i + 1)).isoformat(),
            )

        counting = _CountingStore(store)
        events = _recent_signal_events(
            counting,  # type: ignore[arg-type]
            since=since,
            now=now,
            limit=50,
            page_size=5,
            **SCOPE,
        )
        # newest 3 are in-window; the first page already crosses `since`
        assert sorted(row["idempotency_key"] for row in events) == sorted(in_window)
        assert counting.pages == 1
    finally:
        store.close()


def test_max_events_reached_mid_page_stops_collection_and_paging(tmp_path):
    """limit influencing rows collected mid-page: collection stops at exactly
    `limit`, keeps the newest ones, and no further page is fetched."""
    store = EngramStore(tmp_path / "rm7-maxevents.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        base = now - timedelta(hours=6)
        for i in range(10):
            _seed_influencing(
                store,
                f"sig-{i}",
                created_at=(base + timedelta(minutes=i)).isoformat(),
            )

        counting = _CountingStore(store)
        events = _recent_signal_events(
            counting,  # type: ignore[arg-type]
            since=now - timedelta(hours=24),
            now=now,
            limit=3,
            page_size=100,
            **SCOPE,
        )
        # the newest 3 of the 10, ascending
        assert [row["idempotency_key"] for row in events] == [
            "sig-7",
            "sig-8",
            "sig-9",
        ]
        assert counting.pages == 1
    finally:
        store.close()


def test_empty_store_returns_no_events_and_affect_skips(tmp_path):
    store = EngramStore(tmp_path / "rm7-empty.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        events = _recent_signal_events(
            store,
            since=now - timedelta(hours=24),
            now=now,
            limit=5,
            **SCOPE,
        )
        assert events == []

        result = update_event_grounded_affect(
            store, now=now, record_decision=False, **SCOPE
        )
        assert result["updated"] is False
        assert result["reason"] == "no_recent_events"
    finally:
        store.close()


def test_all_rows_noninfluencing_pages_to_window_end_and_returns_empty(tmp_path):
    """Multiple pages of in-window noise and no signal anywhere: paging walks
    the window to exhaustion, terminates, and returns nothing."""
    store = EngramStore(tmp_path / "rm7-noise-only.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        base = now - timedelta(hours=6)
        for i in range(12):
            _seed_noise(
                store,
                f"noise-{i}",
                created_at=(base + timedelta(minutes=i)).isoformat(),
            )

        counting = _CountingStore(store)
        events = _recent_signal_events(
            counting,  # type: ignore[arg-type]
            since=now - timedelta(hours=24),
            now=now,
            limit=5,
            page_size=4,
            **SCOPE,
        )
        assert events == []
        assert counting.pages == 4  # 12 rows / 4 per page, then the short page

        result = update_event_grounded_affect(
            store, max_events=5, now=now, record_decision=False, **SCOPE
        )
        assert result["updated"] is False
        assert result["reason"] == "no_recent_events"
    finally:
        store.close()


# ── Store cursor contract ────────────────────────────────────────────────────


def test_store_cursor_requires_both_values_and_pages_strictly_older(tmp_path):
    store = EngramStore(tmp_path / "rm7-cursor.db")
    try:
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        base = now - timedelta(hours=6)
        for i in range(6):
            _seed_noise(
                store,
                f"row-{i}",
                created_at=(base + timedelta(minutes=i)).isoformat(),
            )

        with pytest.raises(ValueError):
            store.get_inner_life_events(before_created_at="2026-07-05", **SCOPE)
        with pytest.raises(ValueError):
            store.get_inner_life_events(before_id="some-id", **SCOPE)

        page1 = store.get_inner_life_events(limit=2, recent=True, **SCOPE)
        assert [r["idempotency_key"] for r in page1] == ["row-4", "row-5"]
        page2 = store.get_inner_life_events(
            limit=2,
            recent=True,
            before_created_at=str(page1[0]["created_at"]),
            before_id=str(page1[0]["id"]),
            **SCOPE,
        )
        assert [r["idempotency_key"] for r in page2] == ["row-2", "row-3"]
    finally:
        store.close()
