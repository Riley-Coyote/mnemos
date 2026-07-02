import plistlib
from copy import deepcopy
from pathlib import Path

from mnemos.config.defaults import DEFAULT_CONFIG
from mnemos.inner_life.preflight import PROCESS_FAMILIES
from mnemos.importer.watcher import PaiWatchDoctorCheck, PaiWatchDoctorReport
from mnemos.soak.preflight import build_soak_activation_preflight
from mnemos.soak.tick import run_scheduled_soak_tick, write_soak_tick_launchd_plist
from mnemos.store.sqlite_store import EngramStore


def _enable_tick_config(*, shallow: bool = False, inner_life: tuple[str, ...] = ()):
    config = deepcopy(DEFAULT_CONFIG)
    config["soak"]["tick"]["enabled"] = True
    config["soak"]["families"]["shallow_consolidation"]["enabled"] = shallow
    config["inner_life"]["schedules"]["enabled"] = bool(inner_life)
    for process in PROCESS_FAMILIES:
        config["inner_life"]["schedules"]["processes"][process]["enabled"] = (
            process in inner_life
        )
    return config


def _enable_full_soak_config(tmp_path):
    config = _enable_tick_config(shallow=True, inner_life=PROCESS_FAMILIES)
    snapshot = tmp_path / "pre-soak.db"
    snapshot.write_text("snapshot placeholder", encoding="utf-8")
    config["inner_life"]["activation"]["pre_soak_snapshot_path"] = str(snapshot)
    return config


def _seed_turn_signal(store: EngramStore) -> None:
    store.upsert_inner_life_event(
        idempotency_key="turn:soak:1",
        event_type="turn_finalized",
        process_name="turn-finalizer",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        content_hash="hash",
        content_excerpt="USER: continue\nASSISTANT: verified",
        event_tags=["u6.6-test"],
        source_ids=["turn-1"],
        metadata={"writes_memory": False},
        rollout_tag="u7-test",
        gate_decision="ledger_only",
    )


def test_soak_tick_disabled_skips_without_memory_or_beliefs(tmp_path):
    store = EngramStore(tmp_path / "soak-disabled.db")
    try:
        result = run_scheduled_soak_tick(
            store,
            config=deepcopy(DEFAULT_CONFIG),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="disabled",
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "soak_tick_disabled"
        assert result["families"] == []
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_soak_tick_runs_shallow_consolidation_family_without_deep_passes(tmp_path):
    store = EngramStore(tmp_path / "soak-shallow.db")
    try:
        result = run_scheduled_soak_tick(
            store,
            config=_enable_tick_config(shallow=True),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="shallow-once",
        )

        family = result["families"][0]
        assert result["status"] == "ran"
        assert family["family"] == "shallow_consolidation"
        assert family["status"] == "ran"
        assert family["details"]["cycle_type"] == "shallow"
        assert family["details"]["passes_run"] == ["connection_discovery", "decay"]
        assert "softening" not in family["details"]
        assert "belief_review" not in family["details"]
        assert "reflection" not in family["details"]
        assert store.get_beliefs(agent_id="oliver") == []

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
        )
        assert any(
            row["process_name"] == "shallow_consolidation"
            and row["event_type"] == "tool_event"
            and row["metadata"]["deep"] is False
            for row in rows
        )
    finally:
        store.close()


def test_soak_tick_fans_out_inner_life_family_through_existing_gate(tmp_path):
    store = EngramStore(tmp_path / "soak-affect.db")
    try:
        _seed_turn_signal(store)

        result = run_scheduled_soak_tick(
            store,
            config=_enable_tick_config(inner_life=("affect",)),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="affect-once",
        )

        family = result["families"][0]
        assert result["status"] == "ran"
        assert family["family"] == "affect"
        assert family["status"] == "ran"
        assert family["details"]["gate_decision"] == "run"
        assert family["details"]["generated_memory_writes"] == 0
        assert store.get_latest_emotional_state("oliver") is not None
        assert store.count_engrams(agent_id="oliver") == 0
        assert store.get_beliefs(agent_id="oliver") == []
    finally:
        store.close()


