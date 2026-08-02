"""Versioned host-mutation contract and crash/replay guarantees."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemos.simple_runtime import (
    HOST_MUTATION_PROTOCOL_VERSION,
    HostMutationConflictError,
    MnemosRuntime,
)
from mnemos.store.sqlite_store import SCHEMA_VERSION


SCOPE = {"agent_id": "hermes", "person_id": "local-owner", "project_scope": "demo"}


def runtime(db_path) -> MnemosRuntime:
    return MnemosRuntime(
        db_path=str(db_path), use_dedicated_model=False, **SCOPE
    )


def mutation(rt: MnemosRuntime, key: str, operation: str, **arguments):
    return rt.execute_host_mutation(
        operation,
        arguments,
        host_namespace="mnemos-hermes/v1",
        idempotency_key=key,
    )


def counts(db_path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("engrams", "hypomnema_entries", "host_mutations")
        }
    finally:
        conn.close()


def test_capture_replay_returns_original_result_once(tmp_path):
    db = tmp_path / "memory.db"
    rt = runtime(db)

    first = mutation(rt, "turn-42", "capture", content="Riley prefers tea.")
    replay = mutation(rt, "turn-42", "capture", content="Riley prefers tea.")

    assert first["protocol_version"] == HOST_MUTATION_PROTOCOL_VERSION
    assert first["replayed"] is False
    assert replay == {**first, "replayed": True}
    assert counts(db) == {
        "engrams": 1,
        "hypomnema_entries": 1,
        "host_mutations": 1,
    }


@pytest.mark.parametrize(
    "stage",
    ["save_engram", "write_hypomnema_entry", "mark_hypomnema_promoted"],
)
def test_capture_crash_after_each_multitable_stage_rolls_back(tmp_path, monkeypatch, stage):
    db = tmp_path / f"{stage}.db"
    rt = runtime(db)
    rt._ensure_init()
    store = rt._store
    original = getattr(store, stage)
    crashed = False

    def crash_after(*args, **kwargs):
        nonlocal crashed
        result = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError(f"crash after {stage}")
        return result

    monkeypatch.setattr(store, stage, crash_after)
    with pytest.raises(RuntimeError, match=f"crash after {stage}"):
        mutation(rt, "retry-me", "capture", content="One atomic memory.")
    assert store._transaction_depth == 0
    assert counts(db) == {
        "engrams": 0,
        "hypomnema_entries": 0,
        "host_mutations": 0,
    }

    monkeypatch.setattr(store, stage, original)
    result = mutation(rt, "retry-me", "capture", content="One atomic memory.")
    assert result["replayed"] is False
    assert counts(db) == {
        "engrams": 1,
        "hypomnema_entries": 1,
        "host_mutations": 1,
    }


def test_capture_crash_after_maintenance_rolls_everything_back(tmp_path, monkeypatch):
    db = tmp_path / "maintenance-stage.db"
    rt = runtime(db)
    original = rt.maintain

    def crash_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after maintenance")

    monkeypatch.setattr(rt, "maintain", crash_after)
    with pytest.raises(RuntimeError, match="crash after maintenance"):
        mutation(rt, "capture-maintain", "capture", content="Still atomic.")
    assert rt._store._transaction_depth == 0
    assert counts(db) == {
        "engrams": 0,
        "hypomnema_entries": 0,
        "host_mutations": 0,
    }


def test_correction_crash_restores_archived_memory(tmp_path, monkeypatch):
    db = tmp_path / "correction.db"
    rt = runtime(db)
    captured = mutation(rt, "seed", "capture", content="The launch is Monday.")
    memory_id = captured["result"].split("Memory ID: ", 1)[1].splitlines()[0]
    store = rt._store
    original = store.archive_engram

    def crash_after(*args, **kwargs):
        result = original(*args, **kwargs)
        raise RuntimeError("crash after archive")

    monkeypatch.setattr(store, "archive_engram", crash_after)
    with pytest.raises(RuntimeError, match="crash after archive"):
        mutation(
            rt,
            "correct-1",
            "correct",
            correction="The launch is Tuesday.",
            target_id=memory_id,
        )
    assert store._transaction_depth == 0
    assert store.get_engram(memory_id).state == "active"
    assert store.get_host_mutation("mnemos-hermes/v1", "correct-1") is None

    monkeypatch.setattr(store, "archive_engram", original)
    result = mutation(
        rt,
        "correct-1",
        "correct",
        correction="The launch is Tuesday.",
        target_id=memory_id,
    )
    assert "captured correction" in result["result"]


def test_reflection_crash_does_not_consume_prompt(tmp_path, monkeypatch):
    db = tmp_path / "reflection.db"
    rt = runtime(db)
    captured = mutation(rt, "seed", "capture", content="A surprising turning point.")
    memory_id = captured["result"].split("Memory ID: ", 1)[1].splitlines()[0]
    store = rt._store
    store.enqueue_reflection(
        "impact", memory_id, "What changed?", **SCOPE
    )
    pending_before = {item["id"] for item in rt.pending_reflections()}
    assert pending_before
    original = store.answer_reflection

    def crash_after(*args, **kwargs):
        result = original(*args, **kwargs)
        raise RuntimeError("crash after reflection answer")

    monkeypatch.setattr(store, "answer_reflection", crash_after)
    with pytest.raises(RuntimeError, match="crash after reflection answer"):
        mutation(rt, "reflect-1", "reflect", target_id=memory_id, text="I learned patience.")
    assert store._transaction_depth == 0
    assert store.get_host_mutation("mnemos-hermes/v1", "reflect-1") is None
    assert {item["id"] for item in rt.pending_reflections()} == pending_before


def test_host_reflection_does_not_swallow_note_projection_failure(
    tmp_path, monkeypatch
):
    db = tmp_path / "reflection-note.db"
    rt = runtime(db)
    captured = mutation(rt, "seed", "capture", content="A durable turning point.")
    memory_id = captured["result"].split("Memory ID: ", 1)[1].splitlines()[0]
    store = rt._store
    store.enqueue_reflection("impact", memory_id, "What changed?", **SCOPE)
    original = store.revise_hypomnema_entry

    def fail_projection(*args, **kwargs):
        raise RuntimeError("note projection unavailable")

    monkeypatch.setattr(store, "revise_hypomnema_entry", fail_projection)
    with pytest.raises(RuntimeError, match="note projection unavailable"):
        mutation(rt, "reflect-note", "reflect", target_id=memory_id, text="Patience.")
    assert store._transaction_depth == 0
    assert store.get_engram(memory_id).impact == ""
    assert store.get_host_mutation("mnemos-hermes/v1", "reflect-note") is None
    assert rt.pending_reflections()

    monkeypatch.setattr(store, "revise_hypomnema_entry", original)
    assert mutation(
        rt, "reflect-note", "reflect", target_id=memory_id, text="Patience."
    )["replayed"] is False


def test_introduction_replay_and_atomic_metadata(tmp_path):
    db = tmp_path / "introduction.db"
    rt = runtime(db)
    first = mutation(
        rt, "intro-1", "introduce", agent_model="test-model", agent_name="Nova"
    )
    second = mutation(
        rt, "intro-1", "introduce", agent_model="test-model", agent_name="Nova"
    )
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert rt._get_meta("agent_model") == "test-model"
    assert rt._get_meta("agent_name") == "Nova"


def test_maintenance_replay_is_one_cycle(tmp_path):
    db = tmp_path / "maintain.db"
    rt = runtime(db)
    mutation(rt, "seed", "capture", content="Keep the release evidence.")

    first = mutation(rt, "maintain-1", "maintain", deep=False)
    replay = mutation(rt, "maintain-1", "maintain", deep=False)

    assert first["replayed"] is False
    assert replay == {**first, "replayed": True}
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM host_mutations WHERE idempotency_key='maintain-1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_introduction_failure_rolls_back_all_metadata(tmp_path, monkeypatch):
    db = tmp_path / "introduction-crash.db"
    rt = runtime(db)
    rt._ensure_init()
    store = rt._store
    original = store.set_meta
    calls = 0

    def crash_after(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("crash after identity metadata")
        return result

    monkeypatch.setattr(store, "set_meta", crash_after)
    with pytest.raises(RuntimeError, match="crash after identity metadata"):
        mutation(
            rt, "intro-crash", "introduce",
            agent_model="test-model", agent_name="Nova",
        )
    assert store._transaction_depth == 0
    assert rt._get_meta("agent_model") is None
    assert rt._get_meta("agent_name") is None
    assert store.get_host_mutation("mnemos-hermes/v1", "intro-crash") is None


def test_failure_after_ledger_result_update_rolls_back_claim_and_effects(
    tmp_path, monkeypatch
):
    db = tmp_path / "ledger-complete.db"
    rt = runtime(db)
    rt._ensure_init()
    store = rt._store
    original = store.complete_host_mutation

    def crash_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after ledger completion")

    monkeypatch.setattr(store, "complete_host_mutation", crash_after)
    with pytest.raises(RuntimeError, match="crash after ledger completion"):
        mutation(rt, "ledger-last", "capture", content="No partial commit.")
    assert store._transaction_depth == 0
    assert counts(db) == {
        "engrams": 0,
        "hypomnema_entries": 0,
        "host_mutations": 0,
    }

    monkeypatch.setattr(store, "complete_host_mutation", original)
    assert mutation(
        rt, "ledger-last", "capture", content="No partial commit."
    )["replayed"] is False


def test_os_process_death_before_commit_is_replay_safe(tmp_path):
    db = tmp_path / "killed.db"
    script = r"""
