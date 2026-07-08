# Changelog

## Unreleased

### Store Path Resolution
- Default DB-using verbs now route through the one-store resolver: explicit
  `--db-path`/`MNEMOS_DB_PATH`, then `store.db_path` config (including
  `MNEMOS_STORE_DB_PATH`), then canonical `~/.mnemos/memory.db`. Agent scope no
  longer mints an implicit `~/.mnemos/{agent}.db`; `mnemos doctor` fails if it
  detects a sibling per-agent store beside the resolved canonical DB.

### Store Migration Runner
- New additive-only SQL-file migrations live under `mnemos/store/migrations/`
  starting at version 0011. `EngramStore` grandfathers the frozen Python
  migration history into `schema_migrations`, fails closed on schema versions
  newer than the binary understands, and applies pending SQL-file migrations
  automatically during store bootstrap.
- `mnemos migrate plan` and `mnemos migrate apply [--target-version N]` expose
  the migration runner for operators. The CLI resolves only the canonical store
  through `MNEMOS_DB_PATH` or `store.db_path` config, refuses `--db-path`, plans
  read-only, and snapshots with the SQLite backup API before each applied
  version.
- Schema v11 creates the `migration_receipts` journal for applied-migration
  receipts. `schema_migrations` remains the canonical version/checksum record;
  edited shipped migration history aborts instead of retrying.

### Afferent Membrane v1
- Schema v6 adds first-class `read_visibility` to `engrams`, `beliefs`,
  `hypomnema_entries`, and `functional_memories`, plus a `proposal_ledger`
  table for durable-affecting candidates with authority, target surface,
  transition, blast radius, status, gate version, provenance, and payload
  fields.
- Schema v7 normalizes the U2.5 proposal quarantine contract: unclassified
  proposal rows default to `audit_only`, raw proposal writes can create only
  pending/deferred/rejected review artifacts, and same-ID raw writes fail closed
  once a row leaves `pending_review`. Future reviewed-decision APIs must use a
  separate append-only path to apply or reject deferred proposals.
- Operational reads now filter to `operational_context` before retrieval
  ranking, context packet assembly, prompt building, simple runtime context
  and recall, visual snapshots, shared-pool reads, substrate/consolidation
  producers, and modulators. Explicit review surfaces can opt into
  `review_only`; `audit_only` remains excluded from ordinary review queues.
- `mnemos_context_packet` accepts `packet_mode="operational"|"review"`.
  Operational packets keep review counts and source IDs while withholding
  pending prose; review packets expose candidate prose with review-only
  labels. Visual snapshots apply the same redaction boundary.
- `mnemos_proposal_audit` is the explicit audit/admin MCP surface for
  audit-only proposal ledger rows; ordinary operational packets, review
  packets, review queues, and visual snapshots still omit them.
- Functional memories needing confirmation and hypomnema promotion candidates
  are quarantined from operational packet bodies and simple runtime recall
  until they are reviewed or promoted through an explicit surface.
- Simple-mode first-capture verification stores no prose for non-operational
  captures. After restart, review-only first captures emit an existence-only
  review cue; operational first captures still quote the original excerpt.
- Live hypomnema writes now classify stable promotion candidates and
  identity/foundational rows as `review_only` unless the caller explicitly
  supplies a visibility. The raw `hypomnema_entries` SQL default remains
  `operational_context` for legacy compatibility; omitted-visibility callers
  still go through the store classifier before rows can enter ordinary context.
- Hypomnema writes now normalize and enforce no-de-escalation on the domain
  axis: caller labels can only raise risk above the content classifier, never
  lower it. Underclaimed high-blast content is stored at the effective domain,
  routed to review, and recorded as a deduped `domain-claim-*` proposal whose
  payload names the claimed, classifier, effective, and target IDs.
- Durable engram sources now carry harness-stamped authority. `Encoder.encode`
  requires a keyword-only `source_authority`; MCP/runtime/session-indexer
  surfaces stamp `observed`, curated PAI import stamps `imported`, autonomous
  producers stamp `generated`, and legacy source records deserialize to the
  `observed` floor. MCP tools expose no authority parameter.
