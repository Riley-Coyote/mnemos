# Step 3 S2 generic-save connection firewall evidence

Reviewed implementation commit: `b2ba1fc` (`fix(store): firewall generic connection saves`)

Authority:

- Arc: `/Users/davidef/pai-supervision/reports/STEP3-CONNECTIONS-ARC.md`
- Arc SHA-256: `35a0c120e6a983ebc9952ab0c186c626f13520817f697320277d88fb82fed815`
- Binding ruling: `/Users/davidef/pai-supervision/reports/054-s2-receipts-ruling.md`

## Behavioral proof

- Generic public, inner-life, PAI no-commit, and shared-publish saves never dispatch
  the connection writer, even with populated or tampered `Engram.connections`.
- Existing connection rows are compared as raw 12-column tuples; generic saves leave
  all six S1 rights fields unchanged.
- Explicit combined and edge-only seams persist only declared deltas, deduplicate by
  connection key, roll back mid-batch failures, and reject receipt context in S2.
- Encoder discovery and surprise, retrieval reconsolidation, consolidation discovery,
  softening, and shared conflict retain their deliberate graph mutations through the
  explicit paths; shared publish remains generic and does not copy attached edges.
- Nested explicit saves fail before `BEGIN`, preserving caller-owned transactions.

## Verification

- Focused firewall/producer matrix: 13 firewall tests plus encoder, retrieval,
  discovery, softening, identity, and agent-scoping coverage passed.
- Canonical full suite: `uv run --extra dev --extra mcp pytest -q` — **1,294 passed,
  2 skipped**.
- Ruff check and format check passed for all touched Python files.
- `git diff --cached --check` passed before commit.
- Code-review run: `/tmp/compound-engineering/ce-code-review/20260710-053712-7bce8bff/`;
  correctness, API, performance, reliability, adversarial, standards, Python, and
  agent-native reviewers found no unresolved actionable findings after fixes.
