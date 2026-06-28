"""Diff-focused adversarial review gate for the PAI watcher launch path."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Mapping, Sequence


DEFAULT_U3C_INTENT_PATH = Path("docs/u3c-step3-launch-intent.md")
_LIFECYCLE_TABLES = (
    "engrams",
    "beliefs",
    "hypomnema_entries",
    "pai_import_row_map",
    "archive",
    "engrams_fts",
)
_IDENTITY_COLUMNS = ("id", "target_id", "job_id", "source_path")
_IDENTITY_COLUMN_PATTERN = "|".join(_IDENTITY_COLUMNS)
_PERSISTENCE_ALIASES = {"state_path", "plist_path", "target_path", "output_file"}
_SAFE_WATCHER_WRITE_RECEIVERS = {"tmp", "source", "manifest", "probe"}
_U6_6_INNER_LIFE_MARKERS = (
    "inner-life",
    "inner_life",
    "inner_life_events",
    "session-finalize",
    "turn-finalize",
    "u6.6",
)
_U6_6_INNER_LIFE_SURFACE_FILES = {
    "mnemos/cli.py",
    "mnemos/store/migrations.py",
    "mnemos/store/sqlite_store.py",
}
_U6_6_INNER_LIFE_SCHEMA_TEST_FILES = {
    "tests/test_inner_life_ledger.py",
    "tests/test_u3a_schema_migrations.py",
}
_U6_6_INNER_LIFE_FINALIZER_TEST_FILES = {
    "tests/test_inner_life_ledger.py",
    "tests/test_session_finalizer.py",
    "tests/test_turn_finalizer.py",
}


@dataclass(frozen=True)
class PaiReviewFinding:
    """One no-mistakes-style adversarial finding."""

    ident: str
    severity: str
    file: str
    description: str
    required_proof: str
    status: str
    action: str


@dataclass(frozen=True)
class PaiReviewReport:
    """Aggregated diff review report."""

    base_ref: str
    intent_path: Path
    changed_files: tuple[str, ...]
    findings: tuple[PaiReviewFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass(frozen=True)
class _ProofRequirement:
    ident: str
    label: str
    file: str
    markers: tuple[tuple[str, str], ...]


def run_pai_diff_review_gate(
    *,
    repo_root: str | Path | None = None,
    base_ref: str | None = None,
    intent_path: str | Path = DEFAULT_U3C_INTENT_PATH,
) -> PaiReviewReport:
    """Review the current diff against the U3c launch intent.

    This gate is intentionally narrower and colder than the runtime doctor:
    it asks whether the *changed files* carry their matching proof classes.
    """
    root = Path(repo_root or Path.cwd()).expanduser().resolve()
    intent = Path(intent_path).expanduser()
    if not intent.is_absolute():
        intent = root / intent
    display_base_ref = str(base_ref or "").strip()
    if display_base_ref:
        changed_files = _git_changed_files(root, display_base_ref)
        diff_text = _git_diff(root, display_base_ref)
        committed_review_findings = _committed_review_findings(
            repo_root=root,
            base_ref=display_base_ref,
            changed_files=changed_files,
        )
    else:
        changed_files = []
        diff_text = ""
        committed_review_findings = [
            PaiReviewFinding(
                ident="RG-base-ref-required",
                severity="critical",
                file="mnemos/importer/review_gate.py",
                description="Committed diff review requires an explicit non-HEAD base ref",
                required_proof="pass --base-ref as the merge-base or branch point, not HEAD",
                status="missing",
                action="must-fix",
            )
        ]
    file_texts = {
        rel: _read_repo_file(root, rel)
        for rel in _files_needed_for_review(changed_files)
    }
    intent_text = intent.read_text(encoding="utf-8")
    findings = evaluate_pai_diff_review(
        changed_files=changed_files,
        file_texts=file_texts,
        diff_text=diff_text,
        intent_text=intent_text,
        repo_root=root,
    )
    findings = [*committed_review_findings, *findings]
    return PaiReviewReport(
        base_ref=display_base_ref or "(missing)",
        intent_path=intent,
        changed_files=tuple(changed_files),
        findings=tuple(findings),
    )


def evaluate_pai_diff_review(
    *,
    changed_files: Sequence[str],
    file_texts: Mapping[str, str],
    diff_text: str,
    intent_text: str,
    repo_root: Path | None = None,
) -> list[PaiReviewFinding]:
    """Pure evaluator used by tests and the CLI wrapper."""
    changed = set(changed_files)
    findings: list[PaiReviewFinding] = []
    findings.extend(_intent_findings(intent_text))
    findings.extend(
        _proof_surface_findings(
            changed,
            file_texts=file_texts,
            diff_text=diff_text,
        )
    )
    findings.extend(_forbidden_diff_findings(diff_text))
    findings.extend(
        _repository_content_findings(
            changed,
            file_texts,
            diff_text=diff_text,
            repo_root=repo_root,
        )
    )
    return findings


def _committed_review_findings(
    *,
    repo_root: Path,
    base_ref: str,
    changed_files: Sequence[str],
) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    if _git_commit(repo_root, base_ref) == _git_commit(repo_root, "HEAD"):
        findings.append(
            PaiReviewFinding(
                ident="RG-base-ref-head",
                severity="critical",
                file="mnemos/importer/review_gate.py",
                description="Committed diff review cannot use HEAD as its base ref",
                required_proof="compare against the merge-base or branch point, not the target commit",
                status="violated",
                action="must-fix",
            )
        )
    if not changed_files:
        findings.append(
            PaiReviewFinding(
                ident="RG-empty-diff",
                severity="critical",
                file="mnemos/importer/review_gate.py",
                description="Committed diff review found no changed files to review",
                required_proof="changed-file diff against an explicit non-HEAD base",
                status="missing",
                action="must-fix",
            )
        )
    return findings


def _intent_findings(intent_text: str) -> list[PaiReviewFinding]:
    searchable = re.sub(r"[`*_]", "", intent_text.lower())
    required = {
        "objective": "objective",
        "anti-goals": "anti-goals",
        "live ~/.mnemos": "live DB boundary",
        "representative": "representative DB boundary",
        "claude code": "independent Claude Code review",
    }
    missing = [label for marker, label in required.items() if marker not in searchable]
    if not missing:
        return []
    return [
        PaiReviewFinding(
            ident="RG-intent-1",
            severity="high",
            file=str(DEFAULT_U3C_INTENT_PATH),
            description=(
                "Intent artifact is too thin for adversarial review; missing "
                + ", ".join(missing)
            ),
            required_proof="intent artifact names objective, anti-goals, live DB boundary, representative DB boundary, and independent Claude Code review",
            status="missing",
            action="must-fix",
        )
    ]


def _proof_surface_findings(
    changed: set[str],
    *,
    file_texts: Mapping[str, str] | None = None,
    diff_text: str = "",
) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    u66_inner_life = _is_u6_6_inner_life_diff(
        changed,
        file_texts=file_texts,
        diff_text=diff_text,
    )
    rules = [
        (
            _is_watcher_surface_file,
            {
                "tests/test_u3c_pai_watch_doctor.py",
                "tests/test_u3c_pai_watcher.py",
            },
            "watcher behavior changed without a watcher/doctor regression test in the diff",
        ),
        (
            lambda path: path == "mnemos/importer/operator.py",
            {
                "tests/test_u3b_pai_operator.py",
                "tests/test_u3c_pai_watch_doctor.py",
            },
            "operator backup/live-DB behavior changed without an operator/doctor regression test in the diff",
        ),
        (
            lambda path: path == "mnemos/importer/pai.py"
            or (path == "mnemos/store/migrations.py" and not u66_inner_life),
            {
                "tests/test_u3b_pai_importer.py",
                "tests/test_u3c_pai_watch.py",
                "tests/test_u3c_pai_watch_doctor.py",
            },
            "schema or lifecycle behavior changed without row-map/lifecycle regression coverage in the diff",
        ),
        (
            lambda path: path == "mnemos/cli.py" and not u66_inner_life,
            {
                "tests/test_u3b_pai_operator.py",
                "tests/test_u3c_pai_operator.py",
                "tests/test_u3c_pai_watcher.py",
                "tests/test_u3c_pai_watch_doctor.py",
            },
            "CLI behavior changed without a command-level regression test in the diff",
        ),
        (
            lambda path: path == "mnemos/importer/review_gate.py",
            {"tests/test_u3c_pai_review_gate.py"},
            "diff-review gate changed without review-gate regression tests in the diff",
        ),
    ]
    for matches_source, test_files, description in rules:
        touched = sorted(path for path in changed if matches_source(path))
        if touched and not (changed & test_files):
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-proof-{len(findings) + 1}",
                    severity="high",
                    file=", ".join(touched),
                    description=description,
                    required_proof="matching regression file appears in this diff",
                    status="missing",
                    action="must-test",
                )
            )
    if u66_inner_life:
        schema_touched = bool(
            changed
            & {
                "mnemos/store/migrations.py",
                "mnemos/store/sqlite_store.py",
            }
        )
        finalizer_touched = any(
            path.startswith("mnemos/inner_life/") for path in changed
        )
        if schema_touched and not (
            _U6_6_INNER_LIFE_SCHEMA_TEST_FILES <= changed
        ):
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-proof-{len(findings) + 1}",
                    severity="high",
                    file=", ".join(
                        sorted(
                            changed
                            & {
                                "mnemos/store/migrations.py",
                                "mnemos/store/sqlite_store.py",
                            }
                        )
                    ),
                    description="U6.6 inner-life schema changed without ledger/schema regression tests in the diff",
                    required_proof="tests/test_inner_life_ledger.py and tests/test_u3a_schema_migrations.py appear in this diff",
                    status="missing",
                    action="must-test",
                )
            )
        if finalizer_touched and not (
            changed & _U6_6_INNER_LIFE_FINALIZER_TEST_FILES
        ):
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-proof-{len(findings) + 1}",
                    severity="high",
                    file=", ".join(
                        sorted(
                            path
                            for path in changed
                            if path.startswith("mnemos/inner_life/")
                        )
                    ),
                    description="U6.6 inner-life finalizer changed without finalizer regression tests in the diff",
                    required_proof="inner-life finalizer regression file appears in this diff",
                    status="missing",
                    action="must-test",
                )
            )
        if "mnemos/cli.py" in changed and "tests/test_cli_simple.py" not in changed:
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-proof-{len(findings) + 1}",
                    severity="high",
                    file="mnemos/cli.py",
                    description="U6.6 inner-life CLI changed without command-level regression tests in the diff",
                    required_proof="tests/test_cli_simple.py appears in this diff",
                    status="missing",
                    action="must-test",
                )
            )
    return findings


def _is_u6_6_inner_life_diff(
    changed: set[str],
    *,
    file_texts: Mapping[str, str] | None = None,
    diff_text: str = "",
) -> bool:
    """True for the U6.6 private-ledger lane, not PAI lifecycle work."""
    if changed & {"mnemos/importer/pai.py", "mnemos/importer/operator.py"}:
        return False
    if any(_is_watcher_surface_file(path) for path in changed):
        return False
    has_u66_surface = bool(changed & _U6_6_INNER_LIFE_SURFACE_FILES) or any(
        path.startswith("mnemos/inner_life/") for path in changed
    )
    if not has_u66_surface:
        return False

    marker_text = diff_text
    if not marker_text and file_texts:
        marker_text = "\n".join(
            file_texts.get(path, "") for path in sorted(changed)
        )
    if not marker_text:
        marker_text = "\n".join(sorted(changed))

    lowered = marker_text.lower()
    return any(marker in lowered for marker in _U6_6_INNER_LIFE_MARKERS)


def _forbidden_diff_findings(diff_text: str) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    self_signature_files = {
        "mnemos/importer/review_gate.py",
        "tests/test_u3c_pai_review_gate.py",
    }

    def add_finding(
        *,
        severity: str,
        file: str,
        description: str,
        required_proof: str,
    ) -> None:
        if any(
            finding.file == file and finding.description == description
            for finding in findings
        ):
            return
        findings.append(
            PaiReviewFinding(
                ident=f"RG-diff-{len(findings) + 1}",
                severity=severity,
                file=file,
                description=description,
                required_proof=required_proof,
                status="violated",
                action="must-fix",
            )
        )

    additions: dict[str, list[tuple[int, str]]] = {}
    persistence_aliases_by_file: dict[str, set[str]] = {}
    current_file = ""
    diff_lines = diff_text.splitlines()
    for index, raw_line in enumerate(diff_lines):
        if raw_line.startswith("diff --git "):
            parts = raw_line.split()
            current_file = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            continue
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        if current_file in self_signature_files:
            continue
        line = raw_line[1:]
        additions.setdefault(current_file, []).append((index, line))
        if re.search(r"allow_live_db\s*=\s*True", line) and not (
            _allow_live_db_true_is_intentional_test_probe(current_file, diff_lines, index)
        ):
            add_finding(
                severity="critical",
                file=current_file,
                description=(
                    "Diff adds allow_live_db=True; live DB mutation must never "
                    "become the default"
                ),
                required_proof="no runtime diff may make live DB writes the default",
            )
        if _is_runtime_source_file(current_file):
            review_text = _lifecycle_review_text_for_added_line(
                line, diff_lines, index
            )
            mutation_description = (
                _lifecycle_mutation_violation(review_text) if review_text else None
            )
            if mutation_description:
                add_finding(
                    severity="critical",
                    file=current_file,
                    description=mutation_description,
                    required_proof=(
                        "lifecycle mutations must be scoped by target_id, id, "
                        "job_id, or source_path"
                    ),
                )
        if (
            _is_runtime_source_file(current_file)
            or current_file.endswith(".md")
        ) and re.search(r"content_at_last_import\s*=\s*NULL", line, re.IGNORECASE):
            window = "\n".join(diff_lines[max(0, index - 5): index + 6]).lower()
            if "destructive" not in window:
                add_finding(
                    severity="high",
                    file=current_file,
                    description=(
                        "Diff adds bare content_at_last_import = NULL recovery "
                        "text without a nearby DESTRUCTIVE warning"
                    ),
                    required_proof=(
                        "NULL-baseline recovery text must be framed as "
                        "destructive clobber risk"
                    ),
                )
        if _is_watcher_persistence_file(current_file):
            aliases = persistence_aliases_by_file.setdefault(
                current_file, set(_PERSISTENCE_ALIASES)
            )
            _update_persistence_aliases(line, aliases)
            if _has_direct_persistence_write(line, aliases):
                add_finding(
                    severity="high",
                    file=current_file,
                    description=(
                        "Diff adds direct state/plist write; watcher persistence "
                        "must use temp-file replace"
                    ),
                    required_proof="state and plist writes must use tmp.replace atomicity",
                )
        lowered = line.lower()
        if (
            _is_runtime_source_file(current_file)
            and _has_runtime_safety_claim(line)
        ):
            add_finding(
                severity="medium",
                file=current_file,
                description="Diff adds runtime safety claim in comment without reviewable enforcement link",
                required_proof="runtime safety claims must point at enforcement/test evidence",
            )
        if _is_runtime_source_file(current_file) and any(
            term in lowered
            for term in (
                "time_window",
                "lookback",
                "between",
                "created_at >",
                "updated_at >",
                "event_at >",
                "last_seen <",
                "seen_at <",
                "mtime <",
                "modified_at <",
                "cutoff",
                "older than",
            )
        ) and ("select" in lowered or "where" in lowered or "execute(" in lowered):
            window = "\n".join(diff_lines[max(0, index - 5): index + 6]).lower()
            if (
                ("tombstone" in window or "lifecycle" in window)
                and re.search(r"\b(update|delete|insert)\b", window)
                and (
                "select" in window or "where" in window
                )
            ):
                add_finding(
                    severity="critical",
                    file=current_file,
                    description=(
                        "Diff adds time-window lifecycle selection; watcher "
                        "lifecycle must be source/row-map driven"
                    ),
                    required_proof=(
                        "lifecycle selection must key off current source "
                        "snapshot and row-map identity, not time windows"
                    ),
                )
    for file, file_additions in additions.items():
        if file in self_signature_files or not _is_runtime_source_file(file):
            continue
        added_text = "\n".join(line for _, line in file_additions)
        mutation_description = _added_text_lifecycle_mutation_violation(added_text)
        if mutation_description:
            add_finding(
                severity="critical",
                file=file,
                description=mutation_description,
                required_proof="lifecycle mutations must remain explicit and identity-scoped after string composition",
            )
    return findings


def _lifecycle_review_text_for_added_line(
    line: str,
    diff_lines: Sequence[str],
    index: int,
) -> str:
    if not (
        _line_has_lifecycle_mutation_surface(line)
        or _has_dynamic_lifecycle_mutation(line)
        or _has_composed_lifecycle_mutation(line)
    ):
        return ""
    if "execute(" in line or "executescript(" in line:
        return line
    if _inside_added_execute_call(diff_lines, index):
        return _added_diff_line_window(diff_lines, index, radius=24)
    if _has_dynamic_lifecycle_mutation(line) or _has_composed_lifecycle_mutation(line):
        return line
    return ""


def _line_has_lifecycle_mutation_surface(line: str) -> bool:
    text = _strip_python_comments(line)
    return bool(
        re.search(r"\bdelete\s+from\b", text, re.IGNORECASE)
        or re.search(r"\bupdate\s+(?:[A-Za-z_{\"'])", text, re.IGNORECASE)
        or re.search(r"\breplace\s+into\b", text, re.IGNORECASE)
        or re.search(r"\binsert\s+or\s+replace\s+into\b", text, re.IGNORECASE)
    )


def _inside_added_execute_call(diff_lines: Sequence[str], index: int) -> bool:
    for raw_line in reversed(diff_lines[max(0, index - 8): index + 1]):
        if raw_line.startswith("diff --git ") or raw_line.startswith("@@"):
            return False
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        if re.search(r"\b(?:execute|executescript)\s*\(", raw_line[1:]):
            return True
    return False


def _allow_live_db_true_is_intentional_test_probe(
    current_file: str,
    diff_lines: Sequence[str],
    index: int,
) -> bool:
    if not current_file.startswith("tests/"):
        return False
    lines = [
        raw_line[1:] if raw_line.startswith("+") and not raw_line.startswith("+++") else raw_line
        for raw_line in diff_lines[max(0, index - 40): index + 20]
        if not raw_line.startswith("-")
    ]
    context = "\n".join(lines)
    return bool(
        re.search(r"\bdef\s+test_[\w_]*live_db[\w_]*\s*\(", context)
        and "monkeypatch.setattr" in context
        and "DEFAULT_LIVE_DB_PATH" in context
    )


def _added_diff_line_window(
    diff_lines: Sequence[str],
    index: int,
    *,
    radius: int = 4,
) -> str:
    return "\n".join(
        raw_line[1:]
        for raw_line in diff_lines[max(0, index - radius): index + radius + 1]
        if raw_line.startswith("+") and not raw_line.startswith("+++")
    )


def _added_text_by_file(diff_text: str) -> dict[str, str]:
    added: dict[str, list[str]] = {}
    current_file = ""
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            parts = raw_line.split()
            current_file = (
                parts[3][2:]
                if len(parts) >= 4 and parts[3].startswith("b/")
                else ""
            )
            continue
        if not current_file:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            added.setdefault(current_file, []).append(raw_line[1:])
    return {path: "\n".join(lines) for path, lines in added.items()}


def _repository_content_findings(
    changed: set[str],
    file_texts: Mapping[str, str],
    *,
    diff_text: str = "",
    repo_root: Path | None = None,
) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    added_texts = _added_text_by_file(diff_text) if diff_text else {}
    tests = file_texts.get("tests/test_u3c_pai_watch_doctor.py", "")
    watcher_tests = file_texts.get("tests/test_u3c_pai_watcher.py", "")
    watch_tests = file_texts.get("tests/test_u3c_pai_watch.py", "")
    importer_tests = file_texts.get("tests/test_u3b_pai_importer.py", "")
    operator_tests = file_texts.get("tests/test_u3b_pai_operator.py", "")
    operator_u3c_tests = file_texts.get("tests/test_u3c_pai_operator.py", "")
    review_gate_tests = file_texts.get("tests/test_u3c_pai_review_gate.py", "")
    cli_simple_tests = file_texts.get("tests/test_cli_simple.py", "")
    inner_life_ledger_tests = file_texts.get("tests/test_inner_life_ledger.py", "")
    schema_migration_tests = file_texts.get("tests/test_u3a_schema_migrations.py", "")
    session_finalizer_tests = file_texts.get("tests/test_session_finalizer.py", "")
    turn_finalizer_tests = file_texts.get("tests/test_turn_finalizer.py", "")
    watcher = file_texts.get("mnemos/importer/watcher.py", "")
    launch_doc = file_texts.get("docs/u3c-step3-launch-gate.md", "")
    changed_workflows = sorted(path for path in changed if path.startswith(".github/workflows/"))
    workflow = "\n".join(file_texts.get(path, "") for path in changed_workflows)
    workflow += "\n" + file_texts.get(".github/workflows/release-hardening.yml", "")
    workflow_effective = _non_comment_text(workflow)
    workflow_proofs = _workflow_proof_statuses(workflow)
    watcher_changed = any(_is_watcher_surface_file(path) for path in changed)
    u66_inner_life = _is_u6_6_inner_life_diff(
        changed,
        file_texts=file_texts,
        diff_text=diff_text,
    )

    if watcher_changed:
        findings.extend(
            _missing_required_proofs(
                surface="mnemos/importer/watcher.py",
                severity="high",
                requirements=[
                    _ProofRequirement("watch-plist-lint", "plist lint", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_watch_doctor_passes_with_representative_db_and_plist", tests), ("launchd plist static readiness", watcher))),
                    _ProofRequirement("watch-stale-clone", "stale clone rejection", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_watch_doctor_fails_stale_clone_plist", tests), ("PYTHONPATH must point at this repo root", watcher))),
                    _ProofRequirement("watch-env", "HOME/PATH/PYTHONPATH checks", "tests/test_u3c_pai_watcher.py", (("EnvironmentVariables", watcher_tests), ("HOME", watcher_tests), ("PATH", watcher_tests), ("PYTHONPATH", watcher_tests))),
                    _ProofRequirement("watch-log-writable", "stdout/stderr log writability", "mnemos/importer/watcher.py", (("StandardOutPath", watcher), ("StandardErrorPath", watcher), ("_assert_writable_directory(log_path.parent)", watcher))),
                    _ProofRequirement("watch-copy-unchanged", "copy-apply source DB unchanged", "mnemos/importer/watcher.py", (("_file_fingerprint(db)", watcher), ("source_unchanged=1", watcher))),
                    _ProofRequirement("watch-state-crash", "state crash replay", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_crash_before_state_write_does_not_hide_changed_source", tests),)),
                    _ProofRequirement("watch-source-delete", "source delete tombstone", "mnemos/importer/watcher.py", (("_doctor_destructive_delete_probe", watcher), ("tombstoned", watcher))),
                ],
            )
        )

    if (
        watcher_changed
        or "mnemos/importer/operator.py" in changed
        or ("mnemos/cli.py" in changed and not u66_inner_life)
        or "tests/test_u3c_pai_watch_doctor.py" in changed
    ):
        findings.extend(
            _missing_required_proofs(
                surface="Step 3 launch gate",
                severity="high",
                requirements=[
                    _ProofRequirement("tripwire-backup-keep", "missing --backup-keep tripwire", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_watch_doctor_requires_backup_keep_in_plist", tests),)),
                    _ProofRequirement("tripwire-stale-clone", "stale clone plist tripwire", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_watch_doctor_fails_stale_clone_plist", tests),)),
                    _ProofRequirement("tripwire-stateful", "stateful lifecycle invariant machine", "tests/test_u3c_pai_watch_doctor.py", (("TestPaiLifecycleMachine", tests),)),
                    _ProofRequirement("tripwire-broad-delete", "broad lifecycle delete tripwire", "tests/test_u3c_pai_review_gate.py", (("test_u3c_review_gate_fails_broad_lifecycle_delete_in_diff", review_gate_tests), ("test_u3c_review_gate_fails_dynamic_broad_lifecycle_delete_in_diff", review_gate_tests))),
                    _ProofRequirement("tripwire-null-baseline", "bare NULL-baseline tripwire", "tests/test_u3c_pai_review_gate.py", (("test_u3c_review_gate_fails_bare_null_baseline_in_diff", review_gate_tests), ("test_u3c_review_gate_fails_bare_null_baseline_in_docs_diff", review_gate_tests))),
                    _ProofRequirement("tripwire-atomic-write", "non-atomic watcher write tripwire", "tests/test_u3c_pai_review_gate.py", (("test_u3c_review_gate_fails_direct_state_write_in_diff", review_gate_tests), ("test_u3c_review_gate_fails_renamed_direct_state_write_in_diff", review_gate_tests))),
                    _ProofRequirement("tripwire-time-window", "time-window lifecycle tripwire", "tests/test_u3c_pai_review_gate.py", (("test_u3c_review_gate_fails_time_window_lifecycle_selection_in_diff", review_gate_tests), ("test_u3c_review_gate_fails_last_seen_lifecycle_selection_in_diff", review_gate_tests))),
                    _ProofRequirement("tripwire-preview", "preview mutates neither DB nor state", "tests/test_u3c_pai_watcher.py", (("test_u3c_watch_once_preview_does_not_advance_state", watcher_tests),)),
                ],
            )
        )

    if "mnemos/importer/operator.py" in changed:
        findings.extend(
            _missing_required_proofs(
                surface="mnemos/importer/operator.py",
                severity="high",
                requirements=[
                    _ProofRequirement("operator-live-db", "live DB refusal", "tests/test_u3b_pai_operator.py", (("test_u3b_cli_refuses_default_live_db_without_override", operator_tests), ("refuses the default live database", operator_tests))),
                    _ProofRequirement("operator-backup-integrity", "backup integrity", "tests/test_u3c_pai_watch_doctor.py", (("PRAGMA integrity_check", tests),)),
                    _ProofRequirement("operator-backup-restore", "backup restore drill", "mnemos/importer/watcher.py", (("_assert_sqlite_restore_drill", watcher),)),
                    _ProofRequirement("operator-retention", "backup retention bounded", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_backup_keep_prunes_old_matching_backups", tests),)),
                    _ProofRequirement("operator-unrelated-backups", "unrelated job backups preserved", "tests/test_u3c_pai_watch_doctor.py", (("test_u3c_backup_keep_does_not_prune_unrelated_jobs", tests),)),
                ],
            )
        )

    if changed & {"mnemos/importer/pai.py", "mnemos/store/migrations.py"}:
        if (
            u66_inner_life
            and "mnemos/store/migrations.py" in changed
            and "mnemos/importer/pai.py" not in changed
        ):
            findings.extend(
                _missing_required_proofs(
                    surface="U6.6 inner-life schema",
                    severity="high",
                    requirements=[
                        _ProofRequirement(
                            "u66-inner-life-private-ledger",
                            "private/idempotent ledger coverage",
                            "tests/test_inner_life_ledger.py",
                            (
                                (
                                    "test_inner_life_ledger_schema_is_private_and_idempotent",
                                    inner_life_ledger_tests,
                                ),
                            ),
                        ),
                        _ProofRequirement(
                            "u66-inner-life-schema-five-copy",
                            "schema-five copy migration preserving memory rows",
                            "tests/test_inner_life_ledger.py",
                            (
                                (
                                    "test_inner_life_ledger_migrates_schema_five_copy_without_touching_memory",
                                    inner_life_ledger_tests,
                                ),
                            ),
                        ),
                        _ProofRequirement(
                            "u66-inner-life-bootstrap",
                            "current schema bootstrap includes inner_life_events",
                            "tests/test_u3a_schema_migrations.py",
                            (
                                ("test_migration_version_guards", schema_migration_tests),
                                ("inner_life_events", schema_migration_tests),
                                ("SCHEMA_VERSION", schema_migration_tests),
                            ),
                        ),
                    ],
                )
            )
        else:
            findings.extend(
                _missing_required_proofs(
                    surface="PAI lifecycle/schema",
                    severity="high",
                    requirements=[
                        _ProofRequirement("pai-row-map", "row-map coherence", "tests/test_u3c_pai_watch_doctor.py", (("row_map_targets_are_coherent", tests),)),
                        _ProofRequirement("pai-manual-archive", "manual archive non-resurrection", "tests/test_u3c_pai_watch.py", (("test_u3c_manually_archived_engram_still_refuses_reactivation", watch_tests),)),
                        _ProofRequirement("pai-null-carveout", "pre-v5 NULL carve-out", "tests/test_u3c_pai_watch.py", (("test_u3c_legacy_pai_tombstoned_engram_reactivates_without_row_map_tombstone", watch_tests),)),
                        _ProofRequirement("pai-tombstone-reactivation", "tombstone/reactivation path", "tests/test_u3c_pai_watch.py", (("test_u3c_returned_pai_tombstoned_engram_reactivates", watch_tests), ("test_u3c_removed_engram_section_tombstones_target_idempotently", watch_tests))),
                        _ProofRequirement("pai-no-clobber", "no silent clobber", "tests/test_u3b_pai_importer.py", (("test_u3b_target_content_drift_refuses_clobber_on_operator_edit", importer_tests),)),
                    ],
                )
            )

    if u66_inner_life and (
        changed & {"mnemos/inner_life/session_finalizer.py", "mnemos/inner_life/turn_finalizer.py"}
    ):
        findings.extend(
            _missing_required_proofs(
                surface="U6.6 inner-life finalizers",
                severity="high",
                requirements=[
                    _ProofRequirement(
                        "u66-inner-life-session-finalizer",
                        "bounded transcript finalizer writes no memory",
                        "tests/test_session_finalizer.py",
                        (
                            (
                                "test_session_finalizer_writes_bounded_provenance_below_memory",
                                session_finalizer_tests,
                            ),
                        ),
                    ),
                    _ProofRequirement(
                        "u66-inner-life-turn-finalizer",
                        "turn finalizer writes one idempotent ledger row",
                        "tests/test_turn_finalizer.py",
                        (
                            (
                                "test_turn_finalizer_writes_one_idempotent_provenance_row_only",
                                turn_finalizer_tests,
                            ),
                        ),
                    ),
                ],
            )
        )

    if "mnemos/cli.py" in changed:
        if u66_inner_life:
            findings.extend(
                _missing_required_proofs(
                    surface="U6.6 inner-life CLI",
                    severity="high",
                    requirements=[
                        _ProofRequirement(
                            "u66-inner-life-cli-session",
                            "session-finalize CLI writes private ledger",
                            "tests/test_cli_simple.py",
                            (
                                (
                                    "test_inner_life_session_finalize_cli_writes_private_ledger",
                                    cli_simple_tests,
                                ),
                            ),
                        ),
                        _ProofRequirement(
                            "u66-inner-life-cli-db-required",
                            "representative DB required",
                            "tests/test_cli_simple.py",
                            (
                                (
                                    "test_inner_life_cli_requires_representative_db",
                                    cli_simple_tests,
                                ),
                            ),
                        ),
                        _ProofRequirement(
                            "u66-inner-life-cli-live-refusal",
                            "live DB refusal",
                            "tests/test_cli_simple.py",
                            (
                                (
                                    "test_inner_life_cli_refuses_default_live_db_without_override",
                                    cli_simple_tests,
                                ),
                            ),
                        ),
                    ],
                )
            )
        else:
            findings.extend(
                _missing_required_proofs(
                    surface="mnemos/cli.py",
                    severity="high",
                    requirements=[
                        _ProofRequirement("cli-smoke", "CLI smoke", "tests/test_u3c_pai_operator.py", (("test_u3c_cli_watch_preview_and_apply", operator_u3c_tests),)),
                        _ProofRequirement("cli-missing-arg", "missing-arg failure", "tests/test_u3b_pai_operator.py", (("requires --db-path", operator_tests),)),
                        _ProofRequirement("cli-bad-arg", "bad-arg failure", "tests/test_u3c_pai_watch_doctor.py", (("invalid --backup-keep", watcher), ("test_u3c_watch_doctor_requires_backup_keep_in_plist", tests))),
                        _ProofRequirement("cli-live-db", "no live DB default path", "tests/test_u3b_pai_operator.py", (("test_u3b_cli_refuses_default_live_db_without_override", operator_tests),)),
                        _ProofRequirement("cli-review-gate", "review-gate CLI smoke", "tests/test_u3c_pai_review_gate.py", (("test_u3c_review_gate_cli_reports_green", review_gate_tests),)),
                    ],
                )
            )

    if "docs/u3c-step3-launch-gate.md" in changed:
        for marker in ("Anti-Criteria", "Riley/Daniel Test Taxonomy Crosswalk", "Code Graders"):
            if marker not in launch_doc:
                findings.append(
                    PaiReviewFinding(
                        ident=f"RG-docs-{len(findings) + 1}",
                        severity="medium",
                        file="docs/u3c-step3-launch-gate.md",
                        description=f"Launch-gate doc is missing {marker}",
                        required_proof=f"launch-gate doc contains {marker}",
                        status="missing",
                        action="must-fix",
                    )
                )
        docs_only = not changed & {
            "mnemos/importer/watcher.py",
            "mnemos/importer/operator.py",
            "mnemos/importer/pai.py",
            "mnemos/store/migrations.py",
            "mnemos/cli.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_review_gate.py",
            "tests/test_u3c_pai_watch.py",
            "tests/test_u3b_pai_importer.py",
            "tests/test_u3b_pai_operator.py",
        }
        launch_doc_review_text = added_texts.get(
            "docs/u3c-step3-launch-gate.md",
            launch_doc,
        )
        safety_claim = re.search(
            r"\b(refuses|backup|live DB|watch-doctor|safety|must|verified)\b",
            launch_doc_review_text,
            re.IGNORECASE,
        )
        has_enforcement_links = _has_enforcement_links(
            launch_doc_review_text,
            file_texts=file_texts,
            repo_root=repo_root,
        )
        explicit_docs_only_risk = (
            "documentation-only risk" in launch_doc_review_text.lower()
        )
        if safety_claim and (
            docs_only
            or (not has_enforcement_links and not explicit_docs_only_risk)
        ):
            findings.append(
                PaiReviewFinding(
                    ident="RG-docs-risk-1",
                    severity="medium",
                    file="docs/u3c-step3-launch-gate.md",
                    description="Docs-only safety claim changed without a matching enforcement or regression-test diff",
                    required_proof="safety docs must link to enforcement/test changes or be explicitly marked documentation-only risk",
                    status="missing",
                    action="ask-user",
                )
            )

    for doc_path in sorted(path for path in changed if path.endswith(".md")):
        if doc_path in added_texts:
            doc_text = added_texts[doc_path]
            if not doc_text.strip():
                continue
        else:
            doc_text = file_texts.get(doc_path, "")
        if not doc_text or doc_path == "docs/u3c-step3-launch-gate.md":
            continue
        if _has_safety_claim(doc_text) and not _has_enforcement_links(
            doc_text,
            file_texts=file_texts,
            repo_root=repo_root,
        ):
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-docs-risk-{len(findings) + 1}",
                    severity="medium",
                    file=doc_path,
                    description="Safety doc changed without matching enforcement/test links or explicit docs-only risk",
                    required_proof="safety docs must link to enforcement/test changes or be explicitly marked documentation-only risk",
                    status="missing",
                    action="ask-user",
                )
            )

    if "mnemos/importer/review_gate.py" in changed:
        findings.extend(
            _missing_required_proofs(
                surface="mnemos/importer/review_gate.py",
                severity="high",
                requirements=[
                    _ProofRequirement(
                        "review-gate-rule-signature",
                        "review-gate rule signature",
                        "tests/test_u3c_pai_review_gate.py",
                        (("test_u3c_review_gate_rule_signature", review_gate_tests),),
                    )
                ],
            )
        )

    packaging_changed = bool(changed_workflows) or bool(changed & {"pyproject.toml", "uv.lock"})
    if packaging_changed:
        for ident, label in (
            ("ci-local-build", "local build"),
            ("ci-wheel-content", "wheel content check"),
            ("ci-twine-check", "twine check"),
        ):
            if workflow_proofs.get(ident):
                continue
            findings.append(
                PaiReviewFinding(
                    ident=f"RG-{ident}",
                    severity="medium",
                    file=".github/workflows/release-hardening.yml",
                    description=f"packaging/release workflow changed without required proof: {label}",
                    required_proof=label,
                    status="missing",
                    action="must-fix",
                )
            )
        for marker in (
            "mnemos/importer/pai.py",
            "mnemos/importer/operator.py",
            "mnemos/importer/watcher.py",
            "mnemos/importer/review_gate.py",
        ):
            if marker not in workflow_effective:
                findings.append(
                    PaiReviewFinding(
                        ident=f"RG-ci-{len(findings) + 1}",
                        severity="medium",
                        file=".github/workflows/release-hardening.yml",
                        description=f"Release workflow does not compile/package {marker}",
                        required_proof=f"release workflow compiles and wheel-checks {marker}",
                        status="missing",
                        action="must-fix",
                    )
                )
    return findings


def _non_comment_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _missing_required_proofs(
    *,
    surface: str,
    severity: str,
    requirements: Sequence[_ProofRequirement],
) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    for requirement in requirements:
        missing = [
            label
            for label, text in requirement.markers
            if not _marker_present(requirement.file, label, text)
        ]
        if not missing:
            continue
        findings.append(
            PaiReviewFinding(
                ident=f"RG-{requirement.ident}",
                severity=severity,
                file=requirement.file,
                description=(
                    f"{surface} changed without required proof: {requirement.label}"
                ),
                required_proof=requirement.label,
                status="missing",
                action="must-test" if requirement.file.startswith("tests/") else "must-fix",
            )
        )
    return findings


def _marker_present(file: str, label: str, text: str) -> bool:
    if file.startswith("tests/") and label.startswith("test_"):
        return _test_function_has_proof(text, label)
    if file.startswith("tests/") and label.startswith("Test"):
        return _test_class_has_proof(text, label)
    if file.startswith("tests/") and label == "row_map_targets_are_coherent":
        return _test_method_has_proof(text, label)
    return label in _strip_python_comments(text)


def _test_function_has_proof(text: str, name: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    if _module_has_skip_or_xfail(tree):
        return False
    empty_names = _empty_parametrize_names(tree)
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != name:
            continue
        if _has_skip_or_xfail(node.decorator_list) or _has_empty_parametrize(
            node.decorator_list, empty_names=empty_names
        ):
            return False
        return _body_has_substantive_proof(node.body)
    return False


def _test_class_has_proof(text: str, name: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    if _module_has_skip_or_xfail(tree):
        return False
    empty_names = _empty_parametrize_names(tree)
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ClassDef) or node.name != name:
            continue
        if _has_skip_or_xfail(node.decorator_list) or _has_empty_parametrize(
            node.decorator_list, empty_names=empty_names
        ):
            return False
        return any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not _has_skip_or_xfail(child.decorator_list)
            and not _has_empty_parametrize(
                child.decorator_list, empty_names=empty_names
            )
            and _body_has_substantive_proof(child.body)
            for child in node.body
        )
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "TestCase"
            and isinstance(value.value, ast.Name)
        ):
            return _test_class_has_proof(text, value.value.id)
    return False


def _test_method_has_proof(text: str, name: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    if _module_has_skip_or_xfail(tree):
        return False
    empty_names = _empty_parametrize_names(tree)
    collectible_classes = {
        node.name
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
    }
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id.startswith("Test")
            for target in node.targets
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "TestCase"
            and isinstance(value.value, ast.Name)
        ):
            collectible_classes.add(value.value.id)
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ClassDef) or node.name not in collectible_classes:
            continue
        if _has_skip_or_xfail(node.decorator_list) or _has_empty_parametrize(
            node.decorator_list, empty_names=empty_names
        ):
            continue
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if child.name != name:
                continue
            if _has_skip_or_xfail(child.decorator_list) or _has_empty_parametrize(
                child.decorator_list, empty_names=empty_names
            ):
                return False
            return _body_has_substantive_proof(child.body)
    return False


def _has_skip_or_xfail(decorators: Sequence[ast.expr]) -> bool:
    for decorator in decorators:
        source = ast.unparse(decorator) if hasattr(ast, "unparse") else ""
        if re.search(r"\bmark\.(?:skip|xfail)", source):
            return True
    return False


def _module_has_skip_or_xfail(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        source = ast.unparse(node.value) if hasattr(ast, "unparse") else ""
        if re.search(r"\bmark\.(?:skip|xfail)", source):
            return True
    return False


def _empty_parametrize_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_empty_sequence(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _has_empty_parametrize(
    decorators: Sequence[ast.expr],
    *,
    empty_names: set[str] | None = None,
) -> bool:
    empty_names = empty_names or set()
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        if _call_name(decorator) != "parametrize":
            continue
        values: ast.expr | None = decorator.args[1] if len(decorator.args) >= 2 else None
        for keyword in decorator.keywords:
            if keyword.arg == "argvalues":
                values = keyword.value
                break
        if values is None:
            continue
        if _is_empty_sequence(values):
            return True
        if isinstance(values, ast.Name):
            return values.id in empty_names or values.id.isupper()
    return False


def _is_empty_sequence(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        return not node.elts
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "tuple"}
        and not node.args
        and not node.keywords
    )


def _body_has_substantive_proof(body: Sequence[ast.stmt]) -> bool:
    substantive = [
        node for node in body
        if not (
            isinstance(node, ast.Pass)
            or (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        )
    ]
    if not substantive:
        return False
    if any(_statement_has_skip_or_xfail_call(node) for node in substantive):
        return False
    return _reachable_body_has_proof(substantive)


def _reachable_body_has_proof(body: Sequence[ast.stmt]) -> bool:
    for node in body:
        if _statement_has_substantive_proof(node):
            return True
        if _statement_is_terminal(node):
            return False
    return False


def _statement_has_substantive_proof(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if isinstance(node, ast.Expr):
        return _expr_has_proof_call(node.value)
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return any(_expr_has_proof_call(value) for value in _statement_values(node))
    if isinstance(node, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
        return _reachable_body_has_proof(node.body) or _reachable_body_has_proof(
            getattr(node, "orelse", [])
        )
    if isinstance(node, ast.If):
        truth = _literal_bool(node.test)
        if truth is True:
            return _reachable_body_has_proof(node.body)
        if truth is False:
            return _reachable_body_has_proof(node.orelse)
        return _reachable_body_has_proof(node.body) or _reachable_body_has_proof(node.orelse)
    if isinstance(node, ast.Try):
        return (
            _reachable_body_has_proof(node.body)
            or any(_reachable_body_has_proof(handler.body) for handler in node.handlers)
            or _reachable_body_has_proof(node.orelse)
            or _reachable_body_has_proof(node.finalbody)
        )
    if isinstance(node, ast.Match):
        return any(_reachable_body_has_proof(case.body) for case in node.cases)
    return any(_expr_has_proof_call(value) for value in _statement_values(node))


def _statement_values(node: ast.AST) -> tuple[ast.AST, ...]:
    values: list[ast.AST] = []
    for field_name, value in ast.iter_fields(node):
        if field_name in {"body", "orelse", "finalbody", "handlers", "cases", "decorator_list"}:
            continue
        if isinstance(value, ast.AST):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, ast.AST))
    return tuple(values)


def _expr_has_proof_call(node: ast.AST | None) -> bool:
    if node is None:
        return False
    for child in _walk_without_nested_defs(node):
        if isinstance(child, ast.Call) and _call_name(child) in {
            "main",
            "run_pai_diff_review_gate",
            "evaluate_pai_diff_review",
        }:
            return True
    return False


def _statement_has_skip_or_xfail_call(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child) in {"skip", "xfail"}
        for child in _walk_without_nested_defs(node)
    )


def _walk_without_nested_defs(node: ast.AST):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _literal_bool(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _body_is_terminal(body: Sequence[ast.stmt]) -> bool:
    for node in body:
        if _statement_is_terminal(node):
            return True
    return False


def _statement_is_terminal(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Return, ast.Raise)):
        return True
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _body_is_terminal(node.body)
    if isinstance(node, ast.If):
        truth = _literal_bool(node.test)
        if truth is True:
            return _body_is_terminal(node.body)
        if truth is False:
            return _body_is_terminal(node.orelse)
        return bool(node.orelse) and _body_is_terminal(node.body) and _body_is_terminal(node.orelse)
    if isinstance(node, ast.Try):
        if _body_is_terminal(node.finalbody):
            return True
        return (
            _body_is_terminal(node.body)
            and all(_body_is_terminal(handler.body) for handler in node.handlers)
            and (not node.orelse or _body_is_terminal(node.orelse))
        )
    return False


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _strip_python_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _is_watcher_surface_file(path: str) -> bool:
    return path == "mnemos/importer/watcher.py" or (
        path.startswith("mnemos/importer/watcher")
        and path.endswith(".py")
    )


def _is_watcher_persistence_file(path: str) -> bool:
    return _is_watcher_surface_file(path)


def _is_runtime_source_file(path: str) -> bool:
    return (
        path.startswith("mnemos/")
        and path.endswith(".py")
        and not path.startswith("mnemos/tests/")
    )


def _update_persistence_aliases(line: str, aliases: set[str]) -> None:
    match = re.match(
        r"\s*(\w+)\s*=\s*(state_path|plist_path|target_path|output_file|Path\(args\.(?:state|plist)\))\b",
        line,
    )
    if match:
        aliases.add(match.group(1))
        return
    call_match = re.match(r"\s*(\w+)\s*=\s*(.+)$", line)
    if not call_match:
        return
    target, expression = call_match.groups()
    lowered = expression.lower()
    if any(marker in lowered for marker in ("state", "plist", "target_path", "output_file")):
        aliases.add(target)


def _has_direct_persistence_write(line: str, aliases: set[str]) -> bool:
    receiver_match = re.search(r"\b(\w+)\.write_(?:text|bytes)\(", line)
    if receiver_match:
        receiver = receiver_match.group(1)
        if receiver == "tmp":
            return False
        if receiver in aliases:
            return True
        if receiver in _SAFE_WATCHER_WRITE_RECEIVERS:
            return False
        return (
            "state" in receiver.lower()
            or "plist" in receiver.lower()
            or receiver in {"target_path", "output_file"}
        )
    if "Path(args.state).write_" in line or "Path(args.plist).write_" in line:
        return True
    if re.search(
        r"\bopen\([^)]*(?:state|plist|target_path|output_file)[^)]*,\s*(?:mode\s*=\s*)?['\"]w",
        line,
    ):
        return True
    if re.search(
        r"\bjson\.dump\([^)]*\bopen\([^)]*(?:state|plist|target_path|output_file)[^)]*,\s*(?:mode\s*=\s*)?['\"]w",
        line,
    ):
        return True
    if re.search(r"\bPath\(args\.(?:state|plist)\)\.open\(\s*(?:mode\s*=\s*)?['\"]w", line):
        return True
    receiver_open = re.search(r"\b(\w+)\.open\(\s*['\"]w", line)
    return bool(receiver_open and receiver_open.group(1) in aliases)


def _lifecycle_mutation_violation(text: str) -> str | None:
    # Strip comments first so a decoy "# WHERE id = ..." cannot vouch identity
    # scope for an unscoped mutation on the same line.
    text = _strip_python_comments(text)
    statements = _split_sqlish_statements(text)
    if len(statements) > 1:
        for statement in statements:
            violation = _lifecycle_mutation_violation(statement)
            if violation:
                return violation
        return None
    if not (
        re.search(r"\bdelete\s+from\b", text, re.IGNORECASE)
        or re.search(r"\bupdate\s+(?:[A-Za-z_{\"'])", text, re.IGNORECASE)
        or re.search(r"\breplace\s+into\b", text, re.IGNORECASE)
        or re.search(r"\binsert\s+or\s+replace\s+into\b", text, re.IGNORECASE)
        or _has_dynamic_lifecycle_mutation(text)
        or _has_composed_lifecycle_mutation(text)
    ):
        return None
    if _insert_or_replace_archive_has_identity(text):
        return None
    if _has_identity_scope(text):
        return None
    lowered = text.lower()
    compact = _compact_sqlish(text)
    if _has_composed_lifecycle_mutation(text):
        return (
            "Diff adds composed lifecycle DELETE that is not visibly "
            "target/job/source scoped"
        )
    for table in _LIFECYCLE_TABLES:
        if (
            re.search(rf"\bdelete\s+from\s+{re.escape(table)}\b", lowered)
            or f"deletefrom{table}" in compact
        ):
            return (
                "Diff adds broad lifecycle DELETE without target/job/source "
                f"identity scoping (table: {table})"
            )
        if (
            re.search(rf"\bupdate\s+{re.escape(table)}\b(?:\s+(?:as\s+)?\w+)?\s+set\b", lowered)
            or f"update{table}set" in compact
        ):
            return (
                "Diff adds broad lifecycle UPDATE without target/job/source "
                f"identity scoping (table: {table})"
            )
        if (
            re.search(rf"\breplace\s+into\s+{re.escape(table)}\b", lowered)
            or f"replaceinto{table}" in compact
        ):
            return (
                "Diff adds broad lifecycle REPLACE without target/job/source "
                f"identity scoping (table: {table})"
            )
    if _has_dynamic_lifecycle_mutation(text):
        if "delete" in lowered:
            return (
                "Diff adds dynamic lifecycle DELETE target; review gate "
                "cannot prove identity scoping through dynamic table names"
            )
        return (
            "Diff adds dynamic lifecycle mutation target; review gate cannot "
            "prove identity scoping through dynamic table names"
        )
    return None


def _has_identity_scope(text: str) -> bool:
    where_match = re.search(r"\bwhere\b(?P<where>[^;\n]*)", text, flags=re.IGNORECASE)
    if not where_match:
        return False
    where = where_match.group("where")
    target_qualifiers = _lifecycle_target_qualifiers(text)
    return _where_clause_is_identity_scoped(where, target_qualifiers)


def _where_clause_is_identity_scoped(where: str, target_qualifiers: set[str]) -> bool:
    if _where_has_always_true_predicate(where):
        return False
    disjuncts = re.split(r"\bor\b", where, flags=re.IGNORECASE)
    return all(
        _where_disjunct_has_identity_scope(disjunct, target_qualifiers)
        for disjunct in disjuncts
    )


def _where_disjunct_has_identity_scope(disjunct: str, target_qualifiers: set[str]) -> bool:
    for match in re.finditer(
        rf"(?<![\w.])(?:(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\.)?"
        rf"(?P<column>{_IDENTITY_COLUMN_PATTERN})\b\s*"
        r"(?P<operator>=|\bin\b)\s*"
        r"(?P<rhs>\([^)]*\)|[^\s;)]+)",
        disjunct,
        flags=re.IGNORECASE,
    ):
        qualifier = (match.group("qualifier") or "").lower()
        if qualifier and qualifier not in target_qualifiers:
            continue
        if _identity_scope_rhs_is_bounded(match.group("operator"), match.group("rhs")):
            return True
    return False


def _identity_scope_rhs_is_bounded(operator: str, rhs: str) -> bool:
    lowered = rhs.strip().strip("\"'").lower()
    if "select" in lowered:
        return False
    if re.fullmatch(r"(?:old|new)\.(?:id|target_id|job_id|source_path)", lowered):
        return True
    if re.fullmatch(
        rf"(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:{_IDENTITY_COLUMN_PATTERN})",
        lowered,
        flags=re.IGNORECASE,
    ):
        return False
    if operator.lower() == "in" and lowered in {"()", "( )"}:
        return False
    return True


def _where_has_always_true_predicate(where: str) -> bool:
    return bool(
        re.search(r"(?<![\w.])1\s*=\s*1(?![\w.])", where)
        or re.search(r"\btrue\b", where, flags=re.IGNORECASE)
    )


def _lifecycle_target_qualifiers(text: str) -> set[str]:
    lowered = text.lower()
    qualifiers: set[str] = set()
    for table in _LIFECYCLE_TABLES:
        table_pattern = re.escape(table)
        for pattern in (
            rf"\bupdate\s+{table_pattern}\b(?:\s+(?:as\s+)?(?P<alias>\w+))?\s+set\b",
            rf"\bdelete\s+from\s+{table_pattern}\b(?:\s+(?:as\s+)?(?P<alias>\w+))?",
            rf"\breplace\s+into\s+{table_pattern}\b",
        ):
            match = re.search(pattern, lowered)
            if not match:
                continue
            qualifiers.add(table)
            alias = match.groupdict().get("alias")
            if alias and alias not in {"set", "where"}:
                qualifiers.add(alias)
    return qualifiers


def _split_sqlish_statements(text: str) -> list[str]:
    # Split on ';' only OUTSIDE string literals so a ';' inside a SQL value does
    # not shatter a single scoped statement into a false unscoped mutation.
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _compact_sqlish(text: str) -> str:
    return re.sub(r"[^a-z0-9_{}]+", "", text.lower())


def _has_dynamic_lifecycle_mutation(text: str) -> bool:
    lowered = text.lower()
    if (
        re.search(r"\bdelete\s+from\s*\{", lowered)
        or re.search(r"\bupdate\s*\{", lowered)
        or re.search(r"\breplace\s+into\s*\{", lowered)
    ):
        return True
    return bool(
        re.search(
            r"\b(?:delete\s+from|update|replace\s+into)\s*['\"]?\s*\+",
            lowered,
        )
        or re.search(
            r"\b(?:delete\s+from|update|replace\s+into)\s+['\"][^'\"]*['\"]\s*\+",
            lowered,
        )
        or re.search(
            r"\b(?:delete\s+from|update|replace\s+into)\s+[^\n]*%s",
            lowered,
        )
    )


def _has_composed_lifecycle_mutation(text: str) -> bool:
    lowered = text.lower()
    compact = _compact_sqlish(text)
    if not any(table in lowered for table in _LIFECYCLE_TABLES):
        return False
    if not re.search(r"\b(delete|update|replace)\b", lowered):
        return False
    if ".replace(" in lowered or ".join(" in lowered or ".format(" in lowered:
        return True
    return _has_plus_outside_string(text) and any(
        marker in compact
        for table in _LIFECYCLE_TABLES
        for marker in (
            f"deletefrom{table}",
            f"update{table}set",
            f"replaceinto{table}",
        )
    )


def _has_plus_outside_string(text: str) -> bool:
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote is not None:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "+":
            return True
    return False


def _insert_or_replace_archive_has_identity(text: str) -> bool:
    match = re.search(
        r"\binsert\s+or\s+replace\s+into\s+archive\s*\((?P<columns>[^)]*)\)\s*values\s*\(",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return False
    columns = {
        column.strip().strip('"`[]').lower()
        for column in match.group("columns").split(",")
    }
    return "id" in columns


def _added_text_lifecycle_mutation_violation(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "execute(" not in line and "executescript(" not in line:
            continue
        if (
            _line_has_lifecycle_mutation_surface(line)
            or _has_dynamic_lifecycle_mutation(line)
            or _has_composed_lifecycle_mutation(line)
            or _execute_call_starts_sql_literal_block(lines, index)
        ):
            continue
        window = "\n".join(lines[max(0, index - 4): index + 12])
        violation = _lifecycle_mutation_violation(window)
        if violation:
            return violation
    return None


def _has_broad_delete_in_added_text(text: str) -> bool:
    return _added_text_lifecycle_mutation_violation(text) is not None


def _execute_call_starts_sql_literal_block(
    lines: Sequence[str],
    index: int,
) -> bool:
    line = lines[index].strip()
    if not re.search(r"\b(?:execute|executescript)\s*\(\s*$", line):
        return False
    for following in lines[index + 1: index + 4]:
        stripped = following.strip()
        if not stripped:
            continue
        return stripped.startswith(("'", '"', "f'", 'f"', "r'", 'r"'))
    return False


def _has_safety_claim(text: str) -> bool:
    return bool(
        re.search(
            r"\b(refuses|backup|live DB|safety|safe|must|verified|guarantees|guaranteed|never|recover|protected|cannot|will not|corrupt|lose data|data loss)\b",
            text,
            re.IGNORECASE,
        )
    )


def _has_runtime_safety_claim(line: str) -> bool:
    stripped = line.lstrip()
    if not (
        stripped.startswith("#")
        or stripped.startswith('"""')
        or stripped.startswith("'''")
    ):
        return False
    return bool(
        re.search(
            r"\b(safety|verified|guarantees|guaranteed|protected|will not|corrupt|lose data|data loss)\b",
            line,
            re.IGNORECASE,
        )
    )


