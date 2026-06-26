import hashlib
import json
from pathlib import Path

from mnemos.cli import main
from mnemos.importer import (
    ACTION_NOOP,
    ACTION_TOMBSTONE,
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    apply_pai_manifest,
    apply_pai_watch_manifest,
    preview_pai_watch_manifest,
)
from mnemos.store.sqlite_store import EngramStore


def _write_manifest(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "identity.md"
    source.write_text("# A\nalpha\n\n# B\nbravo", encoding="utf-8")
    manifest = tmp_path / "pai-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "u3c-operator-test",
                "defaults": {
                    "original_substrate": "claude-opus-4-6",
                    "original_timestamp": 1710000000,
                },
                "sources": {source.name: "identity_kernel"},
            }
        ),
        encoding="utf-8",
    )
    return manifest, source


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_u3c_watch_preview_manifest_is_read_only_and_reports_tombstone(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)

    source.write_text("# A\nalpha", encoding="utf-8")
    hash_before = _file_sha256(db_path)
    size_before = db_path.stat().st_size
    artifact = tmp_path / "watch-preview.json"

    run = preview_pai_watch_manifest(
        db_path=db_path,
        manifest_path=manifest,
        artifact_path=artifact,
    )

    assert run.mode == "watch-preview"
    assert run.counts == {ACTION_NOOP: 1, ACTION_TOMBSTONE: 1}
    assert _file_sha256(db_path) == hash_before
    assert db_path.stat().st_size == size_before
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema"] == ARTIFACT_SCHEMA
    assert payload["mode"] == "watch-preview"
    assert payload["counts"] == {ACTION_NOOP: 1, ACTION_TOMBSTONE: 1}


def test_u3c_watch_apply_manifest_backs_up_and_tombstones_removed_source(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    backup_dir = tmp_path / "backups"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)

    source.write_text("# A\nalpha", encoding="utf-8")
    run = apply_pai_watch_manifest(
        db_path=db_path,
        manifest_path=manifest,
        artifact_path=tmp_path / "watch-apply.json",
        backup_dir=backup_dir,
    )

    assert run.mode == "watch-apply"
    assert run.backup_path is not None
    assert run.backup_path.exists()
    tombstone = next(row for row in run.result.rows if row.action == ACTION_TOMBSTONE)
    store = EngramStore(db_path)
    try:
        row = store._get_conn().execute(
            "SELECT state FROM engrams WHERE id = ?",
            (tombstone.target_id,),
        ).fetchone()
        assert row["state"] == "archived"
    finally:
        store.close()


def test_u3c_cli_watch_preview_and_apply(tmp_path, capsys):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    source.write_text("# A\nalpha", encoding="utf-8")

    preview_artifact = tmp_path / "cli-watch-preview.json"
    result = main(
        [
            "pai-import",
            "watch-preview",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
            "--artifact",
            str(preview_artifact),
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI watch preview" in out
    assert "tombstone=1" in out

    apply_artifact = tmp_path / "cli-watch-apply.json"
    result = main(
        [
            "pai-import",
            "watch-apply",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
            "--artifact",
            str(apply_artifact),
            "--backup-dir",
            str(tmp_path / "cli-backups"),
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI watch apply" in out
    assert "Backup:" in out
