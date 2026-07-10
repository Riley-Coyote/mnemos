# Privacy and Security Boundaries

Mnemos is designed to be local-first by default. Simple mode should give agents
continuity without requiring users to send memory data to a third-party model or
configure an external provider.

## Baseline Simple Mode

With no dedicated provider configured, Mnemos:

- stores memory in a local SQLite database
- uses local full-text search and deterministic maintenance
- scopes memory by agent, person, and project
- avoids OpenRouter, Anthropic, OpenAI, and OpenClaw requirements
- does not read arbitrary files or browser history
- does not transmit memory data over the network

Simple mode tools have these local side effects:

- `mnemos_context` can create or migrate the database and log maintenance
- `mnemos_context` and prompt/context assembly can record retrieval citation
  rows for rendered memories
- `mnemos_context(include_graph=true)` can return a scoped SVG identity graph
  artifact and structured graph data
- `mnemos_capture` writes continuity and, for operational captures, durable
  memories; high-blast identity/foundational captures can stay review-only
  instead of being promoted
- `mnemos_recall` can reconsolidate access metadata and write record-only
  retrieval events / retrieval-why receipts
- `mnemos_correct` can archive, revise, or supersede memory
- `mnemos_maintain` runs consolidation and bookkeeping; automatic evidence
  paths do not mutate belief confidence
- `mnemos_introduce` writes the agent's self-declared model/name for affinity
  checks
- `mnemos_health` reports only; opening an older store may still run schema
  migrations before the health card is read

Tool annotations describe these risks to MCP clients, but annotations are only
hints. They are not a security boundary.

## Host-Model Sampling

When an MCP client supports sampling, Mnemos may ask the host client's model for
in-band assistance during an active tool call. The client controls whether that
request is allowed.

Sampling requests should be:

- optional
- tied to the originating client request
- concise
- resilient when declined or unsupported
- free of secrets unless the user intentionally supplied them as memory content

Mnemos must always continue to work without sampling.

## Dedicated Providers

Dedicated model providers are optional. Mnemos should only use them when the
user explicitly configures provider environment variables or Mnemos model
configuration.

Provider keys enable richer maintenance, but they may send selected memory
content to that provider. This must remain an opt-in upgrade path, not a
baseline requirement.

## Scope Isolation

Every memory operation should resolve a scope:

```text
agent_id / person_id / project_scope
```

This prevents multiple agents on the same machine from accidentally sharing
continuity through the same database. Shared memory and federation are advanced
features and should stay opt-in.

In advanced MCP mode, scope-taking tools inherit the server's configured
agent/person/project scope when callers leave the scope args at their default
sentinels. Pass non-default scope args only for an intentional per-call
override.

## Schema Migration Runner

`mnemos migrate plan` and `mnemos migrate apply` are local operator surfaces for
the canonical SQLite store. They resolve the database from `MNEMOS_DB_PATH` or
`store.db_path` config (including `MNEMOS_STORE_DB_PATH`) and intentionally do
not accept `--db-path`; this keeps the runner from mutating an accidental
sibling store while operators are reviewing the live schema path.

`plan` opens the database read-only and reports pending versions, statement
classes, checksums, and snapshot paths without creating migration tables.
`apply` refuses missing stores, schema versions newer than the current binary,
edited shipped migration files, duplicate SQL version files, and non-additive
statements. Before each applied version it creates an integrity-checked SQLite
backup with the backup API under `<db-dir>/backups/migrations/`; rollback is
restoring that snapshot, not a down-migration.

SQL-file migrations are schema-only. They may add tables, additive columns,
indexes, and views, but may not seed, backfill, delete, update, attach another
database, change PRAGMA state, or write their own `schema_migrations` rows.

## Step 1 Instrumentation

Schema v12 adds local, record-only instrumentation. Retrieval events,
retrieval-why receipts, retrieval citations, drift-eval rows, and producer
failure counts stay in the same SQLite store as memory data. They do not alter
retrieval ranking, read visibility, reconsolidation policy, affect computation,
or durable identity decisions.

Runtime receipts are separate from `migration_receipts`; receipt kinds are
validated against a checked-in manifest before insertion. Citation rows are
written only when an output surface renders or prints a surfaced memory, and
context packet / prompt / CLI search citations are marked
`fitting_eligible=false`.

`mnemos drift-eval --db-path <copy.db>` and
`benchmarks/retrieval_benchmark.py --record-db <copy.db>` require explicit DB
paths for writes and refuse live `~/.mnemos` databases unless
`--allow-live-db` is supplied for an authorized live rollout.

