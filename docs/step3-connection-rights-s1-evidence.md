# Step 3 Connections S1 — Edge-Rights DDL Evidence

Date: 2026-07-09
Branch: `feat/step3-s1-edge-rights-ddl`
Base: `c7b6400773502bacc6599a432be3791d8e39cdf1` (`origin/main`)
Initial implementation commit: `7e5789a0525a6ea3a11693f1d90c5c5084aa8963`

## Authority Pins

- `STEP3-CONNECTIONS-ARC.md` SHA-256:
  `35a0c120e6a983ebc9952ab0c186c626f13520817f697320277d88fb82fed815`
- Migration: `mnemos/store/migrations/0014_step3_connection_rights.sql`
- Migration SHA-256:
  `e42e888dd59383741d5bf2af711497ace9d2e0f358b6a9be32b9a7a7b2b31245`
- Version: `14`

## What the Slice Adds

- Six nullable, default-free columns on `connections`:
  `valid_at`, `invalid_at`, `confidence`, `runner_up_label`,
  `runner_up_confidence`, and `classifier_version`.
- The migration contains exactly six `ALTER TABLE ... ADD COLUMN` statements.
- No DML, backfill, trigger, index, constraint, seed, history table, reader,
  writer, lifecycle behavior, or classifier behavior is included.

## Plan Review

- The JIT plan is 143 lines and pins the base commit and governing arc hash.
- Coherence, feasibility, and adversarial review ran before implementation.
- Four findings were resolved in the plan: stop-on-arc-hash-mismatch, exhaustive
  file scope, a non-vacuous v13 fixture, and non-self-referential proof identity.
- The unratified SQL affinities are recorded as an implementation inference:
  timestamps/labels/version use `TEXT`; confidence values use `REAL`.

## Acceptance Runs on the Final Pre-Gate Tree

```text
uv run --all-extras pytest -q tests/test_step3_connection_rights.py
3 passed in 0.18s
```

```text
uv run --all-extras pytest -q tests/test_step3_connection_rights.py tests/test_migration_runner.py
75 passed in 1.10s
```

```text
uv run --all-extras pytest -q
1279 passed, 1 skipped in 36.64s
```

```text
uvx ruff check tests/test_step3_connection_rights.py
All checks passed!
```

```text
git diff --check
clean
```

## Code Review

Tier 2 review run `20260709-223452-00531972` covered correctness, testing,
maintainability, project standards, agent parity, prior learnings, migration
safety, adversarial scope, schema drift, and deployment verification.

Testing and adversarial review independently found one P2 test-proof gap: the
SQL contract rejected defaults and `NOT NULL` but did not reject a `CHECK`
constraint. An independent validator confirmed the finding. The single review
fixer tightened the contract to the six exact bare `ALTER TABLE` statements and
added a failing `CHECK` mutation. Bounded round-one re-review by both personas
confirmed resolution with no new findings. There is no residual actionable
review work.

## Boundary Proof

- Every migration test uses a `tmp_path` database and an injected snapshot root.
- The populated-store proof bootstraps without shipped SQL migrations, applies
  the canonical runner only through v13, asserts v14 absent, inserts one legacy
  connection, then applies v14 and proves the legacy fields and row count remain
  unchanged while all six new values are `NULL`.
- The second v14 apply is a no-op and exactly one v14 ledger row remains.
- `tests/conftest.py` refuses the canonical live `~/.mnemos/memory.db` path.
- This lane ran no `mnemos migrate plan` or `mnemos migrate apply` command and
  did not open, copy, or mutate live `~/.mnemos`.
- The live deploy worktree was verified clean on `main` before S1 began.
- Riley's `upstream` remote was not fetched, pulled, pushed, or otherwise touched.

## Final Gate Identity

This file deliberately does not contain the SHA of the commit that contains
itself. The final handoff at No-Mistakes `checks-passed` supplies the PR head SHA,
PR URL, and run ID that bind this evidence-bearing tree to the gate verdict.