def _has_enforcement_links(
    text: str,
    *,
    file_texts: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> bool:
    lowered = text.lower()
    if "documentation-only risk" in lowered:
        return True
    if _has_explicit_enforcement_links_anchor(
        text,
        file_texts=file_texts,
        repo_root=repo_root,
    ):
        return True
    contexts = _safety_claim_contexts(text)
    if not contexts:
        contexts = (text,)
    return all(
        _context_has_enforcement_links(
            context,
            file_texts=file_texts,
            repo_root=repo_root,
        )
        for context in contexts
    )


def _safety_claim_contexts(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    contexts: list[str] = []
    seen: set[tuple[int, int, int, int]] = set()
    for index, line in enumerate(lines):
        if not _has_safety_claim(line):
            continue
        section_start, section_end = _markdown_section_bounds(lines, index)
        near_start = max(0, index - 4)
        near_end = min(len(lines), index + 5)
        key = (section_start, section_end, near_start, near_end)
        if key in seen:
            continue
        seen.add(key)
        contexts.append(
            "\n".join(
                (
                    "\n".join(lines[section_start:section_end]),
                    "\n".join(lines[near_start:near_end]),
                )
            )
        )
    return tuple(contexts)


def _has_explicit_enforcement_links_anchor(
    text: str,
    *,
    file_texts: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> bool:
    if not re.search(
        r"(?im)^(?:#{1,6}\s+)?enforcement links\s*:?",
        text,
    ):
        return False
    return _context_has_enforcement_links(
        text,
        file_texts=file_texts,
        repo_root=repo_root,
    )


def _markdown_section_bounds(lines: Sequence[str], index: int) -> tuple[int, int]:
    start = 0
    heading = re.compile(r"^\s{0,3}(#{1,6})\s+\S")
    for cursor in range(index, -1, -1):
        if heading.match(lines[cursor]):
            start = cursor
            break
    end = len(lines)
    for cursor in range(index + 1, len(lines)):
        if heading.match(lines[cursor]):
            end = cursor
            break
    return start, end


def _context_has_enforcement_links(
    text: str,
    *,
    file_texts: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> bool:
    test_paths = re.findall(r"\btests/[A-Za-z0-9_./-]+\.py\b", text)
    code_paths = re.findall(r"\bmnemos/[A-Za-z0-9_./-]+\.py\b", text)
    dotted_modules = re.findall(
        r"\bmnemos(?:\.[A-Za-z_][A-Za-z0-9_]*){1,}\b",
        text,
    )
    test_exists = any(
        _repo_path_exists(path, file_texts, repo_root=repo_root)
        for path in test_paths
    )
    code_exists = any(
        _repo_path_exists(path, file_texts, repo_root=repo_root)
        for path in code_paths
    )
    code_exists = code_exists or any(
        _dotted_module_path_exists(
            module,
            file_texts,
            repo_root=repo_root,
        )
        for module in dotted_modules
    )
    return test_exists and code_exists


def _repo_path_exists(
    path: str,
    file_texts: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> bool:
    if file_texts is not None and path in file_texts:
        return bool(file_texts[path])
    root = repo_root or Path.cwd()
    return (root / path).is_file()


def _dotted_module_path_exists(
    dotted: str,
    file_texts: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> bool:
    parts = dotted.split(".")
    for end in range(len(parts), 1, -1):
        path = "/".join(parts[:end]) + ".py"
        if _repo_path_exists(path, file_texts, repo_root=repo_root):
            return True
    return False


def _workflow_proof_statuses(workflow_text: str) -> dict[str, bool]:
    run_blocks = _active_workflow_run_blocks(workflow_text)
    return {
        "ci-local-build": any(_run_block_executes(block, ("uv build",)) for block in run_blocks),
        "ci-wheel-content": any(
            _run_block_executes(
                block,
                ("zipfile", "archive.namelist", "mnemos/importer/review_gate.py"),
            )
            for block in run_blocks
        ),
        "ci-twine-check": any(
            _run_block_executes(block, ("twine check",))
            for block in run_blocks
        ),
    }


def _active_workflow_run_blocks(workflow_text: str) -> list[str]:
    lines = workflow_text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"\s*-\s+name\s*:", line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    runs: list[str] = []
    for block_lines in blocks:
        block = "\n".join(block_lines)
        if re.search(r"^\s*if\s*:\s*false\s*$", block, re.IGNORECASE | re.MULTILINE):
            continue
        if re.search(
            r"^\s*continue-on-error\s*:\s*true\s*$",
            block,
            re.IGNORECASE | re.MULTILINE,
        ):
            continue
        run = _extract_workflow_run(block_lines)
        if run:
            runs.append(run)
    return runs


def _extract_workflow_run(block_lines: Sequence[str]) -> str:
    for index, line in enumerate(block_lines):
        match = re.match(r"^(\s*)run\s*:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        rest = match.group(2).strip()
        if rest and rest not in {"|", ">"}:
            return rest
        collected: list[str] = []
        for following in block_lines[index + 1:]:
            if not following.strip():
                collected.append("")
                continue
            current_indent = len(following) - len(following.lstrip())
            if current_indent <= indent:
                break
            collected.append(following.strip())
        return "\n".join(collected)
    return ""


def _run_block_executes(block: str, markers: Sequence[str]) -> bool:
    lowered = block.lower()
    if ">/dev/null" in lowered or "cat <<eof" in lowered:
        return False
    executable_lines = [
        _strip_shell_comment(line).strip()
        for line in block.splitlines()
    ]
    executable_lines = [line for line in executable_lines if line]
    return all(
        any(_workflow_line_executes_marker(line, marker) for line in executable_lines)
        for marker in markers
    )


def _strip_shell_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    return line


def _workflow_line_executes_marker(line: str, marker: str) -> bool:
    lowered = line.lower().strip()
    marker_lower = marker.lower()
    if marker_lower not in lowered:
        return False
    if lowered.startswith("echo "):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line.strip()):
        return False
    if re.search(r"\bif\s+false\b|\[\[\s*0\s+-eq\s+1\s*\]\]", lowered):
        return False
    if "|| true" in lowered or "|| :" in lowered or "&& false" in lowered:
        return False
    return True


def _files_needed_for_review(changed_files: Sequence[str]) -> set[str]:
    files = set(changed_files)
    files.update(
        {
            ".github/workflows/release-hardening.yml",
            "docs/u3c-step3-launch-gate.md",
            "mnemos/cli.py",
            "mnemos/importer/watcher.py",
            "tests/test_cli_simple.py",
            "tests/test_inner_life_ledger.py",
            "tests/test_u3b_pai_importer.py",
            "tests/test_u3b_pai_operator.py",
            "tests/test_u3a_schema_migrations.py",
            "tests/test_u3c_pai_operator.py",
            "tests/test_u3c_pai_review_gate.py",
            "tests/test_u3c_pai_watch.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_watcher.py",
            "tests/test_session_finalizer.py",
            "tests/test_turn_finalizer.py",
        }
    )
    return files


def _read_repo_file(repo_root: Path, rel: str) -> str:
    path = repo_root / rel
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _git_commit(repo_root: Path, ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git rev-parse failed for {ref!r}: {detail}")
    return completed.stdout.strip()


def _git_changed_files(repo_root: Path, base_ref: str) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            "--diff-filter=ACDMRTUXB",
            base_ref,
            "--",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git diff --name-status failed: {detail}")
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            paths.extend([fields[1], fields[2]])
        elif len(fields) >= 2:
            paths.append(fields[1])
    return paths


def _git_diff(repo_root: Path, base_ref: str) -> str:
    completed = subprocess.run(
        ["git", "diff", "--find-renames", base_ref, "--"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git diff failed: {detail}")
    return completed.stdout
