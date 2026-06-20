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

    def test_invalid_functional_memory_type_fails(self, store):
        with pytest.raises(ValueError):
            store.write_functional_memory(
                "Invalid type",
                memory_type="multiagent",
            )
