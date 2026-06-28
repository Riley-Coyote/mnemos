"""Preflight checks for U6.6/U7 gated inner-life scheduling."""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROCESS_FAMILIES = ("challenge", "observe", "affect", "reflect", "wander", "dream")


def build_inner_life_preflight(
    *,
    config: dict[str, Any],
    db_path: str | Path,
) -> dict[str, Any]:
    """Summarize whether full scheduled inner-life activation is configured."""
    inner_life = config.get("inner_life", {})
    schedules = inner_life.get("schedules", {})
    schedule_processes = schedules.get("processes", {})
    activity_gate = inner_life.get("activity_gate", {})
    activity_processes = activity_gate.get("processes", {})

    missing_schedule_switches = [
        process
        for process in PROCESS_FAMILIES
        if process not in schedule_processes or "enabled" not in schedule_processes[process]
    ]
    disabled_schedule_processes = [
        process
        for process in PROCESS_FAMILIES
        if not bool(schedule_processes.get(process, {}).get("enabled", False))
    ]
    missing_activity_switches = [
        process
        for process in PROCESS_FAMILIES
        if process not in activity_processes or "enabled" not in activity_processes[process]
    ]
    disabled_activity_processes = [
        process
        for process in PROCESS_FAMILIES
        if not bool(activity_processes.get(process, {}).get("enabled", True))
    ]

    expanded_db_path = Path(db_path).expanduser()
    blockers: list[str] = []
    if not expanded_db_path.exists():
        blockers.append("db_missing")
    if not bool(schedules.get("enabled", False)):
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

    return {
        "ready_for_full_scheduled_activation": not blockers,
        "blockers": blockers,
        "db_path": str(expanded_db_path),
        "db_exists": expanded_db_path.exists(),
        "schedules_enabled": bool(schedules.get("enabled", False)),
        "missing_schedule_switches": missing_schedule_switches,
        "disabled_schedule_processes": disabled_schedule_processes,
        "missing_activity_switches": missing_activity_switches,
        "disabled_activity_processes": disabled_activity_processes,
        "processes": {
            process: {
                "scheduled": bool(schedule_processes.get(process, {}).get("enabled", False)),
                "activity_gate": bool(activity_processes.get(process, {}).get("enabled", True)),
                "cadence_minutes": schedule_processes.get(process, {}).get("cadence_minutes"),
                "cooldown_minutes": activity_processes.get(process, {}).get("cooldown_minutes"),
            }
            for process in PROCESS_FAMILIES
        },
    }
