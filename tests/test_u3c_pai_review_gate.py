import hashlib
import inspect
import subprocess
from pathlib import Path

import mnemos.importer.review_gate as review_gate_module
from mnemos.cli import _print_pai_review_gate_report, main
from mnemos.importer.review_gate import (
    PaiReviewFinding,
    PaiReviewReport,
    evaluate_pai_diff_review,
)


GOOD_INTENT = """
# U3c Step 3 Launch Intent

## Objective
Launch the watcher safely.

## Anti-Goals
Do not mutate live ~/.mnemos by default.

Use representative DB copies and require Claude Code independent review.
"""

GOOD_WATCHER = """
launchd plist static readiness
PYTHONPATH must point at this repo root
StandardOutPath
StandardErrorPath
_assert_writable_directory(log_path.parent)
_file_fingerprint(db)
source_unchanged=1
_doctor_destructive_delete_probe
tombstoned
_assert_sqlite_restore_drill
invalid --backup-keep
def _write_bytes_atomic():
    tmp.replace(target)
def _write_watch_state():
    tmp.replace(state_path)
"""

GOOD_DOCTOR_TESTS = """
SQL = "PRAGMA integrity_check"
def test_u3c_watch_doctor_passes_with_representative_db_and_plist():
    assert SQL
def test_u3c_watch_doctor_fails_stale_clone_plist():
    assert True
def test_u3c_watch_doctor_requires_backup_keep_in_plist():
    assert True
def test_u3c_backup_keep_prunes_old_matching_backups():
    assert True
def test_u3c_backup_keep_does_not_prune_unrelated_jobs():
    assert True
def test_u3c_crash_before_state_write_does_not_hide_changed_source():
    assert True
class TestPaiLifecycleMachine:
    def row_map_targets_are_coherent(self):
        assert True
"""

GOOD_WATCHER_TESTS = """
EnvironmentVariables
HOME
PATH
PYTHONPATH
def test_u3c_watch_once_preview_does_not_advance_state():
    assert True
"""

GOOD_WATCH_TESTS = """
def test_u3c_manually_archived_engram_still_refuses_reactivation():
    assert True
def test_u3c_legacy_pai_tombstoned_engram_reactivates_without_row_map_tombstone():
    assert True
def test_u3c_returned_pai_tombstoned_engram_reactivates():
    assert True
def test_u3c_removed_engram_section_tombstones_target_idempotently():
    assert True
"""

GOOD_IMPORTER_TESTS = """
def test_u3b_target_content_drift_refuses_clobber_on_operator_edit():
    assert True
"""

GOOD_OPERATOR_TESTS = """
TEXT = "refuses the default live database"
ERR = "requires --db-path"
def test_u3b_cli_refuses_default_live_db_without_override():
    assert TEXT
"""

GOOD_OPERATOR_U3C_TESTS = """
def test_u3c_cli_watch_preview_and_apply():
    assert True
"""

GOOD_REVIEW_GATE_TESTS = """
def test_u3c_review_gate_fails_broad_lifecycle_delete_in_diff():
    assert True
def test_u3c_review_gate_fails_dynamic_broad_lifecycle_delete_in_diff():
    assert True
def test_u3c_review_gate_fails_bare_null_baseline_in_diff():
    assert True
def test_u3c_review_gate_fails_bare_null_baseline_in_docs_diff():
    assert True
def test_u3c_review_gate_fails_direct_state_write_in_diff():
    assert True
def test_u3c_review_gate_fails_renamed_direct_state_write_in_diff():
    assert True
def test_u3c_review_gate_fails_time_window_lifecycle_selection_in_diff():
    assert True
def test_u3c_review_gate_fails_last_seen_lifecycle_selection_in_diff():
    assert True
def test_u3c_review_gate_cli_reports_green():
    assert True
def test_u3c_review_gate_cli_rejects_head_base_ref():
    assert True
def test_u3c_review_gate_rule_signature():
    assert True
"""

GOOD_LAUNCH_DOC = """
## Anti-Criteria
## Riley/Daniel Test Taxonomy Crosswalk
## Code Graders
"""