## Step 2 Store Hygiene

Schema v13 adds prospective-memory status without adding a new operational read
surface. Only `kind="prospective"` engrams may carry status. New prospective
rows default to `open`; terminal statuses (`fulfilled`, `closed_unfulfilled`,
`retired`) must be written through the store transition API or
`mnemos prospective status`, which appends a `prospective-status-transition`
runtime receipt in the same transaction and refuses reopening.

## Step 3 Connection Rights

Schema v14 adds six nullable, default-free fields to `connections`: `valid_at`,
`invalid_at`, `confidence`, `runner_up_label`, `runner_up_confidence`, and
`classifier_version`. Existing rows receive `NULL`, and no runtime reader,
writer, classifier, lifecycle rule, index, constraint, or backfill is added.
In particular, the new connection `confidence` field does not grant authority
to mutate belief confidence or affect any operational read surface.

## Belief Confidence Authority

Belief confidence is no longer a side effect of automatic interpretation.
Encoder, LLM-classifier, consolidation, and substrate reflection paths can
record surprise, contradiction edges, logs, citations, and receipts, but they do
not raise or lower confidence for ordinary operational beliefs.

Confidence mutation is confined to explicit authority paths:

- pending belief review (`needs_review` or `confidence_pending_review`) when
  the review evidence SUPPORTS or CONTRADICTS the belief
- explicit correction or seeding flows
- operator restoration of false encoder contradiction events

Operational packets, prompt-builder output, bridge belief listings, MCP
`mnemos_beliefs`, and visual snapshots render a challenge line for each visible
belief: `under-challenge`, `revised-down (YYYY-MM-DD)`, or
`never-challenged`. The line is derived from existing belief JSON. Restore
events that annul false encoder contradiction revisions remove those revisions
from challenge-state derivation without deleting history.

`mnemos maintain --restore-false-contradictions --db-path <copy.db>` is the
operator repair path for the removed keyword-negation false-dissent writer. It
is dry-run by default, computes restoration from the current confidence and
false downward deltas, refuses non-raising restores, and appends annulling
revision events plus `belief_confidence_restore` runtime receipts on apply. It
preflights apply targets read-only and refuses live `~/.mnemos` databases unless
`--allow-live-db` is supplied for an authorized live rollout.

## Afferent Membrane Read Visibility

Schema v6/v7 separates ordinary operating context from review and audit
material:

- `read_visibility="operational_context"` is the default surface for retrieval,
  context packets, simple runtime context/recall, prompt building, visual
  snapshots, shared-pool reads, consolidation producers, substrate producers,
  modulators, and direct-ID advanced lookups (`mnemos_inspect`,
  `mnemos_forget`, and the hypomnema revise/supersede/promote mutators).
  Low-level store readers also fail closed to operational rows by default;
  unfiltered admin reads must pass `read_visibility=None` explicitly.
- `read_visibility="review_only"` keeps pending functional confirmations,
  hypomnema promotion candidates, and other review-shaped material out of
  operating context while leaving it visible to explicit review tools.
- Simple-mode first-capture verification stores only the note ID for
  non-operational captures. The restart proof may show a pending-review cue,
  but it must not re-quote review-only or audit-only prose into operational
  context.
- Live hypomnema writes classify stable promotion candidates and
  identity/foundational rows as `review_only` at write time. The bare
  `hypomnema_entries` SQL default is `operational_context` for legacy
  compatibility; callers that omit `read_visibility` still go through the
  store's write-time classifier before durable rows can enter ordinary context.
  The caller's domain label is checked against content classification and can
  only raise risk; underclaimed identity/foundational content is written at the
  effective domain, routed to review, and recorded as one scoped pending
  domain-claim proposal.
- `read_visibility="audit_only"` is excluded from ordinary operational reads
  and ordinary review queues; ProposalLedger audit rows require the explicit
  `mnemos_proposal_audit` admin/audit surface.
- `proposal_ledger` records durable-affecting candidates with authority,
  target surface, transition, blast radius, status, gate version, provenance,
  and payload fields so candidate state is inspectable without becoming
  operating context.
- Schema v10 `dynamic_modulations` rows are inert storage for bounded
  DynamicModulation work, not operational steering. The only store reads are
  by-primary-key lifecycle inspection and integer counts for telemetry/backout;
  no retrieval, salience, context packet, identity, consolidation, substrate, or
  MCP path reads the table into operating context. Every row requires a
  non-empty ASCII-edge-normalized `rollout_tag`, positive `ttl_seconds`,
  `expires_at > created_at`, non-evidentiary authority (`generated` or
  `observed`), no recurrence promotion, no identity authority, and bounded
  magnitude. Backout deletes rows by normalized rollout tag.
