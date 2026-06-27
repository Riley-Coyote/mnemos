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
import sqlite3
import subprocess
import sys
import tempfile
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


@dataclass(frozen=True)
class PaiWatchDoctorCheck:
    """One executable launch-readiness check result."""

    ident: str
    label: str
    status: str
    evidence: str


@dataclass(frozen=True)
class PaiWatchDoctorReport:
    """Aggregated U3c watch-doctor result."""

    checks: tuple[PaiWatchDoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.status == "PASS" for check in self.checks)


def pai_watch_once(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    state_path: str | Path,
    artifact_dir: str | Path | None = None,
    backup_dir: str | Path | None = None,
    backup_keep: int | None = None,
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
            backup_keep=backup_keep,
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
    backup_keep: int | None = None,
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
    if backup_keep is not None:
        _validate_backup_keep(backup_keep)
        args.extend(["--backup-keep", str(backup_keep)])
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
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONPATH": str(repo_root),
        },
    }
    _write_bytes_atomic(plist, plistlib.dumps(payload, sort_keys=True))
    return plist


def run_pai_watch_doctor(
    *,
    manifest_path: str | Path,
    db_path: str | Path,
    state_path: str | Path,
    artifact_dir: str | Path,
    backup_dir: str | Path,
    backup_keep: int,
    plist_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    allow_live_db: bool = False,
) -> PaiWatchDoctorReport:
    """Executable launch gate for U3c `pai-import watch-once`.

    The doctor does not mutate the supplied DB. It previews the real manifest
    read-only, copies the DB before subprocess apply probes, and runs a separate
    destructive-delete lifecycle probe against a fresh temp DB.
    """
    harness = _WatchDoctorHarness()
    repo_root = Path(__file__).resolve().parents[2]

    harness.check(
        "D0",
        "manifest loads as watcher snapshot",
        lambda: _doctor_manifest_check(manifest_path),
    )
    harness.check(
        "D1",
        "representative DB preview is read-only",
        lambda: _doctor_preview_check(
            db_path=db_path,
            manifest_path=manifest_path,
            allow_live_db=allow_live_db,
        ),
    )
    harness.check(
        "D2",
        "state artifact backup and log dirs are writable",
        lambda: _doctor_directory_check(
            state_path=state_path,
            artifact_dir=artifact_dir,
            backup_dir=backup_dir,
        ),
    )
    harness.check(
        "D3",
        "bounded backup retention policy is explicit",
        lambda: _doctor_backup_keep_check(backup_keep),
    )
    harness.check(
        "D4",
        "python executable imports this repo",
        lambda: _doctor_python_check(python_executable, repo_root=repo_root),
    )
    if plist_path is None:
        harness.skip("D5", "launchd plist static readiness", "launchd plist is required")
    else:
        harness.check(
            "D5",
            "launchd plist static readiness",
            lambda: _doctor_plist_check(
                plist_path=plist_path,
                manifest_path=manifest_path,
                db_path=db_path,
                state_path=state_path,
                artifact_dir=artifact_dir,
                backup_dir=backup_dir,
                backup_keep=backup_keep,
                python_executable=python_executable,
                allow_live_db=allow_live_db,
                repo_root=repo_root,
            ),
        )
    harness.check(
        "D6",
        "static negative lifecycle gate",
        lambda: _doctor_static_negative_check(repo_root),
    )
    harness.check(
        "D7",
        "watch-once apply runs only on a DB copy",
        lambda: _doctor_subprocess_copy_check(
            manifest_path=manifest_path,
            db_path=db_path,
            backup_keep=backup_keep,
            python_executable=python_executable,
            repo_root=repo_root,
            allow_live_db=allow_live_db,
        ),
    )
    harness.check(
        "D8",
        "destructive delete lifecycle probe",
        lambda: _doctor_destructive_delete_probe(
            backup_keep=backup_keep,
            python_executable=python_executable,
            repo_root=repo_root,
        ),
    )
    return harness.report()


