# Mnemos Architecture

## Overview

Mnemos is a complete agent cognition system, not just a memory library. It operates in five layers that together give an AI agent persistent identity, living memory, autonomous maintenance, and cross-agent awareness.

```
┌─────────────────────────────────────────────────────────┐
│                    Agent Sessions                        │
│              (user conversations, tasks)                 │
├─────────────────────────────────────────────────────────┤
│                  Identity Architecture                   │
│         SOUL.md  IDENTITY.md  MEMORY.md  AGENTS.md      │
├─────────────────────────────────────────────────────────┤
│                     Cron Suite                           │
│     Observer  Indexer  Substrate  Maintenance  Bridge    │
├─────────────────────────────────────────────────────────┤
│                   Mnemos Core                            │
│      Engrams  Connections  Beliefs  Consolidation        │
├─────────────────────────────────────────────────────────┤
│                    Substrate                             │
│     Decay  Dreaming  Reflection  Modulators  Events     │
├─────────────────────────────────────────────────────────┤
│                 Cross-Agent Layer                        │
│        Shared Pool  Bridge  Federation  Attestation      │
└─────────────────────────────────────────────────────────┘
```

## Layer 1: Mnemos Core (Graph Memory)

The foundation. A living memory graph backed by SQLite.

### Schema Evolution

Python migrations in `mnemos/store/migrations.py` are frozen history through
schema v10. New schema changes use additive-only SQL files under
`mnemos/store/migrations/NNNN_name.sql`, applied by `MigrationRunner` during
store bootstrap or explicitly through `mnemos migrate plan|apply`.

The runner maintains `schema_migrations`: pre-runner Python versions are
grandfathered with sentinel checksum metadata, SQL-file versions record their
real checksum and pre-apply snapshot path, and a checksum mismatch on an
already-applied file aborts as edited history. It fails closed when the store
contains a `schema_migrations` version newer than the binary knows.

Every SQL-file migration is linted as schema-only shadow DDL. Allowed statement
classes are new tables, additive columns with nullable or constant defaults,
indexes, and views. DML, destructive DDL, triggers, PRAGMA writes, `VACUUM`,
`ATTACH`/`DETACH`, `CREATE TABLE AS SELECT`, and migration-file writes to
`schema_migrations` are rejected before apply. Rollback is snapshot restore:
before each applied version the runner uses the SQLite backup API to create an
integrity-checked snapshot under the database directory, then commits the SQL
and `schema_migrations` row in one transaction.

Schema v11 creates `migration_receipts`, the applied-migration receipts journal.
`schema_migrations` remains the canonical version/checksum record.

Schema v12 adds record-only Step 1 instrumentation journals. Runtime receipts
(`runtime_receipts`) are separate from migration receipts and validate against a
checked-in receipt-kind manifest before durable write. Retrieval calls append
`retrieval_events`, rendered output surfaces append `retrieval_citations`, and
drift-eval registry/metric rows live in `drift_eval_runs` and
`drift_eval_observations`. `instrumentation_failures` stores durable
per-producer failure counts. Existing engrams keep `origin_stamp=NULL`; NULL is
pre-instrumentation absence of measurement, not a measured provenance value.

Schema v13 adds `engrams.status` for prospective memory lifecycle tracking.
Only prospective engrams may carry status. New prospective rows default to
`open`, and the only terminal states are `fulfilled`, `closed_unfulfilled`, and
`retired`. Terminal transitions go through `transition_prospective_status()` or
`mnemos prospective status`, which update the row and append a
`prospective-status-transition` receipt in one transaction; direct upserts
cannot reopen or retarget terminal prospective rows.

Schema v14 adds nullable, default-free connection-rights storage: `valid_at`,
`invalid_at`, `confidence`, `runner_up_label`, `runner_up_confidence`, and
`classifier_version`. The migration preserves existing connection values and
leaves every new field `NULL`; no runtime reader, writer, lifecycle rule,
classifier behavior, index, constraint, or backfill is part of this slice.

### Engrams

The fundamental unit of memory. Each engram has:

- **Content**: What happened (mutable through reconsolidation)
- **Impact**: Why it matters (the lasting insight)
- **Dual-trace model**: S0/strength, stability, accessibility — three
  independent dimensions. The stored column is still `strength`; inspect and
  dashboard surfaces label it as S0.
- **Kind**: Episodic (experiences), semantic (facts), procedural (how-to), prospective (future-directed)
- **Prospective status**: Prospective engrams start `open` and may transition
  once to `fulfilled`, `closed_unfulfilled`, or `retired`; non-prospective
  engrams cannot carry a status.
