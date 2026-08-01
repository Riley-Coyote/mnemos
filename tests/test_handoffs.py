"""Independent coverage for agent-written session handoffs and authorship."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from mnemos.backup import create_backup, restore_backup
from mnemos.dream_journal import write_dream_entry
from mnemos.interface.context_packet import build_context_packet
from mnemos.simple_runtime import MnemosRuntime
from mnemos.simple_scope import MnemosScope
from mnemos.store.sqlite_store import EngramStore, SCHEMA_VERSION


SCOPE = {
    "agent_id": "nova",
    "person_id": "riley",
    "project_scope": "mnemos",
}


def _runtime(path: Path, **scope: str) -> MnemosRuntime:
    return MnemosRuntime(
        db_path=str(path),
        use_dedicated_model=False,
        **(scope or SCOPE),
    )


def test_exact_handoff_survives_restart_context_correction_and_backup(tmp_path):
    db_path = tmp_path / "handoff.db"
    text = "  I fixed the parser.\n\nNext: test the Windows path.  "

    first = _runtime(db_path)
    try:
        result = first.handoff(text)
        handoff_id = result.split("Handoff ID: ", 1)[1].splitlines()[0]
        stored = first._store.get_latest_handoff(**SCOPE)
        assert stored["content"] == text
        assert stored["authored_by"] == "agent"
        assert stored["author_id"] == "nova"
    finally:
        first.close()

    second = _runtime(db_path)
    try:
        packet = second.context()
        assert text in packet
        assert packet.count(text) == 1
        assert packet.index("From your previous session, in your own words.") < packet.index(
            "Continuity notes:"
        )
        surfaced = second._store.get_latest_handoff(**SCOPE)
        assert surfaced["surface_count"] == 1
        assert surfaced["last_surfaced_at"] is not None
        backup = create_backup(db_path)
        second.correct("", target_id=handoff_id, action="forget")
        assert second._store.get_latest_handoff(**SCOPE) is None
    finally:
        second.close()

    restore_backup(backup["path"], db_path, replace=True)
    restored = EngramStore(db_path)
    try:
        handoff = restored.get_latest_handoff(**SCOPE)
        assert handoff["content"] == text
        assert handoff["surface_count"] == 1
    finally:
        restored.close()


def test_supersession_is_atomic_and_history_remains_recoverable(tmp_path):
    store = EngramStore(tmp_path / "history.db")
    try:
        first = store.write_handoff("first exact note", **SCOPE)
        second = store.write_handoff("second exact note", **SCOPE)
        active = store.get_latest_handoff(**SCOPE)
        prior = store.get_hypomnema_entry(first, **SCOPE)

        assert active["id"] == second
        assert active["content"] == "second exact note"
        assert prior["active"] is False
        assert prior["superseded_by"] == second
        assert prior["content"] == "first exact note"
        count = store._get_conn().execute(
            """SELECT COUNT(*) FROM hypomnema_entries
               WHERE agent_id=? AND person_id=? AND project_scope=?
                 AND entry_kind='handoff' AND active=1""",
            tuple(SCOPE.values()),
        ).fetchone()[0]
        assert count == 1
    finally:
        store.close()


def test_handoffs_are_isolated_by_agent_person_and_project(tmp_path):
    db_path = tmp_path / "scopes.db"
    scopes = [
        {"agent_id": "nova", "person_id": "riley", "project_scope": "one"},
        {"agent_id": "nova", "person_id": "alex", "project_scope": "one"},
        {"agent_id": "luca", "person_id": "riley", "project_scope": "one"},
        {"agent_id": "nova", "person_id": "riley", "project_scope": "two"},
    ]
    store = EngramStore(db_path)
    try:
        for index, scope in enumerate(scopes):
            store.write_handoff(f"scope-{index}", **scope)
        for index, scope in enumerate(scopes):
            assert store.get_latest_handoff(**scope)["content"] == f"scope-{index}"
    finally:
        store.close()


def test_simultaneous_writers_leave_exactly_one_active_handoff(tmp_path):
    db_path = tmp_path / "concurrent.db"
    seed = EngramStore(db_path)
    seed.close()
    writers = 8
    barrier = Barrier(writers)

    def write(index: int) -> str:
        store = EngramStore(db_path)
        try:
            barrier.wait()
            return store.write_handoff(f"writer {index}\nexact", **SCOPE)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=writers) as pool:
        ids = list(pool.map(write, range(writers)))

    store = EngramStore(db_path)
    try:
        rows = store._get_conn().execute(
            """SELECT id, content, active FROM hypomnema_entries
               WHERE agent_id=? AND person_id=? AND project_scope=?
                 AND entry_kind='handoff'""",
            tuple(SCOPE.values()),
        ).fetchall()
        assert len(rows) == writers
        assert sum(int(row["active"]) for row in rows) == 1
        assert {row["id"] for row in rows} == set(ids)
        assert {row["content"] for row in rows} == {
            f"writer {index}\nexact" for index in range(writers)
        }
    finally:
        store.close()


def test_startup_packet_delivers_handoff_first_once_and_marks_delivery(tmp_path):
    store = EngramStore(tmp_path / "startup.db")
    try:
        store.write_hypomnema_entry(
            "ordinary continuity",
            authored_by="agent",
            author_id="nova",
            **SCOPE,
        )
        text = "I now understand the failure.\nNext: repair the release gate."
        store.write_handoff(text, **SCOPE)
        packet = build_context_packet(store, "continue", **SCOPE)

        assert packet["handoff"]["content"] == text
        assert all(row["entry_kind"] != "handoff" for row in packet["hypomnema"])
        assert packet["prompt"].count(text) == 1
        assert packet["prompt"].index(text) < packet["prompt"].index("### Hypomnema")
        delivered = store.get_latest_handoff(**SCOPE)
        assert delivered["surface_count"] == 1
        assert delivered["last_surfaced_at"] is not None
    finally:
        store.close()


def test_handoffs_and_maintenance_reports_never_promote(tmp_path):
    store = EngramStore(tmp_path / "promotion.db")
    scope = MnemosScope(db_path=str(tmp_path / "promotion.db"), **SCOPE)
    try:
        store.write_handoff("keep this as working state", **SCOPE)
        write_dream_entry(store, scope, "Mnemos connected two memories.")
        ordinary = store.write_hypomnema_entry(
            "stable ordinary continuity",
            authored_by="agent",
            author_id="nova",
            foundational=True,
            confidence=0.95,
            salience=0.9,
            **SCOPE,
        )
        candidates = store.get_hypomnema_promotion_candidates(**SCOPE)
        assert [item["id"] for item in candidates] == [ordinary]
    finally:
        store.close()


def test_health_reports_handoff_delivery_and_authorship(tmp_path):
    runtime = _runtime(tmp_path / "health.db")
    try:
        runtime.handoff("next session note")
        before = runtime.health()
        assert before["handoff"]["authored_by"] == "agent"
        assert before["handoff"]["delivery_count"] == 0
        assert any("handoff" in warning for warning in before["continuity"]["warnings"])

        runtime.context()
        after = runtime.health()
        assert after["handoff"]["delivery_count"] == 1
        assert after["handoff"]["last_surfaced_at"] is not None
    finally:
        runtime.close()


def test_v6_migration_backs_up_and_classifies_without_rewriting(tmp_path):
    db_path = tmp_path / "legacy.db"
    store = EngramStore(db_path)
    scope = MnemosScope(db_path=str(db_path), **SCOPE)
    try:
        observed = store.write_hypomnema_entry("You noticed this about Riley.", **SCOPE)
        coauthored = store.write_hypomnema_entry(
            "We formed this together.", source="co-formed", **SCOPE
        )
        dream = write_dream_entry(store, scope, "I connected two memories.")
    finally:
        store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX idx_hypomnema_one_active_handoff")
        for column in (
            "entry_kind",
            "authored_by",
            "author_id",
            "last_surfaced_at",
            "surface_count",
        ):
            conn.execute(f"ALTER TABLE hypomnema_entries DROP COLUMN {column}")
        conn.execute(
            "UPDATE meta SET value='6' WHERE key='schema_version'"
        )
        conn.commit()
    finally:
        conn.close()

    migrated = EngramStore(db_path)
    try:
        assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert migrated.get_hypomnema_entry(observed, **SCOPE)["authored_by"] == "unknown"
        assert migrated.get_hypomnema_entry(observed, **SCOPE)["content"] == (
            "You noticed this about Riley."
        )
        assert migrated.get_hypomnema_entry(coauthored, **SCOPE)["authored_by"] == "coauthored"
        dream_row = migrated.get_hypomnema_entry(dream, **SCOPE)
        assert dream_row["entry_kind"] == "maintenance_report"
        assert dream_row["authored_by"] == "system"
        assert dream_row["content"] == "I connected two memories."
    finally:
        migrated.close()

    backups = list((tmp_path / "backups").glob("legacy.pre-v7-*.db"))
    assert len(backups) == 1
    backup_conn = sqlite3.connect(backups[0])
    try:
        assert backup_conn.execute(
            "SELECT content FROM hypomnema_entries WHERE id=?", (observed,)
        ).fetchone()[0] == "You noticed this about Riley."
        assert backup_conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == "6"
    finally:
        backup_conn.close()