class _WatchDoctorHarness:
    def __init__(self) -> None:
        self.rows: list[PaiWatchDoctorCheck] = []

    def record(self, ident: str, label: str, status: str, evidence: str) -> None:
        self.rows.append(PaiWatchDoctorCheck(ident, label, status, evidence))

    def check(self, ident: str, label: str, fn) -> None:
        try:
            ok, evidence = fn()
        except Exception as exc:  # noqa: BLE001
            self.record(ident, label, "FAIL", f"{type(exc).__name__}: {exc}")
            return
        self.record(ident, label, "PASS" if ok else "FAIL", evidence)

    def skip(self, ident: str, label: str, evidence: str) -> None:
        self.record(ident, label, "SKIP", evidence)

    def report(self) -> PaiWatchDoctorReport:
        return PaiWatchDoctorReport(tuple(self.rows))


def _doctor_manifest_check(manifest_path: str | Path) -> tuple[bool, str]:
    manifest = load_pai_manifest(manifest_path, allow_missing_sources=True)
    missing = [
        source.source_path
        for source in manifest.sources
        if not Path(source.source_path).exists()
    ]
    return True, (
        f"job={manifest.job_id} sources={len(manifest.sources)} "
        f"missing_sources={len(missing)}"
    )


def _doctor_preview_check(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    allow_live_db: bool,
) -> tuple[bool, str]:
    db = _checked_doctor_db_path(db_path, allow_live_db=allow_live_db)
    if not db.exists():
        raise FileNotFoundError(f"representative DB not found: {db}")
    before = _file_fingerprint(db)
    run = preview_pai_watch_manifest(
        db_path=db,
        manifest_path=manifest_path,
        allow_live_db=allow_live_db,
    )
    after = _file_fingerprint(db)
    if before != after:
        return False, "watch preview mutated representative DB bytes"
    return True, f"db={db} rows={len(run.preview.rows)} counts={_doctor_counts(run.counts)}"


def _doctor_directory_check(
    *,
    state_path: str | Path,
    artifact_dir: str | Path,
    backup_dir: str | Path,
) -> tuple[bool, str]:
    state_parent = Path(state_path).expanduser().parent
    artifact_root = Path(artifact_dir).expanduser()
    backup_root = Path(backup_dir).expanduser()
    for path in (state_parent, artifact_root, backup_root):
        _assert_writable_directory(path)
    return True, f"state_parent={state_parent} artifact_dir={artifact_root} backup_dir={backup_root}"


def _doctor_backup_keep_check(backup_keep: int) -> tuple[bool, str]:
    _validate_backup_keep(backup_keep)
    return True, f"backup_keep={backup_keep}"


def _doctor_python_check(
    python_executable: str | Path | None,
    *,
    repo_root: Path,
) -> tuple[bool, str]:
    python = _resolve_python_executable(python_executable)
    _assert_python_can_import_mnemos(python, cwd=repo_root)
    return True, f"python={python} repo={repo_root}"


