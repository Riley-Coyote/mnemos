"""Scheduled U6.6 inner-life entrypoints and launchd artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from typing import Any

from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity
from ..store.sqlite_store import EngramStore
from ..substrate.config import SubstrateConfig
from ..substrate.events import EventType, SubstrateEvent
from ..substrate.modulators import ModulatorState, compute_modulators
from .activity_gate import evaluate_activity_gate
from .emotional_driver import update_event_grounded_affect
from .preflight import PROCESS_FAMILIES


DEFAULT_SCHEDULE_LABEL_PREFIX = "com.davidef.mnemos.innerlife"


def run_scheduled_inner_life_process(
    store: EngramStore,
    *,
    process_name: str,
    config: dict[str, Any],
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u6.6",
    run_id: str | None = None,
    llm_client: Any | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Run one scheduled inner-life family behind the zero-LLM activity gate."""
    process = _validate_process(process_name)
    gate = evaluate_activity_gate(
        store,
        process_name=process,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        config=config,
        now=now,
        rollout_tag=rollout_tag,
        idempotency_key=f"scheduled-gate:{process}:{run_id}" if run_id else None,
    )
    if not gate["allowed"]:
        return _summary(
            process=process,
            status="skipped",
            gate_decision=gate["gate_decision"],
            reason=gate["reason"],
            signal_count=gate["signal_count"],
        )

    if process == "affect":
        result = update_event_grounded_affect(
            store,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            now=now,
        )
        return _summary(
            process=process,
            status="ran",
            gate_decision=gate["gate_decision"],
            reason=result["reason"],
            signal_count=gate["signal_count"],
            generated_memory_writes=result.get("generated_memory_writes", 0),
            identity_patches=result.get("identity_patches", 0),
            details=result,
        )

    if process == "reflect":
        result = _run_reflect(
            store,
            config=config,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            llm_client=llm_client,
        )
        return _summary(
            process=process,
            status="ran",
            gate_decision=gate["gate_decision"],
            reason="reflection_complete",
            signal_count=gate["signal_count"],
            generated_memory_writes=result.get("generated_memory_writes", 0),
            identity_patches=result.get("identity_patches", 0),
            details=result,
        )

    if process in {"wander", "dream"} and llm_client is None:
        return _record_scheduled_skip(
            store,
            process=process,
            reason="llm_provider_unavailable",
            gate=gate,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            run_id=run_id,
        )

    if process == "wander":
        result = _run_wander(store, agent_id=agent_id, llm_client=llm_client)
        return _summary(
            process=process,
            status="ran",
            gate_decision=gate["gate_decision"],
            reason=result["reason"],
            signal_count=gate["signal_count"],
            generated_memory_writes=result["generated_memory_writes"],
            details=result,
        )

    if process == "dream":
        result = _run_dream(store, agent_id=agent_id, llm_client=llm_client)
        if result["status"] == "skipped":
            return _record_scheduled_skip(
                store,
                process=process,
                reason=result["reason"],
                gate=gate,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                rollout_tag=rollout_tag,
                run_id=run_id,
                source_ids=result.get("source_ids", []),
            )
        return _summary(
            process=process,
            status="ran",
            gate_decision=gate["gate_decision"],
            reason=result["reason"],
            signal_count=gate["signal_count"],
            generated_memory_writes=result["generated_memory_writes"],
            details=result,
        )

    if process == "challenge":
        return _record_scheduled_skip(
            store,
            process=process,
            reason="challenge_reviewer_unconfigured",
            gate=gate,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            run_id=run_id,
        )

    if process == "observe":
        return _record_scheduled_skip(
            store,
            process=process,
            reason="observer_reviewers_unconfigured",
            gate=gate,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            rollout_tag=rollout_tag,
            run_id=run_id,
        )

    return _record_scheduled_skip(
        store,
        process=process,
        reason="scheduled_runner_not_configured",
        gate=gate,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        rollout_tag=rollout_tag,
        run_id=run_id,
    )


def write_inner_life_launchd_plist(
    *,
    plist_path: str | Path,
    process_name: str,
    db_path: str | Path,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    rollout_tag: str = "u6.6",
    interval_seconds: int,
    artifact_dir: str | Path,
    label: str | None = None,
    python_executable: str | Path | None = None,
    allow_live_db: bool = False,
) -> Path:
    """Write, but do not load, a launchd plist for one inner-life process."""
    process = _validate_process(process_name)
    if interval_seconds < 60:
        raise ValueError("interval_seconds must be >= 60")

    from ..importer.operator import _checked_operator_db_path

    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db).resolve()
    if not db.exists():
        raise FileNotFoundError(f"inner-life launchd requires an existing database: {db}")

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
        "inner-life",
        "run",
        "--process",
        process,
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
        "Label": label or f"{DEFAULT_SCHEDULE_LABEL_PREFIX}.{process}",
        "ProgramArguments": args,
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "StandardOutPath": str(artifact_root / f"{process}.out.log"),
        "StandardErrorPath": str(artifact_root / f"{process}.err.log"),
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONPATH": str(repo_root),
        },
    }
    _write_bytes_atomic(plist, plistlib.dumps(payload, sort_keys=True))
    return plist


def _run_reflect(
    store: EngramStore,
    *,
    config: dict[str, Any],
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    llm_client: Any | None,
) -> dict[str, Any]:
    from ..consolidation.reflection import run_reflection_pass

    identity = store.get_identity(agent_id) or AgentIdentity()
    identity.memory_profile.agent_id = agent_id
    emotional_state = store.get_latest_emotional_state(agent_id) or EmotionalState()
    reflection_config = dict(config.get("consolidation", {}))
    reflection_config.update(
        {
            "inner_life_person_id": person_id,
            "inner_life_project_scope": project_scope,
            "inner_life_rollout_tag": rollout_tag,
        }
    )
    return run_reflection_pass(
        store,
        identity,
        emotional_state,
        llm_client,
        reflection_config,
    )