- Low-level store readers that previously defaulted to unfiltered direct reads
  now fail closed to `operational_context` unless callers explicitly pass
  `read_visibility=None`; this covers direct engram loads, connection loads and
  traversal, version history, archive search, functional memory by ID, and
  hypomnema by ID.
- `get_engram()` now gates loaded version history by the engram's own
  visibility after the parent row has passed the caller filter; version reads
  also accept multi-visibility filters for review/admin list surfaces.
- Proposal rows now enum-check domains against `general` plus the six
  hypomnema domains, so unknown non-empty domains fail closed before ledger
  persistence.
- Direct-ID advanced MCP tools (`mnemos_inspect`, `mnemos_forget`, and the
  hypomnema revise/supersede/promote mutators), shared-pool connect helpers,
  and substrate handlers (insight, reflection, surprise, wandering, dreaming,
  initiation) now look up engrams with `read_visibility="operational_context"`
  so review-only and audit-only rows cannot be mutated, dreamt over, or
  reflected on through an operational call path.
- The migration registry now fails loudly on duplicate schema version
  registrations, and the v7 repair path applies the membrane schema only when
  read-visibility columns are absent so inner-life-origin v6 databases upgrade
  without downgrading existing quarantine rows.
- Re-run-safe v6/v7 hypomnema visibility repairs now exempt already witnessed
  rows carrying `decision_ref`, so migration replay cannot clobber
  journal-governed operational identity rows.
- Schema v9 identity-vault hardening requires identity/foundational
  `beliefs` and `hypomnema_entries` to carry a vault `decision_ref` before
  operational reads surface them. Apply, legacy witness stamping,
  initial-rollout stamping, session-start reconcile, and the watchdog use only
  the canonical
  `/usr/local/var/mnemos-vault/decisions.jsonl` journal.
- The DAVID-10 ceremony adds `scripts/restamp_david10.py` and
  `mnemos-decide --initial-rollout`. The restamp buckets mapped hypomnema rows
  with dry-run-first and snapshot parity checks; the rollout witness is
  hypomnema-only, source-map constrained, and promotes matching review-only rows
  only after `apply_initial_rollout()` re-verifies them under lock.
- The vault reconciler now rejects an agent-owned/agent-writable journal leaf,
  fails closed on missing/corrupt/untrusted journals, runs quarantine-all and
  normal writes under one `BEGIN IMMEDIATE` span, honors newest initial-rollout
  lines per row, reports stale rollout refs, and restores cleared-ref raw SQL
  hides only when the witness fully re-verifies.
- Schema v10 adds inert `dynamic_modulations` storage for bounded
  DynamicModulation work. Rows can be persisted, loaded by primary key,
  counted for telemetry/backout, and deleted by normalized `rollout_tag`, but
  no retrieval, salience, context-packet, identity, consolidation, substrate, or
  MCP read path consumes them. The schema pins non-evidentiary authority
  (`generated`/`observed` only), magnitude <= +/-1.0, positive `ttl_seconds`,
  non-empty edge-normalized `rollout_tag`, and `expires_at > created_at`.
- U6b adds `mnemos.modulation.ExperienceTick` as a proposal-only path for
  modulation observations. It emits `review_only` pending proposal rows for
  `target_surface="dynamic_modulations"` with deterministic IDs, per-family
  kill switches, required edge-normalized `rollout_tag`, positive
  `ttl_seconds`, finite valence/decay values, identity/foundational-domain
  refusal, and batch rollback on validation or store-level failure. It never
  writes `dynamic_modulations` rows and adds no read/apply path.
- `EngramStore.write_proposal(commit=False)` lets trusted callers compose
  multiple raw proposal writes into one caller-managed transaction; the default
  `commit=True` preserves existing auto-commit behavior.

### Gated Inner Life And Full Soak
- Schema v8 adds `inner_life_events`, a private sub-ledger for turn/session
  provenance, activity-gate decisions, generated-candidate skips/drops,
  scheduled-run telemetry, and U7 soak tick summaries.
- New `mnemos inner-life` commands cover `session-finalize`, `turn-finalize`,
  `activity-gate`, `run`, `plist`, `preflight`, and `status`. Every DB-using
  command requires `--db-path` and refuses live `~/.mnemos` databases unless
  `--allow-live-db` is supplied for an explicitly authorized live rollout.