- U6b `ExperienceTick` proposes toward that modulation surface through the
  proposal ledger only. Emitted rows are `pending_review` and `review_only`;
  the tick refuses identity/foundational domains, disabled families, invalid
  targets, nonpositive TTL, and non-finite valence/decay values. It writes no
  `dynamic_modulations` row and adds no read path.
- Engram source authority is harness-stamped from the channel:
  `observed` for MCP/runtime/session-indexer surfaces, `imported` for curated
  PAI import, `generated` for autonomous producers, and observed-authority
  bootstrap source for setup seeding. MCP callers cannot pass
  `source_authority`, and payload text claiming `user_stated` or `imported`
  authority does not elevate the row. Hypomnema promotion does not mint
  user-witnessed origin; it stamps inference origin and stays out of shared-pool
  auto-publish.
- Advanced MCP callers also cannot rely on an implicit memory kind:
  `mnemos_remember` and `mnemos_ingest` require an exact `kind` declaration
  (`episodic`, `semantic`, `procedural`, or `prospective`) and reject omitted or
  unknown values before storage.
- Engram `origin_stamp` is a separate Step 1 measurement axis. New write paths
  stamp known origins such as `user-witnessed`, `inference`, or `import`;
  legacy `NULL` means pre-instrumentation absence of measurement, not proof of
  provenance.

## Identity Vault And Reconciliation

The T4 vault adds a second boundary around identity/foundational rows after the
afferent membrane. The canonical vault is active only when the trusted directory
`/usr/local/var/mnemos-vault` exists. Its journal path is pinned to
`/usr/local/var/mnemos-vault/decisions.jsonl`; production apply, legacy witness,
initial-rollout witness, session-start reconcile, and the watchdog do not accept
environment or call-argument redirects for that trust-bearing path.

When the vault is active:

- Operational reads of identity-tier `beliefs` and `hypomnema_entries` require
  `decision_ref`, the hash of an approved journal line.
- The journal file must be root/vault-owned and not agent-writable. A present
  agent-owned or agent-writable `decisions.jsonl` is unusable: apply refuses to
  witness against it, legacy and initial-rollout stamping skip it, and reconcile
  reports `journal_untrusted` while quarantining already witnessed operational
  identity rows.
- `mnemos-decide --initial-rollout` is the one-time DAVID-10 batch witness for
  mapped, restamped `hypomnema_entries` left `review_only`; session-start
  `apply_initial_rollout()` stamps and promotes only rows that still verify
  against the newest rollout line for that row. Beliefs and native/unmapped held
  hypomnema are excluded from the batch.
- Missing, unreadable, corrupt, or untrusted journals fail closed. Reconcile
  leaves no witnessed operational identity row trusted solely because the
  journal cannot be verified.
- Reconcile checks both table-to-journal and journal-to-table. It catches
  de-tiering, re-domain changes, content mutation, lifecycle hiding, stale
  rollout refs, forged or missing refs, and cleared-ref hides that would
  otherwise disappear from an identity-tier query.
- A cleared-ref row is restored only when its witness still fully re-verifies.
  Reconcile restores the verified `decision_ref` and operational visibility in
  the same transaction; genuine witnessed-field changes stay `review_only`
  until re-approved.

## Gated Inner Life And Soak

The U6.6/U7 inner-life and soak surfaces are operator/pre-soak tooling, not
baseline background behavior.

- `mnemos inner-life ...` and `mnemos soak ...` DB-using commands require
  `--db-path` and refuse live `~/.mnemos` databases unless `--allow-live-db`
  is supplied for an explicitly authorized live rollout.
- Schema v8 `inner_life_events` is a private sub-ledger for provenance,
  activity-gate decisions, generated-candidate skips/drops, scheduled-run
  telemetry, and soak tick summaries. It is not an operational context source.
- Generated reflection, wandering, and dream candidates must pass the narrative
  gate before persistence. Passed candidates are private, low-confidence,
  `read_visibility="audit_only"`, not voice exemplars, and not consolidation
  authorized; identity/foundational-domain generated output is dropped. The
  low-stakes engram and its idempotency ledger row are committed atomically so a
  retry cannot duplicate a generated row after a partial failure.
