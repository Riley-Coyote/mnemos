# U3c Step 3 Launch Gate

This is the executable pre-launch gate for the PAI dual-life watcher. It exists
because a code-correct watcher can still fail live through the wrong database,
stale clone paths, launchd environment drift, log unwritability, unbounded
backups, missed destructive deletes, false reactivation, stale state, or partial
writes.

Run this gate before loading or reloading the launchd job.

## Command

Diff-focused review gate:

```bash
uv run --extra dev mnemos pai-import review-gate \
  --base-ref "$(git merge-base HEAD origin/main)" \
  --intent docs/u3c-step3-launch-intent.md
```

Runtime launch doctor:

```bash
uv run --extra dev mnemos pai-import watch-doctor \
  --manifest /path/to/pai-manifest.json \
  --db-path /path/to/representative.db \
  --state /path/to/watch-state.json \
  --artifact-dir /path/to/artifacts \
  --backup-dir /path/to/backups \
  --backup-keep 24 \
  --plist /path/to/com.davidef.mnemos.duallife.plist \
  --python /absolute/path/to/python
```

The doctor must return `Verdict: GREEN` before launchd activation.

## Criteria

| ID | Criterion | Enforcement | Regression |
| --- | --- | --- | --- |
| ISA-U3C-001 | Manifest loads as a full watcher snapshot, including missing files as empty source snapshots. | `mnemos.importer.watcher.run_pai_watch_doctor` D0 | `tests/test_u3c_pai_watch_doctor.py` |
| ISA-U3C-002 | Representative DB preview is read-only and byte-stable. | D1 uses `preview_pai_watch_manifest` and DB fingerprinting | `test_u3c_watch_doctor_passes_with_representative_db_and_plist` |
| ISA-U3C-003 | State, artifact, backup, stdout, and stderr directories are writable before launch. | D2 and D5 directory probes | `test_u3c_watch_doctor_passes_with_representative_db_and_plist` |
| ISA-U3C-004 | Backup retention is explicit and bounded. | `--backup-keep` on apply/watch/apply-once/plist; doctor D3 and D5 | `test_u3c_backup_keep_prunes_old_matching_backups` |
| ISA-U3C-005 | Python executable imports this checkout, not a stale clone. | D4 import check plus D5 WorkingDirectory/PYTHONPATH check | `test_u3c_watch_doctor_fails_stale_clone_plist` |
| ISA-U3C-006 | launchd plist invokes `python -m mnemos.cli pai-import watch-once --apply` with absolute paths and matching retention. | D5 plist lint | `test_u3c_watch_doctor_requires_backup_keep_in_plist` |
| ISA-U3C-007 | Static anti-criteria are absent: broad lifecycle deletes, unsafe NULL-baseline recovery text, non-atomic plist/state writes, time-window lifecycle selection, default-live mutation paths. | D6 static negative scan | `test_u3c_watch_doctor_passes_with_representative_db_and_plist` |
| ISA-U3C-008 | Runtime dry-run applies only against a DB copy, with backup restore proof. | D7 fingerprints the representative DB, copies DB/WAL/SHM to temp, applies only to the copy, checks backup `PRAGMA integrity_check`, and opens a restored backup copy | `test_u3c_watch_doctor_passes_with_representative_db_and_plist` |
| ISA-U3C-009 | File deletion tombstones imported sections and preserves row-map/source/engram coherence. | D8 destructive delete probe | `test_u3c_watch_doctor_passes_with_representative_db_and_plist` |
| ISA-U3C-010 | Crash after apply but before state write does not hide changed sources. | State advances only after `_write_watch_state`; replay remains detectable | `test_u3c_crash_before_state_write_does_not_hide_changed_source` |
| ISA-U3C-011 | Generated lifecycle sequences preserve invariants after every step. | Hypothesis state machine with code-based invariants | `TestPaiLifecycleMachine` |
| ISA-U3C-012 | Dangerous source diffs carry their matching proof surfaces before runtime tests are trusted. | `mnemos.importer.review_gate.run_pai_diff_review_gate` checks changed files against `docs/u3c-step3-launch-intent.md` | `tests/test_u3c_pai_review_gate.py` |

## Enforcement Links

Launch readiness is enforced by `mnemos/importer/watcher.py` and
`mnemos/importer/review_gate.py`, with regressions in
`tests/test_u3c_pai_watch_doctor.py` and `tests/test_u3c_pai_review_gate.py`.

## Anti-Criteria

The gate fails if any of these are true:

- `watch-once` points at any live `~/.mnemos` database without explicit
  `--allow-live-db`.
