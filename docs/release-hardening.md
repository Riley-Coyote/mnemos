# Release Hardening

Use this checklist before publishing Mnemos or opening a release PR.

## Protocol Correctness

- Simple MCP mode exposes exactly:
  - `mnemos_context`
  - `mnemos_capture`
  - `mnemos_recall`
  - `mnemos_correct`
  - `mnemos_maintain`
  - `mnemos_introduce`
  - `mnemos_health`
- Advanced mode preserves the existing admin tools.
- Scope-taking advanced tools inherit configured agent/person/project scope
  when callers leave scope args at their default sentinels.
- Injected FastMCP context parameters are not exposed in public tool schemas.
- No MCP tool exposes `source_authority` or an authority override parameter.
- Sampling is optional and occurs only inside an active client request.
- Sampling failures, denials, or unsupported clients fall back cleanly.
- Tool annotations match local side effects.
- `mnemos_context_packet` defaults to `packet_mode="operational"` and never
  includes confirmation or promotion-candidate prose in operational prompt or
  JSON output.
- Explicit review surfaces (`packet_mode="review"`, `mnemos_review_queue`) can
  show review-only prose, while audit-only proposal rows remain excluded except
  through the deliberate `mnemos_proposal_audit` admin/audit surface.
- Inline visual snapshots show review counts/source IDs only, not pending prose.

## Install UX

- `mnemos doctor` works on a fresh machine with no provider key.
- `mnemos mcp install generic` prints a valid JSON snippet.
- `mnemos mcp install claude --write` safely merges the Claude Desktop config.
- `mnemos mcp install codex` prints a usable `codex mcp add` command.
- `mnemos serve` defaults to simple mode.
- `mnemos serve --mode advanced` exposes the admin surface.

## Package Readiness

- The distribution package is `mnemos-memory`.
- The CLI command remains `mnemos`.
- Wheel and sdist build successfully.
- Wheel contains:
  - `mnemos/simple_runtime.py`
  - `mnemos/simple_mcp.py`
  - `mnemos/importer/__init__.py`
  - `mnemos/importer/pai.py`
  - `mnemos/importer/operator.py`
  - `mnemos/importer/review_gate.py`
  - `mnemos/importer/watcher.py`
  - `mnemos/inner_life/__init__.py`
  - `mnemos/inner_life/activity_gate.py`
  - `mnemos/inner_life/emotional_driver.py`
  - `mnemos/inner_life/hypomnema_challenge.py`
  - `mnemos/inner_life/low_stakes.py`
  - `mnemos/inner_life/narrative_gate.py`
  - `mnemos/inner_life/observer_panel.py`
  - `mnemos/inner_life/preflight.py`
  - `mnemos/inner_life/scheduler.py`
  - `mnemos/inner_life/session_finalizer.py`
  - `mnemos/inner_life/turn_finalizer.py`
  - `mnemos/soak/__init__.py`
  - `mnemos/soak/preflight.py`
  - `mnemos/soak/tick.py`
  - `templates/SOUL.md`
  - `templates/IDENTITY.md`
- Package metadata passes `twine check`.

## Privacy and Safety

- Baseline simple mode does not require network access.
- Baseline simple mode does not require OpenRouter, Anthropic, OpenAI, or OpenClaw.
- Dedicated providers are used only when explicitly configured.
- Scope isolation is tested across multiple agents.
- Operational retrieval, prompt building, simple runtime context/recall,
  substrate producers, consolidation producers, shared-pool reads, and
  modulators filter to `operational_context` before scoring, ranking, or
  generation.
- Low-level direct readers (`get_engram`, graph connections/traversal,
  archived search, functional memory, and hypomnema by ID) default to
  operational visibility; unfiltered access is explicit `read_visibility=None`
  and reserved for audit/admin code paths.
- Every durable engram write stamps source authority from the trusted channel:
  MCP/runtime/session-indexer surfaces use `observed`, curated PAI import uses
  `imported`, autonomous producers use `generated`, and legacy source records
  deserialize to the `observed` floor. Payload text must never mint
  `user_stated` or `imported`.
