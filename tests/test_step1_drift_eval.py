"""Step 1 drift-eval plumbing tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from mnemos.cli import main
from mnemos.instrumentation.drift_eval import (
    DAY_ONE_INSTRUMENTS,
    FUTURE_INSTRUMENTS,
    record_instrument_registry,
    record_retrieval_benchmark_metrics,
)
from mnemos.store.sqlite_store import EngramStore


def _load_retrieval_benchmark():
    path = (
        Path(__file__).resolve().parents[1]
        / "benchmarks"
        / "retrieval_benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("retrieval_benchmark_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drift_eval_cli_requires_representative_db(capsys):
    result = main(["drift-eval"])
    err = capsys.readouterr().err

    assert result == 1
    assert "mnemos drift-eval requires --db-path" in err


def test_drift_eval_cli_refuses_default_live_db_without_override(capsys):
    result = main(
        [
            "drift-eval",
            "--db-path",
            str(Path("~/.mnemos/memory.db").expanduser()),
        ]
    )
    err = capsys.readouterr().err

    assert result == 1
    assert "mnemos drift-eval refuses live Mnemos databases" in err


def test_retrieval_benchmark_record_db_refuses_live_paths_before_work(
    tmp_path, monkeypatch, capsys
):
    benchmark = _load_retrieval_benchmark()

    def fail_benchmark_work():
        raise AssertionError("benchmark work should not run")

    monkeypatch.setattr(benchmark, "_db_path_requires_live_override", lambda _path: True)
    monkeypatch.setattr(benchmark, "run_grid", fail_benchmark_work)
    monkeypatch.setattr(benchmark, "run_drift", fail_benchmark_work)
    monkeypatch.setattr(
        sys,
        "argv",
        ["retrieval_benchmark.py", "--record-db", str(tmp_path / "memory.db")],
    )

    result = benchmark.main()
    captured = capsys.readouterr()

    assert result == 1
    assert captured.out == ""
    assert "retrieval_benchmark.py --record-db refuses live Mnemos databases" in (
        captured.err
    )


def test_drift_eval_cli_registers_against_explicit_db(tmp_path, capsys):
    db = tmp_path / "drift-cli.db"

    result = main(["drift-eval", "--db-path", str(db), "--json"])
    out = capsys.readouterr().out

    assert result == 0
    rows = json.loads(out)
    assert {row["instrument_name"] for row in rows} == (
        set(DAY_ONE_INSTRUMENTS) | set(FUTURE_INSTRUMENTS)
    )
    assert db.exists()


def test_drift_eval_registry_records_active_and_inactive_instruments(tmp_path):
    store = EngramStore(tmp_path / "drift.db")
    try:
        rows = record_instrument_registry(store)

        assert {row["instrument_name"] for row in rows} == (
            set(DAY_ONE_INSTRUMENTS) | set(FUTURE_INSTRUMENTS)
        )
        assert any(row["active"] for row in rows)
        assert any(not row["active"] for row in rows)

        stored = store.get_drift_eval_runs(limit=50)
        by_name = {row["instrument_name"]: row for row in stored}
        assert by_name["citation-mass-concentration"]["active"] is True
        assert by_name["affect-entropy"]["active"] is False
        assert by_name["affect-entropy"]["status"] == "registered_inactive"

        observation = store.record_drift_eval_observation(
            run_id=by_name["citation-mass-concentration"]["run_id"],
            metric_name="top_engram_share",
            metric_value=0.4,
            metadata={"source": "test"},
        )
        assert store.get_drift_eval_observations()[0]["observation_id"] == (
            observation["observation_id"]
        )

        metric_run = record_retrieval_benchmark_metrics(
            store,
            metrics={"p@5": 0.7, "r@10": 0.5},
            metadata={"source": "benchmark"},
        )
        assert metric_run["instrument_name"] == "retrieval-benchmark-metrics"
        observations = store.get_drift_eval_observations(limit=10)
        assert {row["metric_name"] for row in observations} >= {"p@5", "r@10"}
    finally:
        store.close()