- The plist points at a stale checkout through `WorkingDirectory` or
  `PYTHONPATH`.
- The plist omits `--backup-keep`.
- Any lifecycle path uses broad deletes instead of target-id-specific updates.
- Recovery text suggests `content_at_last_import = NULL` without a nearby
  `DESTRUCTIVE` warning.
- Plist or watcher state writes are not atomic temp-file replacements.
- Lifecycle actions are selected by a time window rather than the row-map/source
  snapshot.
- A watcher apply writes state before the DB apply has succeeded.
- Backup files cannot be opened, fail `PRAGMA integrity_check`, or fail a
  restore/open drill.

## Riley/Daniel Test Taxonomy Crosswalk

This is the expanded test taxonomy from the listed repos, mapped to U3c. The
point is to keep the launch gate comprehensive without importing irrelevant web
UI gates into a local SQLite/launchd watcher.

| Source pattern | Test type Riley used | U3c disposition |
| --- | --- | --- |
| Riley Mnemos `scripts/health_check.py` | Phase-coded executable health harness with PASS/FAIL/SKIP/WARN rows, read-only live checks, copy-based mutation, data-hygiene baselines, and pytest as a final phase. | `watch-doctor` is the U3c phase harness. It uses read-only preview, copy-based apply probes, PASS/FAIL/SKIP rows, and pytest regressions. Baseline WARNs are not used because Step 3 is a launch gate, not a live data-hygiene sweep. |
| Riley Mnemos release workflow | Matrix CI, full pytest with MCP extra, py_compile for release entrypoints, wheel content check, build, and twine metadata check. | `.github/workflows/release-hardening.yml` now compiles and packages the PAI importer/operator/watcher/review-gate modules, not only the old MCP entrypoints. |
| no-mistakes review stance | Intent-first diff review that asks whether changed behavior earned its test coverage before trusting the normal suite. | `mnemos pai-import review-gate` maps dangerous changed files to required proof classes and fails on broad deletes, thin intent, missing taxonomy, or missing packaging gates. |
| Riley Mnemos retrieval benchmark | Seeded synthetic benchmark with ground-truth relevance and drift measurement. | Already present for retrieval. Not a U3c launch blocker unless watcher changes retrieval or consolidation behavior. |
| Riley Mnemos scope/env/MCP tests | Scope isolation, ambient env isolation, tool-surface schema checks, stdio smoke. | Covered outside U3c by existing suite. U3c-specific equivalent is live DB refusal, stale clone/PYTHONPATH lint, and plist env checks. |
| Polyphonic `scripts/verify.sh` | One-command shipping gate: typecheck, tests, integration tests, build, payload check. | Python equivalent is release workflow plus `uv run --extra dev --extra mcp pytest -q`, py_compile, build, twine, and `git diff --check`. |
| Polyphonic destructive import tests | Static negative tests forbidding broad deletes and time-window deletes; require explicit provenance filters. | D6 forbids broad lifecycle deletes and time-window lifecycle selection. D8 exercises source-file delete/tombstone behavior. |
| Polyphonic launch readiness tests | Static launch gates for secrets in client code, CORS, auth wrappers, release metadata, payload budget. | Secrets/CORS/web metadata are not relevant. U3c equivalent is plist ProgramArguments, HOME/PATH/PYTHONPATH, log writability, absolute paths, retention, and stale checkout rejection. |
| Polyphonic account portability tests | Archive encryption, chunk validation, ID remapping, secret redaction, disabled proactive jobs, row-map rollback, storage policy checks. | U3c equivalent is row-map/source/target coherence, manual archive no resurrection, pre-v5 baseline handling, backup restore proof, and bounded backup retention that does not prune unrelated jobs. |
| Polyphonic SQL audits | Read-only RLS, owner-scope, cascade, and mislabel audit scripts with accepted-risk review. | SQLite has no RLS. U3c equivalent is read-only representative DB preview, live DB refusal, source/target invariant checks, and accepted manual gates for actual launchd/TCC activation. |
| Polyphonic browser/performance/a11y tests | Route sweeps, console hygiene, focus traps, reduced motion, Lighthouse, initial payload budget. | Not applicable to U3c except the general pattern: if an operator-visible watch UI is added later, it needs its own route/layout/a11y gates. |
| claude-field launchd scripts | Direct executable path, embedded launchd ProgramArguments, WorkingDirectory, HOME/PATH, stdout/stderr logs, launchctl list/load checks, and TCC failure awareness. | D5 covers plist structure/env/log paths. Actual `launchctl bootstrap/list` and TCC permission validation remain a manual Step 3 activation smoke because tests must not load David's live agent. |
| claude-field seal tool | Hash/seal verification, blind wake/reveal/grade commands, and external grader separation. | U3c equivalent is backup hash/open/restore proof and the required independent Claude Code review handoff before commit. |
| opus-echoes sync preflight | `git fetch`, ahead/behind/dirty report, stale clone warning before work. | D4/D5 stale clone checks ensure launchd imports this checkout. General git sync remains operator workflow; no upstream Riley git is touched. |
| opus-echoes invariant verifier | Scripted protocol/state verifier with monotonic sequence, lock discipline, parsing, seed repair, interruption, and max-turn invariants. | `TestPaiLifecycleMachine` is the U3c stateful invariant verifier for edit/delete/restore/delete-file sequences and backup retention. |
| Daniel LifeOS/PAI packs | VERIFY files for installed skill structure, frontmatter, trigger smoke; ISA/Evals/FirstPrinciples/RedTeam/RCA lenses. | Reflected as stable ISA criteria IDs, deterministic code graders first, FirstPrinciples irreversible-harm questions, RedTeam anti-criteria, and a handoff for external Claude Code review. |
| PAIPlugin pentester agent | Security methodology: scope, recon, vulnerability assessment, controlled testing, documentation, remediation. | Used as posture only. U3c has authorized local defensive checks; no network/offensive testing is required for a local watcher launch gate. |