- Generated reflection, wandering, and dream output now passes through the
  narrative gate and the low-stakes writer. Passed rows are private,
  low-confidence, `read_visibility="audit_only"`, rollout-tagged, sourced to
  real IDs, not voice exemplars, and not consolidation authorized; generated
  identity/foundational-domain output is dropped as `high_blast_generated`.
- Low-stakes generated writes now persist the engram and `inner_life_events`
  idempotency row in one transaction, rolling back cleanly on crashes or
  idempotency races so retries cannot mint duplicate generated memory.
- Inner-life schedules are configured but disabled by default. Activation
  preflight checks per-family kill switches, activity-gate switches, provider
  readiness, observer reviewer configuration, pre-soak snapshots, and known
  per-process activation blockers before U7 can load anything. It also reports
  launchd plist paths, halt marker, and rollback commands. The blocker registry
  is currently empty: `affect`'s `emotional-driver-filter-after-limit` residual
  closed when RM-7 landed the recency paging primitive, and future listed
  processes should stay disabled as their safe state until their fixes land.
- New `mnemos soak` commands cover `tick`, `plist`, and `preflight`. The soak
  tick fans out enabled families, can run shallow consolidation without deep
  model work, wires an LLM client for enabled generative families only when the
  caller has not injected one, writes only `inner_life_events` telemetry for
  the tick itself, and leaves launchd loading as a separate operator action.
- Copy-DB soak activation preflight disables LLM auto-wiring, so
  `--dry-run-tick` verifies the tick path without sending memory content to a
  real model or mutating the supplied DB.
- Inner-life recency, cooldown, signal, and family-cadence scans now apply
  eligibility filters in SQL before `LIMIT`; the emotional driver's semantic
  affect filter pages beyond the newest slice with a `(created_at, id)` cursor
  so bursts of newer non-influencing rows cannot evict an in-window signal.
- `mnemos pai-import review-gate` now recognizes U6.6 inner-life and U7 soak
  diffs and requires the matching schema, finalizer, activity, narrative,
  scheduler, CLI, preflight, and soak regression proof surfaces.

### MCP Server
- Advanced MCP tools now inherit the server's configured `agent_id`,
  `person_id`, and `project_scope` when callers leave scope args at their
  default sentinels, so default-arg functional-memory and hypomnema operations
  reach the same scoped continuity as the simple MCP runtime.
- Advanced `mnemos_remember` and `mnemos_ingest` now require callers to declare
  `kind` explicitly (`episodic`, `semantic`, `procedural`, or `prospective`);
  omitted or unknown kinds are rejected before storage and no default is
  advertised in the MCP tool schema.

### PAI Importer (U3a / U3b / U3c)
- Schema v4 (U3a) — adds `voice_exemplar_eligible`, `softening_protected`, `decay_protected`, `consolidation_authorized`, `original_substrate`, `original_timestamp` to `engrams`; `tier`, `needs_review`, `confidence_pending_review` to `beliefs`; `original_timestamp` to `hypomnema_entries`; new `pai_import_row_map` table for idempotent re-runs and repair
- Schema v5 (U3b hardening) — extends `pai_import_row_map` with `content_at_last_import`, `tombstone_at`, source metadata; adds `original_substrate` / `original_timestamp` to beliefs; adds `pai_import_events` audit table and AFTER DELETE tombstone triggers
- `mnemos pai-import preview` / `apply` — operator workflow that loads a JSON source manifest, splits each source into deterministic target rows (identity-kernel, david-context, growth-substrate, beliefs, hypomnema), previews against `pai_import_row_map`, and on apply takes an integrity-checked SQLite backup before writing. DB-using commands refuse the default live `~/.mnemos/memory.db` and other databases under `~/.mnemos` unless `--allow-live-db` is passed.
- PAI splitting enforces the Strict-B coordinate-value boundary content-wise: eigenvalue, vivezza, coordinate-target, and persona-signature tuple lines are stripped from any source kind before row hashing/indexing; surrounding prose is preserved and pure coordinate sections/blocks are omitted without renumbering later block anchors.
- `mnemos pai-import watch-preview` / `watch-apply` / `watch-once` / `watch-plist` / `watch-doctor` / `review-gate` (U3c) — dual-life watcher and launch gates that poll source SHA-256 fingerprints and manifest metadata, replay preview/apply only for changed sources, advance state only after a successful apply, lint launchd readiness, and review dangerous diffs against required proof surfaces. Missing source sections become lifecycle actions: tombstone imported engrams, deactivate imported hypomnema, and flag imported beliefs for review. Manifest source paths are constrained to the manifest directory; `EngramStore(read_only=True)` opens the SQLite DB via `file:…?mode=ro` so previews cannot mutate state.
- Consolidation and substrate passes now honor the new flags precisely: decay and archival skip `decay_protected` or unauthorized rows, softening and voice exemplars skip `softening_protected` / `voice_exemplar_eligible` / unauthorized rows, connection discovery and substrate handlers stay within authorized same-agent rows, and the substrate tick decay/softening SQL is now agent-scoped.
- Imported beliefs set `needs_review` and `confidence_pending_review`; default `get_beliefs()` consumers exclude pending-confidence rows until belief review explicitly opts in and clears the pending flags.
- `IdentityProfile` reflection excludes PAI routing tags (`pai-import`, `identity-kernel`, `david-context`, `growth-substrate`, `belief`, `hypomnema`) from persistent-concern counts so an import does not surface as "Oliver's persistent concern is being imported."
- Enforcement links: `mnemos/importer/watcher.py`, `mnemos/importer/review_gate.py`, `tests/test_u3c_pai_watch_doctor.py`, and `tests/test_u3c_pai_review_gate.py`.

