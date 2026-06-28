from copy import deepcopy

from mnemos.config.defaults import DEFAULT_CONFIG
from mnemos.inner_life.preflight import PROCESS_FAMILIES, build_inner_life_preflight
from mnemos.store.sqlite_store import EngramStore


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
    assert "inner_life_schedules_disabled" in result["blockers"]
    assert "scheduled_process_disabled" in result["blockers"]
    assert result["missing_schedule_switches"] == []
    assert result["missing_activity_switches"] == []
    for process in PROCESS_FAMILIES:
        assert result["processes"][process]["scheduled"] is False
        assert result["processes"][process]["activity_gate"] is True


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
