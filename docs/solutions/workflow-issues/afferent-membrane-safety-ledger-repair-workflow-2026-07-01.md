---
title: Afferent Membrane Safety Ledger Repair Workflow
date: 2026-07-01
category: workflow-issues
module: Mnemos Afferent Membrane
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "Repairing a plan that is derived from an RFC or safety ledger"
  - "A review gate finds repeated safety invariants that the plan should have named first"
  - "A no-mistakes run is active on a stale branch head"
  - "A plan needs exact code chokepoints and executable test selectors"
tags: [afferent-membrane, plan-ledger, no-mistakes, review-safety, mnemos, u2-5]
---

# Afferent Membrane Safety Ledger Repair Workflow

## Context

The Afferent Membrane U2.5 repair exposed a failure mode in planning work: the implementation can move in the right direction while the plan artifact stays too weak to be treated as a safety ledger. The first corrective pass made this mistake again. It added a reusable plan-quality gate, but did not make the original Afferent plan itself carry the required Rule, State, Surface, and Regression ledgers.

That was not a cosmetic miss. The current plan was explicitly suspect, the RFC was the source of truth, and the task was to repair the plan before U3 authority stamping. A derived plan that only links to a gate still lets future work skip the ledger, drift rule mappings, invent rule IDs, or name tests that do not execute.

The repaired state is anchored in:

- `docs/plans/2026-06-30-001-feat-afferent-membrane-v1-plan.md`
- `docs/plans/2026-06-30-002-fix-afferent-u2-5-safety-ledger-plan.md`
- `docs/plans/afferent-membrane-plan-quality-gate.md`
- `tests/test_afferent_plan_quality.py`
- ledger repair commits `9957196` and `e285b3e`, with workflow capture beginning at `37613d7` on `codex/afferent-membrane-v1-ledger`

## Guidance

### 1. Repair the authoritative plan, not only a secondary gate

When the user says the plan is suspect and the RFC is the ledger, the original plan must become the proof-bearing artifact. A separate gate is useful only after the source plan carries the same load.

The repaired Afferent plan now states that it is derived from `/Users/davidef/phenom-felt-review/RFC-v1-proposal-ledger.md`, that the RFC is the safety ledger, and that the plan cannot override, renumber, or weaken it. It then embeds four ledgers directly in the plan:

- **Rule Ledger:** RFC rule -> implementation unit -> code chokepoint -> positive test -> negative test.
- **State Ledger:** durable object -> states/visibility -> defaults/omitted-field behavior -> upsert behavior -> terminal states -> allowed/rejected transitions.
- **Surface Ledger:** every read surface and what operational, review, audit/admin, migration, direct-ID, aggregate/count, visual, MCP, and context paths can see.
- **Regression Ledger:** each invariant gets at least one positive test and one negative test.

The prevention rule is simple: a safety plan is not repaired until the original plan contains the ledger, not merely a pointer to a checklist.

### 2. Keep RFC rule IDs canonical and executable

The Afferent plan had to preserve these mappings:

| RFC rule | Correct implementation unit |
|---|---|
| RFC-R1 | U3 |
| RFC-R2 | U3, with U2 pre-authority guardrails |
| RFC-R3 | U2 |
| RFC-R4 | U4 |
| RFC-R5 | U2 |
| RFC-R6 | U1, extended by U2 |
| RFC-R7 | U5/U6 |
| RFC-R8 | U5/U6 |

There is no operative RFC-R9 or RFC-R10. ExperienceTick is a build-sequence feeder constrained by RFC-R3/R7/R8, not a separate permission rule. A stray U7 mapping was removed from the plan because it contradicted the corrected U5/U6 ownership for R7/R8.

The test guard in `tests/test_afferent_plan_quality.py::test_afferent_main_plan_keeps_rfc_rule_ledger_and_modulation_boundary` now asserts the corrected rule mapping, rejects operative RFC-R9/R10 use, and rejects `U7` in the plan.

### 3. Make durable state and terminal conflicts explicit before code

