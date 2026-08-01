"""Production data-safety checks for backup, restore, archive, and identity."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from mnemos.backup import check_database, create_backup, restore_backup
from mnemos.consolidation.daemon import ConsolidationDaemon
from mnemos.core.engram import Engram
from mnemos.core.identity import AgentIdentity
from mnemos.store.archive import bulk_archive, get_archive_stats, resharpen
from mnemos.store.sqlite_store import EngramStore


def test_verified_backup_restore_roundtrip_preserves_previous_database(tmp_path):
    source = tmp_path / "memory.db"
    store = EngramStore(source)
    original = Engram(content="original continuity")
    store.save_engram(original)
    store.close()

    backup = create_backup(source)
    assert check_database(backup["path"])["integrity"] == "ok"

    store = EngramStore(source)
    store.save_engram(Engram(content="written after backup"))
    store.close()

    restored = restore_backup(backup["path"], source, replace=True)
    assert restored["safety_backup"]
    store = EngramStore(source)
    contents = [e.content for e in store.get_active_engrams(agent_id="default")]
    store.close()
    assert "original continuity" in contents
    assert "written after backup" not in contents
    assert check_database(restored["safety_backup"])["integrity"] == "ok"


def test_backup_is_private_and_corruption_is_rejected(tmp_path):
    source = tmp_path / "memory.db"
    store = EngramStore(source)
    store.save_engram(Engram(content="private continuity"))
    store.close()
    result = create_backup(source)
    if os.name != "nt":
        assert os.stat(result["path"]).st_mode & 0o777 == 0o600

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        check_database(corrupt)


def test_archive_can_be_restored_with_original_words(tmp_path):
    store = EngramStore(tmp_path / "memory.db")
    engram = Engram(content="original exact words", accessibility=0.1, strength=0.2)
    store.save_engram(engram)
    result = bulk_archive(store, [engram.id, "missing"], reason="test")
    assert result == {"archived": 1, "not_found": 1, "already_archived": 0}
    assert get_archive_stats(store)["by_reason"] == {"test": 1}

    restored = resharpen(store, engram.id)
    assert restored is not None
    assert restored.content == "original exact words"
    assert restored.state == "active"
    assert restored.accessibility >= 0.6
    assert store.search_fts('"original"')[0].id == engram.id
    assert get_archive_stats(store)["total_archived"] == 0
    store.close()


def test_identity_kernel_invariants_and_history_are_append_only(tmp_path):
    store = EngramStore(tmp_path / "memory.db")
    identity = AgentIdentity(invariants={"values": ["honesty"], "boundary": "no deception"})
    identity.memory_profile.agent_id = "nova"
    store.save_identity(identity)

    identity.invariants["values"].append("care")
    identity.transition_epoch("growth")
    store.save_identity(identity)

    removed = store.get_identity("nova")
    assert removed is not None
    removed.invariants["values"] = ["care"]
    with pytest.raises(ValueError, match="append-only"):
        store.save_identity(removed)

    rewritten = store.get_identity("nova")
    assert rewritten is not None
    rewritten.epoch_history = []
    with pytest.raises(ValueError, match="history"):
        store.save_identity(rewritten)
    store.close()


def test_maintenance_gate_is_scoped_per_person_and_project(tmp_path):
    store = EngramStore(tmp_path / "memory.db")
    daemon = ConsolidationDaemon(
        store, config={"consolidation": {"min_idle_minutes": 60}}
    )
    first = daemon.run_cycle(
        agent_id="shared", person_id="alice", project_scope="alpha",
        respect_gate=True,
    )
    other = daemon.run_cycle(
        agent_id="shared", person_id="bob", project_scope="beta",
        respect_gate=True,
    )
    repeated = daemon.run_cycle(
        agent_id="shared", person_id="alice", project_scope="alpha",
        respect_gate=True,
    )
    store.close()
    assert not first.get("skipped")
    assert not other.get("skipped")
    assert repeated.get("skipped") is True


def test_separate_process_style_writers_do_not_lose_memories(tmp_path):
    """Independent runtime connections may write to one WAL database."""

    path = tmp_path / "memory.db"
    EngramStore(path).close()
    writer_count = 4
    writes_per_writer = 20
    ready = Barrier(writer_count)

    def write_batch(writer: int) -> None:
        store = EngramStore(path)
        try:
            ready.wait()
            for index in range(writes_per_writer):
                store.save_engram(
                    Engram(
                        content=f"writer {writer} memory {index}",
                        owner_agent_id="nova",
                        person_id="riley",
                        project_scope="concurrency",
                    )
                )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=writer_count) as pool:
        list(pool.map(write_batch, range(writer_count)))

    store = EngramStore(path)
    try:
        memories = store.get_active_engrams(
            agent_id="nova", person_id="riley", project_scope="concurrency"
        )
        assert len(memories) == writer_count * writes_per_writer
    finally:
        store.close()
