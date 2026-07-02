# Gated Inner Life

U6.6 adds a private, low-stakes inner-life layer for pre-soak testing. Schema
v8 adds the `inner_life_events` ledger. This is code and representative-DB
tooling only. Live `~/.mnemos` writes, launchd jobs, and autonomous scheduled
writes still require explicit David authorization.

## Boundaries

- Session and turn finalizers write private provenance rows below memory.
- Activity gates decide whether a process has enough recent grounded activity
  before LLM work.
- Generated reflection, wandering, and dream output must pass the narrative
  gate before persistence.
- Passed generated records are written only through the low-stakes writer.
- Generated records are private, low confidence, low stability/accessibility,
  rollout-tagged, source-grounded, not voice exemplars, and not consolidation
  authorized.
- Generated records carry `read_visibility="audit_only"` and are excluded from
  operational retrieval. Dedup/throttle checks for prior generated rows use
  that private class; seed-selection queries stay on `operational_context`.
- The low-stakes writer persists a generated engram and its idempotency ledger
  row in one transaction. A crash or idempotency race must leave no orphaned or
  duplicate generated memory.
- Recency, cooldown, and cadence gates push eligibility predicates into SQL
  before applying `LIMIT`, so unrelated newer ledger rows cannot evict the row
  a gate needs.
- `affect` is not fully schedulable while the
  `emotional-driver-filter-after-limit` residual remains open. Preflight
  reports this as `known_open_issue:affect:emotional-driver-filter-after-limit`
  when `affect` is schedule-enabled, and a disabled `affect` schedule or
  activity switch is not treated as a misconfiguration until RM-7 lands.
- The layer never writes beliefs, identity patches, hypomnema promotions, or
  shared-pool publications.

## Components

| Component | Role |
| --- | --- |
| `inner_life_events` | Private ledger for turn/session provenance, gate decisions, skips, drops, and generated-write telemetry. |
| `activity_gate.py` | Zero-LLM process preflight with SQL-filtered cooldowns and signal counts. |
| `hypomnema_challenge.py` | Revises or retires stale continuity without deleting audit history. |
| `observer_panel.py` | Writes bounded observer-source findings with reviewer provenance. |
| `emotional_driver.py` | Computes private affect weather from real events; does not journal affect prose. Full scheduling is blocked until RM-7 closes the post-limit semantic-filter residual. |
| `narrative_gate.py` | Drops null, ungrounded, manufactured, rejected, or metrics-only generated candidates. |
| `low_stakes.py` | Writes private low-confidence audit-only generated engrams atomically with their idempotency ledger rows, rollout tags, and source IDs. |
| `scheduler.py` | Runs one process behind the activity gate and writes launchd plists without loading them. |
| `preflight.py` | Reports DB, schedule, provider, snapshot, launchd, kill-switch, and rollback readiness. |
| `soak/tick.py` | Runs the U7 tick over enabled soak families, wiring an LLM client only for generative families when the caller has not injected one, and records tick telemetry below memory. |
| `soak/preflight.py` | Builds the U7 activation artifact without mutating the supplied DB, loading launchd, or constructing a real LLM client during the copy-DB dry run. |
| `consolidation/reflection.py` | Gates generated reflection thoughts while preserving graph-derived `IdentityProfile`. |
| `substrate/handlers/wandering.py` | Keeps authorized source filtering and writes passed wandering only as low-stakes memory. |
| `substrate/handlers/dreaming.py` | Recombines real engrams only; metrics-only dream output is dropped. |

## CLI

All `inner-life` and `soak` DB-using commands require `--db-path`. They refuse
`~/.mnemos` databases unless `--allow-live-db` is supplied, and live use of that
override is reserved for explicit David authorization.

During branch validation, use the repo-local command:

```bash
uv run --extra dev mnemos inner-life ...
```

