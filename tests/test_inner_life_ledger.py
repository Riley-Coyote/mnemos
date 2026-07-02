import sqlite3

from mnemos.store.sqlite_store import EngramStore, SCHEMA_VERSION


def _tables(conn):
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_inner_life_ledger_schema_is_private_and_idempotent(tmp_path):
    store = EngramStore(tmp_path / "ledger.db")
    try:
        conn = store._get_conn()
        assert SCHEMA_VERSION >= 6
        assert "inner_life_events" in _tables(conn)

        first = store.upsert_inner_life_event(
            idempotency_key="turn:session-1:1",
            event_type="turn_finalized",
            process_name="turn-finalizer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-1",
            turn_id="1",
            role="exchange",
            content_hash="abc123",
            content_excerpt="USER: hello\nASSISTANT: hi",
            event_tags=["u6.6", "turn-event"],
            rollout_tag="u6.6-test",
            gate_decision="ledger_only",
            metadata={"writes_memory": False},
        )
        second = store.upsert_inner_life_event(
            idempotency_key="turn:session-1:1",
            event_type="turn_finalized",
            process_name="turn-finalizer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-1",
            turn_id="1",
            role="exchange",
            content_hash="abc123",
            content_excerpt="USER: hello\nASSISTANT: hi again",
            event_tags=["u6.6", "turn-event"],
            rollout_tag="u6.6-test",
            gate_decision="ledger_only",
            metadata={"writes_memory": False, "updated": True},
        )

        assert first["inserted"] is True
        assert second["inserted"] is False
        assert second["updated"] is True

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            session_id="session-1",
        )
        assert len(rows) == 1
        assert rows[0]["content_excerpt"].endswith("hi again")
        assert rows[0]["event_tags"] == ["u6.6", "turn-event"]
        assert rows[0]["metadata"]["writes_memory"] is False

        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
        assert store.search_hypomnema(
            "hello",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        ) == []
    finally:
        store.close()


def test_inner_life_ledger_migrates_schema_five_copy_without_touching_memory(tmp_path):
    db = tmp_path / "legacy-v5.db"
    store = EngramStore(db)
    try:
        store.write_hypomnema_entry(
            "approved continuity stays visible",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        store.close()

        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE meta SET value = '5' WHERE key = 'schema_version'"
            )
            conn.execute("DROP TABLE IF EXISTS inner_life_events")
            conn.commit()
        finally:
            conn.close()

        migrated = EngramStore(db)
        conn = migrated._get_conn()
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert "inner_life_events" in _tables(conn)
        entries = migrated.search_hypomnema(
            "approved continuity",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert len(entries) == 1
        assert entries[0]["content"] == "approved continuity stays visible"
    finally:
        migrated.close()