## Code Graders

| Grader | Type | Target |
| --- | --- | --- |
| `test_u3c_watch_doctor_passes_with_representative_db_and_plist` | binary test | End-to-end launch gate and static/dynamic criteria |
| `test_u3c_watch_doctor_fails_stale_clone_plist` | binary negative test | Stale-clone preflight |
| `test_u3c_watch_doctor_requires_backup_keep_in_plist` | binary negative test | Bounded retention requirement |
| `test_u3c_backup_keep_prunes_old_matching_backups` | binary state check | Backup retention, SQLite integrity, and backup restore-content proof |
| `test_u3c_backup_keep_does_not_prune_unrelated_jobs` | binary negative test | Backup retention must not delete unrelated job backups |
| `test_u3c_crash_before_state_write_does_not_hide_changed_source` | binary failure-injection test | Partial write recovery |
| `TestPaiLifecycleMachine` | Hypothesis stateful test | Generated edit/delete/restore/delete-file sequences and invariants |
| `tests/test_u3c_pai_review_gate.py` | diff-review gate tests | Missing proof surfaces, broad deletes, thin intent, doc taxonomy loss, and package-gate drift |

## Verification

Focused launch-gate eval:

```bash
uv run --extra dev pytest -q tests/test_u3c_pai_review_gate.py
uv run --extra dev pytest -q tests/test_u3c_pai_watch_doctor.py
```

U3b/U3c regression slice:

```bash
uv run --extra dev pytest -q \
  tests/test_u3b_pai_importer.py \
  tests/test_u3b_pai_operator.py \
  tests/test_u3c_pai_watch.py \
  tests/test_u3c_pai_operator.py \
  tests/test_u3c_pai_watcher.py \
  tests/test_u3c_pai_watch_doctor.py \
  tests/test_u3c_pai_review_gate.py
```

CLI smoke:

```bash
uv run --extra dev mnemos pai-import review-gate \
  --base-ref "$(git merge-base HEAD origin/main)" \
  --intent docs/u3c-step3-launch-intent.md

uv run --extra dev mnemos pai-import watch-doctor \
  --manifest /path/to/pai-manifest.json \
  --db-path /path/to/representative.db \
  --state /path/to/watch-state.json \
  --artifact-dir /path/to/artifacts \
  --backup-dir /path/to/backups \
  --backup-keep 24 \
  --plist /path/to/com.davidef.mnemos.duallife.plist \
  --python /absolute/path/to/python
```

## Boundary Decision

- Docs state the criteria and commands.
- Enforcement lives in `mnemos.importer.watcher.run_pai_watch_doctor`,
  `mnemos.importer.review_gate.run_pai_diff_review_gate`,
  `write_pai_watch_launchd_plist`, `pai_watch_once`, and bounded backup pruning.
- Tests encode objective invariants as code graders.
- Schema remains responsible for row-map target identity and tombstone metadata.
- `content_at_last_import` is the importer baseline, not an operator recovery
  switch. Any reset path must be labeled destructive because it permits a future
  source-canonical repair to clobber target edits.
