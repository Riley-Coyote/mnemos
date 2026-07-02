"""Preflight checks for U6.6/U7 gated inner-life scheduling."""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROCESS_FAMILIES = ("challenge", "observe", "affect", "reflect", "wander", "dream")
SOAK_FAMILIES = ("shallow_consolidation",)


def build_inner_life_preflight(
    *,
    config: dict[str, Any],
    db_path: str | Path,
    provider_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize whether full scheduled inner-life activation is configured."""
    inner_life = config.get("inner_life", {})
    soak = config.get("soak", {})
    soak_tick = soak.get("tick", {})
    soak_families = soak.get("families", {})
    schedules = inner_life.get("schedules", {})
    schedule_processes = schedules.get("processes", {})
    activity_gate = inner_life.get("activity_gate", {})
    activity_processes = activity_gate.get("processes", {})
    activation = inner_life.get("activation", {})
    provider = _resolve_provider_status(provider_status, activation)

    missing_schedule_switches = [
        process
        for process in PROCESS_FAMILIES
        if process not in schedule_processes
        or "enabled" not in schedule_processes[process]
    ]
    missing_soak_family_switches = [
        family
        for family in SOAK_FAMILIES
        if family not in soak_families or "enabled" not in soak_families[family]
    ]
    disabled_schedule_processes = [
        process
        for process in PROCESS_FAMILIES
        if not bool(schedule_processes.get(process, {}).get("enabled", False))
    ]
    disabled_soak_families = [
        family
        for family in SOAK_FAMILIES
        if not bool(soak_families.get(family, {}).get("enabled", False))
    ]
    missing_activity_switches = [
        process
        for process in PROCESS_FAMILIES
        if process not in activity_processes
        or "enabled" not in activity_processes[process]
    ]
    disabled_activity_processes = [
        process
        for process in PROCESS_FAMILIES
        if not bool(activity_processes.get(process, {}).get("enabled", True))
    ]

    expanded_db_path = Path(db_path).expanduser()
    snapshot_path = _expand_optional_path(activation.get("pre_soak_snapshot_path"))
    schedules_enabled = bool(schedules.get("enabled", False))
    soak_tick_enabled = bool(soak_tick.get("enabled", False))
    blockers: list[str] = []
    if not expanded_db_path.exists():
        blockers.append("db_missing")
    if "enabled" not in soak_tick:
        blockers.append("missing_soak_tick_kill_switch")
    if not soak_tick_enabled:
        blockers.append("soak_tick_disabled")
    if missing_soak_family_switches:
        blockers.append("missing_soak_family_kill_switch")
    if disabled_soak_families:
        blockers.append("soak_family_disabled")
    if not schedules_enabled:
        blockers.append("inner_life_schedules_disabled")
    if missing_schedule_switches:
        blockers.append("missing_schedule_kill_switch")
    if disabled_schedule_processes:
        blockers.append("scheduled_process_disabled")
    if not bool(activity_gate.get("enabled", True)):
        blockers.append("activity_gate_disabled")
    if missing_activity_switches:
        blockers.append("missing_activity_gate_kill_switch")
    if disabled_activity_processes:
        blockers.append("activity_process_disabled")
    if schedules_enabled:
        if snapshot_path is None or not snapshot_path.exists():
            blockers.append("pre_soak_snapshot_missing")
        if (
            bool(activation.get("require_llm_provider", True))
            and not provider["llm_ready"]
        ):
            blockers.append("llm_provider_unavailable")
        if (
            bool(activation.get("require_observer_reviewers", True))
            and int(provider.get("observer_reviewer_count", 0) or 0) < 1
        ):
            blockers.append("observer_reviewers_unconfigured")

    artifact_dir = Path(
        activation.get("artifact_dir") or "~/.mnemos/inner-life"
    ).expanduser()
    plist_dir = Path(
        activation.get("plist_dir") or "~/Library/LaunchAgents"
    ).expanduser()
    label_prefix = str(activation.get("label_prefix") or "com.davidef.mnemos.innerlife")
    soak_artifact_dir = Path(
        soak_tick.get("artifact_dir") or "~/.mnemos/soak"
    ).expanduser()
    soak_plist_dir = Path(
        soak_tick.get("plist_dir") or "~/Library/LaunchAgents"
    ).expanduser()
    soak_label = str(soak_tick.get("label") or "com.davidef.mnemos.soak.tick")
    halt_marker = Path(
        soak_tick.get("halt_marker_path")
        or activation.get("halt_marker_path")
        or "~/.mnemos/full-soak.halt"
    ).expanduser()

    return {
        "ready_for_full_scheduled_activation": not blockers,
        "blockers": blockers,
        "db_path": str(expanded_db_path),
        "db_exists": expanded_db_path.exists(),
        "soak_tick_enabled": soak_tick_enabled,
        "schedules_enabled": schedules_enabled,
        "provider_readiness": provider,
        "pre_soak_snapshot": {
            "path": str(snapshot_path) if snapshot_path is not None else "",
            "exists": bool(snapshot_path and snapshot_path.exists()),
        },
        "launchd": {
            "artifact_dir": str(artifact_dir),
            "plist_dir": str(plist_dir),
            "label_prefix": label_prefix,
            "soak_tick_artifact_dir": str(soak_artifact_dir),
            "soak_tick_plist_dir": str(soak_plist_dir),
            "soak_tick_label": soak_label,
            "soak_tick_plist_path": str(soak_plist_dir / f"{soak_label}.plist"),
            "halt_marker_path": str(halt_marker),
        },
        "rollback": {
            "disable_first": True,
            "commands": list(activation.get("rollback_commands") or []),
        },
        "missing_schedule_switches": missing_schedule_switches,
        "disabled_schedule_processes": disabled_schedule_processes,
        "missing_soak_family_switches": missing_soak_family_switches,
        "disabled_soak_families": disabled_soak_families,
        "soak_families": {
            family: {
                "scheduled": bool(soak_families.get(family, {}).get("enabled", False)),
                "cadence_minutes": soak_families.get(family, {}).get("cadence_minutes"),
                "kill_switches": [
                    "soak.tick.enabled",
                    f"soak.families.{family}.enabled",
                ],
            }
            for family in SOAK_FAMILIES
        },
        "missing_activity_switches": missing_activity_switches,
        "disabled_activity_processes": disabled_activity_processes,
        "processes": {
            process: {
                "scheduled": bool(
                    schedule_processes.get(process, {}).get("enabled", False)
                ),
                "activity_gate": bool(
                    activity_processes.get(process, {}).get("enabled", True)
                ),
                "cadence_minutes": schedule_processes.get(process, {}).get(
                    "cadence_minutes"
                ),
                "cooldown_minutes": activity_processes.get(process, {}).get(
                    "cooldown_minutes"
                ),
                "launchd_label": f"{label_prefix}.{process}",
                "plist_path": str(plist_dir / f"{label_prefix}.{process}.plist"),
                "kill_switches": [
                    "inner_life.schedules.enabled",
                    f"inner_life.schedules.processes.{process}.enabled",
                    f"inner_life.activity_gate.processes.{process}.enabled",
                ],
            }
            for process in PROCESS_FAMILIES
        },
    }


def _expand_optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def _resolve_provider_status(
    provider_status: dict[str, Any] | None,
    activation: dict[str, Any],
) -> dict[str, Any]:
    if provider_status is not None:
        return {
            "llm_ready": bool(provider_status.get("llm_ready", False)),
            "llm_provider": provider_status.get("llm_provider"),
            "observer_reviewer_count": int(
                provider_status.get("observer_reviewer_count", 0) or 0
            ),
        }

    llm_ready = False
    llm_provider = None
    try:
        from ..llm import resolve_affinity_status

        status = resolve_affinity_status(resolve_if_missing=True)
        llm_ready = bool(status.get("substrate_resolved") and status.get("allowed"))
        llm_provider = status.get("substrate_provider")
    except Exception:
        llm_ready = False
        llm_provider = None

    return {
        "llm_ready": llm_ready,
        "llm_provider": llm_provider,
        "observer_reviewer_count": int(
            activation.get("observer_reviewer_count", 0) or 0
        ),
    }