- **Confidence**: Scored by source reliability (user-explicit → speculative)
- **Source authority**: Harness-stamped provenance (`user_stated`, `imported`,
  `observed`, or `generated`) derived from the ingest channel, never from
  payload text. `Encoder.encode()` requires an explicit `source_authority`
  keyword from trusted code; MCP tools do not expose an authority parameter.
  Advanced MCP capture tools (`mnemos_remember`, `mnemos_ingest`) also require
  callers to declare `kind` explicitly as one of the canonical engram kinds; no
  default kind is inferred at the capture surface. Setup seeding uses bootstrap
  source with observed authority. Hypomnema promotion uses observed authority,
  stamps inference origin, and does not auto-publish to the shared pool.
- **Origin stamp**: Step 1 provenance measurement for new engram writes when
  the producer knows the origin. The closed vocabulary is `user-witnessed`,
  `inference`, `retrieval`, and `import`; current write paths stamp
  user-witnessed, inference, or import. It is separate from source authority.
  Legacy or intentionally unstamped rows use `NULL`, meaning no measurement was
  present.
- **State lifecycle**: Active → consolidating → dormant → archived
- **Resolution**: High → low (details fade through softening, like human memory)
- **Full version history**: Every reconsolidation is tracked
- **PAI import controls** (schema v4/v5): `decay_protected`, `softening_protected`,
  `consolidation_authorized`, `voice_exemplar_eligible`, `original_substrate`,
  `original_timestamp`. Decay skips `decay_protected` rows, softening skips
  `softening_protected` rows and filters voice exemplars through
  `voice_exemplar_eligible`, and consolidation/substrate mutation paths require
  `consolidation_authorized` so imported identity material is not silently
  rewritten by background maintenance.
- **Afferent Membrane controls** (schema v6/v7): producer-fed rows carry
  `read_visibility` (`operational_context`, `review_only`, or `audit_only`).
  Operational retrieval, packets, consolidation, substrate producers, and
  modulators read only `operational_context` rows by default; review APIs opt
  in explicitly. Stable promotion candidates and identity/foundational live
  hypomnema writes classify to `review_only` by default, while the bare
  hypomnema SQL default stays `operational_context` for legacy compatibility;
  omitted-visibility callers still pass through the write-time classifier
  before ordinary use. Caller-supplied hypomnema domains can only escalate
  above the content classifier; underclaimed high-blast content is stored at
  the effective domain, routed to review, and logged as a deduped domain-claim
  proposal. Proposed durable transitions are tracked in
  `proposal_ledger` with authority, target surface, transition, blast radius,
  visibility, status, reason, gate version, provenance, and payload fields.
- **Identity vault controls** (schema v9): identity/foundational rows in
  `beliefs` and `hypomnema_entries` additionally carry `decision_ref`, the hash
  of an approved line in the canonical root/vault-owned decisions journal.
  When the vault directory is installed and trusted, operational reads exclude
  identity-tier rows with no ref, and apply/legacy/initial-rollout witness paths
  read only the canonical journal. A present agent-owned or agent-writable
  journal leaf is unusable-at-read: apply refuses it and reconciliation routes it
  through the same quarantine-all fail-closed handling as a corrupt journal.
- **DynamicModulation storage** (schema v10): the `dynamic_modulations` table is
  an inert persistence and backout surface, not an active retrieval signal.
  `EngramStore.store_dynamic_modulation()` can persist reversible,
  non-evidentiary modulation rows with `generated` or `observed` authority,
  bounded magnitude, positive TTL, normalized non-empty rollout tags, and
  `expires_at > created_at`; `get_dynamic_modulation()`,
  `count_dynamic_modulations()`, and
  `delete_dynamic_modulations_by_rollout_tag()` are lifecycle, telemetry, and
  backout helpers. Retrieval, salience, context packets, identity profiles,
  consolidation, substrate producers, and MCP tools do not read this table.
- **ExperienceTick proposals** (U6b): `mnemos.modulation.ExperienceTick`
  converts `ProposedModulation` observations into review-visible
  `proposal_ledger` rows targeting the modulation surface. It is manually
  invoked, honors per-family kill switches, rejects identity/foundational
  domains, requires rollout tags and positive TTL, uses deterministic proposal
  IDs for re-tick stability, and batches writes atomically through
  `write_proposal(commit=False)`. It does not write `dynamic_modulations` rows
  and does not add a modulation read/apply path.