def _run_wander(
    store: EngramStore,
    *,
    agent_id: str,
    llm_client: Any,
) -> dict[str, Any]:
    from ..substrate.handlers import wandering

    before = store.count_engrams(agent_id=agent_id, read_visibility=None)
    config = _substrate_config(store, agent_id=agent_id)
    event = SubstrateEvent(
        event_type=EventType.SILENCE_EXTENDED,
        payload={"scheduled": True},
        source="inner-life-scheduler",
    )
    wandering.handle(event, config, _modulators(store, agent_id=agent_id), store, llm_client)
    after = store.count_engrams(agent_id=agent_id, read_visibility=None)
    return {
        "reason": "wander_complete",
        "generated_memory_writes": max(0, after - before),
    }


def _run_dream(
    store: EngramStore,
    *,
    agent_id: str,
    llm_client: Any,
) -> dict[str, Any]:
    from ..substrate.handlers import dreaming

    candidates = store.get_active_engrams(
        agent_id=agent_id,
        limit=200,
        require_consolidation_authorized=True,
    )
    if len(candidates) < 2:
        return {
            "status": "skipped",
            "reason": "insufficient_authorized_engrams",
            "generated_memory_writes": 0,
            "source_ids": [engram.id for engram in candidates],
        }
    softened = min(candidates, key=lambda e: float(e.accessibility) * float(e.strength))
    before = store.count_engrams(agent_id=agent_id, read_visibility=None)
    config = _substrate_config(store, agent_id=agent_id)
    event = SubstrateEvent(
        event_type=EventType.MEMORY_SOFTENED,
        payload={"engram_id": softened.id, "scheduled": True},
        source="inner-life-scheduler",
    )
    dreaming.handle(event, config, _modulators(store, agent_id=agent_id), store, llm_client)
    after = store.count_engrams(agent_id=agent_id, read_visibility=None)
    return {
        "status": "ran",
        "reason": "dream_complete",
        "generated_memory_writes": max(0, after - before),
        "source_ids": [softened.id],
    }


def _record_scheduled_skip(
    store: EngramStore,
    *,
    process: str,
    reason: str,
    gate: dict[str, Any],
    agent_id: str,
    person_id: str,
    project_scope: str,
    rollout_tag: str,
    run_id: str | None,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    ids = source_ids if source_ids is not None else list(gate.get("source_ids", []))
    key = run_id or _run_key(process, reason, ids)
    store.upsert_inner_life_event(
        idempotency_key=f"scheduled:{process}:{key}",
        event_type="skip",
        process_name=process,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        source_message_id=ids[0] if ids else None,
        content_hash=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
        content_excerpt=f"scheduled {process} skipped: {reason}",
        event_tags=["u6.6", "scheduled", process, "skip"],
        source_ids=ids,
        metadata={
            "writes_memory": False,
            "generated_memory_writes": 0,
            "identity_patches": 0,
            "belief_writes": 0,
            "shared_pool_writes": 0,
            "activity_gate": gate,
            "reason": reason,
        },
        rollout_tag=rollout_tag,
        gate_decision=f"skip:{reason}",
    )
    return _summary(
        process=process,
        status="skipped",
        gate_decision=gate["gate_decision"],
        reason=reason,
        signal_count=gate.get("signal_count", 0),
    )


def _summary(
    *,
    process: str,
    status: str,
    gate_decision: str,
    reason: str,
    signal_count: int,
    generated_memory_writes: int = 0,
    identity_patches: int = 0,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "process": process,
        "status": status,
        "gate_decision": gate_decision,
        "reason": reason,
        "signal_count": signal_count,
        "generated_memory_writes": int(generated_memory_writes or 0),
        "belief_writes": 0,
        "identity_patches": int(identity_patches or 0),
        "shared_pool_writes": 0,
        "details": details or {},
    }


def _validate_process(process_name: str) -> str:
    process = str(process_name).strip()
    if process not in PROCESS_FAMILIES:
        raise ValueError(f"Unsupported inner-life process: {process_name}")
    return process


def _substrate_config(store: EngramStore, *, agent_id: str) -> SubstrateConfig:
    return SubstrateConfig(
        agent_id=agent_id,
        db_path=str(store.db_path),
        log_dir=str(store.db_path.parent / "logs"),
    )


def _modulators(store: EngramStore, *, agent_id: str) -> ModulatorState:
    try:
        return compute_modulators(
            str(store.db_path),
            agent_id=agent_id,
            require_consolidation_authorized=True,
        )
    except Exception:
        return ModulatorState()


def _run_key(process: str, reason: str, source_ids: list[str]) -> str:
    payload = "|".join([process, reason, *source_ids, datetime.now(timezone.utc).isoformat()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _resolve_python_executable(
    python_executable: str | Path | None,
    *,
    repo_root: Path,
) -> Path:
    if python_executable is None:
        for candidate in (
            repo_root / ".venv" / "bin" / "python3",
            repo_root / ".venv" / "bin" / "python",
        ):
            if candidate.exists():
                return candidate
    candidate = Path(python_executable or sys.executable).expanduser()
    return candidate.resolve()


def _assert_python_can_import_mnemos(python: Path, *, cwd: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(cwd)
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import mnemos.cli, pathlib; print(pathlib.Path(mnemos.cli.__file__).resolve())",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"python cannot import mnemos from {cwd}: {detail}")
    imported = Path(completed.stdout.strip()).resolve()
    if cwd.resolve() not in imported.parents:
        raise RuntimeError(f"python imports stale mnemos package: {imported}")


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)
