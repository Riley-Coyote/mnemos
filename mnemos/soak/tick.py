"""U7 full-soak scheduled tick orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import plistlib
from typing import Any

from ..consolidation.daemon import ConsolidationDaemon
from ..inner_life.preflight import PROCESS_FAMILIES
from ..inner_life.scheduler import (
    _assert_python_can_import_mnemos,
    _resolve_python_executable,
    _write_bytes_atomic,
    run_scheduled_inner_life_process,
)
from ..store.sqlite_store import EngramStore


DEFAULT_SOAK_LABEL = "com.davidef.mnemos.soak.tick"
DEFAULT_SOAK_ARTIFACT_DIR = "~/.mnemos/soak"
DEFAULT_MIN_TICK_INTERVAL_SECONDS = 300
SHALLOW_CONSOLIDATION = "shallow_consolidation"
# Soak families that generate text and therefore need an LLM client (mirrors the
# inner-life `run` CLI). affect/observe/challenge run without one.
LLM_SOAK_FAMILIES = frozenset({"reflect", "wander", "dream"})


def run_scheduled_soak_tick(
    store: EngramStore,
    *,
    config: dict[str, Any],
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u7-soak",
    run_id: str | None = None,
    llm_client: Any | None = None,
    build_llm_client: bool = True,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Run one cheap Polyphonic-style tick over enabled soak families.

    ``build_llm_client=False`` disables the auto-wiring below so a caller can
    guarantee no real LLM is contacted — used by the dry-run preflight, which
    runs on a DB copy and must never send memory content to a real model before
    activation.
    """
    now_dt = _coerce_now(now)
    tick_config = _tick_config(config)
    tick_id = run_id or now_dt.isoformat()

    if _halt_marker_present(tick_config):
        return _record_tick_summary(
            store,
            status="skipped",
            reason="halt_marker_present",
            families=[],
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            tick_id=tick_id,
        )

    if not bool(tick_config.get("enabled", False)):
        return _record_tick_summary(
            store,
            status="skipped",
            reason="soak_tick_disabled",
            families=[],
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            tick_id=tick_id,
        )

    families = _enabled_families(config)
    if not families:
        return _record_tick_summary(
            store,
            status="skipped",
            reason="no_enabled_families",
            families=[],
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            tick_id=tick_id,
        )

    # Wire an LLM client for the generative families (reflect/wander/dream) when
    # the caller didn't inject one — without it the scheduled soak path can never
    # actually reflect/wander/dream (it silently no-ops, which is why the soak as
    # designed had never dreamt through this path). Built only here, after the
    # enabled + family gates above: this is capability, not activation. A disabled
    # tick or a tick with no generative family never constructs a client,
    # per-family kill switches still apply, and an injected client (tests) wins.
    if (
        llm_client is None
        and build_llm_client
        and any(family in LLM_SOAK_FAMILIES for family, _ in families)
    ):
        from ..llm import create_client

        llm_client = create_client()

    outcomes: list[dict[str, Any]] = []
    for family, family_config in families:
        if not _family_due(
            store,
            family=family,
            cadence_minutes=int(family_config.get("cadence_minutes", 60) or 60),
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            now=now_dt,
        ):
            outcomes.append(
                _record_family_event(
                    store,
                    family=family,
                    status="skipped",
                    reason="family_not_due",
                    details={},
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    rollout_tag=rollout_tag,
                    tick_id=tick_id,
                )
            )
            continue

        try:
            if family == SHALLOW_CONSOLIDATION:
                outcome = _run_shallow_consolidation_family(
                    store,
                    config=config,
                    agent_id=agent_id,
                )
            else:
                outcome = run_scheduled_inner_life_process(
                    store,
                    process_name=family,
                    config=config,
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    rollout_tag=rollout_tag,
                    run_id=f"{tick_id}:{family}",
                    llm_client=llm_client,
                    now=now_dt,
                )
            status = str(outcome.get("status", "ran"))
            reason = str(outcome.get("reason", "run"))
            outcomes.append(
                _record_family_event(
                    store,
                    family=family,
                    status=status,
                    reason=reason,
                    details=outcome,
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    rollout_tag=rollout_tag,
                    tick_id=tick_id,
                )
            )
        except Exception as exc:
            outcomes.append(
                _record_family_event(
                    store,
                    family=family,
                    status="error",
                    reason=type(exc).__name__,
                    details={"error": str(exc)},
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    rollout_tag=rollout_tag,
                    tick_id=tick_id,
                )
            )

    status = (
        "error"
        if any(row["status"] == "error" for row in outcomes)
        else ("ran" if any(row["status"] == "ran" for row in outcomes) else "skipped")
    )
    reason = (
        "family_error"
        if status == "error"
        else ("families_ran" if status == "ran" else "all_families_skipped")
    )
    return _record_tick_summary(
        store,
        status=status,
        reason=reason,
        families=outcomes,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        rollout_tag=rollout_tag,
        tick_id=tick_id,
    )