def _doctor_plist_check(
    *,
    plist_path: str | Path,
    manifest_path: str | Path,
    db_path: str | Path,
    state_path: str | Path,
    artifact_dir: str | Path,
    backup_dir: str | Path,
    backup_keep: int,
    python_executable: str | Path | None,
    allow_live_db: bool,
    repo_root: Path,
) -> tuple[bool, str]:
    plist = Path(plist_path).expanduser()
    payload = plistlib.loads(plist.read_bytes())
    args = payload.get("ProgramArguments")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False, "ProgramArguments must be a string list"
    if args[:5] != [args[0], "-m", "mnemos.cli", "pai-import", "watch-once"]:
        return False, "ProgramArguments must invoke python -m mnemos.cli pai-import watch-once"
    if "--apply" not in args:
        return False, "watch plist must run watch-once with --apply"
    if "--allow-live-db" in args and not allow_live_db:
        return False, "plist opts into live DB without doctor allow_live_db"

    expected_python = _resolve_python_executable(python_executable or args[0])
    if Path(args[0]).expanduser().resolve() != Path(expected_python).resolve():
        return False, f"plist python {args[0]} != expected {expected_python}"
    expected_paths = {
        "--manifest": Path(manifest_path).expanduser().resolve(),
        "--db-path": Path(db_path).expanduser().resolve(),
        "--state": Path(state_path).expanduser().resolve(),
        "--artifact-dir": Path(artifact_dir).expanduser().resolve(),
        "--backup-dir": Path(backup_dir).expanduser().resolve(),
    }
    for flag, expected in expected_paths.items():
        actual, error = _single_arg_value(args, flag)
        if error is not None:
            return False, error
        if Path(actual).expanduser().resolve() != expected:
            return False, f"{flag} points at {actual}, expected {expected}"
    keep_value, error = _single_arg_value(args, "--backup-keep")
    if error is not None:
        return False, error
    try:
        keep_int = int(keep_value)
    except ValueError:
        return False, f"invalid --backup-keep value {keep_value!r}"
    if keep_int != backup_keep:
        return False, f"plist backup_keep={keep_int}, expected {backup_keep}"

    working_dir = Path(str(payload.get("WorkingDirectory", ""))).expanduser().resolve()
    if working_dir != repo_root.resolve():
        return False, f"WorkingDirectory points at stale clone: {working_dir}"
    env = payload.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or Path(str(env.get("PYTHONPATH", ""))).expanduser().resolve() != repo_root.resolve():
        return False, "PYTHONPATH must point at this repo root"
    home = env.get("HOME")
    if not isinstance(home, str) or not home:
        return False, "HOME must be set in EnvironmentVariables"
    path_value = env.get("PATH")
    if not isinstance(path_value, str) or not path_value:
        return False, "PATH must be set in EnvironmentVariables"
    for key in ("StandardOutPath", "StandardErrorPath"):
        log_path = Path(str(payload.get(key, ""))).expanduser()
        if not log_path.is_absolute():
            return False, f"{key} must be absolute"
        _assert_writable_directory(log_path.parent)
    return True, f"label={payload.get('Label')} args={len(args)} repo={repo_root}"


def _doctor_static_negative_check(repo_root: Path) -> tuple[bool, str]:
    files = [
        repo_root / "mnemos" / "importer" / "pai.py",
        repo_root / "mnemos" / "importer" / "operator.py",
        repo_root / "mnemos" / "importer" / "watcher.py",
        repo_root / "mnemos" / "cli.py",
        repo_root / "docs" / "release-hardening.md",
    ]
    findings: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if re.search(
            r"DELETE\s+FROM\s+(engrams|beliefs|hypomnema_entries|pai_import_row_map)\b(?!\s+WHERE\s+(id|target_id|job_id|source_path)\s*=)",
            text,
            flags=re.IGNORECASE,
        ):
            findings.append(f"{path.name}: broad lifecycle delete")
        for match in re.finditer(r"content_at_last_import\s*=\s*NULL", text):
            window = text[max(0, match.start() - 220): match.end() + 220]
            if "DESTRUCTIVE" not in window:
                findings.append(f"{path.name}: bare NULL-baseline recovery text")
        if path.name == "watcher.py":
            if "_write_bytes_atomic" not in text or "tmp.replace(target)" not in text:
                findings.append("watcher.py: plist writes are not visibly atomic")
            if "_write_watch_state" not in text or "tmp.replace(state_path)" not in text:
                findings.append("watcher.py: state writes are not visibly atomic")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            lowered = line.lower()
            if not any(
                term in lowered
                for term in ("time_window", "lookback", "between", "created_at >", "updated_at >", "event_at >")
            ):
                continue
            window = "\n".join(lines[max(0, index - 4): index + 5]).lower()
            if ("tombstone" in window or "lifecycle" in window) and (
                "where" in window or "select" in window
            ):
                findings.append(f"{path.name}: time-window lifecycle selection")
                break
    operator_text = (repo_root / "mnemos" / "importer" / "operator.py").read_text(
        encoding="utf-8"
    )
    if "PAI import refuses the default live database" not in operator_text:
        findings.append("operator.py: default live DB refusal text missing")
    if findings:
        return False, "; ".join(findings)
    return True, f"scanned={len(files)} forbidden_patterns=0"