Do not rely on a globally installed `mnemos`; it can be stale relative to this
branch. Generated launchd plists pin this repository's `.venv/bin/python3` and
invoke `-m mnemos.cli inner-life run` or `-m mnemos.cli soak tick`.

```bash
mnemos inner-life session-finalize \
  --db-path /tmp/mnemos-copy.db \
  --transcript /tmp/session.jsonl \
  --session-id session-1 \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos inner-life turn-finalize \
  --db-path /tmp/mnemos-copy.db \
  --session-id session-1 \
  --user-text "..." \
  --assistant-text "..." \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos inner-life activity-gate \
  --db-path /tmp/mnemos-copy.db \
  --process reflect \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos inner-life run \
  --db-path /tmp/mnemos-copy.db \
  --process reflect \
  --agent-id oliver \
  --person-id david \
  --project-scope pai \
  --rollout-tag u6.6
```

```bash
mnemos inner-life plist \
  --db-path /tmp/mnemos-copy.db \
  --process reflect \
  --plist /tmp/com.davidef.mnemos.innerlife.reflect.plist \
  --interval-seconds 3600 \
  --artifact-dir /tmp/mnemos-inner-life \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos inner-life preflight \
  --db-path /tmp/mnemos-copy.db \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos soak preflight \
  --db-path /tmp/mnemos-copy.db \
  --agent-id oliver \
  --person-id david \
  --project-scope pai \
  --soak-plist /tmp/com.davidef.mnemos.soak.tick.plist \
  --watch-manifest /tmp/pai-watch/manifest.json \
  --watch-state /tmp/pai-watch/state.json \
  --watch-artifact-dir /tmp/pai-watch/artifacts \
  --watch-backup-dir /tmp/pai-watch/backups \
  --watch-backup-keep 5 \
  --watch-plist /tmp/com.davidef.mnemos.duallife.plist \
  --dry-run-tick \
  --artifact /tmp/u7-soak-preflight.json
```

```bash
mnemos soak plist \
  --db-path /tmp/mnemos-copy.db \
  --plist /tmp/com.davidef.mnemos.soak.tick.plist \
  --interval-seconds 900 \
  --artifact-dir /tmp/mnemos-soak \
  --agent-id oliver \
  --person-id david \
  --project-scope pai
```

```bash
mnemos soak tick \
  --db-path /tmp/mnemos-copy.db \
  --agent-id oliver \
  --person-id david \
  --project-scope pai \
  --rollout-tag u7-soak
```

```bash
mnemos inner-life status \
  --db-path /tmp/mnemos-copy.db \
  --agent-id oliver \
  --person-id david \
  --project-scope pai \
  --rollout-tag u6.6
```

## Rollback Inspection

Use `inner-life status` to inspect rows by process and gate decision. The
important counters are generated memory writes, belief writes, identity
patches, and shared-pool writes. For U6.6, belief writes, identity patches, and
shared-pool writes should remain zero.

Full scheduled activation remains blocked by default. The config carries a
global `inner_life.schedules.enabled` switch plus per-family schedule switches
for `challenge`, `observe`, `affect`, `reflect`, `wander`, and `dream`. The
activity gate also carries per-family `enabled` switches. `inner-life preflight`
reports missing or disabled switches before U7 can load schedules. A process
with a known activation blocker is the exception: disabling that process is the
safe state, not a readiness failure.

When schedules are enabled, preflight also blocks on:

- missing pre-soak DB snapshot path or file;
- unavailable LLM provider when provider-backed processes are required;
- missing observer reviewer count when observer review is required;
- missing per-family kill switches;
- enabled processes with known open activation blockers. Today that means
  `affect` must stay unscheduled until RM-7 moves its semantic filter before,
  or pages beyond, the recency limit.

It also reports the launchd artifact directory, plist directory, halt marker,
per-process plist path, and rollback commands so U7 can review the exact
activation and backout surface before anything is loaded.

