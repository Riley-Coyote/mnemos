"""Operator-facing PAI import manifest and CLI helpers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import re
import time
from pathlib import Path
from typing import Any

from ..store.migrations import backup_sqlite_db
from ..store.sqlite_store import EngramStore
from .pai import (
    ACTION_ERROR,
    PaiImportPreview,
    PaiImportResult,
    PaiImportSource,
    apply_pai_import,
    preview_pai_import,
)


MANIFEST_SCHEMA = "mnemos.pai_import.manifest.v1"
ARTIFACT_SCHEMA = "mnemos.pai_import.artifact.v1"
DEFAULT_LIVE_DB_PATH = Path("~/.mnemos/memory.db").expanduser()


@dataclass(frozen=True)
class PaiManifest:
    """Loaded operator manifest plus resolved import sources."""

    path: Path
    job_id: str
    sources: tuple[PaiImportSource, ...]


@dataclass(frozen=True)
class PaiOperatorRun:
    """Result of a preview or apply command, including written artifacts."""

    mode: str
    manifest: PaiManifest
    preview: PaiImportPreview
    artifact_path: Path | None
    backup_path: Path | None = None
    result: PaiImportResult | None = None

    @property
    def counts(self) -> dict[str, int]:
        if self.result is not None:
            return self.result.counts
        return self.preview.counts


def load_pai_manifest(path: str | Path) -> PaiManifest:
    """Load a JSON PAI import manifest into canonical import sources.

    Manifest shape:

    {
      "schema": "mnemos.pai_import.manifest.v1",
      "job_id": "u3b-pai-import",
      "defaults": {
        "original_substrate": "claude-opus-4-6",
        "original_timestamp": 1710000000
      },
      "sources": {
        "identity.md": "identity_kernel",
        "beliefs.md": {
          "source_kind": "beliefs",
          "original_timestamp": 1710000100
        }
      }
    }
    """
    manifest_path = Path(path).expanduser()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid PAI manifest JSON: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PAI manifest must be a JSON object")

    schema = payload.get("schema", MANIFEST_SCHEMA)
    if schema != MANIFEST_SCHEMA:
        raise ValueError(f"Unsupported PAI manifest schema: {schema!r}")
    job_id = _clean_required(payload.get("job_id"), "job_id")
    defaults = payload.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("PAI manifest defaults must be an object")

    sources_payload = payload.get("sources")
    entries = _source_entries(sources_payload)
    if not entries:
        raise ValueError("PAI manifest requires at least one source")

    sources: list[PaiImportSource] = []
    for raw_source_path, raw_config in entries:
        config = _source_config(raw_config)
        source_kind = _clean_required(
            config.get("source_kind") or config.get("kind"), "source_kind"
        )
        source_file = _resolve_source_file(manifest_path, raw_source_path)
        source_text = source_file.read_text(encoding="utf-8")
        original_substrate = _clean_required(
            config.get("original_substrate", defaults.get("original_substrate")),
            "original_substrate",
        )
        original_timestamp = _clean_optional_int(
            config.get("original_timestamp", defaults.get("original_timestamp")),
            "original_timestamp",
        )
        sources.append(
            PaiImportSource(
                job_id=job_id,
                source_path=str(source_file),
                source_kind=source_kind,
                source_text=source_text,
                agent_id=_clean_required(
                    config.get("agent_id", defaults.get("agent_id", "oliver")),
                    "agent_id",
                ),
                person_id=_clean_required(
                    config.get("person_id", defaults.get("person_id", "david")),
                    "person_id",
                ),
                project_scope=_clean_required(
                    config.get("project_scope", defaults.get("project_scope", "pai")),
                    "project_scope",
                ),
                original_substrate=original_substrate,
                original_timestamp=original_timestamp,
            )
        )

    return PaiManifest(path=manifest_path, job_id=job_id, sources=tuple(sources))


def preview_pai_manifest(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    artifact_path: str | Path | None = None,
    allow_live_db: bool = False,
) -> PaiOperatorRun:
    """Preview a manifest import and optionally write a JSON artifact."""
    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db)
    manifest = load_pai_manifest(manifest_path)
    store = EngramStore(db)
    try:
        preview = preview_pai_import(store, manifest.sources)
    finally:
        store.close()

    artifact = _artifact_output_path(artifact_path)
    run = PaiOperatorRun(
        mode="preview",
        manifest=manifest,
        preview=preview,
        artifact_path=artifact,
    )
    if artifact is not None:
        write_pai_import_artifact(run, db_path=db)
    return run


def apply_pai_manifest(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    artifact_path: str | Path | None = None,
    backup_dir: str | Path | None = None,
    allow_live_db: bool = False,
) -> PaiOperatorRun:
    """Backup a database, preview the manifest, then apply the import."""
    db = _checked_operator_db_path(db_path, allow_live_db=allow_live_db)
    if not db.exists():
        raise FileNotFoundError(f"PAI apply requires an existing database: {db}")

    manifest = load_pai_manifest(manifest_path)
    backup_path = _backup_path(db, manifest.job_id, backup_dir)
    backup_sqlite_db(db, backup_path)

    store = EngramStore(db)
    try:
        preview = preview_pai_import(store, manifest.sources)
        errors = [row for row in preview.rows if row.action == ACTION_ERROR]
        if errors:
            run = PaiOperatorRun(
                mode="apply",
                manifest=manifest,
                preview=preview,
                artifact_path=_artifact_output_path(artifact_path),
                backup_path=backup_path,
            )
            if run.artifact_path is not None:
                write_pai_import_artifact(run, db_path=db)
            raise ValueError(errors[0].reason)
        result = apply_pai_import(store, preview)
    finally:
        store.close()

    run = PaiOperatorRun(
        mode="apply",
        manifest=manifest,
        preview=preview,
        artifact_path=_artifact_output_path(artifact_path),
        backup_path=backup_path,
        result=result,
    )
    if run.artifact_path is not None:
        write_pai_import_artifact(run, db_path=db)
    return run


def write_pai_import_artifact(run: PaiOperatorRun, *, db_path: str | Path) -> Path:
    """Write a stable JSON preview/apply artifact for operator review."""
    if run.artifact_path is None:
        raise ValueError("artifact_path is required")
    artifact_path = run.artifact_path.expanduser()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            _artifact_payload(run, db_path=Path(db_path).expanduser()),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _artifact_payload(run: PaiOperatorRun, *, db_path: Path) -> dict[str, Any]:
    rows = run.result.rows if run.result is not None else run.preview.rows
    source_counts: dict[str, Counter] = {}
    for row in rows:
        source_counts.setdefault(row.source_path, Counter())[row.action] += 1

    return {
        "schema": ARTIFACT_SCHEMA,
        "mode": run.mode,
        "job_id": run.manifest.job_id,
        "manifest_path": str(run.manifest.path.expanduser()),
        "db_path": str(db_path.expanduser()),
        "backup_path": str(run.backup_path) if run.backup_path is not None else None,
        "row_count": len(rows),
        "counts": dict(Counter(row.action for row in rows)),
        "has_errors": any(row.action == ACTION_ERROR for row in rows),
        "sources": [
            {
                "source_path": source_path,
                "counts": dict(counts),
            }
            for source_path, counts in sorted(source_counts.items())
        ],
        "rows": [
            {
                "source_path": row.source_path,
                "source_kind": row.source_kind,
                "source_anchor": row.source_anchor,
                "target_table": row.target_table,
                "target_id": row.target_id,
                "source_hash": row.source_hash,
                "action": row.action,
                "reason": row.reason,
                "mapped_source_hash": row.mapped_source_hash,
                "agent_id": row.agent_id,
                "person_id": row.person_id,
                "project_scope": row.project_scope,
                "original_substrate": row.original_substrate,
                "original_timestamp": row.original_timestamp,
                "content_chars": len(row.content),
                "content_preview": _content_preview(row.content),
            }
            for row in rows
        ],
    }


def _source_entries(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [(str(path), config) for path, config in value.items()]
    if isinstance(value, list):
        entries = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"PAI source entry {index} must be an object")
            path = item.get("path") or item.get("source_path")
            if path is None:
                raise ValueError(f"PAI source entry {index} requires path")
            config = dict(item)
            config.pop("path", None)
            config.pop("source_path", None)
            entries.append((str(path), config))
        return entries
    raise ValueError("PAI manifest sources must be an object or list")


def _source_config(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"source_kind": value}
    if isinstance(value, dict):
        return dict(value)
    raise ValueError("PAI source config must be a source kind string or object")


def _resolve_source_file(manifest_path: Path, source_path: str) -> Path:
    raw = _clean_required(source_path, "source_path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    resolved = path.resolve()
    root = manifest_path.parent.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("PAI source path must stay within the manifest directory") from exc
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    if not resolved.is_file():
        raise ValueError(f"PAI source path is not a file: {resolved}")
    return resolved


def _checked_operator_db_path(db_path: str | Path, *, allow_live_db: bool) -> Path:
    db = Path(_clean_required(str(db_path), "db_path")).expanduser()
    if _same_path(db, DEFAULT_LIVE_DB_PATH) and not allow_live_db:
        raise ValueError(
            "PAI import refuses the default live database "
            f"{DEFAULT_LIVE_DB_PATH}; use a representative test DB"
        )
    return db


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _backup_path(db: Path, job_id: str, backup_dir: str | Path | None) -> Path:
    root = Path(backup_dir).expanduser() if backup_dir is not None else db.parent / "pai-import-backups"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = time.time_ns()
    clean_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip("-") or "pai-import"
    return root / f"{db.stem}.{clean_job}.{stamp}.{unique}.backup.db"


def _artifact_output_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser()


def _clean_required(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is required")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} is required")
    return cleaned


def _clean_optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _content_preview(content: str) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= 200:
        return compact
    return compact[:197] + "..."