def _doctor_subprocess_copy_check(
    *,
    manifest_path: str | Path,
    db_path: str | Path,
    backup_keep: int,
    python_executable: str | Path | None,
    repo_root: Path,
    allow_live_db: bool,
) -> tuple[bool, str]:
    db = _checked_doctor_db_path(db_path, allow_live_db=allow_live_db)
    python = _resolve_python_executable(python_executable)
    original_before = _file_fingerprint(db)
    with tempfile.TemporaryDirectory(prefix="mnemos-pai-watch-doctor-") as tmp:
        root = Path(tmp)
        db_copy = root / db.name
        _copy_sqlite_family(db, db_copy)
        state = root / "watch-state.json"
        artifacts = root / "artifacts"
        backups = root / "backups"
        command = [
            python,
            "-m",
            "mnemos.cli",
            "pai-import",
            "watch-once",
            "--manifest",
            str(Path(manifest_path).expanduser().resolve()),
            "--db-path",
            str(db_copy),
            "--state",
            str(state),
            "--artifact-dir",
            str(artifacts),
            "--backup-dir",
            str(backups),
            "--backup-keep",
            str(backup_keep),
            "--apply",
            "--force",
        ]
        completed = _run_doctor_command(command, repo_root=repo_root)
        original_after = _file_fingerprint(db)
        if original_after != original_before:
            return False, "copy apply mutated representative DB bytes"
        if completed.returncode != 0:
            return False, _command_failure_evidence(completed)
        backup_paths = sorted(backups.glob("*.backup.db"))
        artifact_paths = sorted(artifacts.glob("*.json"))
        if not backup_paths:
            return False, "copy apply produced no backup"
        _assert_sqlite_integrity(backup_paths[-1])
        _assert_sqlite_restore_drill(backup_paths[-1])
        if len(backup_paths) > backup_keep:
            return False, f"backup retention exceeded: {len(backup_paths)}>{backup_keep}"
        return True, (
            f"copy={db_copy.name} source_unchanged=1 "
            f"backups={len(backup_paths)} artifacts={len(artifact_paths)}"
        )


def _doctor_destructive_delete_probe(
    *,
    backup_keep: int,
    python_executable: str | Path | None,
    repo_root: Path,
) -> tuple[bool, str]:
    python = _resolve_python_executable(python_executable)
    with tempfile.TemporaryDirectory(prefix="mnemos-pai-delete-probe-") as tmp:
        root = Path(tmp)
        db = root / "probe.db"
        source = root / "identity.md"
        manifest = root / "manifest.json"
        state = root / "state.json"
        artifacts = root / "artifacts"
        backups = root / "backups"
        source.write_text("# A\nalpha\n\n# B\nbravo", encoding="utf-8")
        manifest.write_text(
            json.dumps(
                {
                    "schema": "mnemos.pai_import.manifest.v1",
                    "job_id": "watch-doctor-delete-probe",
                    "defaults": {
                        "original_substrate": "watch-doctor",
                        "original_timestamp": 1710000000,
                    },
                    "sources": {source.name: "identity_kernel"},
                }
            ),
            encoding="utf-8",
        )
        command_base = [
            python,
            "-m",
            "mnemos.cli",
            "pai-import",
            "watch-once",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db),
            "--state",
            str(state),
            "--artifact-dir",
            str(artifacts),
            "--backup-dir",
            str(backups),
            "--backup-keep",
            str(backup_keep),
            "--apply",
        ]
        from ..store.sqlite_store import EngramStore

        EngramStore(db).close()
        initial = _run_doctor_command(command_base + ["--force"], repo_root=repo_root)
        if initial.returncode != 0:
            return False, _command_failure_evidence(initial)
        source.unlink()
        deleted = _run_doctor_command(command_base, repo_root=repo_root)
        if deleted.returncode != 0:
            return False, _command_failure_evidence(deleted)
        with sqlite3.connect(db) as conn:
            archived = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE state = 'archived'"
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE state = 'active'"
            ).fetchone()[0]
            tombstoned = conn.execute(
                "SELECT COUNT(*) FROM pai_import_row_map WHERE tombstone_at IS NOT NULL"
            ).fetchone()[0]
        backup_paths = sorted(backups.glob("*.backup.db"))
        if backup_paths:
            _assert_sqlite_integrity(backup_paths[-1])
        ok = archived == 2 and active == 0 and tombstoned == 2
        evidence = (
            f"archived={archived} active={active} tombstoned={tombstoned} "
            f"backups={len(backup_paths)}"
        )
        return ok, evidence


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