import os
import sys
from mnemos.simple_runtime import MnemosRuntime

rt = MnemosRuntime(
    db_path=sys.argv[1], agent_id="hermes", person_id="local-owner",
    project_scope="demo", use_dedicated_model=False,
)
rt._ensure_init()
original = rt._store.complete_host_mutation
def die_after(**kwargs):
    original(**kwargs)
    os._exit(91)
rt._store.complete_host_mutation = die_after
rt.execute_host_mutation(
    "capture", {"content": "Survives a killed worker."},
    host_namespace="mnemos-hermes/v1", idempotency_key="killed-1",
)
"""
    child = subprocess.run([sys.executable, "-c", script, str(db)], check=False)
    assert child.returncode == 91

    rt = runtime(db)
    result = mutation(
        rt, "killed-1", "capture", content="Survives a killed worker."
    )
    assert result["replayed"] is False
    assert counts(db) == {
        "engrams": 1,
        "hypomnema_entries": 1,
        "host_mutations": 1,
    }


def test_same_key_with_different_request_or_scope_fails_closed(tmp_path):
    db = tmp_path / "conflict.db"
    rt = runtime(db)
    mutation(rt, "same-key", "capture", content="Original")

    with pytest.raises(HostMutationConflictError):
        mutation(rt, "same-key", "capture", content="Different")

    other_scope = MnemosRuntime(
        db_path=str(db), agent_id="hermes", person_id="someone-else",
        project_scope="demo", use_dedicated_model=False,
    )
    with pytest.raises(HostMutationConflictError):
        mutation(other_scope, "same-key", "capture", content="Original")


def test_two_process_like_runtimes_apply_concurrent_retry_once(tmp_path):
    db = tmp_path / "concurrent.db"
    one = runtime(db)
    two = runtime(db)
    one._ensure_init()
    two._ensure_init()

    def run(rt):
        return mutation(rt, "concurrent-1", "capture", content="Only one copy.")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (one, two)))

    assert sorted(result["replayed"] for result in results) == [False, True]
    assert counts(db) == {
        "engrams": 1,
        "hypomnema_entries": 1,
        "host_mutations": 1,
    }


def test_schema_v8_adds_ledger_without_losing_v7_handoffs(tmp_path):
    db = tmp_path / "upgrade.db"
    rt = runtime(db)
    handoff = rt.handoff("Continue with the release gate.")
    rt.close()

    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE host_mutations")
    conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    reopened = runtime(db)
    reopened._ensure_init()
    assert reopened._store.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert "release gate" in reopened.context()
    assert mutation(reopened, "post-upgrade", "introduce", agent_model="test")[
        "replayed"
    ] is False
