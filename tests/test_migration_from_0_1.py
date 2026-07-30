"""An older store must open and work, not raise at first query.

`_init_db` runs `CREATE TABLE IF NOT EXISTS`, which does nothing to a table that
already exists — so a database written by an earlier Mnemos whose `engrams`
table lacks columns added since (`resolution`, `impact`, `impact_source`,
`visibility`, `lineage`, `owner_agent_id`, `state`, …) is not upgraded. The
first `SELECT *` or `save_engram` against it then raises `OperationalError` deep
in normal use, not at open — the worst place for an upgrading user to meet it.

Only `impact` and `impact_source` had `ALTER` backfills. This test builds a
store shaped like an old one — a bare `engrams` table with just the columns that
have always existed, plus a real row — and asserts that opening it with the
current `EngramStore` reconciles the schema and that ordinary operations work
and preserve the old row.
"""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.interface.context_packet import build_context_packet
from mnemos.store.sqlite_store import EngramStore

# A plausible early engrams table: the fundamentals only, none of the columns
# added across 0.1 -> 0.2. This is what the reconciler must bring current.
_OLD_ENGRAMS_DDL = """
CREATE TABLE engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'episodic',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL
)
"""


@pytest.fixture
def old_store_path(tmp_path):
    """A database whose engrams table predates the 0.2 columns, with one row."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_ENGRAMS_DDL)
    conn.execute(
        "INSERT INTO engrams (id, content, content_at_encoding, kind, tags, "
        "created_at, last_accessed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "engram_legacy_row",
            "Riley prefers cold brew",
            "Riley prefers cold brew",
            "semantic",
            "[]",
            "2025-01-01T00:00:00+00:00",
            "2025-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    return str(path)


class TestAnOldStoreUpgradesOnOpen:
    def test_opening_reconciles_the_missing_columns(self, old_store_path):
        store = EngramStore(old_store_path)
        try:
            cols = {
                r[1]
                for r in store._get_conn().execute("PRAGMA table_info(engrams)")
            }
        finally:
            store.close()
        for expected in (
            "impact", "impact_source", "resolution", "strength", "stability",
            "accessibility", "encoding_context", "source", "lineage",
            "owner_agent_id", "visibility", "state", "access_count",
            "reconsolidation_count", "schema_refs",
        ):
            assert expected in cols, f"{expected} was not added on upgrade"

    def test_the_old_row_survives_and_reads_back(self, old_store_path):
        store = EngramStore(old_store_path)
        try:
            engrams = store.get_active_engrams(agent_id="default", limit=10)
            contents = [e.content for e in engrams]
        finally:
            store.close()
        assert "Riley prefers cold brew" in contents, (
            "the pre-existing memory was lost or unreadable after upgrade"
        )

    def test_ordinary_writes_and_reads_work_after_upgrade(self, old_store_path):
        from mnemos.core.engram import Engram

        store = EngramStore(old_store_path)
        try:
            store.save_engram(Engram(content="a new memory", impact="it changed"))
            got = store.get_engram("engram_legacy_row")
            assert got is not None and got.content == "Riley prefers cold brew"
            # The packet builder issues the queries that would raise on a
            # half-migrated table.
            packet = build_context_packet(store, query="cold brew", agent_id="default")
            assert isinstance(packet, dict)
        finally:
            store.close()