def _validate_backup_keep(backup_keep: int) -> None:
    if isinstance(backup_keep, bool) or backup_keep < 1:
        raise ValueError("backup_keep must be a positive integer")


def _checked_doctor_db_path(db_path: str | Path, *, allow_live_db: bool) -> Path:
    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db)
    live_root = Path("~/.mnemos").expanduser().resolve()
    try:
        db.expanduser().resolve().relative_to(live_root)
    except ValueError:
        return db
    if not allow_live_db:
        raise ValueError(
            "PAI watch doctor refuses live ~/.mnemos databases; use a "
            "representative DB copy or pass --allow-live-db deliberately"
        )
    return db


def _file_fingerprint(path: Path) -> tuple[tuple[str, int, str], ...]:
    main = _fingerprint_member("", path)
    wal_path = path.with_name(path.name + "-wal")
    wal = _fingerprint_member("-wal", wal_path, empty_is_absent=True)
    shm = ("-shm", -1, "")
    if wal[1] > 0:
        shm = _fingerprint_member("-shm", path.with_name(path.name + "-shm"))
    return (main, wal, shm)


def _fingerprint_member(
    label: str,
    path: Path,
    *,
    empty_is_absent: bool = False,
) -> tuple[str, int, str]:
    if not path.exists():
        return (label, -1, "")
    payload = path.read_bytes()
    if empty_is_absent and not payload:
        return (label, -1, "")
    return (label, len(payload), hashlib.sha256(payload).hexdigest())


def _assert_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".mnemos-watch-doctor-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("ok\n", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def _doctor_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ",".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _arg_values(args: list[str], flag: str) -> list[str | None]:
    values: list[str | None] = []
    for index, arg in enumerate(args):
        if arg != flag:
            continue
        next_index = index + 1
        if next_index >= len(args) or args[next_index].startswith("--"):
            values.append(None)
            continue
        values.append(args[next_index])
    return values


def _single_arg_value(args: list[str], flag: str) -> tuple[str | None, str | None]:
    values = _arg_values(args, flag)
    if not values:
        return None, f"plist missing {flag}"
    if len(values) > 1:
        return None, f"plist duplicate {flag}"
    if values[0] is None:
        return None, f"plist missing value for {flag}"
    return values[0], None


def _copy_sqlite_family(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))


def _run_doctor_command(
    command: list[str],
    *,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", os.environ.get("PATH", ""))
    return subprocess.run(
        command,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _command_failure_evidence(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    if len(detail) > 240:
        detail = detail[:237] + "..."
    return f"rc={completed.returncode} {detail}"


def _assert_sqlite_integrity(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if row is None or row[0] != "ok":
        raise RuntimeError(f"backup integrity_check failed for {path}: {row}")


def _assert_sqlite_restore_drill(path: Path) -> None:
    """Prove the backup is usable as a restored DB, not just an openable file."""
    with tempfile.TemporaryDirectory(prefix="mnemos-pai-restore-drill-") as tmp:
        restored = Path(tmp) / "restored.db"
        shutil.copy2(path, restored)
        _assert_sqlite_integrity(restored)
        conn = sqlite3.connect(restored)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engrams'"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RuntimeError(f"backup restore drill missing engrams table: {path}")


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
