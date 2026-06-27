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
_PERSISTENCE_ALIASES = {"state_path", "plist_path", "target_path", "output_file"}
_SAFE_WATCHER_WRITE_RECEIVERS = {"tmp", "source", "manifest", "probe"}


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
    base_ref: str = "HEAD",
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
    changed_files = _git_changed_files(root, base_ref)
    diff_text = _git_diff(root, base_ref)
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
    )
    return PaiReviewReport(
        base_ref=base_ref,
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
) -> list[PaiReviewFinding]:
    """Pure evaluator used by tests and the CLI wrapper."""
    changed = set(changed_files)
    findings: list[PaiReviewFinding] = []
    findings.extend(_intent_findings(intent_text))
    findings.extend(_proof_surface_findings(changed))
    findings.extend(_forbidden_diff_findings(diff_text))
    findings.extend(_repository_content_findings(changed, file_texts))
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


def _proof_surface_findings(changed: set[str]) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
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
            lambda path: path in {"mnemos/importer/pai.py", "mnemos/store/migrations.py"},
            {
                "tests/test_u3b_pai_importer.py",
                "tests/test_u3c_pai_watch.py",
                "tests/test_u3c_pai_watch_doctor.py",
            },
            "schema or lifecycle behavior changed without row-map/lifecycle regression coverage in the diff",
        ),
        (
            lambda path: path == "mnemos/cli.py",
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
    return findings


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
        if re.search(r"allow_live_db\s*=\s*True", line):
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
            mutation_description = _lifecycle_mutation_violation(line)
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


def _repository_content_findings(
    changed: set[str],
    file_texts: Mapping[str, str],
) -> list[PaiReviewFinding]:
    findings: list[PaiReviewFinding] = []
    tests = file_texts.get("tests/test_u3c_pai_watch_doctor.py", "")
    watcher_tests = file_texts.get("tests/test_u3c_pai_watcher.py", "")
    watch_tests = file_texts.get("tests/test_u3c_pai_watch.py", "")
    importer_tests = file_texts.get("tests/test_u3b_pai_importer.py", "")
    operator_tests = file_texts.get("tests/test_u3b_pai_operator.py", "")
    operator_u3c_tests = file_texts.get("tests/test_u3c_pai_operator.py", "")
    review_gate_tests = file_texts.get("tests/test_u3c_pai_review_gate.py", "")
    watcher = file_texts.get("mnemos/importer/watcher.py", "")
    launch_doc = file_texts.get("docs/u3c-step3-launch-gate.md", "")
    changed_workflows = sorted(path for path in changed if path.startswith(".github/workflows/"))
    workflow = "\n".join(file_texts.get(path, "") for path in changed_workflows)
    workflow += "\n" + file_texts.get(".github/workflows/release-hardening.yml", "")
    workflow_effective = _non_comment_text(workflow)
    workflow_proofs = _workflow_proof_statuses(workflow)
    watcher_changed = any(_is_watcher_surface_file(path) for path in changed)

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

    if watcher_changed or changed & {
        "mnemos/importer/operator.py",
        "mnemos/cli.py",
        "tests/test_u3c_pai_watch_doctor.py",
    }:
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

    if "mnemos/cli.py" in changed:
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
        safety_claim = re.search(
            r"\b(refuses|backup|live DB|watch-doctor|safety|must|verified)\b",
            launch_doc,
            re.IGNORECASE,
        )
        has_enforcement_links = _has_enforcement_links(
            launch_doc, file_texts=file_texts
        )
        explicit_docs_only_risk = "documentation-only risk" in launch_doc.lower()
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
        doc_text = file_texts.get(doc_path, "")
        if not doc_text or doc_path == "docs/u3c-step3-launch-gate.md":
            continue
        if _has_safety_claim(doc_text) and not _has_enforcement_links(
            doc_text, file_texts=file_texts
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
        return _test_function_has_proof(text, label)
    return label in _strip_python_comments(text)


def _test_function_has_proof(text: str, name: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    if _module_has_skip_or_xfail(tree):
        return False
    empty_names = _empty_parametrize_names(tree)
    for node in ast.walk(tree):
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
    for node in ast.walk(tree):
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
    for node in ast.walk(tree):
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
    return any(
        isinstance(node, ast.Assert)
        or (
            isinstance(node, ast.Call)
            and _call_name(node) in {"main", "run_pai_diff_review_gate", "evaluate_pai_diff_review"}
        )
        for node in ast.walk(ast.Module(body=list(substantive), type_ignores=[]))
    )


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
        if receiver in _SAFE_WATCHER_WRITE_RECEIVERS:
            return False
        return (
            receiver in aliases
            or "state" in receiver.lower()
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
        re.search(r"\b(delete\s+from|update|replace\s+into)\b", text, re.IGNORECASE)
        or _has_dynamic_lifecycle_mutation(text)
        or _has_composed_lifecycle_mutation(text)
    ):
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
    if re.search(
        r"(?<![\w.])(?:id|target_id|job_id|source_path)\b\s*(?:=|\bin\b)",
        where,
        flags=re.IGNORECASE,
    ):
        return True
    target_qualifiers = _lifecycle_target_qualifiers(text)
    if not target_qualifiers:
        return False
    qualified = re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\.(id|target_id|job_id|source_path)\b\s*(?:=|\bin\b)",
        where,
        flags=re.IGNORECASE,
    )
    return any(qualifier.lower() in target_qualifiers for qualifier, _ in qualified)


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
    compact = _compact_sqlish(text)
    if any(marker in compact for marker in ("deletefrom{", "update{", "replaceinto{")):
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
    return "+" in text and any(
        marker in compact
        for table in _LIFECYCLE_TABLES
        for marker in (
            f"deletefrom{table}",
            f"update{table}set",
            f"replaceinto{table}",
        )
    )


def _added_text_lifecycle_mutation_violation(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        window = "\n".join(lines[max(0, index - 2): index + 3])
        violation = _lifecycle_mutation_violation(window)
        if violation:
            return violation
    return None


def _has_broad_delete_in_added_text(text: str) -> bool:
    return _added_text_lifecycle_mutation_violation(text) is not None


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
            r"\b(refuses|safety|safe|verified|guarantees|guaranteed|never|protected|cannot|will not|corrupt|lose data|data loss)\b",
            line,
            re.IGNORECASE,
        )
    )


def _has_enforcement_links(
    text: str,
    *,
    file_texts: Mapping[str, str] | None = None,
) -> bool:
    lowered = text.lower()
    if "documentation-only risk" in lowered:
        return True
    test_paths = re.findall(r"\btests/[A-Za-z0-9_./-]+\.py\b", text)
    code_paths = re.findall(r"\bmnemos/[A-Za-z0-9_./-]+\.py\b", text)
    dotted_modules = re.findall(
        r"\bmnemos(?:\.[A-Za-z_][A-Za-z0-9_]*){1,}\b",
        text,
    )
    test_exists = any(_repo_path_exists(path, file_texts) for path in test_paths)
    code_exists = any(_repo_path_exists(path, file_texts) for path in code_paths)
    code_exists = code_exists or any(
        _dotted_module_path_exists(module, file_texts)
        for module in dotted_modules
    )
    return test_exists and code_exists


def _repo_path_exists(
    path: str,
    file_texts: Mapping[str, str] | None = None,
) -> bool:
    if file_texts is not None and path in file_texts:
        return bool(file_texts[path])
    return (Path.cwd() / path).is_file()


def _dotted_module_path_exists(
    dotted: str,
    file_texts: Mapping[str, str] | None = None,
) -> bool:
    parts = dotted.split(".")
    for end in range(len(parts), 1, -1):
        path = "/".join(parts[:end]) + ".py"
        if _repo_path_exists(path, file_texts):
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
            "tests/test_u3b_pai_importer.py",
            "tests/test_u3b_pai_operator.py",
            "tests/test_u3c_pai_operator.py",
            "tests/test_u3c_pai_review_gate.py",
            "tests/test_u3c_pai_watch.py",
            "tests/test_u3c_pai_watch_doctor.py",
            "tests/test_u3c_pai_watcher.py",
        }
    )
    return files


def _read_repo_file(repo_root: Path, rel: str) -> str:
    path = repo_root / rel
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _git_changed_files(repo_root: Path, base_ref: str) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            "--diff-filter=ACMRTUXB",
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