The State Ledger is where the ProposalLedger terminal-state bug should have been caught before implementation. Terminal proposal rows are immutable. If a later write reuses a `proposal_id` whose existing row is `deferred`, `rejected`, or `applied`, the write must be rejected and the original audit record must remain unchanged: reason, provenance, payload, status, and visibility all stay intact. New content needs a new `proposal_id`.

The plan now records that policy in the ProposalLedger row of the State Ledger, and the focused proof includes:

- `tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_rejected_terminal_conflict`
- `tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_applied_terminal_conflict`

This fixes the earlier planning error where ProposalLedger lifecycle rules were treated as implementation detail instead of safety-ledger material.

### 4. Classify omitted hypomnema visibility at write time

The U2 live-write classifier repair prevents fresh high-confidence or foundational hypomnema from becoming operational just because it was written after the migration.

The repaired contract is:

- Omitted `read_visibility` is classified at write time, not treated as a harmless schema default.
- The migration promotion-candidate heuristic is the first classifier:
  `confidence >= 0.82 AND salience >= 0.65 AND (foundational OR revision_count >= 1)` routes to `review_only` unless explicitly overridden by a trusted gate.
- Identity or foundational rows route to review even below the promotion threshold.
- Existing row visibility is preserved only while the row remains ordinary; crossing into promotion/high-blast state strengthens visibility.
- Explicit operational visibility for review-worthy hypomnema is rejected.

The plan records the deliberate RFC deviation: hypomnema promotion candidates use `review_only` rather than the RFC's generic `audit_only` default so David can inspect them in review. ProposalLedger rows still follow the RFC default and omitted proposals default to `audit_only`.

### 5. Treat every read surface as part of the safety boundary

The Surface Ledger exists because earlier fixes kept finding another leak after the first obvious surface was patched. Operational safety is not just packet formatting. It includes search seeds, graph propagation, prompt builders, runtime producers, MCP review/audit tools, CLI/direct-ID reads, status/count aggregates, dashboards, migration/backfill, tag lookup, dream journal paths, and substrate producers.

The fixed plan explicitly says what each surface can see and what it must never see. Examples:

- operational retrieval filters before FTS, embedding, graph propagation, ranking, and formatting;
- audit-only rows are absent from ordinary operational and review surfaces, including counts and IDs unless explicitly allowed;
- proposal sample limits must not alter total queue count;
- default inspect/direct-ID reads are not admin reads;
- review/audit rows must not satisfy operational bootstrap thresholds.

This is why the focused proof includes both behavior tests and plan-quality tests.

### 6. DynamicModulation cannot influence retrieval until both bounds exist

The plan must distinguish scaffolding/storage from active influence. U5 may store DynamicModulation only as inert or conservative bounded state. Active retrieval influence waits until U6 supplies both RFC-R7 bounds:

- persistence bound: TTL/decay/magnitude;
- distribution-shape bound: valence floor, deadband, and fail-toward-width.

The repaired Rule, State, Surface, and Regression ledgers all encode that DynamicModulation cannot cite itself as evidence, promote by recurrence, write belief/identity state, or actively shape retrieval before both bounds exist.

### 7. Test selectors in a ledger must execute

The second repair pass found false coordinates inside the new ledger itself:

- `tests/test_store.py::TestStore::...` was wrong; the real class is `TestEngramStore`.
- `tests/test_retrieval.py::TestReactiveRetrieval::...` was wrong; the real class is `TestReactiveRetriever`.

Those mistakes matter because a ledger with precise-looking but non-executable selectors gives false confidence. The fix was to correct the plan selectors and run the focused node IDs, not just inspect the prose.

Subset focused proof observed during the repair pass:

```bash
uv run --extra dev pytest tests/test_afferent_plan_quality.py -q
uv run --extra dev pytest \
  tests/test_afferent_plan_quality.py \
  tests/test_context_packet.py::test_operational_context_packet_quarantines_review_prose \
  tests/test_context_packet.py::test_operational_proposal_count_uses_total_not_limited_reference_sample \
  tests/test_context_packet.py::test_audit_only_proposal_and_hypomnema_are_absent_from_review_packet \
  tests/test_context_packet.py::test_visual_snapshot_review_count_uses_total_pending_proposals \
  tests/test_store.py::TestEngramStore::test_proposal_ledger_accepts_rfc_authority_and_kind_axes \
  tests/test_store.py::TestEngramStore::test_proposal_ledger_defaults_to_audit_only_and_rejects_pending_operational_visibility \
  tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_rejected_terminal_conflict \
  tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_applied_terminal_conflict \
  tests/test_store.py::TestEngramStore::test_list_proposals_audit_visibility_requires_explicit_audit_read \
  tests/test_store.py::TestEngramStore::test_count_engrams_defaults_to_operational_visibility \
  tests/test_store.py::TestEngramStore::test_get_hypomnema_entries_by_tag_filters_read_visibility_by_default \
  tests/test_hypomnema.py::TestHypomnemaStore::test_identity_or_foundational_hypomnema_defaults_review_only_even_below_promotion_threshold \
  tests/test_mcp_surface.py::test_mcp_hypomnema_write_default_review_only_is_quarantined_from_search_candidates_promote_and_visible_in_review \
  tests/test_mcp_surface.py::test_review_queue_includes_review_only_proposal_rows_with_provenance \
  tests/test_mcp_surface.py::test_review_queue_excludes_terminal_review_only_proposals \
  tests/test_mcp_surface.py::test_audit_admin_proposal_review_lists_audit_only_rows_without_operational_exposure \
  tests/test_retrieval.py::TestReactiveRetriever::test_retrieval_excludes_review_only_fts_and_propagation \
  tests/test_retrieval.py::TestReactiveRetriever::test_review_and_audit_rows_do_not_seed_operational_retrieval \
  tests/test_visualization_data.py::test_dashboard_extracts_operational_visibility_by_default \
  tests/test_visualization_data.py::test_dashboard_audit_mode_extracts_non_operational_rows \
  tests/test_encoding.py::TestEncoder::test_quarantined_engrams_do_not_end_bootstrap_policy \
  -q
git diff --check
```

The observed results were `4 passed`, then `25 passed`, then a clean whitespace check.

Full regression-ledger proof before claiming the current branch head closed:

```bash
uv run --extra dev pytest \
  tests/test_afferent_plan_quality.py \
  tests/test_context_packet.py::test_operational_context_packet_quarantines_review_prose \
  tests/test_context_packet.py::test_operational_proposal_count_uses_total_not_limited_reference_sample \
  tests/test_context_packet.py::test_audit_only_proposal_and_hypomnema_are_absent_from_review_packet \
  tests/test_context_packet.py::test_visual_snapshot_review_count_uses_total_pending_proposals \
  tests/test_store.py::TestEngramStore::test_proposal_ledger_accepts_rfc_authority_and_kind_axes \
  tests/test_store.py::TestEngramStore::test_proposal_ledger_defaults_to_audit_only_and_rejects_pending_operational_visibility \
  tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_rejected_terminal_conflict \
  tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_applied_terminal_conflict \
  tests/test_store.py::TestEngramStore::test_list_proposals_audit_visibility_requires_explicit_audit_read \
  tests/test_store.py::TestEngramStore::test_count_engrams_defaults_to_operational_visibility \
  tests/test_store.py::TestEngramStore::test_get_hypomnema_entries_by_tag_filters_read_visibility_by_default \
  tests/test_store.py::TestEngramStore::test_legacy_v5_migrates_non_candidate_hypomnema_operational_and_candidates_review_only \
  tests/test_store.py::TestEngramStore::test_legacy_v6_hypomnema_review_default_is_repaired \
  tests/test_hypomnema.py::TestHypomnemaStore::test_live_write_classifies_stable_hypomnema_as_review_only \
  tests/test_hypomnema.py::TestHypomnemaStore::test_identity_or_foundational_hypomnema_defaults_review_only_even_below_promotion_threshold \
  tests/test_hypomnema.py::TestHypomnemaStore::test_hypomnema_upsert_preserves_existing_visibility_when_omitted \
  tests/test_hypomnema.py::TestHypomnemaStore::test_explicit_upsert_cannot_downgrade_existing_review_or_audit_visibility \
  tests/test_hypomnema.py::TestHypomnemaStore::test_explicit_operational_visibility_for_review_worthy_hypomnema_is_rejected \
  tests/test_simple_runtime.py::test_capture_foundational_identity_note_is_review_only_and_not_promoted \
  tests/test_cli_simple.py::test_inspect_defaults_to_operational_visibility \
  tests/test_mcp_surface.py::test_mcp_hypomnema_write_default_review_only_is_quarantined_from_search_candidates_promote_and_visible_in_review \
  tests/test_mcp_surface.py::test_review_queue_includes_review_only_proposal_rows_with_provenance \
  tests/test_mcp_surface.py::test_review_queue_excludes_terminal_review_only_proposals \
  tests/test_mcp_surface.py::test_audit_admin_proposal_review_lists_audit_only_rows_without_operational_exposure \
  tests/test_mcp_surface.py::test_inspect_and_forget_reject_non_operational_engrams \
  tests/test_retrieval.py::TestReactiveRetriever::test_retrieval_excludes_review_only_fts_and_propagation \
  tests/test_retrieval.py::TestReactiveRetriever::test_review_and_audit_rows_do_not_seed_operational_retrieval \
  tests/test_visualization_data.py::test_dashboard_extracts_operational_visibility_by_default \
  tests/test_visualization_data.py::test_dashboard_audit_mode_extracts_non_operational_rows \
  tests/test_encoding.py::TestEncoder::test_quarantined_engrams_do_not_end_bootstrap_policy \
  -q
git diff --check
```

