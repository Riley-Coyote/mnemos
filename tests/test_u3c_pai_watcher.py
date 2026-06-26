import json
import plistlib
from pathlib import Path

from mnemos.cli import main
from mnemos.importer import (
    ACTION_TOMBSTONE,
    MANIFEST_SCHEMA,
    WATCH_STATE_SCHEMA,
    apply_pai_manifest,
    pai_watch_once,
    write_pai_watch_launchd_plist,
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
                "job_id": "u3c-watch-test",
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


def test_u3c_watch_once_applies_changed_sources_and_records_state(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)

    source.write_text("# A\nalpha", encoding="utf-8")
    first = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert first.changed is True
    assert first.state_written is True
    assert first.operator_run is not None
    assert first.operator_run.counts[ACTION_TOMBSTONE] == 1
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["schema"] == WATCH_STATE_SCHEMA
    assert payload["job_id"] == "u3c-watch-test"

    second = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
    )
    assert second.changed is False
    assert second.operator_run is None
    assert second.state_written is False


def test_u3c_watch_once_preview_does_not_advance_state(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)

    source.write_text("# A\nalpha", encoding="utf-8")
    preview = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        apply=False,
    )

    assert preview.changed is True
    assert preview.operator_run is not None
    assert preview.state_written is False
    assert not state_path.exists()


def test_u3c_launchd_plist_points_to_watch_once_apply(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    plist_path = tmp_path / "com.davidef.mnemos.duallife.plist"

    written = write_pai_watch_launchd_plist(
        plist_path=plist_path,
        manifest_path=manifest,
        db_path=tmp_path / "representative.db",
        state_path=tmp_path / "watch-state.json",
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        label="com.example.mnemos.duallife",
        interval_seconds=60,
        python_executable="/usr/bin/python3",
        allow_live_db=True,
    )

    payload = plistlib.loads(written.read_bytes())
    assert payload["Label"] == "com.example.mnemos.duallife"
    assert payload["StartInterval"] == 60
    args = payload["ProgramArguments"]
    assert args[:5] == ["/usr/bin/python3", "-m", "mnemos.cli", "pai-import", "watch-once"]
    assert "--apply" in args
    assert "--allow-live-db" in args


def test_u3c_cli_watch_once_apply_and_plist(tmp_path, capsys):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    source.write_text("# A\nalpha", encoding="utf-8")

    result = main(
        [
            "pai-import",
            "watch-once",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
            "--state",
            str(state_path),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--apply",
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI watch apply" in out
    assert state_path.exists()

    plist = tmp_path / "watch.plist"
    result = main(
        [
            "pai-import",
            "watch-plist",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
            "--state",
            str(state_path),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--plist",
            str(plist),
            "--python",
            "/usr/bin/python3",
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI watch launchd plist" in out
    assert plist.exists()
