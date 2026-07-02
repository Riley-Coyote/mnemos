"""U7 full-soak activation preflight artifact builder."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any

from ..inner_life.preflight import build_inner_life_preflight
from .tick import DEFAULT_SOAK_LABEL, run_scheduled_soak_tick


def build_soak_activation_preflight(
    *,
    config: dict[str, Any],
    db_path: str | Path,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u7-soak",
    soak_plist_path: str | Path | None = None,
    watch_doctor_report: Any | None = None,
    watch_label: str = "com.davidef.mnemos.duallife",
    run_tick_dry_run: bool = False,
    launchd_status: dict[str, Any] | None = None,
    provider_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the read-only U7 activation preflight.

    This function never loads launchd jobs and never writes to the supplied DB.
    The optional tick dry run uses SQLite's backup API to copy the DB first.
    """
    db = Path(db_path).expanduser()
    inner = build_inner_life_preflight(
        config=config,
        db_path=db,
        provider_status=provider_status,
    )
    soak_tick = config.get("soak", {}).get("tick", {})
    soak_label = str(soak_tick.get("label") or DEFAULT_SOAK_LABEL)
    resolved_soak_plist = _resolve_soak_plist_path(
        soak_plist_path=soak_plist_path,
        tick_config=soak_tick,
        label=soak_label,
    )
    plist = _lint_soak_plist(
        plist_path=resolved_soak_plist,
        db_path=db,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        rollout_tag=rollout_tag,
    )
    watch = _watch_doctor_summary(watch_doctor_report)
    launchd = launchd_status or inspect_launchd_labels(
        watch_label=watch_label,
        soak_label=soak_label,
    )
    dry_run = (
        _run_tick_copy_dry_run(
            config=config,
            db_path=db,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
        )
        if run_tick_dry_run
        else {
            "ran": False,
            "ok": False,
            "reason": "not_requested",
        }
    )

    blockers = list(inner["blockers"])
    if not watch["configured"]:
        blockers.append("watch_doctor_missing")
    elif not watch["ok"]:
        blockers.append("watch_doctor_not_green")
    if not plist["ok"]:
        blockers.append("soak_tick_plist_not_ready")
    if not launchd.get("checked", False):
        blockers.append("launchd_status_unchecked")
    elif launchd.get("pre_authorization_loaded", False):
        blockers.append("launchd_jobs_already_loaded")
    if not dry_run["ok"]:
        blockers.append(
            "soak_tick_dry_run_missing"
            if not dry_run["ran"]
            else "soak_tick_dry_run_failed"
        )

    blockers = _dedupe(blockers)
    return {
        "schema": "mnemos.u7_soak_preflight.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready_for_u7_activation": not blockers,
        "blockers": blockers,
        "scope": {
            "agent_id": agent_id,
            "person_id": person_id,
            "project_scope": project_scope,
            "rollout_tag": rollout_tag,
        },
        "db": {
            "path": str(db),
            "exists": db.exists(),
            "original_sha256": _sha256_file(db) if db.exists() else "",
        },
        "inner_life": inner,
        "watcher": {
            "label": watch_label,
            "doctor": watch,
        },
        "soak_tick_plist": plist,
        "tick_dry_run": dry_run,
        "launchd": launchd,
        "rollback": {
            "disable_first": True,
            "watcher_commands": [
                f"launchctl bootout gui/$UID ~/Library/LaunchAgents/{watch_label}.plist",
            ],
            "soak_commands": list(soak_tick.get("rollback_commands") or []),
            "restore_rule": (
                "restore the pre-soak DB snapshot only if disabling schedules "
                "does not stop retrieval or identity contamination"
            ),
        },
    }


