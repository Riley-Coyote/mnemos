"""Tests for EngramStore — SQLite persistence layer."""
import sqlite3

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
        assert "proposal_ledger" in table_names

    def test_read_visibility_schema_and_proposal_ledger_are_present(self, store):
        """Fresh stores expose the U2 read-visibility and proposal-ledger schema."""
        conn = store._get_conn()

        for table in (
            "engrams",
            "beliefs",
            "functional_memories",
        ):
            columns = _column_map(conn, table)
            assert "read_visibility" in columns
            assert columns["read_visibility"]["notnull"] == 1
            assert columns["read_visibility"]["dflt_value"] == "'operational_context'"

        hypomnema_columns = _column_map(conn, "hypomnema_entries")
        assert "read_visibility" in hypomnema_columns
        assert hypomnema_columns["read_visibility"]["notnull"] == 1
        assert hypomnema_columns["read_visibility"]["dflt_value"] == "'operational_context'"

        ledger_columns = _column_map(conn, "proposal_ledger")
        for column in (
            "id",
            "source_authority",
            "kind",
            "domain",
            "target_surface",
            "transition",
            "blast_radius",
            "read_visibility",
            "status",
            "reason",
            "gate_version",
            "provenance_ids_json",
        ):
            assert column in ledger_columns

        ledger_sql = _table_sql(conn, "proposal_ledger")
        assert "read_visibility IN ('operational_context', 'review_only', 'audit_only')" in ledger_sql
        assert "status IN ('pending_review', 'deferred', 'approved', 'rejected', 'applied', 'superseded')" in ledger_sql

    def test_save_and_get_engram(self, store):
        """Round-trip save/get for an engram."""
        engram = Engram(content="Python prefers explicit over implicit")
        store.save_engram(engram)

        loaded = store.get_engram(engram.id)
        assert loaded is not None
        assert loaded.id == engram.id
        assert loaded.content == "Python prefers explicit over implicit"
        assert loaded.read_visibility == "operational_context"

    def test_engram_upsert_preserves_stricter_read_visibility(self, store):
        review = Engram(
            id="engram-review-visibility",
            content="Review-only original marker",
            read_visibility="review_only",
        )
        audit = Engram(
            id="engram-audit-visibility",
            content="Audit-only original marker",
            read_visibility="audit_only",
        )
        operational = Engram(
            id="engram-operational-visibility",
            content="Operational original marker",
        )
        for engram in (review, audit, operational):
            store.save_engram(engram)

        store.save_engram(
            Engram(
                id=review.id,
                content="Review-only rewritten through default save marker",
            )
        )
        store.save_engram(
            Engram(
                id=audit.id,
                content="Audit-only rewritten through default save marker",
            )
        )
        store.save_engram(
            Engram(
                id=operational.id,
                content="Operational strengthened to audit-only marker",
                read_visibility="audit_only",
            )
        )

        loaded_review = store.get_engram(review.id)
        loaded_audit = store.get_engram(audit.id)
        loaded_operational = store.get_engram(operational.id)
        hidden_hits = store.search_fts("rewritten strengthened marker", limit=10)

        assert loaded_review is not None
        assert loaded_review.read_visibility == "review_only"
        assert loaded_review.content == "Review-only rewritten through default save marker"
        assert loaded_audit is not None
        assert loaded_audit.read_visibility == "audit_only"
        assert loaded_audit.content == "Audit-only rewritten through default save marker"
        assert loaded_operational is not None
        assert loaded_operational.read_visibility == "audit_only"
        assert store.get_engram(review.id, read_visibility="operational_context") is None
        assert store.get_engram(audit.id, read_visibility="operational_context") is None
        assert store.get_engram(operational.id, read_visibility="operational_context") is None
        assert hidden_hits == []

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

    def test_operational_engram_load_filters_hidden_connection_targets(self, store):
        source = Engram(content="Visible source memory")
        visible_target = Engram(content="Visible target memory")
        review_target = Engram(
            content="Review-only target memory",
            read_visibility="review_only",
        )
        audit_target = Engram(
            content="Audit-only target memory",
            read_visibility="audit_only",
        )
        for engram in (source, visible_target, review_target, audit_target):
            store.save_engram(engram)

        for target in (visible_target, review_target, audit_target):
            store.save_connection(
                source.id,
                Connection(
                    target_id=target.id,
                    relation=ConnectionRelation.SUPPORTS,
                    strength=0.7,
                ),
            )

        operational = store.get_engram(
            source.id,
            read_visibility="operational_context",
        )
        admin = store.get_engram(source.id)
        active = store.get_active_engrams(agent_id="default", load_connections=True)
        loaded_source = next(engram for engram in active if engram.id == source.id)

        assert operational is not None
        assert {conn.target_id for conn in operational.connections} == {
            visible_target.id
        }
        assert admin is not None
        assert {conn.target_id for conn in admin.connections} == {
            visible_target.id,
            review_target.id,
            audit_target.id,
        }
        assert {conn.target_id for conn in loaded_source.connections} == {
            visible_target.id
        }

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
        assert matched.read_visibility == "operational_context"

    def test_belief_upsert_preserves_stricter_read_visibility(self, store):
        review = Belief(
            id="belief-review-visibility",
            content="Review-only original marker",
            confidence=0.8,
            confidence_pending_review=True,
            read_visibility="review_only",
        )
        audit = Belief(
            id="belief-audit-visibility",
            content="Audit-only original marker",
            confidence=0.9,
            confidence_pending_review=True,
            read_visibility="audit_only",
        )
        operational = Belief(
            id="belief-operational-visibility",
            content="Operational original marker",
            confidence=0.6,
        )
        for belief in (review, audit, operational):
            store.save_belief(belief)

        store.save_belief(
            Belief(
                id=review.id,
                content="Review-only rewritten through default save marker",
                confidence=0.5,
            )
        )
        store.save_belief(
            Belief(
                id=audit.id,
                content="Audit-only rewritten through default save marker",
                confidence=0.4,
            )
        )
        store.save_belief(
            Belief(
                id=operational.id,
                content="Operational strengthened to audit-only marker",
                confidence=0.7,
                read_visibility="audit_only",
            )
        )

        beliefs_by_id = {
            belief.id: belief
            for belief in store.get_beliefs(
                include_pending_review=True,
                read_visibility=None,
            )
        }
        default_ids = {belief.id for belief in store.get_beliefs()}

        assert beliefs_by_id[review.id].read_visibility == "review_only"
        assert beliefs_by_id[review.id].content == (
            "Review-only rewritten through default save marker"
        )
        assert beliefs_by_id[audit.id].read_visibility == "audit_only"
        assert beliefs_by_id[audit.id].content == (
            "Audit-only rewritten through default save marker"
        )
        assert beliefs_by_id[operational.id].read_visibility == "audit_only"
        assert review.id not in default_ids
        assert audit.id not in default_ids
        assert operational.id not in default_ids

    def test_save_reviewed_belief_allows_explicit_visibility_promotion(self, store):
        belief = Belief(
            id="belief-reviewed-promotion",
            content="Review pending belief can be accepted.",
            confidence=0.8,
            confidence_pending_review=True,
            read_visibility="review_only",
        )
        store.save_belief(belief)

        reviewed = Belief(
            id=belief.id,
            content="Reviewed belief is operational again.",
            confidence=0.75,
            read_visibility="operational_context",
        )
        store.save_reviewed_belief(reviewed)

        loaded = [b for b in store.get_beliefs() if b.id == belief.id][0]
        assert loaded.read_visibility == "operational_context"
        assert loaded.content == "Reviewed belief is operational again."

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

    def test_pending_belief_review_excludes_audit_only_by_default(self, store):
        review_pending = Belief(
            content="Review pending belief is eligible for review",
            confidence=0.8,
            confidence_pending_review=True,
            read_visibility="review_only",
        )
        audit_pending = Belief(
            content="Audit pending belief must stay out of default review",
            confidence=0.99,
            confidence_pending_review=True,
            read_visibility="audit_only",
        )
        store.save_belief(audit_pending)
        store.save_belief(review_pending)

        default_review_ids = {
            b.id for b in store.get_beliefs(include_pending_review=True)
        }
        explicit_all_ids = {
            b.id
            for b in store.get_beliefs(
                include_pending_review=True,
                read_visibility=None,
            )
        }

        assert review_pending.id in default_review_ids
        assert audit_pending.id not in default_review_ids
        assert audit_pending.id in explicit_all_ids

    def test_proposal_ledger_accepts_rfc_authority_and_kind_axes(self, store):
        """Proposal rows preserve RFC permission-model fields and reject bad enums."""
        proposal = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            domain="identity",
            target_surface="beliefs",
            transition="propose_identity_belief",
            blast_radius="identity",
            status="pending_review",
            reason="Needs explicit human review before operational use.",
            gate_version="affmem-v1",
            target_id="belief_candidate",
            provenance_ids=["engram_source", "hypomnema_source"],
            payload={"content": "candidate belief"},
        )

        loaded = store.get_proposal(proposal["id"])

        assert loaded is not None
        assert loaded["id"] == proposal["id"]
        assert loaded["source_authority"] == "generated"
        assert loaded["kind"] == "semantic"
        assert loaded["target_surface"] == "beliefs"
        assert loaded["read_visibility"] == "audit_only"
        assert loaded["status"] == "pending_review"
        assert loaded["provenance_ids"] == ["engram_source", "hypomnema_source"]
        assert loaded["payload"] == {"content": "candidate belief"}

        alias = store.write_proposal(
            source_authority="agent_generated",
            kind="belief",
            target_surface="beliefs",
            transition="deprecated_aliases_serialize_canonically",
        )
        assert alias["source_authority"] == "generated"
        assert alias["kind"] == "semantic"

        with pytest.raises(ValueError, match="Unsupported read_visibility"):
            store.write_proposal(
                source_authority="generated",
                kind="semantic",
                target_surface="beliefs",
                transition="bad_visibility",
                read_visibility="public",
            )
        with pytest.raises(ValueError, match="Unsupported proposal status"):
            store.write_proposal(
                source_authority="generated",
                kind="semantic",
                target_surface="beliefs",
                transition="bad_status",
                status="live",
            )
        with pytest.raises(ValueError, match="Unsupported target surface"):
            store.write_proposal(
                source_authority="generated",
                kind="semantic",
                target_surface="prompt_builder",
                transition="bad_surface",
            )

    def test_proposal_ledger_defaults_to_audit_only_and_rejects_pending_operational_visibility(self, store):
        deferred = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="defer_candidate",
            status="deferred",
        )
        rejected = store.write_proposal(
            source_authority="observed",
            kind="episodic",
            target_surface="hypomnema_entries",
            transition="reject_candidate",
            status="rejected",
            read_visibility="review_only",
        )

        assert deferred["read_visibility"] == "audit_only"
        assert deferred["status"] == "deferred"
        assert rejected["read_visibility"] == "review_only"
        assert rejected["decided_at"] == rejected["updated_at"]
        assert rejected["applied_at"] is None

        with pytest.raises(ValueError, match="cannot be operational"):
            store.write_proposal(
                source_authority="generated",
                kind="semantic",
                target_surface="beliefs",
                transition="pending_operational",
                status="pending_review",
                read_visibility="operational_context",
            )

        for status in ("approved", "applied", "superseded"):
            with pytest.raises(ValueError, match="cannot be created directly"):
                store.write_proposal(
                    source_authority="generated",
                    kind="semantic",
                    target_surface="beliefs",
                    transition=f"{status}_proposal",
                    status=status,
                )

    def test_proposal_upsert_preserves_stricter_read_visibility(self, store):
        store.write_proposal(
            proposal_id="visibility_monotonic_audit",
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="audit_first",
            read_visibility="audit_only",
        )

        downgraded = store.write_proposal(
            proposal_id="visibility_monotonic_audit",
            source_authority="observed",
            kind="episodic",
            target_surface="hypomnema_entries",
            transition="review_second",
            read_visibility="review_only",
        )

        store.write_proposal(
            proposal_id="visibility_monotonic_review",
            source_authority="observed",
            kind="episodic",
            target_surface="hypomnema_entries",
            transition="review_first",
            read_visibility="review_only",
        )

        strengthened = store.write_proposal(
            proposal_id="visibility_monotonic_review",
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="audit_second",
            read_visibility="audit_only",
        )

        assert downgraded["read_visibility"] == "audit_only"
        assert strengthened["read_visibility"] == "audit_only"
        assert "visibility_monotonic_audit" not in {
            row["id"] for row in store.list_proposals()
        }

    def test_proposal_upsert_rejects_rejected_terminal_conflict(self, store):
        rejected = store.write_proposal(
            proposal_id="terminal_proposal",
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="reject_candidate",
            read_visibility="review_only",
            status="rejected",
            reason="original rejection",
            provenance_ids=["source-a"],
            payload={"content": "Rejected proposal must stay terminal."},
        )

        with pytest.raises(ValueError, match="Terminal proposal rows are immutable"):
            store.write_proposal(
                proposal_id="terminal_proposal",
                source_authority="generated",
                kind="semantic",
                target_surface="beliefs",
                transition="rewrite_as_pending",
                read_visibility="review_only",
                status="pending_review",
                reason="mutating rejection",
                provenance_ids=["source-b"],
                payload={"content": "Pending rewrite must not mutate history."},
            )

        assert rejected["status"] == "rejected"
        assert store.get_proposal("terminal_proposal") == rejected
        assert store.count_proposals() == 0
        assert "terminal_proposal" not in {
            row["id"] for row in store.list_proposals()
        }

    def test_proposal_upsert_rejects_applied_terminal_conflict(self, store):
        conn = store._get_conn()
        conn.execute(
            """
            INSERT INTO proposal_ledger (
                id, agent_id, person_id, project_scope, source_authority, kind,
                domain, target_surface, transition, blast_radius, read_visibility,
                status, reason, gate_version, target_id, provenance_ids_json,
                payload_json, created_at, updated_at, decided_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "applied_terminal_proposal",
                "default",
                "user",
                "global",
                "generated",
                "semantic",
                "identity",
                "beliefs",
                "apply_candidate",
                "identity",
                "audit_only",
                "applied",
                "original applied decision",
                "affmem-v1",
                "target-belief",
                '["source-a"]',
                '{"content":"applied audit record"}',
                100,
                110,
                120,
                130,
            ),
        )
        conn.commit()
        original = store.get_proposal("applied_terminal_proposal")

        with pytest.raises(ValueError, match="Terminal proposal rows are immutable"):
            store.write_proposal(
                proposal_id="applied_terminal_proposal",
                source_authority="observed",
                kind="episodic",
                target_surface="hypomnema_entries",
                transition="reuse_applied_id",
                read_visibility="review_only",
                status="pending_review",
                reason="new pending content",
                provenance_ids=["source-b"],
                payload={"content": "new content needs a new proposal id"},
            )

        assert store.get_proposal("applied_terminal_proposal") == original

    def test_active_proposal_listing_defaults_to_pending_review_only(self, store):
        pending = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="pending_candidate",
            read_visibility="review_only",
        )
        deferred = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="deferred_candidate",
            read_visibility="review_only",
            status="deferred",
        )
        rejected = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="rejected_candidate",
            read_visibility="review_only",
            status="rejected",
        )

        default_rows = store.list_proposals()
        explicit_history = store.list_proposals(status=None)

        assert {row["id"] for row in default_rows} == {pending["id"]}
        assert store.count_proposals() == 1
        assert {row["id"] for row in explicit_history} == {
            pending["id"],
            deferred["id"],
            rejected["id"],
        }

    def test_list_proposals_audit_visibility_requires_explicit_audit_read(self, store):
        audit = store.write_proposal(
            source_authority="generated",
            kind="semantic",
            target_surface="beliefs",
            transition="audit_candidate",
            payload={"content": "audit-only candidate prose"},
        )
        review = store.write_proposal(
            source_authority="observed",
            kind="episodic",
            target_surface="hypomnema_entries",
            transition="review_candidate",
            read_visibility="review_only",
            payload={"content": "review-only candidate prose"},
        )

        default_rows = store.list_proposals()
        audit_rows = store.list_audit_proposals()
        unfiltered_rows = store.list_proposals(read_visibility=None)

        assert [row["id"] for row in default_rows] == [review["id"]]
        assert [row["id"] for row in audit_rows] == [audit["id"]]
        assert {row["id"] for row in unfiltered_rows} == {audit["id"], review["id"]}

    def test_store_reads_filter_read_visibility_before_scoring(self, store):
        """Operational store reads exclude review-only rows before ranking/limits."""
        operational = Engram(
            content="Visible operational membrane anchor",
            read_visibility="operational_context",
        )
        review = Engram(
            content="Hidden review-only membrane anchor",
            read_visibility="review_only",
        )
        store.save_engram(review)
        store.save_engram(operational)

        operational_hits = store.search_fts("membrane anchor", limit=1)
        assert [e.id for e in operational_hits] == [operational.id]

        review_hits = store.search_fts(
            "membrane anchor",
            read_visibility="review_only",
            limit=1,
        )
        assert [e.id for e in review_hits] == [review.id]

        operational_belief = Belief(
            content="Visible operational belief",
            confidence=0.6,
            read_visibility="operational_context",
        )
        review_belief = Belief(
            content="Hidden review-only belief",
            confidence=0.99,
            read_visibility="review_only",
            confidence_pending_review=True,
        )
        store.save_belief(review_belief)
        store.save_belief(operational_belief)

        assert [b.id for b in store.get_beliefs()] == [operational_belief.id]
        assert [b.id for b in store.get_beliefs(
            read_visibility="review_only",
            include_pending_review=True,
        )] == [review_belief.id]

        operational_functional = store.write_functional_memory(
            "Visible operational functional memory",
            confidence=0.4,
            salience=0.4,
            read_visibility="operational_context",
        )
        review_functional = store.write_functional_memory(
            "Hidden review-only functional memory",
            needs_confirmation=True,
            confidence=1.0,
            salience=1.0,
            read_visibility="review_only",
        )
        assert [item["id"] for item in store.load_functional_memories(
            "functional memory",
            limit=1,
        )] == [operational_functional["id"]]
        assert [item["id"] for item in store.load_functional_memories(
            "functional memory",
            read_visibility="review_only",
            limit=1,
        )] == [review_functional["id"]]

        operational_hypo = store.write_hypomnema_entry(
            "Visible operational hypomnema anchor",
            confidence=0.4,
            salience=0.4,
            read_visibility="operational_context",
        )
        review_hypo = store.write_hypomnema_entry(
            "Hidden review-only hypomnema anchor",
            confidence=1.0,
            salience=1.0,
            read_visibility="review_only",
        )
        assert [entry["id"] for entry in store.search_hypomnema(
            "hypomnema anchor",
            limit=1,
        )] == [operational_hypo]
        assert [entry["id"] for entry in store.search_hypomnema(
            "hypomnema anchor",
            read_visibility="review_only",
            limit=1,
        )] == [review_hypo]

    def test_engram_reads_accept_multiple_explicit_visibilities(self, store):
        operational = Engram(
            content="Operational multi visibility membrane marker",
            read_visibility="operational_context",
        )
        review = Engram(
            content="Review multi visibility membrane marker",
            read_visibility="review_only",
        )
        audit = Engram(
            content="Audit multi visibility membrane marker",
            read_visibility="audit_only",
        )
        for engram in (operational, review, audit):
            store.save_engram(engram)

        active = store.get_active_engrams(
            agent_id="default",
            load_connections=False,
            read_visibility=("operational_context", "review_only"),
        )
        hits = store.search_fts(
            "multi visibility membrane",
            read_visibility=("operational_context", "review_only"),
            limit=10,
        )

        assert {engram.id for engram in active} == {operational.id, review.id}
        assert {engram.id for engram in hits} == {operational.id, review.id}
        default_active = store.get_active_engrams(
            agent_id="default",
            load_connections=False,
        )
        default_hits = store.search_fts(
            "multi visibility membrane",
            limit=10,
        )
        assert {engram.id for engram in default_active} == {operational.id}
        assert {engram.id for engram in default_hits} == {operational.id}

    def test_legacy_v5_migrates_non_candidate_hypomnema_operational_and_candidates_review_only(self, tmp_path):
        """Existing databases preserve ordinary continuity and quarantine candidates."""
        db_path = tmp_path / "legacy-v5-read-visibility.db"
        _create_legacy_v5_read_visibility_db(db_path)

        store = EngramStore(db_path)
        try:
            conn = store._get_conn()
            assert store.get_meta("schema_version") == "7"
            assert conn.execute(
                "SELECT read_visibility FROM engrams WHERE id = 'legacy_e'"
            ).fetchone()[0] == "operational_context"
            assert conn.execute(
                "SELECT read_visibility FROM beliefs WHERE id = 'legacy_pending_b'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM functional_memories WHERE id = 'legacy_f'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'legacy_h'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'legacy_low_h'"
            ).fetchone()[0] == "operational_context"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'legacy_identity_low_h'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'legacy_ordinary_h'"
            ).fetchone()[0] == "operational_context"
        finally:
            store.close()

    def test_legacy_v6_proposal_ledger_rows_normalize_to_audit_default(self, tmp_path):
        db_path = tmp_path / "legacy-v6-proposal-ledger.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE proposal_ledger (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL DEFAULT 'default',
                    person_id TEXT NOT NULL DEFAULT 'user',
                    project_scope TEXT NOT NULL DEFAULT 'global',
                    source_authority TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT 'general',
                    target_surface TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    blast_radius TEXT NOT NULL DEFAULT 'medium',
                    read_visibility TEXT NOT NULL DEFAULT 'operational_context',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    reason TEXT NOT NULL DEFAULT '',
                    gate_version TEXT NOT NULL DEFAULT 'affmem-v1',
                    target_id TEXT,
                    provenance_ids_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    decided_at INTEGER,
                    applied_at INTEGER
                );
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta(key, value) VALUES ('schema_version', '6');
                INSERT INTO proposal_ledger (
                    id, source_authority, kind, target_surface, transition,
                    blast_radius, created_at, updated_at
                ) VALUES (
                    'proposal_old_default', 'agent_generated', 'belief', 'beliefs',
                    'old_default_candidate', 'identity', 100, 100
                );
                INSERT INTO proposal_ledger (
                    id, source_authority, kind, target_surface, transition,
                    blast_radius, read_visibility, created_at, updated_at
                ) VALUES (
                    'proposal_review', 'generated', 'semantic', 'beliefs',
                    'review_candidate', 'identity', 'review_only', 200, 200
                );
                INSERT INTO proposal_ledger (
                    id, source_authority, kind, target_surface, transition,
                    blast_radius, read_visibility, status, reason, created_at,
                    updated_at, decided_at, applied_at
                ) VALUES (
                    'proposal_bad_terminal', 'unknown_authority', 'unknown_kind',
                    'beliefs', 'legacy_terminal_candidate', 'foundational',
                    'operational_context', 'applied', 'legacy applied row',
                    300, 400, 350, 375
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        store = EngramStore(db_path)
        try:
            conn = store._get_conn()
            assert store.get_meta("schema_version") == "7"
            default_row = store.get_proposal("proposal_old_default")
            review_row = store.get_proposal("proposal_review")
            terminal_row = store.get_proposal("proposal_bad_terminal")
            read_visibility_column = next(
                column
                for column in conn.execute("PRAGMA table_info(proposal_ledger)")
                if column["name"] == "read_visibility"
            )
            ledger_sql = _table_sql(conn, "proposal_ledger")
            ledger_sql_flat = " ".join(ledger_sql.split())

            assert default_row["read_visibility"] == "audit_only"
            assert default_row["source_authority"] == "generated"
            assert default_row["kind"] == "semantic"
            assert review_row["read_visibility"] == "review_only"
            assert terminal_row["source_authority"] == "generated"
            assert terminal_row["kind"] == "semantic"
            assert terminal_row["read_visibility"] == "audit_only"
            assert terminal_row["status"] == "applied"
            assert terminal_row["decided_at"] == 350
            assert terminal_row["applied_at"] == 375
            assert "legacy terminal proposal status=applied" in terminal_row["reason"]
            assert "back to pending_review" not in terminal_row["reason"]
            assert terminal_row["id"] not in {row["id"] for row in store.list_proposals()}
            assert terminal_row["id"] in {
                row["id"] for row in store.list_audit_proposals()
            }
            assert read_visibility_column["dflt_value"] == "'audit_only'"
            assert "source_authority IN ('user_stated', 'imported', 'observed', 'generated')" in ledger_sql_flat
            assert "kind IN ('episodic', 'semantic', 'procedural', 'prospective')" in ledger_sql_flat
        finally:
            store.close()

    def test_legacy_v6_hypomnema_review_default_is_repaired(self, tmp_path):
        db_path = tmp_path / "legacy-v6-hypomnema-review-default.db"
        _create_legacy_v6_hypomnema_review_default_db(db_path)

        store = EngramStore(db_path)
        try:
            conn = store._get_conn()
            read_visibility_column = next(
                column
                for column in conn.execute("PRAGMA table_info(hypomnema_entries)")
                if column["name"] == "read_visibility"
            )

            assert store.get_meta("schema_version") == "7"
            assert read_visibility_column["dflt_value"] == "'operational_context'"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_ordinary'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_defaulted_ordinary'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_candidate'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_identity'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_low_confidence_review'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_inbox_review'"
            ).fetchone()[0] == "review_only"
            assert conn.execute(
                "SELECT read_visibility FROM hypomnema_entries WHERE id = 'v6_audit'"
            ).fetchone()[0] == "audit_only"
        finally:
            store.close()

    def test_get_stats_excludes_pending_confidence_by_default(self, store):
        pending = Belief(
            content="Imported stats belief awaits confidence review",
            confidence=0.9,
            confidence_pending_review=True,
        )
        reviewed = Belief(
            content="Reviewed stats belief participates in status",
            confidence=0.6,
        )
        store.save_belief(pending)
        store.save_belief(reviewed)

        default_stats = store.get_stats()
        all_stats = store.get_stats(
            include_pending_review=True,
            read_visibility=("operational_context", "review_only"),
        )

        assert default_stats["beliefs_active"] == 1
        assert all_stats["beliefs_active"] == 2

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
        review_tagged = store.write_hypomnema_entry(
            "review tagged note",
            tags=["dream-journal"],
            read_visibility="review_only",
            **in_scope,
        )
        audit_tagged = store.write_hypomnema_entry(
            "audit tagged note",
            tags=["dream-journal"],
            read_visibility="audit_only",
            **in_scope,
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

        review_entries = store.get_hypomnema_entries_by_tag(
            "dream-journal", read_visibility="review_only", **in_scope
        )
        assert {e["id"] for e in review_entries} == {review_tagged}

        all_visible_entries = store.get_hypomnema_entries_by_tag(
            "dream-journal", read_visibility=None, limit=10, **in_scope
        )
        assert {e["id"] for e in all_visible_entries} == {
            tagged_one,
            tagged_two,
            review_tagged,
            audit_tagged,
        }

        including_inactive = store.get_hypomnema_entries_by_tag(
            "dream-journal", active_only=False, limit=10, **in_scope
        )
        assert {e["id"] for e in including_inactive} == {tagged_one, tagged_two, archived}

        # Quote-delimited matching keeps tags token-exact.
        assert store.get_hypomnema_entries_by_tag("dream", **in_scope) == []

    def test_get_hypomnema_entries_by_tag_filters_read_visibility_by_default(self, store):
        in_scope = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
        operational = store.write_hypomnema_entry(
            "operational tagged note",
            tags=["dream-journal"],
            read_visibility="operational_context",
            **in_scope,
        )
        review = store.write_hypomnema_entry(
            "review tagged note",
            tags=["dream-journal"],
            read_visibility="review_only",
            **in_scope,
        )
        audit = store.write_hypomnema_entry(
            "audit tagged note",
            tags=["dream-journal"],
            read_visibility="audit_only",
            **in_scope,
        )

        default_entries = store.get_hypomnema_entries_by_tag("dream-journal", **in_scope)
        review_entries = store.get_hypomnema_entries_by_tag(
            "dream-journal",
            read_visibility="review_only",
            **in_scope,
        )
        all_entries = store.get_hypomnema_entries_by_tag(
            "dream-journal",
            read_visibility=None,
            **in_scope,
        )

        assert [entry["id"] for entry in default_entries] == [operational]
        assert [entry["id"] for entry in review_entries] == [review]
        assert {entry["id"] for entry in all_entries} == {operational, review, audit}


def _column_map(conn, table: str):
    return {
        row["name"]: dict(row)
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    assert row is not None
    return row["sql"]


def _create_legacy_v5_read_visibility_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE engrams (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_at_encoding TEXT NOT NULL,
                impact TEXT NOT NULL DEFAULT '',
                resolution REAL NOT NULL DEFAULT 1.0,
                kind TEXT NOT NULL DEFAULT 'episodic',
                tags TEXT NOT NULL DEFAULT '[]',
                schema_refs TEXT NOT NULL DEFAULT '[]',
                strength REAL NOT NULL DEFAULT 0.5,
                stability REAL NOT NULL DEFAULT 0.1,
                accessibility REAL NOT NULL DEFAULT 0.5,
                encoding_context TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT '{}',
                lineage TEXT NOT NULL DEFAULT '{}',
                owner_agent_id TEXT NOT NULL DEFAULT 'default',
                visibility TEXT NOT NULL DEFAULT 'private',
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                reconsolidation_count INTEGER NOT NULL DEFAULT 0,
                voice_exemplar_eligible INTEGER NOT NULL DEFAULT 1
                    CHECK (voice_exemplar_eligible IN (0, 1)),
                softening_protected INTEGER NOT NULL DEFAULT 0
                    CHECK (softening_protected IN (0, 1)),
                original_substrate TEXT,
                original_timestamp INTEGER,
                consolidation_authorized INTEGER NOT NULL DEFAULT 1
                    CHECK (consolidation_authorized IN (0, 1)),
                decay_protected INTEGER NOT NULL DEFAULT 0
                    CHECK (decay_protected IN (0, 1))
            );

            CREATE TABLE beliefs (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL DEFAULT 'default',
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.3,
                domain TEXT NOT NULL DEFAULT 'general',
                created_at TEXT NOT NULL,
                last_revised TEXT NOT NULL,
                last_challenged TEXT NOT NULL,
                revision_history TEXT NOT NULL DEFAULT '[]',
                superseded_by TEXT,
                supporting_engram_ids TEXT NOT NULL DEFAULT '[]',
                tier TEXT CHECK (tier IS NULL OR tier IN ('foundational', 'operational', 'tactical')),
                needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
                confidence_pending_review INTEGER NOT NULL DEFAULT 0
                    CHECK (confidence_pending_review IN (0, 1))
            );

            CREATE TABLE hypomnema_entries (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL DEFAULT 'default',
                person_id TEXT NOT NULL DEFAULT 'user',
                project_scope TEXT NOT NULL DEFAULT 'global',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'observed'
                    CHECK (source IN ('observed', 'synthesized', 'co-formed')),
                density REAL NOT NULL DEFAULT 0.5,
                domain TEXT NOT NULL DEFAULT 'topical'
                    CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
                tags_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0.5,
                salience REAL NOT NULL DEFAULT 0.5,
                active INTEGER NOT NULL DEFAULT 1,
                foundational INTEGER NOT NULL DEFAULT 0,
                revision_count INTEGER NOT NULL DEFAULT 0,
                revisions_json TEXT NOT NULL DEFAULT '[]',
                original_timestamp INTEGER,
                related_session_id TEXT,
                related_engram_id TEXT,
                graduated_to_engram_id TEXT,
                superseded_by TEXT,
                created_at TEXT NOT NULL,
                last_revised_at TEXT NOT NULL,
                last_challenged_at TEXT
            );

            CREATE TABLE functional_memories (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                agent_id TEXT NOT NULL DEFAULT 'default',
                person_id TEXT NOT NULL DEFAULT 'user',
                project_scope TEXT NOT NULL DEFAULT 'global',
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL DEFAULT 'working'
                    CHECK (memory_type IN (
                        'working', 'preference', 'fact', 'decision', 'commitment',
                        'open_question', 'correction', 'profile', 'project'
                    )),
                confidence REAL NOT NULL DEFAULT 0.5,
                salience REAL NOT NULL DEFAULT 0.5,
                needs_confirmation INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'agent_observed',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                promoted_to_hypomnema_id TEXT
            );

            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '5');

            INSERT INTO engrams (
                id, content, content_at_encoding, impact, created_at, last_accessed
            ) VALUES (
                'legacy_e', 'legacy operational engram', 'legacy operational engram',
                '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO beliefs (
                id, agent_id, content, confidence, domain,
                created_at, last_revised, last_challenged,
                needs_review, confidence_pending_review
            ) VALUES (
                'legacy_pending_b', 'default', 'legacy pending belief', 0.9,
                'identity', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', 1, 1
            );

            INSERT INTO functional_memories (
                id, content, needs_confirmation, created_at, updated_at
            ) VALUES (
                'legacy_f', 'legacy confirmation memory', 1,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                created_at, last_revised_at
            ) VALUES (
                'legacy_h', 'legacy promotion candidate', 0.95, 0.9, 1,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                created_at, last_revised_at
            ) VALUES (
                'legacy_low_h', 'legacy ordinary hypomnema', 0.6, 0.5, 0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO hypomnema_entries (
                id, content, domain, confidence, salience, foundational,
                created_at, last_revised_at
            ) VALUES (
                'legacy_identity_low_h', 'legacy identity hypomnema', 'identity',
                0.4, 0.4, 0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                created_at, last_revised_at
            ) VALUES (
                'legacy_ordinary_h', 'legacy ordinary continuity', 0.6, 0.5, 0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _create_legacy_v6_hypomnema_review_default_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE hypomnema_entries (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL DEFAULT 'default',
                person_id TEXT NOT NULL DEFAULT 'user',
                project_scope TEXT NOT NULL DEFAULT 'global',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'observed'
                    CHECK (source IN ('observed', 'synthesized', 'co-formed')),
                density REAL NOT NULL DEFAULT 0.5,
                domain TEXT NOT NULL DEFAULT 'topical'
                    CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
                read_visibility TEXT NOT NULL DEFAULT 'review_only'
                    CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
                tags_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0.5,
                salience REAL NOT NULL DEFAULT 0.5,
                active INTEGER NOT NULL DEFAULT 1,
                foundational INTEGER NOT NULL DEFAULT 0,
                revision_count INTEGER NOT NULL DEFAULT 0,
                revisions_json TEXT NOT NULL DEFAULT '[]',
                original_timestamp INTEGER,
                related_session_id TEXT,
                related_engram_id TEXT,
                graduated_to_engram_id TEXT,
                superseded_by TEXT,
                created_at TEXT NOT NULL,
                last_revised_at TEXT NOT NULL,
                last_challenged_at TEXT
            );

            CREATE TABLE proposal_ledger (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL DEFAULT 'default',
                person_id TEXT NOT NULL DEFAULT 'user',
                project_scope TEXT NOT NULL DEFAULT 'global',
                source_authority TEXT NOT NULL,
                kind TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT 'general',
                target_surface TEXT NOT NULL,
                transition TEXT NOT NULL,
                blast_radius TEXT NOT NULL DEFAULT 'medium',
                read_visibility TEXT NOT NULL DEFAULT 'review_only',
                status TEXT NOT NULL DEFAULT 'pending_review',
                reason TEXT NOT NULL DEFAULT '',
                gate_version TEXT NOT NULL DEFAULT 'affmem-v1',
                target_id TEXT,
                provenance_ids_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                applied_at INTEGER
            );

            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '6');

            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_ordinary', 'v6 ordinary hypomnema', 0.4, 0.4, 0,
                'review_only', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                created_at, last_revised_at
            ) VALUES (
                'v6_defaulted_ordinary', 'v6 ordinary hypomnema from stale default',
                0.4, 0.4, 0, '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_candidate', 'v6 candidate hypomnema', 0.95, 0.9, 1,
                'review_only', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, domain, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_identity', 'v6 low-salience identity hypomnema', 'identity',
                0.4, 0.4, 0, 'review_only',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_low_confidence_review',
                'v6 explicit low-confidence review hypomnema',
                0.2, 0.2, 0, 'review_only',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, tags_json, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_inbox_review', 'v6 inbox review hypomnema', '["inbox"]',
                0.3, 0.3, 0, 'review_only',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO hypomnema_entries (
                id, content, confidence, salience, foundational,
                read_visibility, created_at, last_revised_at
            ) VALUES (
                'v6_audit', 'v6 explicit audit hypomnema', 0.4, 0.4, 0,
                'audit_only', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