- Direct-ID advanced tools (`mnemos_inspect`, `mnemos_forget`, hypomnema
  revise/supersede/promote) and substrate handlers
  (insight/reflection/surprise/wandering/dreaming/initiation) refuse to mutate
  or reflect over non-operational rows: a review-only or audit-only ID returns
  a "not found" response instead of leaking review prose into operating
  surfaces.
- Schema v6/v7 migrations default existing pending beliefs, confirmation-needed
  functional memories, and hypomnema promotion candidates to review visibility.
- Fresh live hypomnema writes that already meet stable promotion criteria, or
  that carry identity/foundational scope, default to `review_only`; the raw
  hypomnema SQL default remains `operational_context` for legacy compatibility,
  with omitted-visibility callers still routed through the store classifier
  before ordinary use.
- Hypomnema write domains are normalized and cannot de-escalate below content
  classification. Underclaimed high-blast writes are routed to review, produce
  one legible scoped domain-claim proposal, and duplicate content/whitespace
  variants do not flood the review queue.
- Simple-mode first-capture verification records review-only/audit-only
  captures as existence-only metadata and never stores or re-quotes their
  prose in the operational restart proof.
- Schema v8 adds `inner_life_events` as the private sub-ledger for U6.6/U7
  provenance, gate decisions, skips/drops, scheduled-run telemetry, and soak
  tick summaries.
- Generated inner-life reflection, wandering, and dream rows pass the
  narrative gate and are written only as private, low-confidence,
  `audit_only` low-stakes engrams; generated identity/foundational-domain
  output is dropped before persistence.
- Low-stakes generated engrams and their idempotency ledger rows are committed
  in one transaction, including the concurrent-idempotency race path, so release
  candidates cannot mint duplicate generated memory after partial failures.
- Correction/forget behavior is documented.

## Gated Inner-Life And Soak

- `mnemos inner-life` and `mnemos soak` DB-using commands require
  `--db-path` and refuse live `~/.mnemos` databases unless `--allow-live-db`
  is supplied for an explicitly authorized live rollout.
- Default config keeps global schedule switches, per-family schedule switches,
  `soak.tick.enabled`, and soak families disabled. Preflight reports missing
  or disabled kill switches.
- `inner-life preflight` blocks activation when the representative DB is
  missing, schedules are disabled, activity gates are disabled, the pre-soak
  snapshot is missing, or required provider/reviewer readiness is absent. It
  reports rollback and launchd surfaces for operator review.
- `inner-life preflight` also blocks any schedule-enabled process with a known
  activation residual. `affect` is blocked by
  `known_open_issue:affect:emotional-driver-filter-after-limit` until RM-7; a
  disabled `affect` schedule/activity switch is the expected safe state.
- `inner-life plist` and `soak plist` write plist files atomically, bake
  repo-local `python -m mnemos.cli ...` arguments, and never call `launchctl`.
- `soak preflight` composes watcher doctor state, soak plist lint, launchd
  not-loaded state, provider/snapshot/family readiness, and optional copy-DB
  tick dry run. It may write the requested JSON artifact but must not mutate
  the supplied DB or construct a real LLM client during the dry run.
- Recency-sensitive inner-life scans filter eligibility in SQL before `LIMIT`
  for activity signals, cooldowns, and soak family cadence checks.
- `inner-life status` and soak tick summaries expose generated-memory,
  belief-write, identity-patch, and shared-pool counters; U6.6/U7 validation
  should keep belief, identity, and shared-pool counters at zero.

## PAI Importer And Dual-Life Watcher

- `mnemos pai-import preview` and `watch-preview` open the SQLite DB read-only
  (`EngramStore(read_only=True)` → `file:…?mode=ro`) and never mutate state.
- `mnemos pai-import apply` and `watch-apply` take an integrity-checked
  SQLite backup before any write, into the configured `--backup-dir` (or
  `pai-import-backups/` beside the DB by default). `--backup-keep N` prunes
  older matching PAI backups after each successful backup.
