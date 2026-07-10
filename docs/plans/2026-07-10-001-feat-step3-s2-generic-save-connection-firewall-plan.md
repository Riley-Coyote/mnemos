---
title: "feat: Add Step 3 S2 generic-save connection firewall"
type: feat
status: completed
date: 2026-07-10
base_sha: c20c4b7709504f414719f6605df7368fd8968587
arc_sha256: 35a0c120e6a983ebc9952ab0c186c626f13520817f697320277d88fb82fed815
---

# feat: Add Step 3 S2 generic-save connection firewall

## Summary

Make generic engram persistence structurally unable to write `connections`, while
moving deliberate existing graph writers onto a separate explicit transactional
path and preserving ordinary engram, FTS, version, validation, and rollback behavior.

## Requirements

- **R3:** `save_engram()` and its no-commit helper never insert, replace, update,
  delete, strengthen, reclassify, or otherwise mutate a connection row, regardless
  of the supplied `Engram.connections` contents.
- Existing connection rows remain byte-for-byte identical across generic saves,
  including relation, strength, provenance, and all six S1 rights columns.
- Explicit connection APIs and deliberate encoder, consolidation, retrieval,
  softening, and shared-conflict writers continue their current behavior.
- Empty, stale, duplicated, hidden, or tampered connection collections have zero
  connection-table effect through every generic route, including inner-life and PAI.
- Focused positive, negative, mutation, boundary, rollback, and integration tests,
  then the full suite, lint/diff checks, document stage, and evidence are required.

## Rulings Applied

- Ruling 054 keeps inherited deliberate writers operating unreceipted until their
  owning slices; S2 adds no receipt emission and strictly reduces mutation surface.
- The explicit seam reserves an opaque, keyword-only future receipt context. S2
  fails closed on non-`None` context, defining no receipt shape or semantics.

## Scope Boundaries

- No close/reopen lifecycle, lineage receipts, classifier or typed outcomes, lifecycle
  batch machinery, retrieval-event wiring, re-judgment, confirmation/decay, belief endpoints,
  schema nodes, observability surfaces, or legacy disposition audit.
- No schema, migration, index, trigger, backfill, or new meaning for S1 columns.
- No repair of reverse-direction encoding, relation-key reclassification, explicit
  `INSERT OR REPLACE`, or source-target-wide removal semantics.
- No live `~/.mnemos`, deployment, global configuration, external arc, or upstream work.

## Context & Research

- `mnemos/store/sqlite_store.py` holds the sole generic writer: its connection
  loop reaches a six-column `INSERT OR REPLACE` that nulls all six S1 fields.
- The no-commit helper is shared by public, inner-life, and PAI save routes; removing
  the loop firewalls all three without caller-specific guards.
- Encoder, discovery, reconsolidation, softening, and shared conflict are the
  deliberate dependents; S1 explicitly deferred this hazard to S2.
- Separate auto-committed edge writes would lose atomicity. Use explicit
  transaction and prove it with raw 12-column snapshots and mutation tests.

## Key Technical Decisions

- Delete connection persistence from the generic no-commit helper; do not add a flag
  that can re-enable it, because a flag leaves generic save structurally capable.
- Add a narrow explicit transaction seam: `_save_engram_no_commit()` handles only
  ordinary rows, then separately supplied deltas reach `_save_connection_no_commit()`.
- Put keyword-only context on outer seams and the helper; outer guards reject it even
  for empty deltas, helper guards independently, and callers cannot pass it positionally.
- Make in-memory addition return the exact created/reinforced edge; transaction seams
  deduplicate deltas by `(target_id, relation)` before one helper dispatch per final row.
- Use the combined seam only for encoder, reconsolidation, and softening. Discovery
  uses one edge-only batch transaction per source; shared conflict uses the same
  explicit edge seam. Relation selection is preserved but not ratified.
- `SharedPool.publish()` remains generic and stops copying attached edges; canonical
  v1 excludes cross-store graph behavior, so S2 creates no replacement contract.

## Implementation Units

### U1. Firewall generic save and prove the store boundary

