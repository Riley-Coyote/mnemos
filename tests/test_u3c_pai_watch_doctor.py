import json
import plistlib
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from mnemos.cli import main
from mnemos.importer import (
    MANIFEST_SCHEMA,
    apply_pai_manifest,
    pai_watch_once,
    run_pai_watch_doctor,
    write_pai_watch_launchd_plist,
)
from mnemos.store.sqlite_store import EngramStore
import mnemos.importer.operator as pai_operator
import mnemos.importer.watcher as watcher_module


def _write_manifest(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "identity.md"
    source.write_text("# A\nalpha\n\n# B\nbravo", encoding="utf-8")
    manifest = tmp_path / "pai-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "watch-doctor-test",
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


def _doctor_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "state_path": tmp_path / "watch-state.json",
        "artifact_dir": tmp_path / "artifacts",
        "backup_dir": tmp_path / "backups",
    }


def _check_status(report, ident: str) -> str:
    return next(check.status for check in report.checks if check.ident == ident)


def test_u3c_watch_doctor_passes_with_representative_db_and_plist(tmp_path, capsys):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )

    result = main(
        [
            "pai-import",
            "watch-doctor",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
            "--state",
            str(paths["state_path"]),
            "--artifact-dir",
            str(paths["artifact_dir"]),
            "--backup-dir",
            str(paths["backup_dir"]),
            "--backup-keep",
            "2",
            "--plist",
            str(plist),
            "--python",
            sys.executable,
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "PAI watch doctor" in out
    assert "[PASS] D5" in out
    assert "Verdict: GREEN" in out
    assert not paths["state_path"].exists()


def test_u3c_watch_doctor_without_plist_is_not_green(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "SKIP"
    assert "required" in next(check.evidence for check in report.checks if check.ident == "D5")


def test_u3c_watch_doctor_cli_requires_plist(tmp_path, capsys):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "pai-import",
                "watch-doctor",
                "--manifest",
                str(manifest),
                "--db-path",
                str(db_path),
                "--state",
                str(paths["state_path"]),
                "--artifact-dir",
                str(paths["artifact_dir"]),
                "--backup-dir",
                str(paths["backup_dir"]),
                "--backup-keep",
                "2",
                "--python",
                sys.executable,
            ]
        )

    assert exc.value.code == 2
    assert "--plist" in capsys.readouterr().err


def test_u3c_watch_doctor_fails_stale_clone_plist(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )
    payload = plistlib.loads(plist.read_bytes())
    payload["WorkingDirectory"] = str(tmp_path / "stale-clone")
    payload["EnvironmentVariables"]["PYTHONPATH"] = str(tmp_path / "stale-clone")
    plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        plist_path=plist,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "FAIL"
    assert "stale clone" in next(
        check.evidence for check in report.checks if check.ident == "D5"
    )


def test_u3c_watch_doctor_requires_backup_keep_in_plist(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        python_executable=sys.executable,
        **paths,
    )

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        plist_path=plist,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "FAIL"
    assert "missing --backup-keep" in next(
        check.evidence for check in report.checks if check.ident == "D5"
    )


def test_u3c_watch_doctor_live_db_requires_matching_plist_flag(tmp_path, monkeypatch):
    fake_live = tmp_path / ".mnemos" / "memory.db"
    fake_live.parent.mkdir(parents=True)
    monkeypatch.setattr(pai_operator, "DEFAULT_LIVE_DB_PATH", fake_live)
    manifest, _source = _write_manifest(tmp_path)
    db_path = fake_live.parent / "watch.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        allow_live_db=True,
        **paths,
    )
    payload = plistlib.loads(plist.read_bytes())
    payload["ProgramArguments"].remove("--allow-live-db")
    plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        plist_path=plist,
        python_executable=sys.executable,
        allow_live_db=True,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "FAIL"
    assert "must include --allow-live-db" in next(
        check.evidence for check in report.checks if check.ident == "D5"
    )


def test_u3c_watch_doctor_rejects_duplicate_plist_path_flags(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )
    payload = plistlib.loads(plist.read_bytes())
    payload["ProgramArguments"].extend(["--db-path", str(tmp_path / "other.db")])
    plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        plist_path=plist,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "FAIL"
    assert "duplicate --db-path" in next(
        check.evidence for check in report.checks if check.ident == "D5"
    )