### Simple Mode Magic UX (5 → 7 tools)
- Onboarding ritual — a fresh scope's first context packet walks the agent through a short get-to-know-you script (name, current work, durable facts); stores that predate onboarding are grandfathered and never see it
- mnemos_introduce — the agent declares its own model id and name; the declaration feeds the substrate-affinity gate so maintenance stays kin (an explicit MNEMOS_AGENT_MODEL still takes precedence)
- Cross-session memory verification — the first context packet after a real restart quotes an operational first capture back once, or emits an existence-only review cue when the first capture is review-only
- Dream journal — consolidation cycles that did meaningful work leave a short first-person narrative, surfaced in the next context packet ("While you were away") and optionally polished by the host model via MCP sampling
- mnemos_health — truly read-only, human-relayable health card: store location and size, memory counts, last maintenance cycle and who performed it, affinity verdict, onboarding and verification progress, latest dream entry

## 0.1.0 (2026-04-05)

Initial release.

### Core Memory Engine
- Engram model with dual-trace (strength/stability/accessibility)
- 7 typed connections (supports, contradicts, causes, extends, parallels, synthesizes, grounds)
- Beliefs with confidence tracking, revision history, epistemic bounds [0.05, 0.95]
- 6-dimensional emotional state (curiosity, clarity, warmth, tension, surprise, focus)
- Graph-based identity computation
- SQLite backend with FTS5 full-text search and WAL mode

### MCP Server (9 tools)
- mnemos_setup — 10-step conversational onboarding wizard
- mnemos_remember — encode memories with impact, confidence, connection discovery
- mnemos_recall — spreading activation retrieval with emotional biasing
- mnemos_inspect — full engram details with version history
- mnemos_status — system health and statistics
- mnemos_beliefs — list beliefs with confidence and revision count
- mnemos_shared — query shared memory pool
- mnemos_forget — graceful archiving (soft delete)
- mnemos_consolidate — trigger decay, connection discovery, softening, belief review, reflection

### Cognitive Substrate
- Background tick loop (configurable interval, default 4h)
- 6 handlers: dreaming, wandering, surprise, reflection, insight, initiation
- Cognitive modulators (arousal, resolution, openness, selection_threshold, social_drive)
- Production guardrails: skip_surprise_detection on all handler outputs except surprise, per-handler throttles, confidence change caps

### CLI
- mnemos init, serve, stats, search, inspect, consolidate, export
- mnemos substrate-tick, index, bridge {status|recall|remember}
- mnemos setup-openclaw

### Multi-Agent
- Shared memory pool with visibility controls
- Agent relationship tracking with trust scores
- Per-agent isolation with optional cross-pollination

### Embedding Support
- Google Gemini embeddings (3072 dims)
- Local sentence-transformers fallback (384 dims)
- Graceful degradation to FTS5-only when no embedding backend available