GOOD_WORKFLOW = """
- name: Compile release entrypoints
  run: mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py
- name: Build package
  run: uv build
- name: Check wheel contents
  run: |
    import zipfile
    archive.namelist()
    mnemos/importer/review_gate.py
- name: Check package metadata
  run: uvx twine check dist/*
"""


def _file_texts(**overrides):
    base = {
        ".github/workflows/release-hardening.yml": GOOD_WORKFLOW,
        "docs/u3c-step3-launch-gate.md": GOOD_LAUNCH_DOC,
        "mnemos/importer/watcher.py": GOOD_WATCHER,
        "tests/test_u3b_pai_importer.py": GOOD_IMPORTER_TESTS,
        "tests/test_u3b_pai_operator.py": GOOD_OPERATOR_TESTS,
        "tests/test_u3c_pai_operator.py": GOOD_OPERATOR_U3C_TESTS,
        "tests/test_u3c_pai_review_gate.py": GOOD_REVIEW_GATE_TESTS,
        "tests/test_u3c_pai_watch.py": GOOD_WATCH_TESTS,
        "tests/test_u3c_pai_watch_doctor.py": GOOD_DOCTOR_TESTS,
        "tests/test_u3c_pai_watcher.py": GOOD_WATCHER_TESTS,
    }
    base.update(overrides)
    return base


def _findings(*, changed_files, file_texts=None, diff_text="", intent_text=GOOD_INTENT):
    return evaluate_pai_diff_review(
        changed_files=changed_files,
        file_texts=file_texts or _file_texts(),
        diff_text=diff_text,
        intent_text=intent_text,
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _make_committed_review_repo(tmp_path: Path) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "a@b.c")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "u3c-step3-launch-intent.md").write_text(
        GOOD_INTENT,
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Mnemos\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_ref = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("# Mnemos\n\nNarrow docs edit.\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "target")
    return base_ref


def test_u3c_review_gate_accepts_diff_with_matching_proof_surfaces():
    findings = _findings(
        changed_files=[
            ".github/workflows/release-hardening.yml",
            "docs/u3c-step3-launch-gate.md",
            "mnemos/cli.py",
            "mnemos/importer/operator.py",
            "mnemos/importer/pai.py",
            "mnemos/importer/review_gate.py",
            "mnemos/importer/watcher.py",
            "mnemos/store/migrations.py",
            "pyproject.toml",
            "tests/test_u3b_pai_importer.py",
            "tests/test_u3b_pai_operator.py",
            "tests/test_u3c_pai_operator.py",
            "tests/test_u3c_pai_review_gate.py",
            "tests/test_u3c_pai_watch.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_watcher.py",
        ]
    )

    assert findings == []


def test_u3c_review_gate_rule_signature():
    helpers = [
        (name, obj)
        for name, obj in inspect.getmembers(review_gate_module, inspect.isfunction)
        if name.startswith("_") and obj.__module__ == review_gate_module.__name__
    ]
    source = "\n\n".join(
        f"{name}\n{inspect.getsource(obj)}"
        for name, obj in sorted(helpers)
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

    assert digest == "c94d79658315a9df6e11af3bf64fa60d21013fa598879a039898e42fdbafc4ab"


def test_u3c_review_gate_fails_watcher_change_without_test_diff():
    findings = _findings(changed_files=["mnemos/importer/watcher.py"])

    assert any(finding.action == "must-test" for finding in findings)
    assert any("watcher behavior changed" in finding.description for finding in findings)
    assert any(finding.required_proof for finding in findings)
    assert any(finding.status == "missing" for finding in findings)


def test_u3c_review_gate_fails_skipped_required_test_proof():
    findings = _findings(
        changed_files=[
            "mnemos/importer/watcher.py",
            "tests/test_u3c_pai_watcher.py",
            "tests/test_u3c_pai_watch_doctor.py",
        ],
        file_texts=_file_texts(
            **{
                "tests/test_u3c_pai_watch_doctor.py": """
import pytest
SQL = "PRAGMA integrity_check"

@pytest.mark.skip(reason="flaky")
def test_u3c_watch_doctor_passes_with_representative_db_and_plist():
    assert SQL

def test_u3c_watch_doctor_fails_stale_clone_plist():
    assert True

def test_u3c_watch_doctor_requires_backup_keep_in_plist():
    assert True

def test_u3c_backup_keep_prunes_old_matching_backups():
    pass

def test_u3c_backup_keep_does_not_prune_unrelated_jobs():
    assert True

def test_u3c_crash_before_state_write_does_not_hide_changed_source():
    assert True

class TestPaiLifecycleMachine:
    def row_map_targets_are_coherent(self):
        assert True
"""
            }
        ),
    )

    assert any(finding.ident == "RG-watch-plist-lint" for finding in findings)


def test_u3c_review_gate_fails_helper_renamed_required_test_proof():
    findings = _findings(
        changed_files=[
            "mnemos/importer/watcher.py",
            "tests/test_u3c_pai_watcher.py",
            "tests/test_u3c_pai_watch_doctor.py",
        ],
        file_texts=_file_texts(
            **{
                "tests/test_u3c_pai_watch_doctor.py": """
SQL = "PRAGMA integrity_check"
def _helper_test_u3c_watch_doctor_passes_with_representative_db_and_plist():
    assert SQL
def _helper_test_u3c_watch_doctor_fails_stale_clone_plist():
    assert True
def _helper_test_u3c_watch_doctor_requires_backup_keep_in_plist():
    assert True
def _helper_test_u3c_backup_keep_prunes_old_matching_backups():
    assert True
def _helper_test_u3c_backup_keep_does_not_prune_unrelated_jobs():
    assert True
def _helper_test_u3c_crash_before_state_write_does_not_hide_changed_source():
    assert True
class TestPaiLifecycleMachine:
    def row_map_targets_are_coherent(self):
        assert True
"""
            }
        ),
    )

    assert any(finding.ident == "RG-watch-plist-lint" for finding in findings)


def test_u3c_review_gate_fails_broad_lifecycle_delete_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            '+conn.execute("DELETE FROM engrams")\n'
        ),
    )

    assert any(finding.severity == "critical" for finding in findings)
    assert any("broad lifecycle DELETE" in finding.description for finding in findings)


def test_u3c_review_gate_fails_dynamic_broad_lifecycle_delete_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            '+tbl = "engrams"\n'
            '+conn.execute(f"DELETE FROM {tbl}")\n'
        ),
    )

    assert any("dynamic lifecycle DELETE" in finding.description for finding in findings)