`soak preflight` is the U7 activation artifact. It composes `watch-doctor`,
soak tick plist lint, launchd not-loaded status, provider/snapshot/family
readiness, and an optional copy-DB tick dry run. The dry run disables automatic
LLM-client construction even when generative families are enabled, so preflight
cannot send memory content to a model before activation. It writes a JSON
artifact when `--artifact` is provided, but it does not call `launchctl` and
does not mutate the supplied DB.

`inner-life plist` and `soak plist` only write plist files. They do not call
`launchctl`, do not bootstrap schedules, and print `Loaded: false`. U7 live
launch remains a DAVID-AUTH gate.

Generated memory writes should be:

- tagged `u6.6`, `generated`, `low-stakes`, and `rollout:<tag>`;
- process-tagged as `dream`, `reflection`, or `wandering` work; observer
  findings are ledger-only unless a future caller explicitly routes an observer
  candidate through the low-stakes writer;
- `read_visibility=audit_only`;
- `visibility=private`;
- `voice_exemplar_eligible=false`;
- `consolidation_authorized=false`;
- traceable to source session, turn, engram, or hypomnema IDs.

For scheduled `wander` and `dream`, the scheduler passes the requested
`person_id`, `project_scope`, and `rollout_tag` through the substrate event
payload, so gate rows and low-stakes records remain in the rollback scope.

Backout order for U7/U8 remains disable-first:

1. Disable config and unload any launchd jobs.
2. Verify scheduled writes stop.
3. Inspect rollout-tagged rows and generated records.
4. Restore the pre-soak DB snapshot only if generated low-stakes records
   contaminate retrieval or identity behavior.

## Validation

The focused U6.6/U7 suite covers:

- private/idempotent event ledger migration;
- schema v8 migration from empty, v5, and inner-life-origin v6 databases;
- representative live-copy schema migration;
- activity gate run/skip/cooldown behavior;
- SQL-filtered recency, cooldown, signal, and family-cadence scans;
- challenge, observer, and affect safety boundaries;
- `affect` activation blocking on the known emotional-driver recency residual;
- narrative gate null, source, manufactured, introspection, and metrics-only
  drops;
- scheduled runner activity-gate skip/run behavior and launchd plist static
  readiness;
- soak tick disabled, shallow-consolidation, inner-life fanout, plist,
  preflight blocked, and preflight ready behavior;
- soak activation preflight blocking/ready behavior and copy-DB tick proof;
- low-stakes writer privacy, transactional idempotency, race handling, and
  non-promotion invariants;
- gated reflection, wandering, and dream persistence;
- CLI live DB refusal, activity-gate preflight, scheduled run, plist writing,
  activation preflight, and telemetry status;
- existing dream journal separation.

## Enforcement Links

Enforcement lives in the implementation and tests, not in this document:

- `mnemos/inner_life/narrative_gate.py` with `tests/test_narrative_gate.py`;
- `mnemos/inner_life/low_stakes.py` with `tests/test_low_stakes_writer.py`;
- `mnemos/consolidation/reflection.py` with `tests/test_gated_reflection.py`;
- `mnemos/substrate/handlers/wandering.py` with `tests/test_gated_wandering.py`;
- `mnemos/substrate/handlers/dreaming.py` with `tests/test_gated_dreaming.py`;
- `mnemos/cli.py` with `tests/test_cli_simple.py`;
- `mnemos/inner_life/preflight.py` with `tests/test_inner_life_preflight.py`;
- `mnemos/inner_life/scheduler.py` with `tests/test_inner_life_scheduler.py`;
- `mnemos/soak/preflight.py` and `mnemos/soak/tick.py` with
  `tests/test_soak_tick.py`;
- `mnemos/store/migrations.py` and `mnemos/store/sqlite_store.py` with
  `tests/test_inner_life_ledger.py`, `tests/test_u3a_schema_migrations.py`,
  and `tests/test_t2_5_safety_gate_repairs.py`.
