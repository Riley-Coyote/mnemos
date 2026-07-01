# Afferent Membrane Plan-Quality Gate

Use this gate before implementing any future Afferent Membrane unit that can affect durable memory, retrieval, review queues, proposal state, modulation, or authority. The goal is to catch safety-ledger gaps during planning, not after no-mistakes has to discover them through repeated repair rounds.

The RFC remains the source of truth. A plan is executable only when it proves its derivation from the RFC and names the exact code chokepoints and tests that protect each invariant.

## Required Gate

Before implementation starts, the plan must include all of these items:

1. **Rule Ledger**
   - Map RFC-R1 through RFC-R8 to implementation units, code chokepoints, and proof surfaces.
   - Use the exact shape: RFC rule -> implementation unit -> chokepoint -> positive test -> negative test.
   - State that the plan is derived from the RFC and cannot override, renumber, or weaken it.
   - State that RFC-R9 and RFC-R10 do not exist; ExperienceTick is constrained by RFC-R3/R7/R8 rather than a separate permission rule.

2. **State Ledger**
   - For each durable object, name allowed states, defaults, omitted-field behavior, upsert behavior, terminal states, allowed transitions, and rejected transitions.
   - Record write-time defaults for each durable surface.
   - Record whether conflict/upsert behavior preserves, strengthens, rejects, or intentionally relaxes visibility/status.
   - For visibility-bearing rows, generic upserts must not downgrade `review_only` or `audit_only` into operational use.
   - Name every terminal state and whether it is mutable.
   - For ProposalLedger rows, terminal conflicts are immutable: a later write with the same `proposal_id` must be rejected and the original terminal audit record must remain unchanged.

3. **Surface Ledger**
   - For each read surface, define what operational, review, audit, admin, migration, direct-ID, aggregate/count, visual, MCP, and context paths can see.
   - Prove audit-only rows are absent from ordinary operational and review surfaces, including counts and IDs unless explicitly allowed.
   - Treat raw SQL helpers, stats/count methods, direct-ID reads, and bootstrap heuristics as chokepoints when they can influence operational behavior.

4. **Migration and legacy-row policy**
   - Name which legacy rows remain operational, which become review-only, and which become audit-only.
   - Add a populated legacy database test when defaults or backfills change.

5. **Regression Ledger**
   - Every invariant needs at least one positive test and one negative test.
   - Include explicit tests for fresh high-confidence/foundational review-only classification, duplicate/upsert non-promotion, terminal proposal immutability, and sample limits not altering total queue counts.

6. **DynamicModulation bound check**
   - State whether DynamicModulation is inert, conservative, or actively influencing retrieval.
   - Active influence is not allowed until both bounds exist: TTL/decay/magnitude and valence floor/deadband/fail-toward-width.

7. **No-mistakes error budget**
   - Before no-mistakes, run a local adversarial review against the plan and implementation.
   - If the expected no-mistakes review would plausibly find more than three material safety-ledger issues, stop and repair the plan first.
   - Any `ask-user` finding that resolves a policy must be folded back into the plan or this gate before the next implementation round.

## Stop Conditions

Stop before implementation when any of these are true:

- The RFC ledger is missing, incomplete, or uses invented rule IDs.
- A code chokepoint is named only generically, without writer/reader/test surfaces.
- A plan says "default" without saying whether it is schema-time, write-time, migration-time, or review-gate-set.
- A terminal state can be overwritten without an explicit immutable or reviewed-transition rule.
- A review/audit row can influence operational retrieval, context, dashboard, bootstrap, aggregation, CLI inspect, MCP output, promotion, or producer behavior.
- DynamicModulation can affect retrieval before both RFC-R7 bounds exist.
- The plan relies on no-mistakes to discover basic ledger invariants instead of using no-mistakes as validation.

## Evidence From U2.5

The U2.5 no-mistakes run found repeated omissions that this gate is meant to prevent:

- proposal queue/status/terminal immutability gaps;
- multi-visibility read API mismatches;
- engram, belief, and functional-memory upsert visibility downgrades;
- review context and confirmation queue leakage;
- dashboard, CLI inspect, and visualization bypasses;
- PAI audit-only downgrade paths;
- direct hypomnema correction silent quarantine;
- bootstrap counts influenced by quarantined engrams;
- stale documentation of schema defaults versus write-time classification.

Future plans should assume these classes recur unless the plan explicitly proves otherwise.

See `docs/solutions/workflow-issues/afferent-membrane-safety-ledger-repair-workflow-2026-07-01.md` for the reusable workflow lesson and full failure catalogue from the U2.5 repair.