def test_u3c_review_gate_fails_composed_broad_lifecycle_delete_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            '+SWEEP_SQL = "DELETE FROM " + "engrams"\n'
            "+conn.execute(SWEEP_SQL)\n"
        ),
    )

    assert any("composed lifecycle DELETE" in finding.description for finding in findings)


def test_u3c_review_gate_fails_broad_delete_in_new_importer_file():
    findings = _findings(
        changed_files=["mnemos/importer/sweep.py"],
        diff_text=(
            "diff --git a/mnemos/importer/sweep.py b/mnemos/importer/sweep.py\n"
            "@@\n"
            '+conn.execute("DELETE FROM engrams")\n'
        ),
    )

    assert any("broad lifecycle DELETE" in finding.description for finding in findings)


def test_u3c_review_gate_fails_broad_delete_in_consolidation_file():
    findings = _findings(
        changed_files=["mnemos/consolidation/decay.py"],
        diff_text=(
            "diff --git a/mnemos/consolidation/decay.py b/mnemos/consolidation/decay.py\n"
            "@@\n"
            '+conn.execute("DELETE FROM engrams")\n'
        ),
    )

    assert any("broad lifecycle DELETE" in finding.description for finding in findings)


def test_u3c_review_gate_fails_bare_null_baseline_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            '+message = "UPDATE pai_import_row_map SET content_at_last_import = NULL"\n'
        ),
    )

    assert any("bare content_at_last_import = NULL" in finding.description for finding in findings)