def write_soak_tick_launchd_plist(
    *,
    plist_path: str | Path,
    db_path: str | Path,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u7-soak",
    interval_seconds: int,
    artifact_dir: str | Path = DEFAULT_SOAK_ARTIFACT_DIR,
    label: str | None = None,
    python_executable: str | Path | None = None,
    allow_live_db: bool = False,
) -> Path:
    """Write, but do not load, the launchd plist for the scheduled soak tick."""
    if interval_seconds < DEFAULT_MIN_TICK_INTERVAL_SECONDS:
        raise ValueError(
            f"interval_seconds must be >= {DEFAULT_MIN_TICK_INTERVAL_SECONDS}"
        )

    from ..importer.operator import _checked_operator_db_path

    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db).resolve()
    if not db.exists():
        raise FileNotFoundError(
            f"soak tick launchd requires an existing database: {db}"
        )

    plist = Path(plist_path).expanduser()
    plist.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = Path(artifact_dir).expanduser()
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_root.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    python = _resolve_python_executable(python_executable, repo_root=repo_root)
    _assert_python_can_import_mnemos(python, cwd=repo_root)

    args = [
        str(python),
        "-m",
        "mnemos.cli",
        "soak",
        "tick",
        "--db-path",
        str(db),
        "--agent-id",
        agent_id,
        "--person-id",
        person_id,
        "--project-scope",
        project_scope,
        "--rollout-tag",
        rollout_tag,
    ]
    if allow_live_db:
        args.append("--allow-live-db")

    payload = {
        "Label": label or DEFAULT_SOAK_LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "StandardOutPath": str(artifact_root / "soak-tick.out.log"),
        "StandardErrorPath": str(artifact_root / "soak-tick.err.log"),
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONPATH": str(repo_root),
        },
    }
    _write_bytes_atomic(plist, plistlib.dumps(payload, sort_keys=True))
    return plist


def _run_shallow_consolidation_family(
    store: EngramStore,
    *,
    config: dict[str, Any],
    agent_id: str,
) -> dict[str, Any]:
    daemon = ConsolidationDaemon(store=store, config=config, llm_client=None)
    stats = daemon.run_cycle(deep=False, agent_id=agent_id)
    return {
        "family": SHALLOW_CONSOLIDATION,
        "status": "ran",
        "reason": "shallow_consolidation_complete",
        "cycle_type": stats.get("cycle_type"),
        "passes_run": list(stats.get("passes_run", [])),
        "generated_memory_writes": 0,
        "belief_writes": 0,
        "identity_patches": 0,
        "shared_pool_writes": 0,
        "details": stats,
        **stats,
    }


def _record_tick_summary(
    store: EngramStore,
    *,
    status: str,
    reason: str,
    families: list[dict[str, Any]],
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    tick_id: str,
) -> dict[str, Any]:
    metadata = {
        "status": status,
        "reason": reason,
        "families_considered": len(families),
        "families_ran": sum(1 for row in families if row["status"] == "ran"),
        "families_skipped": sum(1 for row in families if row["status"] == "skipped"),
        "families_error": sum(1 for row in families if row["status"] == "error"),
        "generated_memory_writes": sum(
            int(row.get("generated_memory_writes", 0) or 0) for row in families
        ),
        "belief_writes": sum(int(row.get("belief_writes", 0) or 0) for row in families),
        "identity_patches": sum(
            int(row.get("identity_patches", 0) or 0) for row in families
        ),
        "shared_pool_writes": sum(
            int(row.get("shared_pool_writes", 0) or 0) for row in families
        ),
    }
    event_type = (
        "tool_event" if status == "ran" else "error" if status == "error" else "skip"
    )
    store.upsert_inner_life_event(
        idempotency_key=f"soak:{tick_id}:tick",
        event_type=event_type,
        process_name="soak-tick",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        content_hash=_hash_text(f"{status}:{reason}:{tick_id}"),
        content_excerpt=f"soak tick {status}: {reason}",
        event_tags=["u7", "soak", "tick", status],
        metadata=metadata,
        rollout_tag=rollout_tag,
        gate_decision=f"{status}:{reason}",
    )
    return {
        "status": status,
        "reason": reason,
        "families": families,
        **metadata,
    }


