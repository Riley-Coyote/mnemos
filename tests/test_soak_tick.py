import plistlib
from copy import deepcopy
from pathlib import Path

from mnemos.config.defaults import DEFAULT_CONFIG
from mnemos.inner_life.preflight import PROCESS_FAMILIES
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
    assert Path(payload["WorkingDirectory"]).name == "mnemos-install"
    assert Path(payload["StandardOutPath"]).parent == artifact_dir
