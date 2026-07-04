"""Tests for the functional memory layer."""

import pytest


class TestFunctionalMemoryStore:
    def test_init_creates_functional_memory_tables(self, store):
        conn = store._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in rows}

        assert "memory_sessions" in table_names
        assert "functional_memories" in table_names

    def test_start_session_and_write_functional_memory(self, store):
        session = store.start_memory_session(
            session_id="session-1",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            title="Turnkey memory build",
        )
        memory = store.write_functional_memory(
            "Riley wants multi-agent memory held for a separate design pass.",
            session_id=session["id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            memory_type="decision",
            confidence=0.95,
            salience=0.9,
            pinned=True,
        )

        results = store.load_functional_memories(
            "multi agent",
            session_id="session-1",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )

        assert memory["id"] == results[0]["id"]
        assert results[0]["memory_type"] == "decision"
        assert results[0]["pinned"] is True

    def test_functional_memory_review_queue(self, store):
        store.write_functional_memory(
            "Confirm whether the substrate should run by default.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            memory_type="open_question",
            needs_confirmation=True,
        )
        store.write_functional_memory(
            "This item does not need review.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )

        queue = store.load_functional_memories(
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation_only=True,
        )

        assert len(queue) == 1
        assert queue[0]["memory_type"] == "open_question"
        assert queue[0]["needs_confirmation"] is True

    def test_confirmation_queue_excludes_audit_only_by_default(self, store):
        operational = store.write_functional_memory(
            "Operational confirmation can enter the default review queue.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            read_visibility="operational_context",
        )
        review = store.write_functional_memory(
            "Review-only confirmation can enter the default review queue.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            read_visibility="review_only",
        )
        audit = store.write_functional_memory(
            "Audit-only confirmation requires an explicit audit read.",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            read_visibility="audit_only",
        )

        default_ids = {
            item["id"]
            for item in store.load_functional_memories(
                agent_id="vektor",
                person_id="riley",
                project_scope="mnemos",
                needs_confirmation_only=True,
            )
        }
        audit_ids = {
            item["id"]
            for item in store.load_functional_memories(
                agent_id="vektor",
                person_id="riley",
                project_scope="mnemos",
                needs_confirmation_only=True,
                read_visibility="audit_only",
            )
        }
        all_ids = {
            item["id"]
            for item in store.load_functional_memories(
                agent_id="vektor",
                person_id="riley",
                project_scope="mnemos",
                needs_confirmation_only=True,
                read_visibility=None,
            )
        }
        default_stats = store.get_functional_stats(
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            read_visibility=("operational_context", "review_only"),
        )

        assert default_ids == {operational["id"], review["id"]}
        assert audit_ids == {audit["id"]}
        assert all_ids == {operational["id"], review["id"], audit["id"]}
        assert default_stats["functional_needs_confirmation"] == 2

    @pytest.mark.parametrize(
        (
            "memory_id",
            "initial_visibility",
            "incoming_visibility",
            "expected_visibility",
        ),
        [
            ("review-functional-default", "review_only", None, "review_only"),
            ("audit-functional-default", "audit_only", None, "audit_only"),
            (
                "review-functional-explicit-operational",
                "review_only",
                "operational_context",
                "review_only",
            ),
            (
                "audit-functional-explicit-operational",
                "audit_only",
                "operational_context",
                "audit_only",
            ),
            (
                "operational-functional-explicit-review",
                "operational_context",
                "review_only",
                "review_only",
            ),
        ],
    )
    def test_functional_upsert_preserves_or_strengthens_read_visibility(
        self,
        store,
        memory_id,
        initial_visibility,
        incoming_visibility,
        expected_visibility,
    ):
        original = store.write_functional_memory(
            f"{memory_id} before ordinary update.",
            memory_id=memory_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=initial_visibility == "review_only",
            read_visibility=initial_visibility,
        )

        kwargs = {}
        if incoming_visibility is not None:
            kwargs["read_visibility"] = incoming_visibility
        store.write_functional_memory(
            f"{memory_id} after ordinary update.",
            memory_id=original["id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=False,
            **kwargs,
        )

        # Inspect the stored visibility of a possibly-quarantined row: admin
        # opt-in under R5/D8-A. The operational membership check below stays
        # on the default filter.
        updated = store.get_functional_memory(original["id"], read_visibility=None)
        operational_ids = {
            item["id"]
            for item in store.load_functional_memories(
                memory_id,
                agent_id="vektor",
                person_id="riley",
                project_scope="mnemos",
            )
        }

        assert updated["read_visibility"] == expected_visibility
        assert original["id"] not in operational_ids

    @pytest.mark.parametrize(
        ("initial_visibility", "queue_visibility"),
        [
            ("review_only", None),
            ("audit_only", "audit_only"),
        ],
    )
    def test_functional_upsert_preserves_confirmation_for_quarantined_rows(
        self,
        store,
        initial_visibility,
        queue_visibility,
    ):
        original = store.write_functional_memory(
            f"{initial_visibility} confirmation before ordinary update.",
            memory_id=f"{initial_visibility}-confirmation",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            read_visibility=initial_visibility,
        )

        store.write_functional_memory(
            f"{initial_visibility} confirmation after ordinary update.",
            memory_id=original["id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=False,
            read_visibility="operational_context",
        )

        # Admin inspection of the quarantined row's stored state (R5/D8-A).
        updated = store.get_functional_memory(original["id"], read_visibility=None)
        kwargs = {}
        if queue_visibility is not None:
            kwargs["read_visibility"] = queue_visibility
        queue_ids = {
            item["id"]
            for item in store.load_functional_memories(
                agent_id="vektor",
                person_id="riley",
                project_scope="mnemos",
                needs_confirmation_only=True,
                **kwargs,
            )
        }

        assert updated["read_visibility"] == initial_visibility
        assert updated["needs_confirmation"] is True
        assert original["id"] in queue_ids

    def test_functional_upsert_can_clear_operational_confirmation(self, store):
        original = store.write_functional_memory(
            "Operational confirmation before ordinary update.",
            memory_id="operational-confirmation",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            read_visibility="operational_context",
        )

        store.write_functional_memory(
            "Operational confirmation after ordinary update.",
            memory_id=original["id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=False,
            read_visibility="operational_context",
        )

        updated = store.get_functional_memory(original["id"])

        assert updated["read_visibility"] == "operational_context"
        assert updated["needs_confirmation"] is False

    def test_close_session_promotes_functional_context_to_hypomnema(self, store):
        store.start_memory_session(
            session_id="session-2",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            title="Functional memory test",
        )
        store.write_functional_memory(
            "The turnkey stack should include functional memory, hypomnema, Mnemos, and visibility.",
            session_id="session-2",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            memory_type="decision",
            confidence=0.9,
            salience=0.85,
        )

        result = store.close_session_to_hypomnema(
            "session-2",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )
        entry = store.get_hypomnema_entry(
            result["hypomnema_id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )
        remaining = store.load_functional_memories(
            session_id="session-2",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )

        assert result["functional_memories"] == 1
        assert entry["source"] == "synthesized"
        assert entry["related_session_id"] == "session-2"
        assert remaining == []

    def test_close_session_preserves_review_only_functional_memories(self, store):
        store.start_memory_session(
            session_id="session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            title="Functional memory review test",
        )
        operational = store.write_functional_memory(
            "Operational session memory can be promoted.",
            session_id="session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            confidence=0.9,
            salience=0.85,
            read_visibility="operational_context",
        )
        review = store.write_functional_memory(
            "Review-only session memory must remain active.",
            session_id="session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            needs_confirmation=True,
            confidence=0.95,
            salience=0.9,
            read_visibility="review_only",
        )

        result = store.close_session_to_hypomnema(
            "session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )
        remaining_review = store.load_functional_memories(
            session_id="session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            read_visibility="review_only",
            limit=10,
        )
        all_rows = store.load_functional_memories(
            session_id="session-review",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            include_deleted=True,
            read_visibility=None,
            limit=10,
        )

        by_id = {item["id"]: item for item in all_rows}
        assert result["functional_memories"] == 1
        assert [item["id"] for item in remaining_review] == [review["id"]]
        assert by_id[operational["id"]]["is_deleted"] is True
        assert (
            by_id[operational["id"]]["promoted_to_hypomnema_id"]
            == result["hypomnema_id"]
        )
        assert by_id[review["id"]]["is_deleted"] is False
        assert by_id[review["id"]]["promoted_to_hypomnema_id"] is None

    def test_close_session_review_worthy_synthesis_keeps_source_memory_active(
        self, store
    ):
        store.start_memory_session(
            session_id="session-high-blast",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            title="High blast functional memory test",
        )
        source = store.write_functional_memory(
            "Riley always wants identity-memory summaries reviewed before becoming durable.",
            session_id="session-high-blast",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            memory_type="preference",
            confidence=0.95,
            salience=0.9,
            read_visibility="operational_context",
        )

        result = store.close_session_to_hypomnema(
            "session-high-blast",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )
        entry = store.get_hypomnema_entry(
            result["hypomnema_id"],
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            read_visibility=None,
        )
        all_rows = store.load_functional_memories(
            session_id="session-high-blast",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
            include_deleted=True,
            read_visibility=None,
            limit=10,
        )
        source_after = {item["id"]: item for item in all_rows}[source["id"]]
        operational_entries = store.search_hypomnema(
            "identity-memory summaries",
            agent_id="vektor",
            person_id="riley",
            project_scope="mnemos",
        )

        assert entry["read_visibility"] == "review_only"
        assert source_after["is_deleted"] is False
        assert source_after["promoted_to_hypomnema_id"] is None
        assert operational_entries == []

    def test_invalid_functional_memory_type_fails(self, store):
        with pytest.raises(ValueError):
            store.write_functional_memory(
                "Invalid type",
                memory_type="multiagent",
            )