- **Gated inner-life controls** (schema v8): generated reflection, wandering,
  and dream rows pass through the narrative gate and low-stakes writer before
  persistence. Passed rows are private, low-confidence, audit-only engrams
  written transactionally with their idempotency ledger rows; provenance, skips,
  drops, scheduled-run telemetry, and soak tick summaries live in
  `inner_life_events` instead of ordinary proposal or review queues.
- **PAI coordinate guard**: before imported text becomes retrievable rows, the
  splitter strips Strict-B eigenvalue, vivezza, coordinate-target, and
  persona-signature tuple-value lines from any source kind. These values are
  runtime steering substrate; Mnemos imports the surrounding prose, not the
  coordinate values.

### Connections

Typed edges between engrams that form the memory graph:

- `supports`, `contradicts`, `causes`, `extends`, `parallels`, `synthesizes`, `grounds`
- Connections have strength that evolves through co-retrieval and consolidation
- Connection discovery runs automatically during consolidation
- Schema v14 reserves nullable connection-rights fields, but they are inert
  storage until a later slice defines and implements their runtime contract

### Beliefs

Higher-order knowledge structures extracted from patterns across engrams:

- Confidence tracking with tier-based change detection. Automatic encoder,
  classifier, consolidation, and reflection paths can detect support or
  contradiction for surprise, edges, and bookkeeping, but they do not mutate
  belief confidence. Confidence changes require explicit pending-review,
  correction, seeding, or restore authority.
- Domain categorization (engineering, social, preferences, etc.)
- Beliefs can be evaluated during deep consolidation. Only beliefs already
  queued through `needs_review` or `confidence_pending_review` can have
  confidence changed by belief review; non-pending SUPPORTS/CONTRADICTS results
  are logged without mutation, and NO_BEARING leaves pending beliefs pending.
- Full revision history
- Operational belief renders include launch-minimal challenge state:
  `under-challenge` for pending review, `revised-down (YYYY-MM-DD)` for the
  latest non-annulled downward revision, or `never-challenged` otherwise.
  Restore events can annul false encoder contradiction revisions so they no
  longer render as live challenge evidence.
- PAI import metadata: `tier` (foundational | operational | tactical),
  `needs_review`, and `confidence_pending_review` mark beliefs whose
  upstream source has changed but has not been re-reviewed by the operator.
  Default `get_beliefs()` consumers exclude pending-confidence beliefs;
  belief review opts in so it can resolve them, clear the pending flags, and
  move approved rows back to operational read visibility.

### Encoding Pipeline

Content → LLM classification → engram creation → connection discovery →
embedding (optional). LLM belief comparisons classify relationships only:
SUPPORTS/CONTRADICTS can contribute surprise and contradiction edges, but they
do not directly revise belief confidence. The former keyword-negation fallback
that inferred dissent from word overlap plus negation tokens has been removed.

### Retrieval

Cue → read-visibility prefilter → FTS5 search + embedding similarity → graph
activation/scoring → reconsolidation → results

Operational retrieval uses `read_visibility="operational_context"` before FTS,
embedding hits, graph propagation, scoring, and reconsolidation. Store helpers
that load by ID, traverse connections, read versions, search archive rows, or
load functional/hypomnema rows also default to operational visibility; explicit
`read_visibility=None` is the unfiltered admin opt-in. Version history inherits
the parent engram's visibility after the parent row has cleared the caller's
filter; list/read paths that intentionally include multiple visibilities gate
versions with an `IN` filter rather than leaking ungated history. Explicit
review callers can request `review_only`; audit-only rows stay out of normal
review flows.

Step 1 instrumentation records the retrieval path without changing ranking:
each `ReactiveRetriever.retrieve()` call writes one retrieval event, surfaced
results receive stable `retrieval_why` metadata, and surfaced results also emit
`retrieval-why` receipts with immediacy in the receipt envelope. Context packet,
prompt-builder, and CLI search surfaces mark citations only when a surfaced
memory is actually rendered or printed; those citation rows carry metadata tiers
and are not fitting-eligible.

With reconsolidation enabled, successful retrieval updates the returned visible
memory: access count, stability, accessibility, and new connections. The stored
strength column is S0 and remains fixed after encoding.
Operational paths return operational rows. Memories are living traces.

### Operational Context And Review

Context packets have two modes:

- `operational`: the default for agents. It includes normal functional memory,
  hypomnema, beliefs, and engrams, plus review counts/source IDs with prose
  withheld.
- `review`: the explicit operator surface. It can include review-only
  confirmation and promotion-candidate prose with labels and provenance cues.

`proposal_ledger` holds candidate durable transitions separately from the
operational packet so generated or review-shaped material can be audited before
it becomes operating context. Audit-only proposal rows require the explicit
`mnemos_proposal_audit` admin/audit surface and do not appear in ordinary
operational or review packets.