### 8. No-mistakes is validation, not the first safety ledger author

The reusable gate in `docs/plans/afferent-membrane-plan-quality-gate.md` records the process lesson: if no-mistakes would plausibly find more than three material safety-ledger issues, stop and repair the plan before running it.

The U2.5 run surfaced repeated classes that should have been planned:

- proposal queue/status/terminal immutability gaps;
- multi-visibility read API mismatches;
- engram, belief, and functional-memory upsert visibility downgrades;
- review context and confirmation queue leakage;
- dashboard, CLI inspect, and visualization bypasses;
- PAI audit-only downgrade paths;
- direct hypomnema correction silent quarantine;
- bootstrap counts influenced by quarantined engrams;
- stale documentation of schema defaults versus write-time classification.

Fold every no-mistakes `ask-user` policy decision back into the plan or gate. In this lane, David resolved the terminal conflict policy: terminal proposal conflicts must be immutable, and later writes with the same terminal `proposal_id` are rejected.

### 9. Do not claim no-mistakes coverage for a stale head

After the final ledger commit, `no-mistakes axi status` still showed an active run on the same branch but at stale head `f52fcf90`, with CI running. The ledger repair head was `e285b3e`; later workflow/doc-review commits moved the branch again.

The correct action was to report that distinction precisely and not claim the new commit was no-mistakes-covered. Do not abort or restart an active run unless David explicitly authorizes that. The control-plane check that matters is:

```bash
no-mistakes axi
no-mistakes axi status
git log --oneline -3
```

If `no-mistakes` reports a PR for an old head, the branch may still contain newer pushed commits that are only locally tested. Say that. Do not treat the old run as proof for the new head. The closeout invariant is same-SHA proof: `git rev-parse --short HEAD` must match the active no-mistakes run head before U3 can start.

### 10. Capture the whole failure catalogue, not just the headline miss

The compound record needs to preserve the small failures because they are where the next plan will probably leak. The Afferent U2.5 chain produced these error classes and fixes:

| Failure class | Solution now recorded |
|---|---|
| RFC ledger drift and invented rule IDs | Source the plan from the RFC, map RFC-R1 through RFC-R8, reject operative R9/R10, and test the mapping. |
| Missing executable planning ledgers | Add Rule, State, Surface, and Regression ledgers directly to the main plan. |
| No-mistakes used as the first discovery surface | Add a pre-implementation plan-quality gate, local adversarial review, and no-mistakes error budget. |
| `ask-user` policy decisions not folded back | Require policy-resolving `ask-user` findings to update the plan or gate before the next implementation round. |
| U2/U3/U4 scope confusion | Defer authority stamping and David-only review; let U2.5 add only fail-closed visibility/proposal guardrails. |
| DynamicModulation active too early | Make U5 inert/conservative; require TTL/decay/magnitude plus valence floor/deadband/fail-toward-width before active influence. |
| ProposalLedger contract gaps | Use RFC axes, `audit_only` default, no pending operational rows, no raw `approved`/`applied` creation, immutable terminals, and explicit audit inspection. |
| Hypomnema write classification leak | Route stable/foundational/identity rows through the shared classifier before operational use. |
| Hypomnema migration/default drift | Preserve raw SQL `operational_context` for legacy compatibility while routing omitted live writes through classifier and repairing stale v6 defaults. |
| Upsert/revision downgrade leaks | Preserve or strengthen `read_visibility` for engrams, beliefs, functional memory, hypomnema, and proposals. |
| Runtime capture/maintenance bypass | Classify before encoding/promoting; review-only captures stay review-only and maintenance promotes only operational candidates. |
| Packet/review prose leaks | Use `packet_mode = operational | review`, operational counts/source IDs only, and explicit review mode for prose. |
| Audit-only review leakage | Exclude audit-only rows from ordinary review/operational packets and expose them only through explicit audit/admin tools. |
| Direct-ID, MCP, and CLI bypasses | Default inspect/forget/mutators to operational visibility, with explicit review/audit/admin flags where appropriate. |
| Retrieval, tag, dream, dashboard, bootstrap, and count leaks | Filter operational reads before ranking/propagation, tag lookup, dashboard defaults, and scoped counts. |
| Documentation drift | Sync user/operator docs and update the plan/gate so schema defaults, write-time classification, and review/audit behavior are not stale. |

## Why This Matters

Safety-ledger work fails when the plan feels rigorous but does not force executable invariants into the artifact future implementers will actually read. The failure is not lack of effort; it is the wrong proof surface. A separate review gate, a green focused test, or a no-mistakes run on an older head can all look like closure while the actual plan still lacks the ledgers that prevent U3/U4/U5 from laundering authority, visibility, or modulation behavior.

The repaired workflow changes the default:

- RFC first, derived plan second.
- Plan ledgers before implementation.
- State lifecycle before storage helper convenience.
- Surface matrix before assuming one redaction point is enough.
- Positive and negative executable selectors before closure.
- No-mistakes as validator, not author.
- Current branch head, not stale validation state.

## When to Apply

- Use this before any future Afferent unit that can affect durable memory, retrieval, review queues, proposal state, modulation, or authority.
- Use this when a plan is derived from an RFC and the user says the RFC is source of truth.
- Use this when review finds repeated omissions across state, surface, and regression proof.
- Use this when a no-mistakes run is active but not at the current commit.
- Use this when a plan names tests or chokepoints that need to be executable, not thematic.

## Examples

### Bad repair

```text
Add a plan-quality gate document and say future work should use it.
Leave the original plan with incomplete ledgers, vague proof surfaces, invented rule IDs, or file-level test names.
```

This fails because future work can continue from the original plan and miss the same invariants.

### Correct repair

```text
Patch the original plan so it contains:
- Rule Ledger
- State Ledger
- Surface Ledger
- Regression Ledger

Then add tests that fail if those ledgers disappear, if RFC-R9/R10 become operative, if U7 returns as an implementation unit, or if critical executable selectors are missing.
```

### Correct no-mistakes closeout

```text
Current branch head: <git rev-parse --short HEAD>
Active no-mistakes run head: f52fcf90
Active no-mistakes status/PR: <status and PR URL from no-mistakes axi status>

Result: if the two heads differ, the current branch head is locally tested/pushed only; no-mistakes covers the active run head, not the current head.
```

## Related

- `docs/plans/2026-06-30-001-feat-afferent-membrane-v1-plan.md`
- `docs/plans/2026-06-30-002-fix-afferent-u2-5-safety-ledger-plan.md`
- `docs/plans/afferent-membrane-plan-quality-gate.md`
- `tests/test_afferent_plan_quality.py`
- `/Users/davidef/phenom-felt-review/RFC-v1-proposal-ledger.md`