def _record_family_event(
    store: EngramStore,
    *,
    family: str,
    status: str,
    reason: str,
    details: dict[str, Any],
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    tick_id: str,
) -> dict[str, Any]:
    metadata = {
        "tick_family": family,
        "status": status,
        "reason": reason,
        "details": details,
        "generated_memory_writes": int(details.get("generated_memory_writes", 0) or 0),
        "belief_writes": int(details.get("belief_writes", 0) or 0),
        "identity_patches": int(details.get("identity_patches", 0) or 0),
        "shared_pool_writes": int(details.get("shared_pool_writes", 0) or 0),
    }
    if family == SHALLOW_CONSOLIDATION:
        metadata["deep"] = False
        metadata["passes_run"] = list(details.get("passes_run", []))
    event_type = (
        "tool_event" if status == "ran" else "error" if status == "error" else "skip"
    )
    store.upsert_inner_life_event(
        idempotency_key=f"soak:{tick_id}:{family}",
        event_type=event_type,
        process_name=family,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        content_hash=_hash_text(f"{family}:{status}:{reason}:{tick_id}"),
        content_excerpt=f"soak tick {family} {status}: {reason}",
        event_tags=["u7", "soak", "tick", family, status],
        metadata=metadata,
        rollout_tag=rollout_tag,
        gate_decision=f"{status}:{reason}",
    )
    return {
        "family": family,
        "status": status,
        "reason": reason,
        "details": details,
        "generated_memory_writes": metadata["generated_memory_writes"],
        "belief_writes": metadata["belief_writes"],
        "identity_patches": metadata["identity_patches"],
        "shared_pool_writes": metadata["shared_pool_writes"],
    }


def _enabled_families(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    families: list[tuple[str, dict[str, Any]]] = []
    shallow = _soak_families(config).get(SHALLOW_CONSOLIDATION, {})
    if bool(shallow.get("enabled", False)):
        families.append((SHALLOW_CONSOLIDATION, shallow))

    inner_life = config.get("inner_life", {})
    schedules = inner_life.get("schedules", {})
    if bool(schedules.get("enabled", False)):
        process_configs = schedules.get("processes", {})
        for process in PROCESS_FAMILIES:
            process_config = process_configs.get(process, {})
            if bool(process_config.get("enabled", False)):
                families.append((process, process_config))
    return families


def _family_due(
    store: EngramStore,
    *,
    family: str,
    cadence_minutes: int,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    now: datetime,
) -> bool:
    last = _last_family_attempt_at(
        store,
        family=family,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        rollout_tag=rollout_tag,
    )
    if last is None:
        return True
    return (now - last).total_seconds() >= max(1, cadence_minutes) * 60


def _last_family_attempt_at(
    store: EngramStore,
    *,
    family: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
) -> datetime | None:
    rows = store.get_inner_life_events(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        rollout_tag=rollout_tag,
        limit=500,
        recent=True,  # family last-run cadence: fold max() over the newest rows
    )
    latest: datetime | None = None
    for row in rows:
        if row.get("process_name") != family:
            continue
        if row.get("gate_decision") == "skipped:family_not_due":
            continue
        created = _coerce_now(row.get("created_at"))
        if latest is None or created > latest:
            latest = created
    return latest


def _tick_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("soak", {}).get("tick", {})


def _soak_families(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("soak", {}).get("families", {})


def _halt_marker_present(tick_config: dict[str, Any]) -> bool:
    marker = str(tick_config.get("halt_marker_path") or "").strip()
    return bool(marker) and Path(marker).expanduser().exists()


def _coerce_now(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