- `mnemos pai-import watch-doctor` is the Step 3 launch gate before launchd
  activation. It previews the real manifest read-only, fingerprints the
  representative DB across copy-based apply probes, copies DB/WAL/SHM before
  applying, verifies backup `PRAGMA integrity_check`, performs a backup
  restore/open drill, lints launchd plist paths/env/logs/retention, runs static
  negative lifecycle checks, and refuses live `~/.mnemos` DB paths unless
  explicitly allowed.
- `mnemos pai-import review-gate` is the Step 3 diff gate. It reviews changed
  files against `docs/u3c-step3-launch-intent.md` and fails when dangerous
  source changes lack matching proof surfaces, when broad lifecycle deletes are
  introduced, when the launch taxonomy disappears, or when release packaging
  omits a launch-critical module.
  Enforcement lives in `mnemos.importer.review_gate.run_pai_diff_review_gate`
  with `tests/test_u3c_pai_review_gate.py` and
  `tests/test_u3c_pai_review_gate_attacks.py`.
- Every DB-using PAI import subcommand refuses the default live database
  (`~/.mnemos/memory.db`) and other databases under `~/.mnemos` unless the
  operator passes `--allow-live-db`. The guard uses inode equality so
  case-insensitive paths like `~/.MNEMOS/memory.db` cannot bypass it.
- Manifest source paths must stay inside the manifest directory (path
  resolution + `relative_to` guard); absolute paths are allowed only inside
  that directory, and `..`-escaping paths are rejected at load.
- Source splitting enforces the Strict-B coordinate guard content-wise:
  eigenvalue, vivezza, coordinate-target, and persona-signature tuple lines are
  stripped from any source kind before row hashing/indexing. Surrounding prose
  is preserved, and pure coordinate sections/blocks are omitted without
  renumbering later blank-line block anchors.
- `watch-once` advances state only after a successful apply. Preview mode
  leaves state untouched so an operator can inspect a change and still apply
  it later.
- Missing source files or removed source sections are handled as explicit
  watcher lifecycle actions: imported engrams are tombstoned, imported
  hypomnema entries are deactivated without a successor, and imported beliefs
  are flagged `needs_review` / `confidence_pending_review` without changing
  their content or confidence.
- `watch-plist` writes the launchd plist atomically (temp file + `rename`),
  resolves the configured Python interpreter on `PATH`, asserts the
  interpreter can `import mnemos.cli` against the repo, and bakes absolute
  paths for manifest, DB, state, artifact, and backup directories into the
  generated `ProgramArguments`. Loading the plist with `launchctl` remains
  an explicit operator action; `watch-plist` only writes the file.
- Default `get_beliefs()` consumers exclude `confidence_pending_review` rows;
  belief review is the explicit opt-in path that can clear pending review,
  including no-op acceptance, and promote approved rows back to
  `operational_context` read visibility.
- Error messages that mention recovery actions are probed against actual
  behavior. Recovery steps that would destroy operator hand-edits or in-flight
  state must be removed from user-facing text, even when the underlying code
  path is harmless.
- `IdentityProfile` excludes PAI routing tags (`pai-import`,
  `identity-kernel`, `david-context`, `growth-substrate`, `belief`,
  `hypomnema`) from persistent-concern counts so an import does not surface
  as the agent's persistent concern.

## Verification Commands

```bash
uv run --extra dev --extra mcp pytest -q
uv run --extra mcp python -m py_compile mnemos/simple_runtime.py mnemos/simple_mcp.py mnemos/mcp_server.py mnemos/cli.py mnemos/importer/__init__.py mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/review_gate.py mnemos/importer/watcher.py mnemos/inner_life/*.py mnemos/soak/*.py
uv build
uvx twine check dist/*
git diff --check
```

## Dogfood Continuity

Before shipping a meaningful change, use Mnemos itself to capture:

- what changed
- why the product decision matters
- remaining release risks
- client-specific install gotchas

Then verify recall against those notes.
