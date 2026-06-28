# U3c Step 3 Launch Intent

## Objective

Turn the PAI dual-life watcher from a code-correct local feature into a launchable
operator workflow that can be activated through launchd only after executable
proof says the lifecycle is safe.

## Anti-Goals

- Do not mutate live `~/.mnemos` by default.
- Do not let documentation warnings substitute for enforcement when enforcement
  is feasible.
- Do not treat a passing happy-path watcher run as proof that deletion,
  reactivation, retention, stale clone, or partial-write behavior is safe.
- Do not touch Riley/upstream git.
- Do not write the debrief from this implementation lane.

## Required Boundaries

- Use explicit representative DBs, temp DB copies, and test manifests.
- Any path that can write to live `~/.mnemos` must require deliberate
  `--allow-live-db` opt-in.
- Preview paths must not mutate DB bytes, watcher state, backups, or launchd
  artifacts.
- Apply paths must take an integrity-checked backup before writes, then preserve
  bounded retention without deleting unrelated job backups.
- launchd activation is not complete until plist paths, HOME, PATH, PYTHONPATH,
  working directory, logs, retention, stale checkout risk, and dry-run behavior
  are checked.
- Claude Code review remains an independent evaluation step. Codex owns the
  implementation and proof, then fixes Claude Code findings or rebuts them with
  evidence.

## Enforcement Links

- Diff/intent readiness: `mnemos.importer.review_gate.run_pai_diff_review_gate`
  with `tests/test_u3c_pai_review_gate.py`.
- Runtime launch readiness: `mnemos.importer.watcher.run_pai_watch_doctor`
  with `tests/test_u3c_pai_watch_doctor.py`.
- Lifecycle semantics: `mnemos.importer.pai.apply_pai_watch_update` with
  `tests/test_u3c_pai_watch.py` and `tests/test_u3b_pai_importer.py`.

## Irreversible Harms To Prevent

- Silent clobber of operator-edited imported content.
- Manual archive resurrection.
- False source reactivation after a deleted/tombstoned section.
- Strict-B coordinate values becoming retrievable memory through PAI import
  rows.
- Duplicate lifecycle events hiding row-map divergence.
- Unbounded backup growth.
- Backup retention deleting another job's backups.
- State advancing before apply succeeds.
- launchd running a stale checkout or wrong Python.
- A default live DB mutation path.