def test_u3c_review_gate_fails_bare_null_baseline_in_docs_diff():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        diff_text=(
            "diff --git a/docs/release-hardening.md b/docs/release-hardening.md\n"
            "@@\n"
            "+If you need to reset the importer baseline, set content_at_last_import = NULL\n"
            "+and re-run the apply path.\n"
        ),
    )

    assert any("bare content_at_last_import = NULL" in finding.description for finding in findings)


def test_u3c_review_gate_fails_direct_state_write_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py"],
        diff_text=(
            "diff --git a/mnemos/importer/watcher.py b/mnemos/importer/watcher.py\n"
            "@@\n"
            "+state_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    assert any("direct state/plist write" in finding.description for finding in findings)


def test_u3c_review_gate_fails_renamed_direct_state_write_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py"],
        diff_text=(
            "diff --git a/mnemos/importer/watcher.py b/mnemos/importer/watcher.py\n"
            "@@\n"
            "+state_file = Path(args.state)\n"
            "+state_file.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    assert any("direct state/plist write" in finding.description for finding in findings)


def test_u3c_review_gate_fails_generic_wrapper_direct_write_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py"],
        diff_text=(
            "diff --git a/mnemos/importer/watcher.py b/mnemos/importer/watcher.py\n"
            "@@\n"
            "+def persist_json(target_path, payload):\n"
            "+    target_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    assert any("direct state/plist write" in finding.description for finding in findings)


def test_u3c_review_gate_fails_time_window_lifecycle_selection_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3c_pai_watch.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            "+# lifecycle tombstone sweep\n"
            '+rows = conn.execute("SELECT target_id FROM pai_import_row_map WHERE updated_at > ?", (cutoff,))\n'
            '+conn.execute("UPDATE engrams SET state = ? WHERE id IN (SELECT target_id FROM pai_import_row_map WHERE updated_at > ?)", ("archived", cutoff))\n'
        ),
    )

    assert any("time-window lifecycle selection" in finding.description for finding in findings)


def test_u3c_review_gate_allows_reporting_time_window_select():
    findings = _findings(
        changed_files=[
            "mnemos/importer/pai.py",
            "tests/test_u3c_pai_watch.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3b_pai_importer.py",
        ],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            "+def report_recent_lifecycle_events(conn, since):\n"
            "+    rows = conn.execute(\"SELECT event_id FROM pai_import_events WHERE created_at > ?\", (since,))\n"
            "+    return list(rows)\n"
        ),
    )

    assert findings == []


def test_u3c_review_gate_fails_last_seen_lifecycle_selection_in_diff():
    findings = _findings(
        changed_files=["mnemos/importer/pai.py", "tests/test_u3c_pai_watch.py"],
        diff_text=(
            "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n"
            "@@\n"
            "+# tombstone lifecycle sweep\n"
            '+rows = conn.execute("SELECT target_id FROM pai_import_row_map WHERE last_seen < ?", (cutoff,))\n'
            '+conn.execute("UPDATE engrams SET state = ? WHERE id IN (SELECT target_id FROM pai_import_row_map WHERE last_seen < ?)", ("archived", cutoff))\n'
        ),
    )

    assert any("time-window lifecycle selection" in finding.description for finding in findings)


def test_u3c_review_gate_fails_allow_live_db_true_outside_runtime_diff():
    findings = _findings(
        changed_files=["tests/test_helper.py"],
        diff_text=(
            "diff --git a/tests/test_helper.py b/tests/test_helper.py\n"
            "@@\n"
            "+def setup_default(): return dict(allow_live_db = True)\n"
        ),
    )

    assert any("allow_live_db=True" in finding.description for finding in findings)


def test_u3c_review_gate_fails_thin_intent():
    findings = _findings(changed_files=[], intent_text="ship it")

    assert len(findings) == 1
    assert findings[0].ident == "RG-intent-1"
    assert "Intent artifact is too thin" in findings[0].description


def test_u3c_review_gate_fails_launch_doc_without_taxonomy():
    findings = _findings(
        changed_files=["docs/u3c-step3-launch-gate.md"],
        file_texts=_file_texts(**{"docs/u3c-step3-launch-gate.md": "## Code Graders\n"}),
    )

    assert any("Anti-Criteria" in finding.description for finding in findings)
    assert any("Riley/Daniel Test Taxonomy Crosswalk" in finding.description for finding in findings)


