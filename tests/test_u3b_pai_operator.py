import json
import sqlite3
from pathlib import Path

import mnemos.importer.operator as pai_operator
from mnemos.cli import main
from mnemos.importer import (
    ACTION_INSERT,
    ACTION_NOOP,
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    apply_pai_manifest,
    load_pai_manifest,
    preview_pai_manifest,
)
from mnemos.store.sqlite_store import EngramStore


def _write_manifest(tmp_path: Path, *, source_name: str = "identity.md") -> Path:
    source = tmp_path / source_name
    source.write_text("# Core\nI am Oliver.", encoding="utf-8")
    beliefs = tmp_path / "beliefs.md"
    beliefs.write_text("David context is foundational.", encoding="utf-8")
    manifest = tmp_path / "pai-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "u3b-operator-test",
                "defaults": {
                    "original_substrate": "claude-opus-4-6",
                    "original_timestamp": 1710000000,
                },
                "sources": {
                    source.name: "identity_kernel",
                    beliefs.name: {
                        "source_kind": "beliefs",
                        "original_timestamp": 1710000100,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_u3b_manifest_mapping_loads_sources_with_defaults(tmp_path):
    manifest_path = _write_manifest(tmp_path)

    manifest = load_pai_manifest(manifest_path)

    assert manifest.job_id == "u3b-operator-test"
    assert [source.source_kind for source in manifest.sources] == [
        "identity_kernel",
        "beliefs",
    ]
    assert manifest.sources[0].original_substrate == "claude-opus-4-6"
    assert manifest.sources[0].original_timestamp == 1710000000
    assert manifest.sources[1].original_timestamp == 1710000100
    assert Path(manifest.sources[0].source_path).is_absolute()


def test_u3b_manifest_rejects_sources_outside_manifest_directory(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "identity.md"
    outside.write_text("# Core\nI am outside.", encoding="utf-8")
    manifest = tmp_path / "pai-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "u3b-operator-test",
                "defaults": {"original_substrate": "claude-opus-4-6"},
                "sources": {str(outside): "identity_kernel"},
            }
        ),
        encoding="utf-8",
    )

    try:
        try:
            load_pai_manifest(manifest)
        except ValueError as exc:
            assert "within the manifest directory" in str(exc)
        else:
            raise AssertionError("external manifest source should be rejected")
    finally:
        outside.unlink(missing_ok=True)
        outside_dir.rmdir()


def test_u3b_preview_writes_operator_artifact(tmp_path):
    manifest_path = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    artifact = tmp_path / "preview.json"

    run = preview_pai_manifest(
        db_path=db_path,
        manifest_path=manifest_path,
        artifact_path=artifact,
    )

    assert run.counts == {ACTION_INSERT: 2}
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema"] == ARTIFACT_SCHEMA
    assert payload["mode"] == "preview"
    assert payload["job_id"] == "u3b-operator-test"
    assert payload["counts"] == {ACTION_INSERT: 2}
    assert payload["has_errors"] is False
    assert {row["source_kind"] for row in payload["rows"]} == {
        "identity_kernel",
        "beliefs",
    }
    assert payload["rows"][0]["content_preview"]


def test_u3b_apply_backs_up_before_target_writes(tmp_path):
    manifest_path = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    backup_dir = tmp_path / "backups"
    artifact = tmp_path / "apply.json"
    store = EngramStore(db_path)
    store.close()

    run = apply_pai_manifest(
        db_path=db_path,
        manifest_path=manifest_path,
        artifact_path=artifact,
        backup_dir=backup_dir,
    )

    assert run.counts == {ACTION_INSERT: 2}
    assert run.backup_path is not None
    assert run.backup_path.exists()
    assert json.loads(artifact.read_text(encoding="utf-8"))["backup_path"] == str(run.backup_path)

    with sqlite3.connect(run.backup_path) as conn:
        row_count = conn.execute("SELECT COUNT(*) FROM pai_import_row_map").fetchone()[0]
    assert row_count == 0

    store = EngramStore(db_path)
    try:
        engram_rows = [row for row in run.result.rows if row.target_table == "engrams"]
        assert store.get_engram(engram_rows[0].target_id) is not None
    finally:
        store.close()


def test_u3b_backup_paths_do_not_collide_inside_one_second(tmp_path, monkeypatch):
    db_path = tmp_path / "representative.db"
    db_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(pai_operator.time, "strftime", lambda _fmt: "20260626-010203")
    values = iter((100, 101))
    monkeypatch.setattr(pai_operator.time, "time_ns", lambda: next(values))

    first = pai_operator._backup_path(db_path, "same-job", tmp_path / "backups")
    second = pai_operator._backup_path(db_path, "same-job", tmp_path / "backups")

    assert first != second
    assert first.name.endswith(".100.backup.db")
    assert second.name.endswith(".101.backup.db")


def test_u3b_cli_preview_apply_and_rerun_noop(tmp_path, capsys):
    manifest_path = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()

    preview_artifact = tmp_path / "cli-preview.json"
    result = main(
        [
            "pai-import",
            "preview",
            "--manifest",
            str(manifest_path),
            "--db-path",
            str(db_path),
            "--artifact",
            str(preview_artifact),
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI import preview" in out
    assert "Counts:   insert=2" in out
    assert preview_artifact.exists()

    apply_artifact = tmp_path / "cli-apply.json"
    result = main(
        [
            "pai-import",
            "apply",
            "--manifest",
            str(manifest_path),
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
    assert "PAI import apply" in out
    assert "Backup:" in out
    assert "Counts:   insert=2" in out
    assert apply_artifact.exists()

    rerun = preview_pai_manifest(db_path=db_path, manifest_path=manifest_path)
    assert rerun.counts == {ACTION_NOOP: 2}


def test_u3b_cli_refuses_default_live_db_without_override(tmp_path, capsys):
    manifest_path = _write_manifest(tmp_path)
    artifact = tmp_path / "should-not-exist.json"

    result = main(
        [
            "pai-import",
            "preview",
            "--manifest",
            str(manifest_path),
            "--db-path",
            str(Path("~/.mnemos/memory.db").expanduser()),
            "--artifact",
            str(artifact),
        ]
    )
    err = capsys.readouterr().err

    assert result == 1
    assert "refuses the default live database" in err
    assert not artifact.exists()
