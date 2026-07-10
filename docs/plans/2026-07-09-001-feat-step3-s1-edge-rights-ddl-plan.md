---
title: "feat: Add Step 3 S1 edge-rights DDL"
type: feat
status: completed
date: 2026-07-09
base_sha: c7b6400773502bacc6599a432be3791d8e39cdf1
arc_sha256: 35a0c120e6a983ebc9952ab0c186c626f13520817f697320277d88fb82fed815
---

# feat: Add Step 3 S1 edge-rights DDL

## Summary

Add the six nullable Step 3 edge-rights columns to `connections` through one
additive SQL migration, prove legacy rows remain unchanged, and leave every
reader, writer, lifecycle rule, and classifier untouched.

## Requirements

- **R1. Edge-rights DDL:** add nullable `valid_at`, `invalid_at`, `confidence`,
  `runner_up_label`, `runner_up_confidence`, and `classifier_version` to
  `connections` through the ratified migration runner.
- The migration is schema-only: no DML, backfill, trigger, default, constraint,
  seed row, history table, or index.
- Verification uses only virgin or pre-v14 temporary stores. Live `~/.mnemos`
  remains untouched.
- The slice finishes with focused migration tests, the canonical full suite,
  lint/diff checks, an evidence artifact, and one No-Mistakes run stopped at
  `checks-passed` for David's merge.

## Scope Boundaries

- No edits to connection models, readers, writers, save/update/remove methods,
  base-schema creation, or frozen Python migrations.
- No lifecycle close/reopen behavior, generic-save firewall, receipts beyond the
  runner's migration receipt, classifier behavior, confidence policy, or live apply.
- No migration-runner hardening; known deferred runner issues remain separate.
- No S2 or later-slice contracts are designed here.

## Context & Research

### Relevant Code and Patterns

- `mnemos/store/migrations/0012_step1_instrumentation.sql` and
  `mnemos/store/migrations/0013_prospective_status.sql` establish the shipped
  SQL-file header and additive-column pattern.
- `mnemos/store/migration_runner.py` discovers, lints, snapshots, applies, and
  journals SQL migrations atomically; `0014` is the next free version.
- `tests/test_step1_instrumentation_schema.py` demonstrates schema inspection,
  ledger verification, and integrity proof on temporary stores.
- `tests/test_migration_runner.py` already proves generic lint, checksum,
  transaction, duplicate-version, and shipped-file attestation behavior.

### Institutional Learnings

- Lint classification alone does not prove nullability; focused tests must inspect
  `PRAGMA table_info(connections)` and assert no required/defaulted values.
- Public behavior and existing row values can remain byte-equivalent even though
  the SQLite file necessarily changes when the schema changes.
- The current omitted-column `INSERT OR REPLACE` hazard belongs to S2; repairing it
  here would violate the slice boundary.

## Key Technical Decisions

- Use `TEXT` affinity for `valid_at`, `invalid_at`, `runner_up_label`, and
  `classifier_version`; use `REAL` for both confidence columns. The live authority
  does not ratify affinities, so this is an explicit implementation inference from
  existing `connections.formed_at` (`TEXT`) and `connections.strength` (`REAL`),
  corroborated but not governed by the retired quarry.
- Give all six columns no `NOT NULL` clause and no default. Existing rows therefore
  receive SQL `NULL` without inventing meaning or requiring a backfill.
- Keep the migration to exactly six `ALTER TABLE ... ADD COLUMN` statements.

## Implementation Units

### U1. Add and prove the inert v14 edge-rights schema

**Goal:** Land R1 as an additive, behavior-inert schema change with durable proof.

**Requirements:** R1

**Dependencies:** Fresh `origin/main` at `c7b6400`; pinned arc hash above.

**Files:**
- Create: `docs/plans/2026-07-09-001-feat-step3-s1-edge-rights-ddl-plan.md`
- Create: `mnemos/store/migrations/0014_step3_connection_rights.sql`
- Create: `tests/test_step3_connection_rights.py`
- Create: `docs/step3-connection-rights-s1-evidence.md`

**Approach:**
- Follow the shipped migration header pattern and add only the six inferred-affinity,
  nullable, default-free columns.
- Build focused tests around the shipped migration and isolated temporary databases.
- For the preservation fixture, bootstrap with an empty migrations directory,
  apply the canonical runner only through v13, seed the legacy row, assert v14 is
  absent, then apply through v14; an ordinary store constructor would skip this proof
  by auto-applying every shipped migration.
- Write the evidence artifact after local verification. It records the tested
  implementation commit, migration checksum/version, test/lint results, and
  live-store non-contact. The final evidence-bearing PR head SHA and No-Mistakes
  run ID live in the external `checks-passed` handoff, avoiding a self-referential SHA.

**Patterns to follow:**
- `mnemos/store/migrations/0013_prospective_status.sql`
- `tests/test_step1_instrumentation_schema.py`
- `tests/test_migration_runner.py`
- `docs/step1-instrumentation-evidence.md`

**Test scenarios:**
- **Positive:** a virgin temporary store reaches v14, exposes exactly the six new
  columns with the declared affinities, records version 14, and passes integrity.
- **Mutation/preservation:** a pre-v14 temporary store containing a representative
  legacy connection is confirmed at v13 before v14 applies; afterward the original
  six fields and row count are unchanged and every new field is `NULL`.
- **Negative:** a focused mutation that makes a new column required/defaulted or
  introduces DML fails the nullability/lint proof rather than being accepted.
- **Boundary/idempotency:** a second apply/reopen is a no-op with one v14 ledger row
  and unchanged connection data.

**Verification:**
- The actual migration lints as exactly six additive-column statements and carries
  the required attestation.
- Focused tests and the canonical full suite pass on the reviewed commit.
- Diff/lint checks pass; the artifact names the tested implementation commit and
  the external handoff names the final evidence-bearing commit and gate run.
- No production code reads or writes any new column, and live `~/.mnemos` is unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| An affinity choice silently becomes policy | Label it as inference; add no constraints, defaults, or runtime use. |
| Existing rows gain invented values | Assert all six fields are `NULL` after migrating a populated v13 store. |
| Adjacent lifecycle work enters S1 | Structural diff check: migration, focused tests, plan, and evidence only. |
| Arc changes mid-slice | Recompute and compare before implementation and the gate; stop immediately on mismatch. |

## Sources & References

**External authority repository:** `pai-supervision`

- `reports/STEP3-CONNECTIONS-ARC.md` — live contract, hash pinned above.
- `reports/053-step3-replan-ruling.md` — retirement rationale and S1 exclusions.
- `specs/migration-runner-spec-2026-07-07.md` — ratified DDL runner contract.
