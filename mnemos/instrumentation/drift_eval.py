"""Step 1 drift-eval registry and record-only helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DriftInstrumentSpec:
    """A drift-eval instrument registration, active or dormant."""

    name: str
    active: bool
    metric_names: tuple[str, ...]
    input_gate: str


DAY_ONE_INSTRUMENTS: dict[str, DriftInstrumentSpec] = {
    "citation-mass-concentration": DriftInstrumentSpec(
        "citation-mass-concentration",
        True,
        ("top_engram_share", "gini_estimate"),
        "retrieval_citations",
    ),
    "category-breadth": DriftInstrumentSpec(
        "category-breadth",
        True,
        ("distinct_categories", "category_entropy"),
        "retrieval_events",
    ),
    "person-relational-share": DriftInstrumentSpec(
        "person-relational-share",
        True,
        ("person_share", "relational_share"),
        "retrieval_events",
    ),
    "serendipity-recording": DriftInstrumentSpec(
        "serendipity-recording",
        True,
        ("unexpected_relevant_count",),
        "operator_annotation",
    ),
    "retrieval-benchmark-metrics": DriftInstrumentSpec(
        "retrieval-benchmark-metrics",
        True,
        ("p@5", "p@10", "r@5", "r@10", "hot_to_cold_intrusion"),
        "benchmarks/retrieval_benchmark.py",
    ),
    "latency": DriftInstrumentSpec(
        "latency",
        True,
        ("retrieval_ms", "citation_write_ms"),
        "retrieval_events",
    ),
    "monoculture-tripwire": DriftInstrumentSpec(
        "monoculture-tripwire",
        True,
        ("top_relation_share", "hot_topic_share"),
        "connections/retrieval_events",
    ),
}

FUTURE_INSTRUMENTS: dict[str, DriftInstrumentSpec] = {
    "affect-entropy": DriftInstrumentSpec(
        "affect-entropy", False, ("entropy",), "future_affect_state"
    ),
    "stamp-distribution": DriftInstrumentSpec(
        "stamp-distribution", False, ("origin_stamp_share",), "origin_stamp_backfill"
    ),
    "pride-play-share": DriftInstrumentSpec(
        "pride-play-share", False, ("pride_share", "play_share"), "future_receipts"
    ),
    "correction-accessibility": DriftInstrumentSpec(
        "correction-accessibility", False, ("correction_recall_rate",), "future_tests"
    ),
    "h5-fire-drop-lines": DriftInstrumentSpec(
        "h5-fire-drop-lines", False, ("fire_count", "drop_count"), "future_h5_grader"
    ),
    "h7-bond-probes": DriftInstrumentSpec(
        "h7-bond-probes", False, ("bond_probe_score",), "future_bond_model"
    ),
}


def registered_instruments() -> dict[str, DriftInstrumentSpec]:
    """Return every registered day-one and future instrument."""

    return {**DAY_ONE_INSTRUMENTS, **FUTURE_INSTRUMENTS}


def record_instrument_registry(store: Any) -> list[dict[str, Any]]:
    """Persist a record-only registry snapshot to the provided store."""

    rows: list[dict[str, Any]] = []
    for spec in registered_instruments().values():
        rows.append(
            store.record_drift_eval_run(
                instrument_name=spec.name,
                active=spec.active,
                status="registered" if spec.active else "registered_inactive",
                metrics={name: None for name in spec.metric_names},
                metadata={"input_gate": spec.input_gate},
            )
        )
    return rows


def record_retrieval_benchmark_metrics(
    store: Any,
    *,
    metrics: dict[str, float],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist retrieval benchmark metrics as record-only drift evidence."""

    run = store.record_drift_eval_run(
        instrument_name="retrieval-benchmark-metrics",
        active=True,
        status="recorded",
        metrics=metrics,
        metadata=metadata or {},
    )
    for metric_name, value in metrics.items():
        store.record_drift_eval_observation(
            run_id=run["run_id"],
            metric_name=metric_name,
            metric_value=float(value),
            metadata=metadata or {},
        )
    return run
