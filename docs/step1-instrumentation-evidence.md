# Step 1 Instrumentation Evidence

Date: 2026-07-07
Branch: `step1-instrumentation`
PR: https://github.com/davidefitz/mnemos/pull/20

## What Shipped

- Added additive SQL migration `0012_step1_instrumentation.sql`.
- Added runtime `runtime_receipts` journal with fail-closed manifest validation.
- Added retrieval event logging, retrieval-why receipts, and citation rows.
- Kept runtime receipts separate from `migration_receipts`; receipt kinds are a code manifest, not SQL seed rows.
- Added stable retrieval why metadata to context packet and prompt surfaces without leaking volatile event IDs into read payloads.
- Marked context packet, prompt, and CLI search citations with metadata tiers and `fitting_eligible=false`.
- Added nullable `origin_stamp` on engrams and stamped known producer write paths as `user-witnessed`, `inference`, or `import`.
- Added drift-eval registry, run/observation tables, CLI registration command, and benchmark metric recording.
- Added durable per-store instrumentation producer failure counts to stats/current CLI surface.
- Applied Oliver Claude Code review fixes before no-mistakes: render-tier citation metadata, nullable origin stamps, centralized origin enum validation, and closed immediacy vocabulary.

## Boundary Proof

- Live `~/.mnemos/memory.db` mtime before/after verification:
  `1783451825721869165` -> `1783451825721869165`.
- All migration and benchmark checks used temporary stores.
- No Riley/upstream git remote was used.
- Work ran in Treehouse worktree slot 2, not the dirty soak checkout.

## Acceptance Runs

```text
uv run --extra dev pytest tests/test_step1_instrumentation_schema.py tests/test_step1_receipts.py tests/test_step1_retrieval_logging.py tests/test_step1_drift_eval.py -q
11 passed in 0.33s
```

```text
uv run --extra dev pytest tests/test_step1_instrumentation_schema.py tests/test_step1_receipts.py tests/test_step1_retrieval_logging.py tests/test_step1_drift_eval.py tests/test_retrieval.py tests/test_context_packet.py tests/test_cli_simple.py tests/test_migration_runner.py -q
135 passed in 2.32s
```

```text
uv run --extra dev --extra mcp pytest -q
1162 passed, 2 skipped in 25.82s
```

```text
uv run --extra dev ruff check <changed Step 1 files>
All checks passed!
```

```text
git diff --check
clean
```

```text
MNEMOS_DB_PATH=<tmp>/migrate.db uv run --extra dev mnemos init
MNEMOS_DB_PATH=<tmp>/migrate.db uv run --extra dev mnemos migrate plan
Current schema version: 12
No pending migrations.
```

```text
uv run --extra dev python benchmarks/retrieval_benchmark.py --grid --record-db <tmp>/bench.db --record-json
drift_runs=14
observations=4
benchmark_runs=1
```

## Known Ambiguity

- Whole-repo `uv run --extra dev ruff check .` is red on pre-existing unrelated files. The Step 1 changed-file ruff pass is clean; I did not rewrite package reexports or legacy unused imports outside this branch's scope.
- `origin_stamp` for pre-existing migrated rows is NULL. NULL means pre-instrumentation: absence of a measurement, not a measured stamp.
- The session `observed` to `user-witnessed` mapping remains David-owned per review F3; no additional ruling was made in this fix pass.