def test_u3c_watch_doctor_rejects_duplicate_plist_retention_flags(tmp_path):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)
    plist = tmp_path / "watch.plist"
    write_pai_watch_launchd_plist(
        plist_path=plist,
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )
    payload = plistlib.loads(plist.read_bytes())
    payload["ProgramArguments"].extend(["--backup-keep", "99"])
    plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        plist_path=plist,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D5") == "FAIL"
    assert "duplicate --backup-keep" in next(
        check.evidence for check in report.checks if check.ident == "D5"
    )


def test_u3c_watch_doctor_preview_catches_wal_family_mutation(tmp_path, monkeypatch):
    manifest, _source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    paths = _doctor_paths(tmp_path)

    def mutate_wal(**kwargs):
        Path(kwargs["db_path"]).with_name(Path(kwargs["db_path"]).name + "-wal").write_bytes(
            b"dirty-wal"
        )
        return SimpleNamespace(preview=SimpleNamespace(rows=[]), counts={})

    monkeypatch.setattr(watcher_module, "preview_pai_watch_manifest", mutate_wal)

    report = run_pai_watch_doctor(
        manifest_path=manifest,
        db_path=db_path,
        backup_keep=2,
        python_executable=sys.executable,
        **paths,
    )

    assert report.ok is False
    assert _check_status(report, "D1") == "FAIL"
    assert "mutated representative DB bytes" in next(
        check.evidence for check in report.checks if check.ident == "D1"
    )