def test_soak_tick_launchd_plist_invokes_orchestrator_without_loading(tmp_path):
    db = tmp_path / "soak-plist.db"
    EngramStore(db).close()
    plist = tmp_path / "com.davidef.mnemos.soak.tick.plist"
    artifact_dir = tmp_path / "artifacts"
    python = Path.cwd() / ".venv" / "bin" / "python3"

    written = write_soak_tick_launchd_plist(
        plist_path=plist,
        db_path=db,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        rollout_tag="u7-test",
        interval_seconds=900,
        artifact_dir=artifact_dir,
    )

    assert written == plist
    payload = plistlib.loads(plist.read_bytes())
    args = payload["ProgramArguments"]
    assert args[:5] == [str(python), "-m", "mnemos.cli", "soak", "tick"]
    assert "--allow-live-db" not in args
    assert payload["StartInterval"] == 900
    assert payload["RunAtLoad"] is True
    # WorkingDirectory is the resolved repo root (worktree-agnostic; not a
    # hardcoded checkout name — report 003b). Not weakened to "any dir".
    assert Path(payload["WorkingDirectory"]).resolve() == Path.cwd().resolve()
    assert Path(payload["StandardOutPath"]).parent == artifact_dir


def test_soak_activation_preflight_blocks_without_watcher_plist_or_dry_run(tmp_path):
    db = tmp_path / "soak-preflight-blocked.db"
    EngramStore(db).close()

    result = build_soak_activation_preflight(
        config=deepcopy(DEFAULT_CONFIG),
        db_path=db,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        launchd_status={
            "checked": True,
            "pre_authorization_loaded": False,
            "labels": {},
        },
    )

    assert result["ready_for_u7_activation"] is False
    assert "soak_tick_disabled" in result["blockers"]
    assert "watch_doctor_missing" in result["blockers"]
    assert "soak_tick_plist_not_ready" in result["blockers"]
    assert "soak_tick_dry_run_missing" in result["blockers"]
    assert result["soak_tick_plist"]["exists"] is False


def test_soak_activation_preflight_ready_after_watch_plist_launchd_and_copy_tick(tmp_path):
    db = tmp_path / "soak-preflight-ready.db"
    EngramStore(db).close()
    config = _enable_full_soak_config(tmp_path)
    plist = tmp_path / "com.davidef.mnemos.soak.tick.plist"
    write_soak_tick_launchd_plist(
        plist_path=plist,
        db_path=db,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        rollout_tag="u7-test",
        interval_seconds=900,
        artifact_dir=tmp_path / "artifacts",
    )
    report = PaiWatchDoctorReport(
        checks=(
            PaiWatchDoctorCheck(
                ident="D0",
                label="watch-doctor synthetic pass",
                status="PASS",
                evidence="green",
            ),
        )
    )

    result = build_soak_activation_preflight(
        config=config,
        db_path=db,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        rollout_tag="u7-test",
        soak_plist_path=plist,
        watch_doctor_report=report,
        run_tick_dry_run=True,
        launchd_status={
            "checked": True,
            "pre_authorization_loaded": False,
            "labels": {
                "com.davidef.mnemos.duallife": {"loaded": False},
                "com.davidef.mnemos.soak.tick": {"loaded": False},
            },
        },
        provider_status={
            "llm_ready": True,
            "llm_provider": "StubClient",
            "observer_reviewer_count": 3,
        },
    )

    assert result["ready_for_u7_activation"] is True
    assert result["blockers"] == []
    assert result["watcher"]["doctor"]["ok"] is True
    assert result["soak_tick_plist"]["ok"] is True
    assert result["tick_dry_run"]["ran"] is True
    assert result["tick_dry_run"]["source_db_unchanged"] is True
    assert result["tick_dry_run"]["belief_writes"] == 0
    assert result["tick_dry_run"]["identity_patches"] == 0
    assert result["tick_dry_run"]["shared_pool_writes"] == 0