The T4 identity vault is the additional reviewer gate for identity/foundational
belief and hypomnema rows. `apply_identity_decision()` applies only a
chain-verified approved/rejected journal line whose content hash still matches
the proposal under the write lock; `apply_legacy_witness()` stamps pre-vault
identity rows from legacy witness lines without promoting rows that were already
review/audit-only. `apply_initial_rollout()` is the one-time DAVID-10 companion:
it reads approved `witness="initial-rollout"` lines, accepts only the newest
line per `(table, row_id)`, re-verifies mapped review-only identity rows under
the write lock, then stamps and promotes matching rows to
`operational_context`. `reconcile_identity_vault()` runs at session start and
from the watchdog. It holds a `BEGIN IMMEDIATE` span lock while it checks both
directions (table -> journal and journal -> table), quarantines orphaned,
forged, de-tiered, content-mutated, lifecycle-hidden, stale-ref, or unverified
rows, and restores operational visibility only when the journal witness fully
re-verifies. For a raw-SQL hide that cleared `decision_ref`, restore writes the
verified ref and visibility together so the next pass does not re-orphan the
row; an unstamped initial-rollout row still at `review_only` is treated as a
benign pending-apply state, while a curator-held row is reported without being
promoted.

## Layer 2: Substrate (Inner Life / Consolidation)

The "sleeping brain" — autonomous processing that runs between sessions.

### Consolidation Cycle

1. **Decay**: Recalculate stability and accessibility while preserving S0.
   Unused memories fade.
2. **Connection Discovery**: Find new semantic relationships between engrams.
3. **Softening**: LLM-mediated lossy compression. Low-resolution memories get rewritten preserving essence.
4. **Belief Review**: Evaluate beliefs against new evidence, resolving
   explicit pending-review confidence queues only when evidence bears on the
   belief.
5. **Reflection**: Generate gated low-stakes thoughts and refresh the
   graph-derived identity profile; reflection does not revise belief confidence
   automatically.

### Gated Inner Life

The U6.6 inner-life layer is a private pre-soak path below ordinary operating
context. Session and turn finalizers write provenance rows to
`inner_life_events`; activity gates decide whether `challenge`, `observe`,
`affect`, `reflect`, `wander`, or `dream` have enough grounded signal to run.
Generated reflection, wandering, and dream candidates must pass the narrative
gate, which drops null, ungrounded, manufactured, metrics-only, introspection-
rejected, and identity/foundational-domain output.

Passed generated candidates are written only as private
`read_visibility="audit_only"` low-stakes engrams. They are rollout-tagged,
source-grounded, not voice exemplars, not consolidation authorized, and never
belief writes, identity patches, hypomnema promotions, or shared-pool
publications. Scheduled `wander` and `dream` pass person/project/rollout scope
through the substrate event payload so their generated rows remain backoutable
by rollout scope.

Activity, cooldown, and cadence gates select eligible recent rows in SQL before
applying scan limits. The emotional driver's semantic influence filter cannot
move into SQL, so its recency scan instead pages newest-first with a
`(created_at, id)` cursor until enough influencing rows are collected or the
window is exhausted (RM-7) — closing the former
`emotional-driver-filter-after-limit` residual with unchanged affect semantics.

The U7 soak tick composes enabled families from `soak.families` and
`inner_life.schedules`, records telemetry in `inner_life_events`, and keeps
launchd loading behind an explicit operator authorization gate. It wires a
model client for enabled generative families (`reflect`, `wander`, `dream`)
only when the caller has not supplied one; soak activation preflight disables
that auto-wiring during the copy-DB dry run. The default config leaves both
inner-life schedules and soak ticks disabled.

### Event System

The substrate produces events based on what it discovers:

| Event | Trigger | Handler |
|-------|---------|---------|
| `MEMORY_SOFTENED` | Vividness below threshold | Dreaming |
| `CONNECTION_DISCOVERED` | New semantic link | Insight |
| `BELIEF_CONTRADICTED` | Explicit review/correction lowers confidence across a tier | Reflection |
| `BELIEF_CONFIRMED` | Explicit review/correction raises confidence across a tier | — |
| `SILENCE_EXTENDED` | No memories in 6+ hours | Wandering |
| `SALIENCE_ACCUMULATED` | Multiple high-salience events | Initiation |

### Modulators

Six emotional modulators that influence retrieval and encoding:

- **Arousal**: Overall activation level
- **Openness**: Willingness to form new connections
- **Resolution**: Detail preservation threshold
- **Selection threshold**: How strong a memory must be to surface
- **Temperature**: LLM creativity parameter
- **Surprise sensitivity**: Threshold for surprise detection

Modulators are recalculated every substrate tick based on recent activity.
They are separate from schema v10 DynamicModulation rows, which are stored
inertly for future bounded influence activation work and are not read by
substrate modulator recalculation. U6b ExperienceTick proposals do not change
that: they are review artifacts in the proposal ledger, not substrate
modulator inputs.

## Layer 3: Cron Suite (Sensory System)

The agent's autonomous processes — the things that happen in the background to keep the agent alive between sessions.

| Cron | Schedule | Purpose |
|------|----------|---------|
| **Observer** | Every 30 min | Reads session transcripts → updates active-context.md |
| **Session Indexer** | Every 30 min | Extracts memories from conversations → encodes into graph |
| **Substrate Tick** | Every 4 hours | Runs consolidation cycle (decay, dreaming, beliefs) |
| **Memory Maintenance** | Every 6 hours | Reviews sessions → updates MEMORY.md |
| **Cross-Agent Bridge** | Every 2 hours | Syncs context between agents |
| **Morning Brief** | Daily 10 AM | Generates daily summary and priorities |
| **Daily Debrief** | Daily 5 AM | End-of-day recap and handoff |

These crons run as isolated OpenClaw sessions — they don't interfere with active conversations.

### Data Flow

```
User Session → transcript.jsonl
     ↓
Session Indexer (every 30 min)
     ↓ extracts memories
Mnemos Graph ←─── Substrate Tick (every 4h)
     │                    ↓ produces events
     │              Event Handlers (dreaming, reflection, insight)
     │                    ↓ may create new engrams
     ↓
Observer (every 30 min) → active-context.md
     ↓
Memory Maintenance (every 6h) → MEMORY.md
     ↓
Morning Brief (daily) → daily/morning-brief-{date}.md
```

## Layer 4: Identity Architecture (Persistent Self)

The files that define who the agent is. Together they form a complete identity that persists across sessions.

| File | Purpose | Update Frequency |
|------|---------|-----------------|
| **SOUL.md** | Essence, personality, philosophy, voice | Rarely (manual) |
| **IDENTITY.md** | Role, capabilities, boundaries, protocols | Occasionally (manual) |
| **MEMORY.md** | Living memory — facts, projects, patterns | Every 6 hours (cron) |
| **AGENTS.md** | Multi-agent topology and protocols | Rarely (manual) |
| **HEARTBEAT.md** | Health monitoring configuration | Rarely (manual) |
| **active-context.md** | Current threads, open questions, where we left off | Every 30 min (cron) |

### Session Startup

When an agent starts a new session, it loads:

1. **SOUL.md** → knows who it is
2. **IDENTITY.md** → knows what it can do
3. **MEMORY.md** → knows what it knows
4. **active-context.md** → knows what's happening right now
5. **cross-agent-context.md** → knows what other agents are doing

This gives the agent complete continuity across session boundaries.

## Layer 5: Cross-Agent Infrastructure (Multi-Agent Awareness)

Enables multiple agents to work together without direct communication.

### Shared Memory Pool

- Dedicated database (`~/.mnemos/shared.db`) accessible to all agents
- Agents publish memories with visibility controls (private/shared/public)
- Conflict resolution when agents disagree (confidence > S0/strength > recency)
- Relationship and trust tracking between agents

### Cross-Agent Bridge

- Reads each agent's `active-context.md`
- Writes per-agent summaries to shared directory
- Generates combined `cross-agent-context.md`
- Distributes combined context back to each agent's workspace

### Federation (Planned)

- Cross-instance memory synchronization
- Selective memory sharing across network boundaries

### Attestation (Planned)

- Cryptographic provenance for shared memories
- Trust verification across federated instances

## How They Connect

The five layers form a feedback loop:

1. **User talks to agent** → session transcript created
2. **Session Indexer** extracts memories → stored in **Mnemos Core**
3. **Substrate** consolidates memories → events trigger handlers → new insights
4. **Observer** reads transcripts → updates **active-context.md** (Identity layer)
5. **Memory Maintenance** reviews sessions → updates **MEMORY.md** (Identity layer)
6. **Cross-Agent Bridge** syncs context → other agents gain awareness
7. **Next session** loads identity files → agent has full continuity
8. **Morning Brief** synthesizes everything → user starts day with context

The system is designed to be self-maintaining. Once bootstrapped, the cron suite keeps everything current without manual intervention. The agent's memory grows, consolidates, and evolves autonomously.