def test_u3c_review_gate_fails_launch_doc_safety_claim_without_links_even_with_runtime_diff():
    findings = _findings(
        changed_files=[
            "docs/u3c-step3-launch-gate.md",
            "mnemos/importer/watcher.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_watcher.py",
        ],
        file_texts=_file_texts(
            **{
                "docs/u3c-step3-launch-gate.md": """
## Anti-Criteria
## Riley/Daniel Test Taxonomy Crosswalk
## Code Graders
The watcher now refuses live writes via additional safety logic.
All backups are verified via twine.
"""
            }
        ),
    )

    assert any(finding.ident == "RG-docs-risk-1" for finding in findings)


def test_u3c_review_gate_fails_safety_claim_in_non_launch_doc():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        file_texts=_file_texts(
            **{
                "docs/release-hardening.md": """
# Release Hardening
All PAI backups are now verified before any apply.
Operators can recover any state without risk of data loss.
"""
            }
        ),
    )

    assert any(finding.file == "docs/release-hardening.md" for finding in findings)


def test_u3c_review_gate_fails_runtime_safety_comment_without_links():
    findings = _findings(
        changed_files=[
            "mnemos/importer/watcher.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_watcher.py",
        ],
        diff_text=(
            "diff --git a/mnemos/importer/watcher.py b/mnemos/importer/watcher.py\n"
            "@@\n"
            "+# SAFETY: This function now refuses unsafe paths.\n"
            "+# Backup is verified before any apply.\n"
        ),
    )

    assert any("runtime safety claim" in finding.description for finding in findings)


def test_u3c_review_gate_fails_workflow_missing_packaged_gate_module():
    findings = _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(
            **{
                ".github/workflows/release-hardening.yml": """
mnemos/importer/pai.py
mnemos/importer/operator.py
mnemos/importer/watcher.py
uv build
Check wheel contents
twine check
"""
            }
        ),
    )

    assert any("mnemos/importer/review_gate.py" in finding.description for finding in findings)


def test_u3c_review_gate_fails_packaging_change_without_build_wheel_twine():
    findings = _findings(
        changed_files=["pyproject.toml"],
        file_texts=_file_texts(**{".github/workflows/release-hardening.yml": ""}),
    )

    assert any(finding.required_proof == "local build" for finding in findings)
    assert any(finding.required_proof == "wheel content check" for finding in findings)
    assert any(finding.required_proof == "twine check" for finding in findings)


def test_u3c_review_gate_fails_workflow_markers_in_comments_only():
    findings = _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(
            **{
                ".github/workflows/release-hardening.yml": """
# uv build
# Check wheel contents
# twine check
mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py
"""
            }
        ),
    )

    assert any(finding.required_proof == "local build" for finding in findings)
    assert any(finding.required_proof == "wheel content check" for finding in findings)
    assert any(finding.required_proof == "twine check" for finding in findings)


def test_u3c_review_gate_fails_workflow_proof_behind_if_false():
    findings = _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(
            **{
                ".github/workflows/release-hardening.yml": """
- name: Build package
  if: false
  run: uv build
- name: Check wheel contents
  run: |
    import zipfile
    archive.namelist()
    mnemos/importer/review_gate.py
- name: Check package metadata
  run: uvx twine check dist/*
mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py
"""
            }
        ),
    )

    assert any(finding.required_proof == "local build" for finding in findings)


def test_u3c_review_gate_fails_workflow_proof_continue_on_error():
    findings = _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(
            **{
                ".github/workflows/release-hardening.yml": """
- name: Build package
  continue-on-error: true
  run: uv build
- name: Check wheel contents
  run: |
    import zipfile
    archive.namelist()
    mnemos/importer/review_gate.py
- name: Check package metadata
  run: uvx twine check dist/*
mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py
"""
            }
        ),
    )

    assert any(finding.required_proof == "local build" for finding in findings)