- Inner-life status and soak tick telemetry count generated memory writes,
  belief writes, identity patches, and shared-pool writes so rollbacks can
  verify that belief, identity, and shared-pool counters remain zero.
- Default config keeps `inner_life.schedules.enabled`, every per-process
  schedule, `soak.tick.enabled`, and `soak.families.*.enabled` disabled.
  Preflight reports missing/disabled kill switches, provider readiness,
  observer reviewer configuration, pre-soak snapshot readiness, launchd paths,
  halt marker, rollback commands, and known per-process activation blockers
  before activation. The blocker registry is currently empty — `affect`'s
  `emotional-driver-filter-after-limit` residual closed when RM-7 landed the
  recency paging primitive; a process listed there in the future must remain
  unscheduled, with its disabled schedule/activity switch treated as the safe
  state rather than a readiness penalty.
- `inner-life plist` and `soak plist` write launchd plist files but do not call
  `launchctl`. `soak preflight --dry-run-tick` runs the tick against a SQLite
  backup copy, disables real LLM-client construction, and does not mutate the
  supplied DB.

## Visual Artifacts

Identity graph artifacts are generated from the same scoped local memory data
used by `mnemos_context`. They should not include raw database paths, provider
keys, unscoped cross-agent memories, or review-only prose in operational views.
Hosts that render images may display the SVG inline; hosts that do not can
ignore it and continue using the text and structured content.

## PAI Importer

The `mnemos pai-import` operator workflow replays a JSON source manifest
(identity-kernel, david-context, growth-substrate, beliefs, hypomnema) into a
Mnemos store. It is opt-in and intended for operators bringing a pre-existing
PAI-shaped corpus into a fresh agent — not for end users.

Safety boundaries enforced by the importer:

- `preview` and `watch-preview` open the DB read-only and never mutate state.
- `apply` and `watch-apply` take an integrity-checked SQLite backup before
  writing, into the configured `--backup-dir`.
- Every DB-using subcommand refuses the default live database
  (`~/.mnemos/memory.db`) and other databases under `~/.mnemos` unless
  `--allow-live-db` is passed. The guard is inode-based so case-insensitive
  path variants cannot bypass it.
- Manifest source paths must stay inside the manifest directory after
  resolution; absolute paths are allowed only when they still resolve inside
  that directory, and `..`-escaping paths are rejected at load.
- Source splitting strips Strict-B coordinate-value lines from any source kind
  before row hashing/indexing. Eigenvalue, vivezza, coordinate-target, and
  persona-signature tuple values are runtime substrate rather than retrievable
  memory; prose around those lines is preserved.
- The dual-life watcher (`watch-once` / `watch-plist`) advances its state only
  after a successful apply. Preview mode leaves state untouched. Missing source
  files are treated as an empty current snapshot so removed sections become
  explicit tombstone, deactivate, or review actions instead of silent drift.
  Open prospective engram targets are not tombstoned or rewritten by import
  paths; they surface review/refusal receipts until an explicit prospective
  status transition closes the want.
- Imported rows carry `decay_protected`, `softening_protected`, and
  `consolidation_authorized` flags so the consolidation and substrate passes
  cannot silently rewrite imported identity material.
- Imported beliefs carry `confidence_pending_review` and `review_only`
  read visibility, so they are excluded from default belief consumers until
  explicit belief review with SUPPORTS/CONTRADICTS evidence clears the flag and
  restores `operational_context`. NO_BEARING review output leaves them pending.
- Enforcement links: `mnemos/importer/operator.py`, `mnemos/importer/watcher.py`,
  `mnemos/importer/review_gate.py`, `tests/test_u3b_pai_operator.py`,
  `tests/test_u3c_pai_watch_doctor.py`, and
  `tests/test_u3c_pai_review_gate.py`.

## Correction and Forgetting

Mnemos favors audited correction over hard deletion:

- corrections can archive old engrams
- continuity notes can be revised or superseded
- audit trails remain available to advanced/admin tools

Future user-facing forget flows should make the difference between archive,
supersede, and hard deletion explicit.

## Release Review Checklist

Before a release:

- verify simple mode works with no provider keys
- verify simple mode exposes only seven tools
- verify advanced mode preserves admin tools
- verify advanced tools inherit configured agent/person/project scope
- verify gated inner-life and soak schedules remain disabled unless an
  authorized activation artifact is being tested
- verify `mnemos doctor` does not leak secrets
- verify package artifacts include templates and simple-mode modules
- verify MCP sampling failures do not break tool calls
- verify agent/person/project scope isolation
- verify docs say provider keys are optional