def test_u3c_backup_keep_prunes_old_matching_backups(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    backup_dir = tmp_path / "backups"
    EngramStore(db_path).close()

    apply_pai_manifest(
        db_path=db_path,
        manifest_path=manifest,
        backup_dir=backup_dir,
        backup_keep=1,
    )
    source.write_text("# A\nalpha changed\n\n# B\nbravo", encoding="utf-8")
    apply_pai_manifest(
        db_path=db_path,
        manifest_path=manifest,
        backup_dir=backup_dir,
        backup_keep=1,
    )

    backups = sorted(backup_dir.glob("representative.watch-doctor-test.*.backup.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        contents = [
            row[0]
            for row in conn.execute(
                "SELECT content FROM engrams WHERE content LIKE '# A%'"
            )
        ]
    assert any("alpha" in content for content in contents)
    assert all("alpha changed" not in content for content in contents)


def test_u3c_backup_keep_does_not_prune_unrelated_jobs(tmp_path):
    manifest, source = _write_manifest(tmp_path)
    other_manifest = tmp_path / "other-manifest.json"
    other_manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": "other-job",
                "defaults": {
                    "original_substrate": "claude-opus-4-6",
                    "original_timestamp": 1710000000,
                },
                "sources": {source.name: "identity_kernel"},
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "representative.db"
    backup_dir = tmp_path / "backups"
    EngramStore(db_path).close()

    apply_pai_manifest(
        db_path=db_path,
        manifest_path=other_manifest,
        backup_dir=backup_dir,
        backup_keep=1,
    )
    apply_pai_manifest(
        db_path=db_path,
        manifest_path=manifest,
        backup_dir=backup_dir,
        backup_keep=1,
    )
    source.write_text("# A\nalpha changed\n\n# B\nbravo", encoding="utf-8")
    apply_pai_manifest(
        db_path=db_path,
        manifest_path=manifest,
        backup_dir=backup_dir,
        backup_keep=1,
    )

    assert len(sorted(backup_dir.glob("representative.watch-doctor-test.*.backup.db"))) == 1
    assert len(sorted(backup_dir.glob("representative.other-job.*.backup.db"))) == 1


def test_u3c_crash_before_state_write_does_not_hide_changed_source(tmp_path, monkeypatch):
    manifest, source = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    paths = _doctor_paths(tmp_path)
    EngramStore(db_path).close()
    apply_pai_manifest(db_path=db_path, manifest_path=manifest)
    source.unlink()

    def fail_state_write(*args, **kwargs):
        raise RuntimeError("simulated state write crash")

    monkeypatch.setattr("mnemos.importer.watcher._write_watch_state", fail_state_write)
    with pytest.raises(RuntimeError, match="simulated state write crash"):
        pai_watch_once(
            db_path=db_path,
            manifest_path=manifest,
            apply=True,
            backup_keep=3,
            **paths,
        )
    assert not paths["state_path"].exists()

    monkeypatch.undo()
    replay = pai_watch_once(
        db_path=db_path,
        manifest_path=manifest,
        apply=True,
        backup_keep=3,
        **paths,
    )

    assert replay.changed is True
    assert replay.state_written is True
    assert paths["state_path"].exists()


class PaiLifecycleMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="mnemos-pai-stateful-")
        self.root = Path(self._tmp.name)
        self.db = self.root / "stateful.db"
        self.source = self.root / "identity.md"
        self.manifest = self.root / "manifest.json"
        self.state = self.root / "state.json"
        self.artifacts = self.root / "artifacts"
        self.backups = self.root / "backups"
        self.a_text = "alpha"
        self.b_text = "bravo"
        self.b_present = True
        self.source_exists = True
        EngramStore(self.db).close()
        self._write_source()
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": MANIFEST_SCHEMA,
                    "job_id": "stateful-watch",
                    "defaults": {
                        "original_substrate": "hypothesis",
                        "original_timestamp": 1710000000,
                    },
                    "sources": {self.source.name: "identity_kernel"},
                }
            ),
            encoding="utf-8",
        )

    def teardown(self):
        self._tmp.cleanup()

    def _write_source(self):
        if not self.source_exists:
            self.source.unlink(missing_ok=True)
            return
        parts = [f"# A\n{self.a_text}"]
        if self.b_present:
            parts.append(f"# B\n{self.b_text}")
        self.source.write_text("\n\n".join(parts), encoding="utf-8")

    @rule()
    def force_cycle(self):
        pai_watch_once(
            db_path=self.db,
            manifest_path=self.manifest,
            state_path=self.state,
            artifact_dir=self.artifacts,
            backup_dir=self.backups,
            backup_keep=3,
            apply=True,
            force=True,
        )

    @rule()
    def normal_cycle(self):
        pai_watch_once(
            db_path=self.db,
            manifest_path=self.manifest,
            state_path=self.state,
            artifact_dir=self.artifacts,
            backup_dir=self.backups,
            backup_keep=3,
            apply=True,
        )

    @rule()
    def edit_a(self):
        self.source_exists = True
        self.a_text += "!"
        self._write_source()

    @rule()
    def remove_b(self):
        self.source_exists = True
        self.b_present = False
        self._write_source()

    @rule()
    def restore_b(self):
        self.source_exists = True
        self.b_present = True
        self.b_text += "?"
        self._write_source()

    @rule()
    def delete_source_file(self):
        self.source_exists = False
        self._write_source()

    @invariant()
    def row_map_targets_are_coherent(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT target_table, target_id, tombstone_at
                FROM pai_import_row_map
                WHERE job_id = 'stateful-watch'
                """
            ).fetchall()
            for row in rows:
                if row["target_table"] != "engrams":
                    continue
                target = conn.execute(
                    "SELECT state FROM engrams WHERE id = ?",
                    (row["target_id"],),
                ).fetchone()
                assert target is not None
                if row["tombstone_at"] is None:
                    assert target["state"] == "active"
                else:
                    assert target["state"] == "archived"
        finally:
            conn.close()

    @invariant()
    def lifecycle_events_are_not_duplicated(self):
        conn = sqlite3.connect(self.db)
        try:
            events = conn.execute(
                """
                SELECT target_id, action
                FROM pai_import_events
                WHERE action IN (
                    'insert', 'repair', 'update', 'tombstone', 'deactivate', 'review'
                )
                ORDER BY event_id
                """
            ).fetchall()
            open_lifecycle: dict[tuple[str, str], str] = {}
            for target_id, action in events:
                if action in {"insert", "repair", "update"}:
                    for key in list(open_lifecycle):
                        if key[0] == target_id:
                            del open_lifecycle[key]
                    continue
                key = (target_id, action)
                assert key not in open_lifecycle
                open_lifecycle[key] = action
        finally:
            conn.close()

    @invariant()
    def backup_retention_bound_holds(self):
        backups = sorted(self.backups.glob("stateful.stateful-watch.*.backup.db"))
        assert len(backups) <= 3


TestPaiLifecycleMachine = PaiLifecycleMachine.TestCase
TestPaiLifecycleMachine.settings = settings(
    max_examples=20,
    stateful_step_count=10,
    deadline=None,
)