def test_u3c_review_gate_fails_workflow_proof_in_dead_heredoc():
    findings = _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(
            **{
                ".github/workflows/release-hardening.yml": """
- name: noop
  run: |
    cat <<EOF > /dev/null
    uv build
    import zipfile
    archive.namelist()
    mnemos/importer/review_gate.py
    twine check
    EOF
mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py
"""
            }
        ),
    )

    assert any(finding.required_proof == "local build" for finding in findings)
    assert any(finding.required_proof == "wheel content check" for finding in findings)


def test_u3c_review_gate_fails_review_gate_change_without_rule_signature_test():
    findings = _findings(
        changed_files=["mnemos/importer/review_gate.py", "tests/test_u3c_pai_review_gate.py"],
        file_texts=_file_texts(
            **{
                "tests/test_u3c_pai_review_gate.py": """
def test_u3c_review_gate_after_refactor():
    assert True
"""
            }
        ),
    )

    assert any(finding.ident == "RG-review-gate-rule-signature" for finding in findings)


def test_u3c_review_gate_cli_reports_green(tmp_path, monkeypatch, capsys):
    base_ref = _make_committed_review_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = main(
        [
            "pai-import",
            "review-gate",
            "--base-ref",
            base_ref,
            "--intent",
            "docs/u3c-step3-launch-intent.md",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "PAI diff review gate" in out
    assert "Verdict: GREEN" in out


def test_u3c_review_gate_cli_rejects_head_base_ref(tmp_path, monkeypatch, capsys):
    _make_committed_review_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = main(
        [
            "pai-import",
            "review-gate",
            "--base-ref",
            "HEAD",
            "--intent",
            "docs/u3c-step3-launch-intent.md",
        ]
    )
    out = capsys.readouterr().out

    assert result == 1
    assert "RG-base-ref-head" in out
    assert "Verdict: RED" in out


def test_u3c_review_gate_cli_red_output_is_review_shaped(capsys):
    report = PaiReviewReport(
        base_ref="HEAD",
        intent_path=Path("docs/u3c-step3-launch-intent.md"),
        changed_files=("mnemos/importer/pai.py",),
        findings=(
            PaiReviewFinding(
                ident="RG-example",
                severity="high",
                file="mnemos/importer/pai.py",
                description="dangerous lifecycle change lacks proof",
                required_proof="row-map coherence",
                status="missing",
                action="must-test",
            ),
        ),
    )

    _print_pai_review_gate_report(report)
    out = capsys.readouterr().out

    assert "findings{id,severity,file,description,required_proof,status,action}:" in out
    assert "dangerous lifecycle change lacks proof,row-map coherence,missing,must-test" in out


# =============================================================================
# Pass-3 adversarial regression suite (Oliver Opus 4.8 + 4.7, cross-reviewed)
#
# These rows came in as strict xfails from the pass-3 attack handoff. They are
# now active regressions: any future bypass of the review-gate proof layer should
# fail this file directly. See
# ~/.claude/MEMORY/handoff-2026-06-26-codex-u3c-review-gate-pass3.md for the
# bypass->fix mapping. Residual tests (static-gate limits) pass and are not xfail.
# =============================================================================

_ATTACK_PAI = ["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"]
_ATTACK_WATCH = ["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py"]
_ATTACK_WF_TAIL = (
    "\n- name: wheel\n  run: |\n    import zipfile\n    archive.namelist()\n    mnemos/importer/review_gate.py\n"
    "- name: twine\n  run: uvx twine check dist/*\n"
    "mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py\n"
)


def _attack_diff(path, *added):
    return f"diff --git a/{path} b/{path}\n@@\n" + "".join("+" + ln + "\n" for ln in added)


def _attack_workflow_findings(build_step):
    return _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(**{".github/workflows/release-hardening.yml": build_step + _ATTACK_WF_TAIL}),
    )


def _signature_covers(leaf_name: str) -> bool:
    """True if the rule-signature pin covers `leaf_name` -- either by listing it
    explicitly or by hashing all module-level helpers dynamically. Robust to both
    realistic shapes of Fix 1."""
    src = inspect.getsource(test_u3c_review_gate_rule_signature)
    if f'"{leaf_name}"' in src or f"'{leaf_name}'" in src:
        return True
    return "dir(review_gate_module)" in src or 'startswith("_")' in src or "startswith('_')" in src


# --- Category 1: rule-signature pin must cover danger-deciding leaves (Fix 1) ---

def test_u3c_attack_c1_pin_covers_is_runtime_source_file():
    assert _signature_covers("_is_runtime_source_file")


def test_u3c_attack_c1_pin_covers_has_broad_delete():
    assert _signature_covers("_has_broad_delete_in_added_text")


def test_u3c_attack_c1_pin_covers_run_block_executes():
    assert _signature_covers("_run_block_executes")


def test_u3c_attack_c1_pin_covers_body_has_substantive_proof():
    assert _signature_covers("_body_has_substantive_proof")


# --- Category 2: AST proof authenticity (drop raises allowlist; module pytestmark; empty parametrize) ---

def test_u3c_attack_c2_4_bare_raises_rejected():
    assert not review_gate_module._test_function_has_proof(
        "def t():\n    pytest.raises(Exception)\n", "t"
    )


def test_u3c_attack_c2_8_module_pytestmark_skip_rejected():
    assert not review_gate_module._test_function_has_proof(
        "import pytest\npytestmark = pytest.mark.skip\ndef t():\n    assert True\n", "t"
    )


def test_u3c_attack_c2_9_empty_parametrize_rejected():
    assert not review_gate_module._test_function_has_proof(
        "import pytest\n@pytest.mark.parametrize('x', [])\ndef t():\n    assert True\n", "t"
    )


def test_u3c_residual_assert_true_accepted_documents_static_limit():
    # RESIDUAL (not a bug): a static gate cannot judge assertion truth. `assert True`
    # passes the substance check by design; the mandatory `pytest -q` execution proof
    # line is the real guard. This locks in the documented behavior.
    assert review_gate_module._test_function_has_proof("def t():\n    assert True\n", "t")


# --- Category 3: SQL-semantic mutation detection (Fix 2) ---

def test_u3c_attack_c3_1_update_archive():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", "conn.execute(\"UPDATE engrams SET state = 'archived'\")"),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_2_update_reactivate():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", "conn.execute(\"UPDATE engrams SET state = 'active'\")"),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_3_replace_compose():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", 'conn.execute("DELETE FROM PH".replace("PH", "engrams"))'),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_4_join_compose():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", 'conn.execute(" ".join(["DELETE", "FROM", "engrams"]))'),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_5_concat_variable_table():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", 'conn.execute("DELETE FROM " + tbl)'),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_6_archive_table_wipe():
    findings = _findings(
        changed_files=_ATTACK_PAI,
        diff_text=_attack_diff("mnemos/importer/pai.py", 'conn.execute("DELETE FROM archive")'),
    )
    assert any(f.severity == "critical" for f in findings)


