import plistlib
from copy import deepcopy
from pathlib import Path

from mnemos.config.defaults import DEFAULT_CONFIG
from mnemos.inner_life.scheduler import (
    run_scheduled_inner_life_process,
    write_inner_life_launchd_plist,
)
from mnemos.store.sqlite_store import EngramStore


def _seed_signal(store: EngramStore) -> None:
    store.upsert_inner_life_event(
        idempotency_key="turn:scheduled:1",
        event_type="turn_finalized",
        process_name="turn-finalizer",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        content_hash="hash",
        content_excerpt="USER: verify\nASSISTANT: GREEN proof",
        event_tags=["u6.6-test"],
        source_ids=["turn-1"],
        metadata={"writes_memory": False},
        rollout_tag="u6.6-test",
        gate_decision="ledger_only",
    )


def test_scheduled_runner_activity_gate_skip_writes_no_memory(tmp_path):
    store = EngramStore(tmp_path / "scheduled-skip.db")
    try:
        result = run_scheduled_inner_life_process(
            store,
            process_name="affect",
            config=deepcopy(DEFAULT_CONFIG),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            run_id="skip-once",
        )

        assert result["process"] == "affect"
        assert result["status"] == "skipped"
        assert result["gate_decision"] == "skip:no_recent_activity"
        assert result["generated_memory_writes"] == 0
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_scheduled_runner_affect_runs_after_activity_gate_without_memory_write(
    tmp_path,
):
    store = EngramStore(tmp_path / "scheduled-affect.db")
    try:
        _seed_signal(store)

        result = run_scheduled_inner_life_process(
            store,
            process_name="affect",
            config=deepcopy(DEFAULT_CONFIG),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            run_id="affect-once",
        )

        assert result["process"] == "affect"
        assert result["status"] == "ran"
        assert result["gate_decision"] == "run"
        assert result["generated_memory_writes"] == 0
        assert result["identity_patches"] == 0
        assert store.get_latest_emotional_state("oliver") is not None
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_scheduled_runner_observe_skips_without_reviewers_or_memory_write(tmp_path):
    store = EngramStore(tmp_path / "scheduled-observe.db")
    try:
        _seed_signal(store)

        result = run_scheduled_inner_life_process(
            store,
            process_name="observe",
            config=deepcopy(DEFAULT_CONFIG),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
            run_id="observe-once",
        )

        assert result["process"] == "observe"
        assert result["status"] == "skipped"
        assert result["reason"] == "observer_reviewers_unconfigured"
        assert result["generated_memory_writes"] == 0
        assert result["identity_patches"] == 0
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_inner_life_launchd_plist_invokes_scheduled_runner_without_loading(tmp_path):
    db = tmp_path / "plist.db"
    EngramStore(db).close()
    plist = tmp_path / "com.davidef.mnemos.innerlife.affect.plist"
    artifact_dir = tmp_path / "artifacts"
    python = Path.cwd() / ".venv" / "bin" / "python3"

    written = write_inner_life_launchd_plist(
        plist_path=plist,
        process_name="affect",
        db_path=db,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        rollout_tag="u6.6-test",
        interval_seconds=3600,
        artifact_dir=artifact_dir,
    )

    assert written == plist
    payload = plistlib.loads(plist.read_bytes())
    args = payload["ProgramArguments"]
    assert args[:5] == [str(python), "-m", "mnemos.cli", "inner-life", "run"]
    assert "--process" in args
    assert args[args.index("--process") + 1] == "affect"
    assert "--allow-live-db" not in args
    assert payload["StartInterval"] == 3600
    assert payload["RunAtLoad"] is True
    # WorkingDirectory is the resolved repo root (worktree-agnostic; not a
    # hardcoded checkout name — report 003b). Not weakened to "any dir".
    assert Path(payload["WorkingDirectory"]).resolve() == Path.cwd().resolve()
    assert Path(payload["StandardOutPath"]).parent == artifact_dir


def test_scheduled_wander_counts_audit_only_low_stakes_write(tmp_path):
    """Finding A completeness (review 003d): the generated_memory_writes rollback
    counter must count audit_only low-stakes writes, not just operational engrams —
    otherwise a successful scheduled wander/dream reports 0 and blinds the rollout
    monitoring the gated-inner-life spec depends on."""
    from mnemos.core.engram import Engram
    from mnemos.inner_life.scheduler import _run_wander

    class _StubLLM:
        def structured_complete(self, system, user, temperature):
            return '{"thought": "a quiet grounded wandering", "origin": "authorized"}'

    store = EngramStore(tmp_path / "sched-count.db")
    try:
        store.save_engram(
            Engram(
                content="AUTHORIZED-WANDER-SEED", impact="src", owner_agent_id="oliver"
            )
        )
        result = _run_wander(store, agent_id="oliver", llm_client=_StubLLM())

        assert result["generated_memory_writes"] == 1
        # the write landed audit_only — invisible to the operational default count,
        # visible to the read_visibility=None accounting the fix now uses.
        assert store.count_engrams(agent_id="oliver") == 1  # only the operational seed
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == 2
    finally:
        store.close()
