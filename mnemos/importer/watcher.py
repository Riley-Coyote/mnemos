"""U3c dual-life watcher helpers for PAI source mirrors."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sys
import time
from typing import Any

from .operator import (
    PaiManifest,
    PaiOperatorRun,
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
    manifest = load_pai_manifest(manifest_path)
    state = _read_watch_state(state_path)
    current = _source_fingerprints(manifest)
    changed_sources = _changed_sources(state.get("sources", {}), current)
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
        _write_watch_state(
            state_path,
            manifest=manifest,
            source_fingerprints=current,
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
    python = str(python_executable or sys.executable)
    args = [
        python,
        "-m",
        "mnemos.cli",
        "pai-import",
        "watch-once",
        "--manifest",
        str(Path(manifest_path).expanduser()),
        "--db-path",
        str(Path(db_path).expanduser()),
        "--state",
        str(Path(state_path).expanduser()),
        "--artifact-dir",
        str(Path(artifact_dir).expanduser()),
        "--backup-dir",
        str(Path(backup_dir).expanduser()),
        "--apply",
    ]
    if allow_live_db:
        args.append("--allow-live-db")
    out_path = Path(artifact_dir).expanduser() / "launchd.out.log"
    err_path = Path(artifact_dir).expanduser() / "launchd.err.log"
    payload = {
        "Label": label,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "StandardOutPath": str(out_path),
        "StandardErrorPath": str(err_path),
    }
    plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
    return plist


def _source_fingerprints(manifest: PaiManifest) -> dict[str, dict[str, Any]]:
    fingerprints: dict[str, dict[str, Any]] = {}
    for source in manifest.sources:
        path = Path(source.source_path)
        stat = path.stat()
        fingerprints[source.source_path] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "source_kind": source.source_kind,
        }
    return fingerprints


def _changed_sources(
    previous: dict[str, Any],
    current: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    changed = []
    for source_path, fingerprint in current.items():
        prior = previous.get(source_path)
        if not isinstance(prior, dict) or prior.get("sha256") != fingerprint["sha256"]:
            changed.append(source_path)
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
        "manifest_path": str(manifest.path.expanduser()),
        "sources": source_fingerprints,
        "updated_at": int(time.time()),
    }
    tmp = state_path.with_name(f"{state_path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path)


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
