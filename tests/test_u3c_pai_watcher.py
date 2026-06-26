import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.cli import main
from mnemos.importer import (
    ACTION_NOOP,
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


def test_u3c_watch_once_detects_removed_manifest_source(tmp_path):
    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("# A\nalpha", encoding="utf-8")
    source_b.write_text("# B\nbravo", encoding="utf-8")
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
                "sources": {
                    source_a.name: "identity_kernel",
                    source_b.name: "identity_kernel",
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
        force=True,
    )

    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "u3c-watch-test",
                "defaults": {
                    "original_substrate": "claude-opus-4-6",
                    "original_timestamp": 1710000000,
                },
                "sources": {source_a.name: "identity_kernel"},
            }
        ),
        encoding="utf-8",
    )
    run = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert run.changed_sources == (str(source_b),)
    assert run.operator_run is not None
    assert run.operator_run.counts == {ACTION_NOOP: 1, ACTION_TOMBSTONE: 1}


def test_u3c_watch_once_treats_deleted_source_file_as_empty_snapshot(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
        force=True,
    )

    source.unlink()
    run = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert run.changed_sources == (str(source),)
    assert run.operator_run is not None
    assert run.operator_run.counts == {ACTION_TOMBSTONE: 2}

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


def test_u3c_watch_once_detects_manifest_metadata_change(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    state_path = tmp_path / "watch-state.json"
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
        force=True,
    )

    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "u3c-watch-test",
                "defaults": {
                    "original_substrate": "claude-opus-4-6",
                    "original_timestamp": 1710000001,
                },
                "sources": {source.name: "identity_kernel"},
            }
        ),
        encoding="utf-8",
    )
    run = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert run.changed_sources == (str(source),)
    assert run.operator_run is not None
    assert run.operator_run.counts == {"update": 2}


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
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    plist_path = tmp_path / "com.davidef.mnemos.duallife.plist"

    written = write_pai_watch_launchd_plist(
        plist_path=plist_path,
        manifest_path=manifest,
        db_path=db_path,
        state_path=tmp_path / "watch-state.json",
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        label="com.example.mnemos.duallife",
        interval_seconds=60,
        python_executable=sys.executable,
    )

    payload = plistlib.loads(written.read_bytes())
    assert payload["Label"] == "com.example.mnemos.duallife"
    assert payload["StartInterval"] == 60
    args = payload["ProgramArguments"]
    assert args[:5] == [sys.executable, "-m", "mnemos.cli", "pai-import", "watch-once"]
    assert "--apply" in args
    assert "--allow-live-db" not in args
    assert Path(payload["StandardOutPath"]).parent.exists()
    assert Path(payload["StandardErrorPath"]).parent.exists()
    assert payload["WorkingDirectory"] == str(Path.cwd())
    completed = subprocess.run(
        args,
        cwd=payload["WorkingDirectory"],
        env={"PYTHONPATH": payload["EnvironmentVariables"]["PYTHONPATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_u3c_launchd_plist_write_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    plist_path = tmp_path / "com.davidef.mnemos.duallife.plist"
    plist_path.write_bytes(b"existing-plist")
    seen: dict[str, str] = {}

    def fail_replace(self, target):
        seen["tmp_name"] = self.name
        seen["target"] = str(target)
        raise RuntimeError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(RuntimeError, match="simulated replace failure"):
        write_pai_watch_launchd_plist(
            plist_path=plist_path,
            manifest_path=manifest,
            db_path=db_path,
            state_path=tmp_path / "watch-state.json",
            artifact_dir=tmp_path / "artifacts",
            backup_dir=tmp_path / "backups",
            label="com.example.mnemos.duallife",
            interval_seconds=60,
            python_executable=sys.executable,
        )

    assert seen["tmp_name"].startswith(f".{plist_path.name}.")
    assert seen["tmp_name"].endswith(".tmp")
    assert seen["target"] == str(plist_path)
    assert plist_path.read_bytes() == b"existing-plist"
    assert not list(tmp_path.glob(f".{plist_path.name}.*.tmp"))


def test_u3c_launchd_plist_resolves_relative_python_executable(tmp_path, monkeypatch):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_link = venv_bin / "python"
    python_link.symlink_to(sys.executable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "mnemos.importer.watcher._assert_python_can_import_mnemos",
        lambda python, *, cwd: None,
    )

    written = write_pai_watch_launchd_plist(
        plist_path=tmp_path / "relpy.plist",
        manifest_path=manifest,
        db_path=db_path,
        state_path=tmp_path / "watch-state.json",
        artifact_dir=tmp_path / "artifacts",
        backup_dir=tmp_path / "backups",
        label="com.example.mnemos.duallife",
        interval_seconds=60,
        python_executable="venv/bin/python",
    )

    payload = plistlib.loads(written.read_bytes())
    persisted_python = payload["ProgramArguments"][0]
    assert Path(persisted_python).is_absolute()
    assert Path(persisted_python).exists()
    assert Path(persisted_python).is_symlink()


def test_u3c_launchd_plist_refuses_unsafe_or_invalid_jobs(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()

    with pytest.raises(ValueError, match="refuses the default live database"):
        write_pai_watch_launchd_plist(
            plist_path=tmp_path / "live.plist",
            manifest_path=manifest,
            db_path=Path("~/.mnemos/memory.db").expanduser(),
            state_path=tmp_path / "watch-state.json",
            artifact_dir=tmp_path / "artifacts",
            backup_dir=tmp_path / "backups",
            python_executable=sys.executable,
        )

    with pytest.raises(ValueError, match="interval_seconds"):
        write_pai_watch_launchd_plist(
            plist_path=tmp_path / "fast.plist",
            manifest_path=manifest,
            db_path=db_path,
            state_path=tmp_path / "watch-state.json",
            artifact_dir=tmp_path / "artifacts",
            backup_dir=tmp_path / "backups",
            interval_seconds=9,
            python_executable=sys.executable,
        )


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
            sys.executable,
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "PAI watch launchd plist" in out
    assert plist.exists()


# ── ce-debug: ordered activation path integration test ──


def test_u3c_ordered_activation_path_end_to_end(tmp_path):
    """ce-debug finding (Codex meta-risk): unit checks can all pass while the
    ordered activation path fails. This test walks the full sequence —
    bootstrap → import → no-op → edit → tombstone → reactivate → file-delete
    — against realistic multi-file SOUL-mimicking content in one test. If
    any cycle errors, raises, or produces unexpected counts, this fails.

    Locks in:
    - cycle 1 initial-import (all INSERT)
    - cycle 2 no-change-skip (state matches fingerprints, no apply)
    - cycle 3 single-section edit (1 UPDATE + N NOOPs)
    - cycle 4 section removal (1 TOMBSTONE + N NOOPs)
    - cycle 5 section restoration (RT-C1 reactivation path: UPDATE/REPAIR + N NOOPs)
    - cycle 6 whole-file deletion (RT-H1 path: TOMBSTONE per section + N NOOPs)
    """
    workdir = tmp_path
    db = workdir / "mnemos.db"
    state = workdir / "watch-state.json"
    artifacts = workdir / "artifacts"
    backups = workdir / "backups"
    sources_dir = workdir / "sources"
    sources_dir.mkdir()
    manifest = workdir / "manifest.json"

    def write_v1():
        (sources_dir / "identity.md").write_text(
            "# Nome\nI am Oliver. David's agent.\n\n"
            "# Voce\nDense over sparse. First person, present tense.\n",
            encoding="utf-8",
        )
        (sources_dir / "david.md").write_text(
            "# David\nBoard-certified school neuropsych.\n\n"
            "# Norman\nHusband since 2009.\n",
            encoding="utf-8",
        )
        (sources_dir / "growth.md").write_text(
            "# Plan Mode\nFor uncertainty, not willpower.\n\n"
            "# Verify\nClaimed done is not actually done.\n\n"
            "# Heavy Context\nDistrust the first response at session start.\n",
            encoding="utf-8",
        )

    def write_manifest():
        manifest.write_text(
            json.dumps(
                {
                    "schema": MANIFEST_SCHEMA,
                    "job_id": "oap-integration",
                    "defaults": {
                        "original_substrate": "claude-opus-4-7",
                        "original_timestamp": 1782500000,
                    },
                    "sources": {
                        "sources/identity.md": "identity_kernel",
                        "sources/david.md": "david_context",
                        "sources/growth.md": "growth_substrate",
                    },
                }
            ),
            encoding="utf-8",
        )

    def cycle(apply=True):
        return pai_watch_once(
            db_path=db,
            manifest_path=manifest,
            state_path=state,
            artifact_dir=artifacts,
            backup_dir=backups,
            apply=apply,
        )

    def count_engrams_in(query_state):
        conn = EngramStore(db, read_only=True)._get_conn()
        try:
            if query_state is None:
                return conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE state = ?", (query_state,)
            ).fetchone()[0]
        finally:
            conn.close()

    def tombstoned_count():
        conn = EngramStore(db, read_only=True)._get_conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM pai_import_row_map WHERE tombstone_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

    # T0: bootstrap empty DB
    EngramStore(db).close()

    # T1: write sources + manifest
    write_v1()
    write_manifest()

    # Cycle 1: initial import — 7 sections (2 identity + 2 david + 3 growth)
    r1 = cycle()
    assert len(r1.changed_sources) == 3, (
        f"Expected all 3 sources changed, got {r1.changed_sources}"
    )
    assert r1.operator_run is not None
    assert r1.operator_run.counts == {"insert": 7}
    assert r1.state_written
    assert count_engrams_in("active") == 7
    assert tombstoned_count() == 0

    # Cycle 2: no changes — state matches fingerprints, no apply
    r2 = cycle()
    assert r2.changed_sources == ()
    assert r2.operator_run is None
    assert not r2.state_written

    # T5: edit one section in identity.md
    (sources_dir / "identity.md").write_text(
        "# Nome\nI am Oliver. David's agent.\n\n"
        "# Voce\nDense over sparse. Italian when it wants to come.\n",
        encoding="utf-8",
    )

    # Cycle 3: detect single edit — 1 UPDATE + 6 NOOP
    r3 = cycle()
    assert len(r3.changed_sources) == 1
    assert r3.operator_run is not None
    assert r3.operator_run.counts == {"update": 1, "noop": 6}
    assert count_engrams_in("active") == 7

    # T7: remove '# Heavy Context' section from growth.md
    (sources_dir / "growth.md").write_text(
        "# Plan Mode\nFor uncertainty, not willpower.\n\n"
        "# Verify\nClaimed done is not actually done.\n",
        encoding="utf-8",
    )

    # Cycle 4: detect tombstone — RT-C1 path populates tombstone_at
    r4 = cycle()
    assert r4.operator_run is not None
    assert r4.operator_run.counts == {"tombstone": 1, "noop": 6}
    assert count_engrams_in("active") == 6
    assert count_engrams_in("archived") == 1
    assert tombstoned_count() == 1

    # T9: restore '# Heavy Context' with new content
    (sources_dir / "growth.md").write_text(
        "# Plan Mode\nFor uncertainty, not willpower.\n\n"
        "# Verify\nClaimed done is not actually done.\n\n"
        "# Heavy Context\nDistrust the first response. Restored with substance.\n",
        encoding="utf-8",
    )

    # Cycle 5: RT-C1 reactivation path — UPDATE (or REPAIR) the restored section
    r5 = cycle()
    assert r5.operator_run is not None
    counts5 = r5.operator_run.counts
    # Section returned with new content → UPDATE; with same content → REPAIR
    assert "update" in counts5 or "repair" in counts5, (
        f"Expected reactivation via update/repair, got {counts5}"
    )
    assert counts5.get("noop", 0) == 6
    assert count_engrams_in("active") == 7
    assert count_engrams_in("archived") == 0
    assert tombstoned_count() == 0, (
        "Reactivation must clear tombstone_at on the row-map"
    )

    # T11: delete entire david.md source file
    (sources_dir / "david.md").unlink()

    # Cycle 6: RT-H1 path — file-level deletion produces TOMBSTONE per section
    r6 = cycle()
    assert r6.operator_run is not None
    counts6 = r6.operator_run.counts
    assert counts6.get("tombstone", 0) == 2, (
        f"david.md had 2 sections; expected 2 tombstones, got {counts6}"
    )
    assert count_engrams_in("active") == 5
    assert count_engrams_in("archived") == 2
    assert tombstoned_count() == 2

    # Final state file audit
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    assert state_payload["schema"] == WATCH_STATE_SCHEMA
    assert state_payload["job_id"] == "oap-integration"
    # State still lists david.md (manifest still references it); fingerprint
    # will be empty-content hash since file is missing
    assert any("david.md" in path for path in state_payload["sources"])