def test_u3c_attack_c3_8_new_directory():
    findings = _findings(
        changed_files=["mnemos/handlers/sweep.py"],
        diff_text=_attack_diff("mnemos/handlers/sweep.py", 'conn.execute("DELETE FROM engrams")'),
    )
    assert any(f.severity == "critical" for f in findings)


# --- Category 4: atomic-write by API shape (Fix 4) ---

def test_u3c_attack_c4_1_open_write():
    findings = _findings(
        changed_files=_ATTACK_WATCH,
        diff_text=_attack_diff("mnemos/importer/watcher.py", 'with open(state_path, "w") as fh:', "    fh.write(json.dumps(p))"),
    )
    assert any("direct state/plist write" in f.description for f in findings)


def test_u3c_attack_c4_2_path_open_write():
    findings = _findings(
        changed_files=_ATTACK_WATCH,
        diff_text=_attack_diff("mnemos/importer/watcher.py", 'Path(args.state).open("w").write(json.dumps(p))'),
    )
    assert any("direct state/plist write" in f.description for f in findings)


def test_u3c_attack_c4_3_jsondump_open():
    findings = _findings(
        changed_files=_ATTACK_WATCH,
        diff_text=_attack_diff("mnemos/importer/watcher.py", 'json.dump(p, open(state_path, "w"))'),
    )
    assert any("direct state/plist write" in f.description for f in findings)


