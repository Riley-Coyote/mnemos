"""Tests for EngramStore — SQLite persistence layer."""
import pytest

from mnemos.core.engram import Connection, Engram
from mnemos.core.belief import Belief
from mnemos.core.types import ConnectionRelation
from mnemos.store.sqlite_store import EngramStore


class TestEngramStore:
    """EngramStore CRUD operations."""

    def test_init_creates_tables(self, store):
        """Verify store init creates the schema (engrams, connections, beliefs, etc.)."""
        conn = store._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in rows}
        assert "engrams" in table_names
        assert "connections" in table_names
        assert "beliefs" in table_names

    def test_save_and_get_engram(self, store):
        """Round-trip save/get for an engram."""
        engram = Engram(content="Python prefers explicit over implicit")
        store.save_engram(engram)

        loaded = store.get_engram(engram.id)
        assert loaded is not None
        assert loaded.id == engram.id
        assert loaded.content == "Python prefers explicit over implicit"

    def test_fts_search(self, store):
        """Save engram, search by content via FTS5."""
        engram = Engram(content="Riley likes dark mode in all editors")
        store.save_engram(engram)

        results = store.search_fts("dark mode")
        assert len(results) >= 1
        assert any(e.id == engram.id for e in results)

    def test_save_and_get_connection(self, store):
        """Create a typed connection between two engrams and retrieve it."""
        e1 = Engram(content="First memory")
        e2 = Engram(content="Second memory")
        store.save_engram(e1)
        store.save_engram(e2)

        conn_obj = Connection(
            target_id=e2.id,
            relation=ConnectionRelation.SUPPORTS,
            strength=0.7,
        )
        store.save_connection(e1.id, conn_obj)

        connections = store.get_connections(e1.id)
        assert len(connections) >= 1
        assert connections[0].target_id == e2.id
        assert connections[0].relation == ConnectionRelation.SUPPORTS

    def test_save_and_get_belief(self, store):
        """Belief round-trip."""
        belief = Belief(
            content="Type hints improve code quality",
            confidence=0.7,
            domain="technical",
        )
        store.save_belief(belief)

        beliefs = store.get_beliefs()
        assert len(beliefs) >= 1
        assert any(b.id == belief.id for b in beliefs)

        matched = [b for b in beliefs if b.id == belief.id][0]
        assert matched.content == "Type hints improve code quality"
        assert matched.confidence == pytest.approx(0.7)

    def test_get_beliefs_excludes_pending_confidence_by_default(self, store):
        """Pending-confidence beliefs require an explicit opt-in."""
        pending = Belief(
            content="Imported belief awaits confidence review",
            confidence=0.9,
            confidence_pending_review=True,
        )
        reviewed = Belief(
            content="Reviewed belief participates in consumers",
            confidence=0.6,
        )
        store.save_belief(pending)
        store.save_belief(reviewed)

        default_beliefs = store.get_beliefs()
        assert reviewed.id in {b.id for b in default_beliefs}
        assert pending.id not in {b.id for b in default_beliefs}

        all_beliefs = store.get_beliefs(include_pending_review=True)
        assert pending.id in {b.id for b in all_beliefs}
        assert reviewed.id in {b.id for b in all_beliefs}

    def test_count_engrams(self, store):
        """Count engrams by state."""
        e1 = Engram(content="Active memory one")
        e2 = Engram(content="Active memory two")
        store.save_engram(e1)
        store.save_engram(e2)

        count = store.count_engrams()
        assert count >= 2

    def test_read_only_get_recent_opens_lazy_connection(self, tmp_db):
        writable = EngramStore(tmp_db)
        engram = Engram(content="Read-only recent memory")
        writable.save_engram(engram)
        writable.close()

        read_only = EngramStore(tmp_db, read_only=True)
        try:
            assert read_only._conn is None
            recent = read_only.get_recent_engrams(limit=10)
        finally:
            read_only.close()

        assert engram.id in {item.id for item in recent}

    def test_delete_engram(self, store):
        """Verify delete removes the engram."""
        engram = Engram(content="Temporary memory to delete")
        store.save_engram(engram)

        # Confirm it exists
        assert store.get_engram(engram.id) is not None

        store.delete_engram(engram.id)

        # Confirm it's gone
        assert store.get_engram(engram.id) is None

    def test_meta_set_get_roundtrip(self, store):
        """get_meta returns None/default when absent; set then get round-trips."""
        assert store.get_meta("nonexistent") is None
        assert store.get_meta("nonexistent", "fallback") == "fallback"

        store.set_meta("watermark", "2026-06-11T00:00:00Z")
        assert store.get_meta("watermark") == "2026-06-11T00:00:00Z"

    def test_meta_overwrite_persists_across_reopen(self, tmp_db):
        """set_meta upserts; the latest value survives a close and reopen."""
        first = EngramStore(tmp_db)
        first.set_meta("watermark", "first-value")
        first.set_meta("watermark", "second-value")
        first.close()

        reopened = EngramStore(tmp_db)
        try:
            assert reopened.get_meta("watermark") == "second-value"
            # The schema_version bookkeeping row is untouched by the meta API.
            assert reopened.get_meta("schema_version") is not None
        finally:
            reopened.close()

    def test_get_hypomnema_entries_by_tag_scoped_and_active_only(self, store):
        """Tag lookup respects scope and active filtering, newest first."""
        in_scope = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}

        tagged_one = store.write_hypomnema_entry(
            "first tagged note", tags=["dream-journal"], **in_scope
        )
        tagged_two = store.write_hypomnema_entry(
            "second tagged note", tags=["dream-journal", "continuity"], **in_scope
        )
        store.write_hypomnema_entry("untagged note", tags=["continuity"], **in_scope)
        store.write_hypomnema_entry(
            "other scope note",
            tags=["dream-journal"],
            agent_id="vektor",
            person_id="riley",
            project_scope="demo",
        )
        archived = store.write_hypomnema_entry(
            "archived tagged note", tags=["dream-journal"], **in_scope
        )
        store.archive_hypomnema_entry(archived, reason="test cleanup", **in_scope)

        entries = store.get_hypomnema_entries_by_tag("dream-journal", **in_scope)
        assert {e["id"] for e in entries} == {tagged_one, tagged_two}
        assert all(e["active"] for e in entries)
        # Newest first by last_revised_at.
        assert entries[0]["id"] == tagged_two

        including_inactive = store.get_hypomnema_entries_by_tag(
            "dream-journal", active_only=False, limit=10, **in_scope
        )
        assert {e["id"] for e in including_inactive} == {tagged_one, tagged_two, archived}

        # Quote-delimited matching keeps tags token-exact.
        assert store.get_hypomnema_entries_by_tag("dream", **in_scope) == []
