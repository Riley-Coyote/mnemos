from copy import deepcopy

from mnemos.config.defaults import DEFAULT_CONFIG
from mnemos.inner_life.preflight import PROCESS_FAMILIES, build_inner_life_preflight
from mnemos.store.sqlite_store import EngramStore


def _enabled_schedule_config(tmp_path):
    config = deepcopy(DEFAULT_CONFIG)
    config["soak"]["tick"]["enabled"] = True
    config["soak"]["families"]["shallow_consolidation"]["enabled"] = True
    schedules = config["inner_life"]["schedules"]
    schedules["enabled"] = True
    for process in PROCESS_FAMILIES:
        schedules["processes"][process]["enabled"] = True
    config["inner_life"]["activation"]["pre_soak_snapshot_path"] = str(
        tmp_path / "pre-soak.db"
    )
    return config


def test_inner_life_preflight_defaults_block_full_scheduled_activation(tmp_path):
    db = tmp_path / "preflight.db"
    store = EngramStore(db)
    store.close()

    result = build_inner_life_preflight(
        config=deepcopy(DEFAULT_CONFIG),
        db_path=db,
    )

    assert result["ready_for_full_scheduled_activation"] is False
    assert result["db_exists"] is True
    assert result["schedules_enabled"] is False
    assert result["soak_tick_enabled"] is False
    assert "soak_tick_disabled" in result["blockers"]
    assert "soak_family_disabled" in result["blockers"]
    assert "inner_life_schedules_disabled" in result["blockers"]
    assert "scheduled_process_disabled" in result["blockers"]
    assert result["missing_schedule_switches"] == []
    assert result["missing_activity_switches"] == []
    for process in PROCESS_FAMILIES:
        assert result["processes"][process]["scheduled"] is False
        assert result["processes"][process]["activity_gate"] is True
    assert result["soak_families"]["shallow_consolidation"]["scheduled"] is False


def test_inner_life_preflight_missing_activity_kill_switch_blocks_activation(tmp_path):
    db = tmp_path / "preflight-missing.db"
    store = EngramStore(db)
    store.close()
    config = deepcopy(DEFAULT_CONFIG)
    del config["inner_life"]["activity_gate"]["processes"]["reflect"]["enabled"]

    result = build_inner_life_preflight(config=config, db_path=db)

    assert result["ready_for_full_scheduled_activation"] is False
    assert "missing_activity_gate_kill_switch" in result["blockers"]
    assert result["missing_activity_switches"] == ["reflect"]


def test_preflight_enabled_schedules_require_snapshot_and_provider_readiness(tmp_path):
    db = tmp_path / "preflight-provider.db"
    EngramStore(db).close()
    config = _enabled_schedule_config(tmp_path)

    result = build_inner_life_preflight(
        config=config,
        db_path=db,
        provider_status={
            "llm_ready": False,
            "llm_provider": None,
            "observer_reviewer_count": 0,
        },
    )

    assert result["ready_for_full_scheduled_activation"] is False
    assert "pre_soak_snapshot_missing" in result["blockers"]
    assert "llm_provider_unavailable" in result["blockers"]
    assert "observer_reviewers_unconfigured" in result["blockers"]
    assert result["provider_readiness"]["llm_ready"] is False
    assert result["pre_soak_snapshot"]["exists"] is False


def test_preflight_ready_when_enabled_snapshot_provider_and_kill_switches_exist(tmp_path):
    db = tmp_path / "preflight-ready.db"
    EngramStore(db).close()
    config = _enabled_schedule_config(tmp_path)
    snapshot = tmp_path / "pre-soak.db"
    snapshot.write_text("snapshot placeholder", encoding="utf-8")

    result = build_inner_life_preflight(
        config=config,
        db_path=db,
        provider_status={
            "llm_ready": True,
            "llm_provider": "StubClient",
            "observer_reviewer_count": 3,
        },
    )

    assert result["ready_for_full_scheduled_activation"] is True
    assert result["blockers"] == []
    assert result["pre_soak_snapshot"]["exists"] is True
    assert result["provider_readiness"]["observer_reviewer_count"] == 3
    assert result["rollback"]["disable_first"] is True
    assert result["soak_tick_enabled"] is True
    assert result["soak_families"]["shallow_consolidation"]["scheduled"] is True