**Goal:** Remove all connection authority from generic save and add an explicit,
atomic path for declared connection updates.
**Requirements:** R3 and ordinary persistence/rollback invariants.
**Dependencies:** Pinned base and arc hash above.
**Files:**
- Modify: `mnemos/core/engram.py`
- Modify: `mnemos/store/sqlite_store.py`
- Create: `tests/test_step3_connection_firewall.py`
**Approach:** Remove only the generic connection loop. Add combined engram-plus-delta
and edge-only batch transactions over `_save_connection_no_commit()`; both forward
the reserved context to that helper, which rejects non-`None` before mutation.
**Execution note:** Start with public failing mutation tests, including a raw S1 row
snapshot and a fail-on-call connection helper, before changing the store.
**Patterns to follow:** `tests/test_step3_connection_rights.py`, `tests/test_store.py`,
and inner-life rollback coverage in `tests/test_t2_5_safety_gate_repairs.py`.
**Test scenarios:**
- **Positive:** new and existing generic saves update engram fields, FTS, and versions
  while creating zero edges and leaving the raw connection table identical.
- **Mutation:** populated, same-key tampered, new-target, stale, duplicate, empty, and
  visibility-filtered collections cannot call the connection writer or alter 12 fields.
- **Boundary:** public save, inner-life atomic save, and the PAI no-commit route inherit
  the firewall; explicit save/update/remove and the new explicit transaction still work.
- **Failure:** validation, non-`None` context with empty/populated deltas, positional
  context, and injected mid-batch failures leave every table unchanged.
**Verification:** Restoring the old loop makes focused tests fail; generic paths have
no connection-write call edge, while explicit transaction behavior remains atomic.

### U2. Reroute deliberate writers and prove the real chains

**Goal:** Preserve intentional graph formation without restoring caller-controlled
generic mutation, then document and bind the reviewed tree to evidence.
**Requirements:** R3, explicit-writer continuity, integration and documentation proof.
**Dependencies:** U1.
**Files:**
- Modify: `mnemos/encoding/encoder.py`, `mnemos/retrieval/reconsolidation.py`
- Modify: `mnemos/consolidation/connection_discovery.py`, `mnemos/consolidation/softening.py`
- Modify: `mnemos/multiagent/shared_pool.py`
- Modify: `tests/test_retrieval.py`, `tests/test_identity_diff.py`, `tests/test_agent_scoping.py`
- Modify: `docs/architecture.md`, `CHANGELOG.md`
- Create: `docs/step3-s2-generic-save-connection-firewall-evidence.md`
**Approach:** Pass only each flow's newly created or reinforced edge through its
matrix-selected explicit path. Convert topology fixtures to explicit seeding. Shared
publish copies no edges; in-store shared conflict remains edge-only.
**Test scenarios:**
- **Integration:** each producer reaches an explicit path and persists only declared
  deltas; discovery is all-or-none per source; unrelated rows retain all S1 fields.
- **Continuity:** real softening writes `DISTILLED_INTO`; real shared conflict writes
  its one declared edge; tests assert persistence authority, not later semantics.
- **Negative:** shared publish and unrelated saves of loaded engrams do not copy,
  replace, strengthen, or erase attached edges.
- **Mutation:** replacing explicit calls with generic save breaks the producer tests;
  reintroducing whole-collection persistence breaks raw-row invariance.
- **Boundary:** prove FTS, versions, validation, visibility, and rollback without
  canonizing later-owned relation selection, close/delete, classifier, or receipt shape.
**Verification:** Focused producer/firewall tests and the canonical full suite pass;
touched Python files pass Ruff/format, `git diff --check` is clean, plan/code reviews
have no unresolved actionable finding, and evidence names the exact reviewed commit.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| An intentional writer silently stops | Enumerate every `add_connection` production call and prove each retained chain. |
| The explicit seam becomes a generic bypass | Separate argument, explicit name, narrow callers, and fail-on-call generic tests. |
| S2 ratifies later semantics | Preserve current outputs only; defer every semantic correction named above. |
| Arc changes mid-slice | Recompute before implementation and No-Mistakes; stop on mismatch. |

## Sources & References

**External authority repository:** `pai-supervision`

- `reports/STEP3-CONNECTIONS-ARC.md` — R3, invariants, exclusions, and slice lifecycle.
- `reports/054-s2-receipts-ruling.md` — legacy-writer decision and future-context pin.
- `specs/mnemos-design-v1.md` §9.1 — shared pool and cross-agent behavior are out of scope.
- `docs/plans/2026-07-09-001-feat-step3-s1-edge-rights-ddl-plan.md` — prior slice pattern.
- PR #24 / `c20c4b7709504f414719f6605df7368fd8968587` — verified S1 merge/base.
