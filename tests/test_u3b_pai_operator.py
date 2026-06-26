import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

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
    EngramStore(db_path).close()  # preview is read-only; bootstrap the DB first
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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_u3b_preview_does_not_mutate_db_bytes(tmp_path):
    """Hardening B3-operator-2: preview must not touch the DB on disk.

    Without read-only mode, `EngramStore(db)` runs `executescript`,
    `ALTER TABLE`, `run_migrations`, and writes `meta.schema_version` on every
    instantiation. The probe Boris ran showed schema_version 1→4 and 12KB→217KB
    on what should have been a read-only operation. This test enforces the
    contract at the byte level.
    """
    manifest_path = _write_manifest(tmp_path)
    db_path = tmp_path / "representative.db"
    EngramStore(db_path).close()
    hash_before = _file_sha256(db_path)
    size_before = db_path.stat().st_size

    preview_pai_manifest(db_path=db_path, manifest_path=manifest_path)

    assert _file_sha256(db_path) == hash_before
    assert db_path.stat().st_size == size_before


def test_u3b_preview_against_missing_db_raises(tmp_path):
    """Read-only preview cannot create a DB it doesn't find."""
    manifest_path = _write_manifest(tmp_path)
    missing_db = tmp_path / "does-not-exist.db"

    with pytest.raises(FileNotFoundError, match="requires an existing database"):
        preview_pai_manifest(db_path=missing_db, manifest_path=manifest_path)
    assert not missing_db.exists()


def test_u3b_read_only_engramstore_blocks_writes(tmp_path):
    """The URI `?mode=ro` connection must reject INSERT at SQLite layer."""
    db_path = tmp_path / "ro.db"
    EngramStore(db_path).close()
    ro = EngramStore(db_path, read_only=True)
    try:
        conn = ro._get_conn()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('hardening-probe', '1')"
            )
    finally:
        ro.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filenames cannot contain the '?' URI metacharacter",
)
def test_u3b_read_only_engramstore_escapes_uri_metacharacters(tmp_path):
    db_path = tmp_path / "ro?query#fragment.db"
    EngramStore(db_path).close()
    ro = EngramStore(db_path, read_only=True)
    try:
        conn = ro._get_conn()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('hardening-probe', '1')"
            )
    finally:
        ro.close()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="APFS case-insensitive bypass surface; meaningful only on darwin",
)
def test_u3b_same_path_blocks_apfs_case_variant(tmp_path, monkeypatch):
    """Hardening B3-operator-3: live-DB guard must not be bypassable via case.

    On APFS (default macOS filesystem) ~/.MNEMOS/memory.db and ~/.mnemos/memory.db
    address the same on-disk inode but Path.resolve() preserves the case spelling.
    The resolved-string equality check the guard used before this hardening pass
    returned False for case variants, letting a typo bypass the live-DB refusal
    and silently mutate the production DB. os.path.samefile compares inodes.
    """
    fake_live = tmp_path / ".mnemos" / "memory.db"
    fake_live.parent.mkdir(parents=True, exist_ok=True)
    fake_live.write_bytes(b"placeholder")
    monkeypatch.setattr(pai_operator, "DEFAULT_LIVE_DB_PATH", fake_live)

    case_variant = tmp_path / ".MNEMOS" / "memory.db"
    # On APFS the parent dir created above is case-insensitively addressable
    assert case_variant.exists(), (
        "test precondition: APFS resolves .MNEMOS to .mnemos"
    )

    with pytest.raises(ValueError, match="refuses the default live database"):
        pai_operator._checked_operator_db_path(case_variant, allow_live_db=False)


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
