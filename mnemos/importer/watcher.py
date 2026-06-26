"""U3c dual-life watcher helpers for PAI source mirrors."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

from .operator import (
    PaiManifest,
    PaiOperatorRun,
    _checked_operator_db_path,
    apply_pai_watch_manifest,
    load_pai_manifest,
    preview_pai_watch_manifest,
)


WATCH_STATE_SCHEMA = "mnemos.pai_watch.state.v1"
DEFAULT_WATCH_LABEL = "com.davidef.mnemos.duallife"


@dataclass(frozen=True)
class PaiWatchOnceRun:
    """Outcome of one U3c watcher poll."""

    manifest: PaiManifest
    state_path: Path
    changed_sources: tuple[str, ...]
    operator_run: PaiOperatorRun | None
    state_written: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changed_sources)


def pai_watch_once(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    state_path: str | Path,
    artifact_dir: str | Path | None = None,
    backup_dir: str | Path | None = None,
    apply: bool = False,
    force: bool = False,
    allow_live_db: bool = False,
) -> PaiWatchOnceRun:
    """Run one dual-life watcher poll.

    State advances only after a successful apply. Preview mode intentionally
    leaves state untouched so an operator can inspect the same source change and
    still apply it later.
    """
    manifest = load_pai_manifest(manifest_path, allow_missing_sources=True)
    state = _read_watch_state(state_path)
    current = _source_fingerprints(manifest)
    changed_sources = _changed_sources(state, current, manifest=manifest)
    if not changed_sources and not force:
        return PaiWatchOnceRun(
            manifest=manifest,
            state_path=Path(state_path).expanduser(),
            changed_sources=(),
            operator_run=None,
            state_written=False,
        )

    artifact_path = _watch_artifact_path(
        artifact_dir,
        manifest=manifest,
        mode="watch-apply" if apply else "watch-preview",
    )
    if apply:
        operator_run = apply_pai_watch_manifest(
            db_path=db_path,
            manifest_path=manifest_path,
            artifact_path=artifact_path,
            backup_dir=backup_dir,
            allow_live_db=allow_live_db,
        )
        applied_fingerprints = _source_fingerprints(operator_run.manifest)
        _write_watch_state(
            state_path,
            manifest=operator_run.manifest,
            source_fingerprints=applied_fingerprints,
        )
        state_written = True
    else:
        operator_run = preview_pai_watch_manifest(
            db_path=db_path,
            manifest_path=manifest_path,
            artifact_path=artifact_path,
            allow_live_db=allow_live_db,
        )
        state_written = False

    return PaiWatchOnceRun(
        manifest=manifest,
        state_path=Path(state_path).expanduser(),
        changed_sources=changed_sources,
        operator_run=operator_run,
        state_written=state_written,
    )


def write_pai_watch_launchd_plist(
    *,
    plist_path: str | Path,
    manifest_path: str | Path,
    db_path: str | Path,
    state_path: str | Path,
    artifact_dir: str | Path,
    backup_dir: str | Path,
    label: str = DEFAULT_WATCH_LABEL,
    interval_seconds: int = 60,
    python_executable: str | Path | None = None,
    allow_live_db: bool = False,
) -> Path:
    """Write, but do not load, a launchd plist for `pai-import watch-once`."""
    if interval_seconds < 10:
        raise ValueError("interval_seconds must be >= 10")
    plist = Path(plist_path).expanduser()
    plist.parent.mkdir(parents=True, exist_ok=True)
    manifest = load_pai_manifest(manifest_path, allow_missing_sources=True)
    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db).resolve()
    if not db.exists():
        raise FileNotFoundError(f"PAI watch launchd requires an existing database: {db}")
    state = Path(state_path).expanduser()
    state.parent.mkdir(parents=True, exist_ok=True)
    state = state.resolve()
    artifact_root = Path(artifact_dir).expanduser()
    backup_root = Path(backup_dir).expanduser()
    artifact_root.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_root.resolve()
    backup_root = backup_root.resolve()
    python = _resolve_python_executable(python_executable)
    repo_root = Path(__file__).resolve().parents[2]
    _assert_python_can_import_mnemos(python, cwd=repo_root)
    args = [
        python,
        "-m",
        "mnemos.cli",
        "pai-import",
        "watch-once",
        "--manifest",
        str(manifest.path.expanduser().resolve()),
        "--db-path",
        str(db),
        "--state",
        str(state),
        "--artifact-dir",
        str(artifact_root),
        "--backup-dir",
        str(backup_root),
        "--apply",
    ]
    if allow_live_db:
        args.append("--allow-live-db")
    out_path = artifact_root / "launchd.out.log"
    err_path = artifact_root / "launchd.err.log"
    payload = {
        "Label": label,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "StandardOutPath": str(out_path),
        "StandardErrorPath": str(err_path),
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {"PYTHONPATH": str(repo_root)},
    }
    _write_bytes_atomic(plist, plistlib.dumps(payload, sort_keys=True))
    return plist


def _write_bytes_atomic(path: str | Path, payload: bytes) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _source_fingerprints(manifest: PaiManifest) -> dict[str, dict[str, Any]]:
    fingerprints: dict[str, dict[str, Any]] = {}
    for source in manifest.sources:
        content = source.source_text.encode("utf-8")
        fingerprints[source.source_path] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "agent_id": source.agent_id,
            "person_id": source.person_id,
            "project_scope": source.project_scope,
            "source_kind": source.source_kind,
            "original_substrate": source.original_substrate,
            "original_timestamp": source.original_timestamp,
        }
    return fingerprints


def _changed_sources(
    state: dict[str, Any],
    current: dict[str, dict[str, Any]],
    *,
    manifest: PaiManifest,
) -> tuple[str, ...]:
    previous = state.get("sources", {})
    if not isinstance(previous, dict):
        return tuple(sorted(current))

    if state.get("job_id") not in {None, manifest.job_id}:
        return tuple(sorted(current))

    manifest_path = str(manifest.path.expanduser().resolve())
    if state.get("manifest_path") not in {None, manifest_path}:
        return tuple(sorted(current))

    changed = set(previous) - set(current)
    semantic_keys = {
        "sha256",
        "agent_id",
        "person_id",
        "project_scope",
        "source_kind",
        "original_substrate",
        "original_timestamp",
    }
    for source_path, fingerprint in current.items():
        prior = previous.get(source_path)
        if not isinstance(prior, dict):
            changed.add(source_path)
            continue
        for key in semantic_keys:
            if prior.get(key) != fingerprint.get(key):
                changed.add(source_path)
                break
    return tuple(sorted(changed))


def _read_watch_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path).expanduser()
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid PAI watch state JSON: {state_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PAI watch state must be a JSON object")
    if payload.get("schema") != WATCH_STATE_SCHEMA:
        raise ValueError(f"Unsupported PAI watch state schema: {payload.get('schema')!r}")
    return payload


def _write_watch_state(
    path: str | Path,
    *,
    manifest: PaiManifest,
    source_fingerprints: dict[str, dict[str, Any]],
) -> None:
    state_path = Path(path).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": WATCH_STATE_SCHEMA,
        "job_id": manifest.job_id,
        "manifest_path": str(manifest.path.expanduser().resolve()),
        "sources": source_fingerprints,
        "updated_at": int(time.time()),
    }
    tmp = state_path.with_name(f".{state_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(state_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _resolve_python_executable(python_executable: str | Path | None) -> str:
    raw = str(python_executable or sys.executable)
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if not candidate.exists():
            raise FileNotFoundError(f"Python executable not found: {candidate}")
        if candidate.is_absolute():
            return str(candidate)
        return os.path.abspath(candidate)
    resolved = shutil.which(raw)
    if resolved is None:
        raise FileNotFoundError(f"Python executable not found on PATH: {raw}")
    return resolved


def _assert_python_can_import_mnemos(python: str, *, cwd: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(cwd)
    check = subprocess.run(
        [python, "-c", "import mnemos.cli"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        detail = (check.stderr or check.stdout).strip()
        raise RuntimeError(
            f"Python executable cannot import mnemos.cli: {python}"
            + (f" ({detail})" if detail else "")
        )


def _watch_artifact_path(
    artifact_dir: str | Path | None,
    *,
    manifest: PaiManifest,
    mode: str,
) -> Path | None:
    if artifact_dir is None:
        return None
    root = Path(artifact_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    clean_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", manifest.job_id).strip("-") or "pai"
    return root / f"{clean_job}.{mode}.{int(time.time())}.{time.time_ns()}.json"