def inspect_launchd_labels(*, watch_label: str, soak_label: str) -> dict[str, Any]:
    """Read launchd state for the two U7 labels without mutating anything."""
    if shutil.which("launchctl") is None:
        return {
            "checked": False,
            "available": False,
            "reason": "launchctl_unavailable",
            "labels": {
                watch_label: {"loaded": None},
                soak_label: {"loaded": None},
            },
            "pre_authorization_loaded": False,
        }

    labels: dict[str, dict[str, Any]] = {}
    for label in (watch_label, soak_label):
        result = subprocess.run(
            ["launchctl", "list", label],
            check=False,
            capture_output=True,
            text=True,
        )
        loaded = result.returncode == 0
        labels[label] = {
            "loaded": loaded,
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
    return {
        "checked": True,
        "available": True,
        "labels": labels,
        "pre_authorization_loaded": any(row["loaded"] for row in labels.values()),
    }


def _resolve_soak_plist_path(
    *,
    soak_plist_path: str | Path | None,
    tick_config: dict[str, Any],
    label: str,
) -> Path:
    if soak_plist_path is not None:
        return Path(soak_plist_path).expanduser()
    plist_dir = Path(
        tick_config.get("plist_dir") or "~/Library/LaunchAgents"
    ).expanduser()
    return plist_dir / f"{label}.plist"


def _lint_soak_plist(
    *,
    plist_path: Path,
    db_path: Path,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
) -> dict[str, Any]:
    if not plist_path.exists():
        return {
            "path": str(plist_path),
            "exists": False,
            "ok": False,
            "reason": "missing",
        }
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        return {
            "path": str(plist_path),
            "exists": True,
            "ok": False,
            "reason": f"parse_error:{type(exc).__name__}",
        }

    args = [str(value) for value in payload.get("ProgramArguments", [])]
    required_pairs = {
        "--db-path": str(db_path.resolve()) if db_path.exists() else str(db_path),
        "--agent-id": agent_id,
        "--person-id": person_id,
        "--project-scope": project_scope,
        "--rollout-tag": rollout_tag,
    }
    failures: list[str] = []
    if args[:5] != [args[0] if args else "", "-m", "mnemos.cli", "soak", "tick"]:
        failures.append("program_arguments_not_soak_tick")
    for key, expected in required_pairs.items():
        if not _args_contain_pair(args, key, expected):
            failures.append(f"missing_or_mismatched_{key.removeprefix('--')}")
    if "--allow-live-db" in args:
        failures.append("plist_contains_allow_live_db")
    if not payload.get("RunAtLoad", False):
        failures.append("run_at_load_false")
    if int(payload.get("StartInterval", 0) or 0) < 300:
        failures.append("start_interval_too_low")
    if not payload.get("WorkingDirectory"):
        failures.append("working_directory_missing")
    return {
        "path": str(plist_path),
        "exists": True,
        "ok": not failures,
        "failures": failures,
        "label": payload.get("Label"),
        "start_interval": payload.get("StartInterval"),
        "run_at_load": payload.get("RunAtLoad"),
        "working_directory": payload.get("WorkingDirectory"),
    }


def _watch_doctor_summary(report: Any | None) -> dict[str, Any]:
    if report is None:
        return {
            "configured": False,
            "ok": False,
            "checks": [],
        }
    checks_raw = getattr(
        report, "checks", report.get("checks", []) if isinstance(report, dict) else []
    )
    checks = [_jsonable(check) for check in checks_raw]
    ok = bool(
        getattr(
            report, "ok", report.get("ok", False) if isinstance(report, dict) else False
        )
    )
    return {
        "configured": True,
        "ok": ok,
        "checks": checks,
    }


def _run_tick_copy_dry_run(
    *,
    config: dict[str, Any],
    db_path: Path,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "ran": True,
            "ok": False,
            "reason": "db_missing",
        }
    before = _sha256_file(db_path)
    with tempfile.TemporaryDirectory(prefix="mnemos-soak-preflight-") as tmp:
        copy_path = Path(tmp) / "soak-preflight-copy.db"
        _sqlite_backup(db_path, copy_path)
        from ..store.sqlite_store import EngramStore

        store = EngramStore(copy_path)
        try:
            result = run_scheduled_soak_tick(
                store,
                config=config,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                rollout_tag=rollout_tag,
                run_id="preflight-copy",
            )
        finally:
            store.close()
    after = _sha256_file(db_path)
    return {
        "ran": True,
        "ok": before == after and result.get("status") != "error",
        "reason": result.get("reason"),
        "status": result.get("status"),
        "families_considered": result.get("families_considered", 0),
        "families_ran": result.get("families_ran", 0),
        "families_skipped": result.get("families_skipped", 0),
        "families_error": result.get("families_error", 0),
        "generated_memory_writes": result.get("generated_memory_writes", 0),
        "belief_writes": result.get("belief_writes", 0),
        "identity_patches": result.get("identity_patches", 0),
        "shared_pool_writes": result.get("shared_pool_writes", 0),
        "source_db_unchanged": before == after,
    }


def _sqlite_backup(source: Path, target: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _args_contain_pair(args: list[str], key: str, expected: str) -> bool:
    for index, value in enumerate(args[:-1]):
        if value == key and args[index + 1] == expected:
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