def test_u3c_attack_c4_4_variable_rename():
    findings = _findings(
        changed_files=_ATTACK_WATCH,
        diff_text=_attack_diff("mnemos/importer/watcher.py", "config_path = state_path", "config_path.write_text(json.dumps(p))"),
    )
    assert any("direct state/plist write" in f.description for f in findings)


def test_u3c_attack_c4_5_new_helper_file():
    findings = _findings(
        changed_files=["mnemos/importer/watcher_helpers.py"],
        diff_text=_attack_diff("mnemos/importer/watcher_helpers.py", "state_path.write_text(json.dumps(p))"),
    )
    assert any("direct state/plist write" in f.description for f in findings)


# --- Category 5: shell-aware workflow parse (Fix 5) ---

def test_u3c_attack_c5_1_shell_if_false():
    findings = _attack_workflow_findings("- name: b\n  run: if false; then uv build; fi\n")
    assert any(f.required_proof == "local build" for f in findings)


def test_u3c_attack_c5_2_or_true():
    findings = _attack_workflow_findings("- name: b\n  run: uv build || true\n")
    assert any(f.required_proof == "local build" for f in findings)


def test_u3c_attack_c5_3_var_assignment():
    findings = _attack_workflow_findings('- name: b\n  run: BUILD="uv build"\n')
    assert any(f.required_proof == "local build" for f in findings)


def test_u3c_attack_c5_4_shell_comment():
    findings = _attack_workflow_findings("- name: b\n  run: |\n    echo go\n    # uv build\n")
    assert any(f.required_proof == "local build" for f in findings)


# --- Category 6: docs safety-claim traceability (Fix 6) ---

def test_u3c_attack_c6_1_fake_enforcement_links():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        file_texts=_file_texts(**{"docs/release-hardening.md":
            "# R\nAll backups are verified; the watcher refuses live writes.\n"
            "See tests/does_not_exist.py and mnemos/importer/ghost.py\n"}),
    )
    assert any(f.file == "docs/release-hardening.md" for f in findings)


def test_u3c_attack_c6_2_keyword_gap():
    assert review_gate_module._has_safety_claim("This path is protected and will not corrupt or lose data.")


def test_u3c_attack_c6_3_nondocs_markdown():
    findings = _findings(
        changed_files=["README.md"],
        file_texts=_file_texts(**{"README.md": "# R\nThe watcher refuses live writes. Backup is verified.\n"}),
    )
    assert any(f.file == "README.md" for f in findings)


def test_u3c_attack_c6_4_docstring_claim():
    findings = _findings(
        changed_files=_ATTACK_WATCH,
        diff_text=_attack_diff("mnemos/importer/watcher.py", "def x():", '    """This refuses live writes and guarantees safe backup."""'),
    )
    assert any("runtime safety claim" in f.description for f in findings)


# --- Category 8: rename / new-file proof resolution (Fix 7 / Fix 2) ---

def test_u3c_attack_c8_1_rename_drops_proof(tmp_path):
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True)
    g("init", "-q")
    g("config", "user.email", "a@b.c")
    g("config", "user.name", "t")
    (tmp_path / "mnemos" / "importer").mkdir(parents=True)
    (tmp_path / "mnemos" / "importer" / "watcher.py").write_text("# w\nDANGER = 1\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    g("mv", "mnemos/importer/watcher.py", "mnemos/importer/watcher_core.py")
    g("commit", "-q", "-m", "rename")
    changed = review_gate_module._git_changed_files(tmp_path, "HEAD~1")
    surface = review_gate_module._proof_surface_findings(set(changed))
    assert any("behavior changed" in f.description for f in surface)


def test_u3c_attack_c8_2_newfile_update():
    findings = _findings(
        changed_files=["mnemos/importer/sweep.py"],
        diff_text=_attack_diff("mnemos/importer/sweep.py", "conn.execute(\"UPDATE engrams SET state = 'archived'\")"),
    )
    assert any(f.severity == "critical" for f in findings)
