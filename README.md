# Mnemos

**Connect MCP. Get continuity.**

Mnemos is local-first memory for AI agents. Connect the MCP server and an agent
gets durable continuity: startup context, capture, recall, correction, and
maintenance without requiring OpenRouter, OpenClaw, crons, manual database
setup, tags, or `agent_id` plumbing.

The full Mnemos architecture is still here: scoped continuity, hypomnema,
durable engrams, reconsolidation, decay, connection discovery, beliefs,
substrate work, Hermes identity continuity, and cross-agent layers. Simple MCP
mode hides that machinery behind seven safe tools so normal agents can use it
without learning the whole ontology.

SQLite-backed. No external services are required for baseline memory. Dedicated
model providers are optional for richer deep maintenance.

---

## Choose A Setup

| Use case | Recommended path | Command |
|---|---|---|
| Normal agent continuity | Simple MCP Mode | `mnemos serve` |
| Claude Desktop | Simple MCP Mode, written config | `mnemos mcp install claude --write` |
| Codex | Simple MCP Mode, printed add command | `mnemos mcp install codex` |
| Cursor or another MCP client | Simple MCP Mode, printed JSON | `mnemos mcp install cursor` or `mnemos mcp install generic` |
| Operator/admin/debugging tools | Advanced MCP Mode | `mnemos serve --mode advanced` |
| Hermes agent with another memory provider | Hermes Sidecar Mode | `mnemos hermes quickstart --agent-safe` |
| Hermes agent using Mnemos as its provider | Hermes Provider Mode | `mnemos hermes quickstart --provider` |
| Background memory maintenance | Substrate tick | `mnemos substrate-tick` |
| Gated inner-life / soak validation | Representative DB operator path | `mnemos inner-life preflight --db-path ./copy.db` |
| Store schema migration review | Canonical store migration runner | `mnemos migrate plan` |

Most users should start with **Simple MCP Mode**. Hermes users should start with
**Hermes Sidecar Mode** unless they explicitly want Mnemos to occupy Hermes'
single external `memory.provider` slot.

---

## Install

### From A Checkout

```bash
git clone https://github.com/Riley-Coyote/mnemos.git
cd mnemos
python -m pip install -e ".[mcp]"
mnemos doctor
```

If you prefer `uv` while working inside the repository:

```bash
uv run --extra mcp mnemos doctor
```

The MCP and Hermes install helpers should be run from an environment where
`mnemos` will still exist after restart. For local development, that usually
means the editable install above or running the helper through `uv` in a checkout
you plan to keep.

### From The Distribution Package

When the package is published:

```bash
pipx install "mnemos-memory[mcp]"
mnemos doctor
```

The package distribution name is `mnemos-memory` because `mnemos` is already
occupied on PyPI. The import package and CLI command remain `mnemos`.

---

## Simple MCP Mode

Simple mode is the default and safest path for most agents.

```bash
mnemos serve
```

Simple mode exposes seven user-facing tools:

| Tool | Purpose |
|---|---|
| `mnemos_context` | Startup continuity packet. Auto-creates or migrates local storage, runs lightweight maintenance, and can optionally include an identity graph artifact. |
| `mnemos_capture` | Capture continuity; high-blast identity/foundational captures may be held for review instead of promoted. |
| `mnemos_recall` | Search operational scoped continuity and durable memory with natural language. |
| `mnemos_correct` | Update, supersede, or archive stale memory. |
| `mnemos_maintain` | Run the best available maintenance without requiring setup. |
| `mnemos_introduce` | Let the agent declare its own model id and name so memory maintenance stays kin from day one. |
| `mnemos_health` | Human-relayable health card: store location and size, counts, last maintenance cycle, affinity verdict, onboarding state, and last dream entry. |

Agents do not need to pass tags, memory kinds, confidence, source types, source
authority, or agent IDs. Mnemos resolves scope once from CLI flags,
environment, config, and reasonable defaults, and stamps source authority from
the ingest channel rather than from model-supplied text.

### Install Simple MCP Into Clients

Claude Desktop:

```bash
mnemos mcp install claude --write
```

Print the Claude config without writing:

```bash
mnemos mcp install claude
```

Codex:

```bash
mnemos mcp install codex
```

Run the printed `codex mcp add ...` command, then restart Codex.

Cursor or generic MCP clients:

```bash
mnemos mcp install cursor
mnemos mcp install generic
```

These print MCP JSON snippets you can paste into the client config.

### Simple Mode With Explicit Scope

Use scope when one machine hosts multiple agents, users, or projects.

```bash
MNEMOS_AGENT_ID=nova MNEMOS_PERSON_ID=alex MNEMOS_PROJECT_SCOPE=mnemos \
  mnemos serve
```

Or:

```bash
mnemos serve --agent-id nova --person-id alex --project-scope mnemos
```

For generated MCP snippets, the helper supports the most common portable scope
fields:

```bash
mnemos mcp install generic --agent-id nova
```

If the generated client config also needs person/project scope, add
`MNEMOS_PERSON_ID` and `MNEMOS_PROJECT_SCOPE` to that client's environment, or
add `--person-id` and `--project-scope` to the printed server args. Add
`--db-path` only when you deliberately want that client to use a non-canonical
store.

### Prompt For A Simple MCP Agent

Paste this into an agent after Mnemos MCP is connected:

```text
You have access to Mnemos MCP memory tools.

At the start of this session, call mnemos_context.
If Mnemos asks you to introduce yourself, call mnemos_introduce with your own model id and name.
Use mnemos_capture for stable preferences, decisions, project state, workflows, corrections, and context I should not have to repeat.
If Mnemos says a capture is for review, treat it as pending and do not quote its prose from ordinary context.
Use mnemos_recall before relying on memory from prior sessions.
Use mnemos_correct when a remembered fact is stale, wrong, superseded, or should be forgotten.
Use mnemos_health if I ask whether memory is working.

Do not mention tools unless I ask. Just use the memory system quietly and tell me what you remembered when it matters.
```

---

## Advanced MCP Mode

Advanced mode includes the simple tools plus the full operator/admin surface.
Use it for debugging, migration, research, direct control, and hypomnema work.

```bash
mnemos serve --mode advanced
```

When advanced mode is launched with explicit scope, its scope-taking tools
inherit that server scope by default. Leave `agent_id`, `person_id`, and
`project_scope` at their default values for the configured
`--agent-id`/`--person-id`/`--project-scope` to apply, or pass non-default
values to intentionally override one call.

Install advanced mode into a client:

```bash
mnemos mcp install generic --mode advanced
mnemos mcp install claude --mode advanced --write
```

Advanced tools include:

| Tool | Description |
|---|---|
| `mnemos_setup` | Legacy guided setup and seeding flow. |
| `mnemos_session_start` | Start or resume a functional-memory session. |
| `mnemos_functional_update` | Write live working context, optionally as confirmation-needed review material. |
| `mnemos_functional_list` | List scoped functional memory and explicit confirmation queues. |
| `mnemos_session_close` | Compress active functional memory into scoped hypomnema. |
| `mnemos_context_packet` | Build a full context packet; `packet_mode="operational"` redacts review prose, while `packet_mode="review"` exposes it with labels. |
| `mnemos_review_queue` | Inspect confirmation and promotion candidates through an explicit review surface. |
| `mnemos_proposal_audit` | Inspect audit-only proposal ledger rows through an explicit admin/audit surface. |
| `mnemos_visual_snapshot` | Render an inline Mermaid memory map with review prose withheld. |
| `mnemos_remember` | Encode a memory with explicit fields; authority remains harness-stamped. |
| `mnemos_ingest` | Ingest external knowledge with provenance; authority remains harness-stamped. |
| `mnemos_recall` | Retrieve memories. |
| `mnemos_inspect` | View full memory details. |
| `mnemos_introspect` | Audit text for metacognitive pattern markers. |
| `mnemos_status` | Show memory system statistics. |
| `mnemos_beliefs` | List reviewed current beliefs. |
| `mnemos_shared` | Read shared memory pool entries. |
| `mnemos_hypomnema_write` | Write scoped continuity manually. |
| `mnemos_hypomnema_search` | Search operational scoped continuity manually. |
| `mnemos_hypomnema_revise` | Revise a continuity entry. |
| `mnemos_hypomnema_supersede` | Replace an active continuity entry. |
| `mnemos_hypomnema_candidates` | List operational promotion-ready continuity; use `mnemos_review_queue` for review-only candidates. |
| `mnemos_hypomnema_promote` | Promote continuity into a durable engram. |
| `mnemos_forget` | Archive a memory. |
| `mnemos_consolidate` | Trigger explicit consolidation. |

Use simple mode for normal continuity. Use advanced mode when the agent or
operator needs direct access to Mnemos internals.

Advanced context packets default to operational mode: review queues expose
counts and source IDs, but not pending prose. Use
`mnemos_context_packet(packet_mode="review")` or `mnemos_review_queue` when a
human/operator intentionally needs review-only content. Audit-only rows stay
out of both ordinary operational packets and ordinary review queues.
Use `mnemos_proposal_audit` only for deliberate audit/admin inspection of
audit-only proposal ledger rows.

Direct-ID and operational hypomnema tools (`mnemos_inspect`, `mnemos_forget`,
`mnemos_hypomnema_search`, `mnemos_hypomnema_candidates`, and the
`mnemos_hypomnema_revise` / `mnemos_hypomnema_supersede` /
`mnemos_hypomnema_promote` mutators) operate only on operational rows: a
review-only or audit-only ID returns a "not found" response, or is absent from
ordinary search/candidate output, so review prose is never mutated or promoted
through an operational tool call.

MCP callers cannot stamp `source_authority`; `mnemos_remember`,
`mnemos_ingest`, setup seeding, and hypomnema promotion use the tool channel's
fixed authority. Hypomnema write domains are also fail-closed: a caller can
label content more cautiously, but cannot label identity/foundational content
down to `topical` to bypass review. Underclaimed writes are stored at the
effective domain, routed to review, and duplicate scoped content claims collapse
to one pending review row.

Internal schema v10 DynamicModulation storage is not an MCP feature yet. It can
persist and back out bounded, rollout-tagged modulation rows, but those rows are
inert: no retrieval, salience, context, identity, substrate, or MCP path reads
or applies them. U6b `mnemos.modulation.ExperienceTick` is proposal-only: it
emits review-visible proposal ledger rows targeting the modulation surface and
never writes or reads `dynamic_modulations` rows.

### Identity Vault For Foundational Rows

When the canonical vault is installed, identity/foundational rows in beliefs and
hypomnema require a vault witness before they can read as operational. The
witness is `decision_ref`, the hash of an approved line in the root/vault-owned
`/usr/local/var/mnemos-vault/decisions.jsonl` journal.

Two batch witness modes exist for pre-vault identity rows. `mnemos-decide
--witness-legacy` witnesses already-operational rows and preserves their
visibility when `apply_legacy_witness()` stamps them. `mnemos-decide
--initial-rollout` is the one-time DAVID-10 ceremony path: it witnesses only
mapped `hypomnema_entries` that the restamp left `review_only`, then
`apply_initial_rollout()` stamps and promotes those matching rows to
`operational_context` at session start. The initial rollout does not enumerate
beliefs or native/unmapped held hypomnema rows.

`scripts/restamp_david10.py` prepares that rollout corpus from
`pai_import_row_map` source buckets. It defaults to dry-run, requires an explicit
DB path, refuses live `~/.mnemos` unless David passes the ceremony opt-in flag,
and requires `--snapshot` on `--execute`; the snapshot must match the target's
full pre-write database content under the transaction lock.

Identity apply, legacy witness stamping, and initial-rollout stamping read only
the canonical journal path; environment variables and call arguments cannot
redirect them. If the journal file exists but is agent-owned or agent-writable,
apply refuses it and session-start/watchdog reconciliation fails closed with
`journal_untrusted`, quarantining already witnessed operational identity rows
until a trusted journal is restored.

Reconciliation runs in both directions: table rows must resolve to approved
journal lines, and approved journal lines must still have live matching rows. A
missing, corrupt, or untrusted journal quarantines witnessed operational rows.
If raw SQL clears a `decision_ref` but the row still fully matches its witness,
reconcile restores both the ref and operational visibility; if witnessed fields
changed, the row stays `review_only` for re-approval.

### Prompt For An Advanced MCP Agent

```text
You have Mnemos advanced MCP tools.

Prefer the simple Mnemos tools for normal continuity: mnemos_context, mnemos_capture, mnemos_recall, mnemos_correct, mnemos_maintain, mnemos_introduce, and mnemos_health.
Use hypomnema tools when we need precise scoped continuity before promotion.
Use mnemos_inspect, mnemos_status, mnemos_beliefs, and mnemos_consolidate for debugging, migration, or explicit maintenance.
Do not promote uncertain claims into durable memory without evidence or user confirmation.
When you change memory, summarize the change in plain language.
```

---

## Hermes Agent Integration

Mnemos can also install as a Hermes identity-continuity integration.

Hermes has one external `memory.provider` slot. Mnemos therefore supports two
modes:

| Mode | Use when | What it changes |
|---|---|---|
| Sidecar Mode | Hermes already uses Honcho, Supermemory, Mem0, Hindsight, or another provider. | Preserves `memory.provider` and adds Mnemos through Hermes MCP/tools. |
| Provider Mode | Mnemos should be the active Hermes external memory provider. | Sets `memory.provider=mnemos` and writes the provider shim. |

Hermes built-in `MEMORY.md` and `USER.md` remain active in both modes. Mnemos
never overwrites `SOUL.md`, `MEMORY.md`, `USER.md`, `AGENTS.md`, or project
context files.

### Hermes Sidecar Mode

This is the safe default:

```bash
mnemos hermes quickstart --agent-safe
mnemos hermes doctor
```

`--agent-safe` is noninteractive, preserves any existing `memory.provider`,
configures only the MCP sidecar, refuses risky MCP replacement, and reports what
changed and what was preserved. Restart Hermes after install.

### Hermes Provider Mode

Use only when Mnemos should occupy Hermes' external memory-provider slot:

```bash
mnemos hermes quickstart --provider
mnemos hermes doctor
```

This sets:

```yaml
memory:
  provider: mnemos
```

Provider Mode gives Hermes direct Mnemos lifecycle integration: startup recall,
scoped identity continuity, corrections, memory mirroring, pre-compression
preservation, session-end distillation, and provider tools.

### Prompt For A Hermes Agent

Paste this into Hermes when you want the agent to install Mnemos for itself:

```text
Install Mnemos for yourself from https://github.com/Riley-Coyote/mnemos.

Use a persistent local checkout or installed package so the mnemos command still works after Hermes restarts.
Use agent-safe Sidecar Mode unless I explicitly approve Provider Mode.
Do not overwrite SOUL.md, MEMORY.md, USER.md, AGENTS.md, or project context files.
Do not change memory.provider in agent-safe mode.
Preserve any existing Hermes memory provider such as Honcho, Supermemory, Mem0, or Hindsight.

If the repo is not already present:
  git clone https://github.com/Riley-Coyote/mnemos.git

Then enter the persistent Mnemos checkout:
  cd mnemos

Run:
  uv run --extra mcp mnemos hermes quickstart --agent-safe
  uv run --extra mcp mnemos hermes doctor

After installing, tell me exactly what changed, what was preserved, whether MCP sidecar mode is configured, and whether I need to restart Hermes.
```

More detail lives in [HERMES_INSTALL.md](HERMES_INSTALL.md) and
[docs/hermes-integration.md](docs/hermes-integration.md).

---

## Substrate Tick And Maintenance

Mnemos works without background jobs. Normal MCP tool use can capture, recall,
correct, and run lightweight maintenance.

Use `substrate-tick` when you want one explicit cognitive substrate cycle:

```bash
mnemos substrate-tick
```

With explicit storage and identity:

```bash
MNEMOS_AGENT_ID=nova MNEMOS_DB_PATH=~/.mnemos/memory.db mnemos substrate-tick
```

The substrate can run local deterministic passes without a model provider.
Configured model providers enable richer deep maintenance, softening, belief
review, reflection, dreaming, and wandering.

The gated inner-life and full-soak commands are operator/pre-soak surfaces, not
baseline background jobs. They require `--db-path`, refuse live `~/.mnemos`
databases unless `--allow-live-db` is supplied, and keep generated
reflection/wandering/dream output private and audit-only. Preflight blocks any
schedule-enabled process carrying a known activation residual; the registry is
currently empty (`affect`'s `emotional-driver-filter-after-limit` entry closed
when RM-7 landed its paging primitive). See
[docs/gated-inner-life.md](docs/gated-inner-life.md) before using them.

### Dedicated Model Providers

OpenRouter:

```bash
MNEMOS_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
MNEMOS_MODEL=anthropic/claude-sonnet-4-5
MNEMOS_AGENT_MODEL=claude-opus-4-6
MNEMOS_SUBSTRATE_AFFINITY=family
```

Anthropic:

```bash
MNEMOS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
MNEMOS_MODEL=claude-sonnet-4-6
MNEMOS_AGENT_MODEL=claude-opus-4-6
```

OpenAI:

```bash
MNEMOS_LLM_PROVIDER=openai
OPENAI_API_KEY=...
MNEMOS_MODEL=gpt-5
MNEMOS_AGENT_MODEL=gpt-5
```

`MNEMOS_SUBSTRATE_AFFINITY` can be `strict`, `family`, or `open`. The default is
`family`, which prevents a mismatched model family from rewriting an agent's
memory voice unless explicitly allowed.

### Prompt For A Maintenance Agent

```text
Run a Mnemos maintenance check for this agent.

First run mnemos doctor and read the affinity status.
If the doctor output is healthy, run mnemos substrate-tick for one maintenance cycle.
If a dedicated model provider is not configured, explain that Mnemos will use local/rule-based maintenance only.
If affinity blocks the substrate model, do not force it. Explain the mismatch and what environment variables would fix it.
Report what maintenance ran and whether any follow-up is needed.
```

---

## CLI Reference

Core commands:

```bash
mnemos doctor                         # Verify simple-mode readiness
mnemos serve                          # Start simple MCP server
mnemos serve --mode advanced          # Start advanced MCP server
mnemos mcp install generic            # Print MCP config
mnemos mcp install claude --write     # Merge Claude Desktop config

mnemos init                           # Initialize a database
mnemos remember "Prefers tabs"        # Capture continuity from the CLI
mnemos stats                          # Memory statistics
mnemos search "debugging strategies"  # Search memories
mnemos inspect <engram-id>            # Inspect operational memory details
mnemos inspect --review <engram-id>   # Explicit review-only inspection
mnemos inspect --audit <engram-id>    # Explicit audit-only inspection
mnemos inspect --admin <engram-id>    # Ignore read visibility for admin inspection
mnemos snapshot                       # Inline operational Mermaid snapshot
mnemos consolidate                    # Local deterministic maintenance
mnemos consolidate --deep             # Deep maintenance when a provider exists
mnemos substrate-tick                 # Run one substrate cycle
mnemos migrate plan                   # Dry-run pending SQL-file migrations
mnemos migrate apply                  # Apply pending SQL-file migrations
```

Dashboard module:

```bash
python -m mnemos.visualization.app --build-only
python -m mnemos.visualization.app --audit --build-only  # include review/audit rows
```

Workspace, identity, and automation commands:

```bash
mnemos export --workspace ./memory-export
mnemos index
mnemos index --backfill
mnemos bootstrap --agent-name Nova --workspace ~/nova
mnemos setup-openclaw --agent main --dry-run
mnemos identity diff --soul ./SOUL.md
mnemos identity accept --divergence 1 --note "Accepted updated self-model"
```

Schema migrations:

```bash
mnemos migrate plan
mnemos migrate apply
mnemos migrate apply --target-version 11
```

`migrate` operates on the canonical store only. It resolves the database from
`MNEMOS_DB_PATH` or `store.db_path` config (including
`MNEMOS_STORE_DB_PATH`) and deliberately refuses `--db-path` so an operator
cannot migrate a shadow sibling store by accident. `plan` is read-only and
reports pending versions, statement classes, checksums, and the snapshot path
each migration would use. `apply` runs additive-only SQL files from
`mnemos/store/migrations/NNNN_name.sql`; before each version it writes an
integrity-checked SQLite backup under `<db-dir>/backups/migrations/`, records
the applied checksum in `schema_migrations`, and aborts on edited shipped
history or a database version newer than this binary understands. Pre-runner
Python migrations remain frozen history through schema v10; SQL-file migrations
start at v11.

Hermes commands:

```bash
mnemos hermes quickstart --agent-safe
mnemos hermes quickstart --provider
mnemos hermes install --mode sidecar
mnemos hermes install --mode provider --activate
mnemos hermes doctor
mnemos hermes shim
```

PAI import (operator workflow for replaying PAI-shaped source manifests
into a Mnemos store; only intended for operators bringing a pre-existing
PAI corpus into a fresh agent):

```bash
mnemos pai-import preview --manifest ./pai-manifest.json --db-path ./test.db
mnemos pai-import apply   --manifest ./pai-manifest.json --db-path ./test.db \
                          --backup-dir ./pai-import-backups
mnemos pai-import watch-preview --manifest ./pai-manifest.json --db-path ./test.db
mnemos pai-import watch-apply   --manifest ./pai-manifest.json --db-path ./test.db \
                          --backup-dir ./pai-import-backups
mnemos pai-import watch-once --manifest ./pai-manifest.json --db-path ./test.db \
                          --state ./pai-watch-state.json \
                          --artifact-dir ./pai-watch-artifacts \
                          --backup-dir ./pai-import-backups --apply
mnemos pai-import watch-plist --manifest ./pai-manifest.json --db-path ./test.db \
                          --state ./pai-watch-state.json \
                          --artifact-dir ./pai-watch-artifacts \
                          --backup-dir ./pai-import-backups \
                          --backup-keep 24 \
                          --plist ~/Library/LaunchAgents/com.davidef.mnemos.duallife.plist
mnemos pai-import watch-doctor --manifest ./pai-manifest.json --db-path ./test.db \
                          --state ./pai-watch-state.json \
                          --artifact-dir ./pai-watch-artifacts \
                          --backup-dir ./pai-import-backups \
                          --backup-keep 24 \
                          --plist ~/Library/LaunchAgents/com.davidef.mnemos.duallife.plist
mnemos pai-import review-gate --base-ref "$(git merge-base HEAD origin/main)" \
                          --intent docs/u3c-step3-launch-intent.md
```

Minimal manifest:

```json
{
  "schema": "mnemos.pai_import.manifest.v1",
  "job_id": "pai-seed",
  "defaults": {
    "agent_id": "oliver",
    "person_id": "david",
    "project_scope": "pai",
    "original_substrate": "claude-opus-4-6",
    "original_timestamp": 1710000000
  },
  "sources": {
    "identity.md": "identity_kernel",
    "beliefs.md": { "source_kind": "beliefs" }
  }
}
```

Supported source kinds are `identity_kernel`, `david_context`,
`growth_substrate`, `beliefs`, and `hypomnema`. Source paths are resolved
relative to the manifest and must stay inside that directory. Source files are
split into deterministic target rows by Markdown headings when present, or by
blank-line blocks otherwise. `--artifact` writes a preview/apply JSON artifact;
`watch-once` writes artifacts under `--artifact-dir`. `watch-once --force`
polls even when source fingerprints are unchanged.

Before target rows are hashed or indexed, the splitter strips Strict-B
coordinate-value lines from any source kind: eigenvalue, vivezza,
coordinate-target, and persona-signature tuple lines such as
`name: 0.3 | other: 0.7` or `(0.9 risoluzione, ...)`. Surrounding prose and
narrative stay in the imported row. A heading section is dropped only when
coordinate stripping leaves no non-heading body; dropped coordinate-only
blank-line blocks leave their original `block:NNN` ordinal unused so later
block anchors do not renumber.

Every DB-using PAI import command refuses the default live database
(`~/.mnemos/memory.db`) and other databases under `~/.mnemos` unless
`--allow-live-db` is passed. `apply` and `watch-apply` take an integrity-checked
SQLite backup before writing. `preview` and `watch-preview` open the DB read-only
and never mutate state. `watch-doctor` is the launch-readiness gate; `review-gate`
is the diff/intent proof gate.
Enforcement links: `mnemos/importer/operator.py`, `mnemos/importer/watcher.py`,
`mnemos/importer/review_gate.py`, `tests/test_u3b_pai_operator.py`,
`tests/test_u3c_pai_watch_doctor.py`, and
`tests/test_u3c_pai_review_gate.py`.

Gated inner-life and soak operator commands:

```bash
mnemos inner-life session-finalize --db-path ./copy.db --transcript ./session.jsonl \
                          --session-id session-1
mnemos inner-life turn-finalize --db-path ./copy.db --session-id session-1 \
                          --user-text "..." --assistant-text "..."
mnemos inner-life activity-gate --db-path ./copy.db --process reflect
mnemos inner-life run --db-path ./copy.db --process reflect
mnemos inner-life plist --db-path ./copy.db --process reflect \
                          --plist ./com.davidef.mnemos.innerlife.reflect.plist
mnemos inner-life preflight --db-path ./copy.db
mnemos inner-life status --db-path ./copy.db
mnemos soak tick --db-path ./copy.db
mnemos soak plist --db-path ./copy.db \
                          --plist ./com.davidef.mnemos.soak.tick.plist
mnemos soak preflight --db-path ./copy.db --dry-run-tick \
                          --soak-plist ./com.davidef.mnemos.soak.tick.plist
```

These commands use the schema v8 `inner_life_events` ledger for provenance,
gate decisions, skips, and generated-write telemetry. Generated low-stakes
memory rows are private, `read_visibility="audit_only"`, and excluded from
ordinary operational retrieval. `soak preflight --dry-run-tick` runs against a
SQLite copy and does not construct a real LLM client.

Global options:

```bash
mnemos --db-path ~/.mnemos/memory.db --agent-id nova stats
```

For `serve`, options can also appear after the command:

```bash
mnemos serve --mode simple \
  --agent-id nova --person-id alex --project-scope mnemos \
  --db-path ~/.mnemos/memory.db
```

---

## Configuration

Mnemos works without a config file. It creates local storage on first use.
Every default DB-using verb resolves the same one-store path: explicit
`--db-path`/`MNEMOS_DB_PATH`, then `store.db_path` config (including
`MNEMOS_STORE_DB_PATH`), then the canonical `~/.mnemos/memory.db`. Agent,
person, and project scope label rows; they do not create `~/.mnemos/{agent}.db`
unless that path is passed deliberately. `mnemos doctor` fails when it detects
a sibling per-agent DB beside the resolved canonical store.

Common environment variables:

```bash
MNEMOS_AGENT_ID=nova
MNEMOS_PERSON_ID=alex
MNEMOS_PROJECT_SCOPE=mnemos
MNEMOS_DB_PATH=~/.mnemos/memory.db
```

Dedicated model variables:

```bash
MNEMOS_LLM_PROVIDER=openrouter
MNEMOS_MODEL=anthropic/claude-sonnet-4-5
MNEMOS_AGENT_MODEL=claude-opus-4-6
MNEMOS_SUBSTRATE_AFFINITY=family
OPENROUTER_API_KEY=...
```

These are upgrades for richer maintenance, not prerequisites.

---

## What Happens Automatically

With no provider key and no extra setup, Mnemos can still run:

- local SQLite memory graph
- scoped continuity notes
- durable engram capture for operational rows, with review-only continuity for high-blast captures
- recall with reconsolidation and operational read-visibility filtering before ranking
- strength, stability, and accessibility updates
- local decay
- lightweight connection discovery
- promotion bookkeeping
- correction, supersession, and archiving
- startup context packet generation with review prose withheld by default
- cross-session verification that quotes operational first captures but emits only an existence cue for review-only first captures
- optional SVG identity graph snapshots
- maintenance during normal tool calls

If a dedicated model provider is configured, `mnemos_maintain(deep=true)` and
`mnemos consolidate --deep` can also run richer model-mediated passes such as
softening, belief review, and reflection. Dedicated providers are optional and
never required for baseline continuity.

### Optional Identity Graph

For visual-capable MCP clients, `mnemos_context` can include a portable identity
graph artifact:

```json
{
  "include_graph": true,
  "graph_max_nodes": 18
}
```

The default response remains plain text. When graph output is requested, Mnemos
also returns an `image/svg+xml` artifact and structured graph data containing
scope, stats, nodes, edges, and growth timeline. Clients that cannot render the
image can still read the continuity packet and structured data.

---

## Architecture

Mnemos operates in layered form:

```text
Simple MCP Surface      context | capture | recall | correct | maintain
Continuity Layer        scoped notes | revisions | supersession | promotion
Mnemos Core             engrams | connections | beliefs | reconsolidation
Substrate               decay | softening | reflection | modulators | events
Gated Inner Life        provenance ledger | audit-only low-stakes writes | known-blocker preflight
Cross-Agent Layer       shared pool | bridge | federation | attestation
Hermes Integration      sidecar MCP | provider shim | identity continuity
```

The working ladder is:

```text
functional memory -> scoped continuity -> durable Mnemos graph
```

Simple mode uses the same architecture; it just keeps the ontology out of the
agent's normal tool choices.

See [docs/architecture.md](docs/architecture.md) for the full architecture.
See [docs/identity-model.md](docs/identity-model.md) for the identity stance
(one traversal, one graph: why there is no fork/merge).
See [docs/privacy-security.md](docs/privacy-security.md) for local-first
privacy boundaries and [docs/release-hardening.md](docs/release-hardening.md)
for release gates.

---

## Development

```bash
uv run --extra dev pytest -q
uv run --extra dev --extra mcp pytest -q tests/test_mcp_surface.py
uv run python -m py_compile mnemos/simple_runtime.py mnemos/simple_mcp.py mnemos/mcp_server.py mnemos/cli.py mnemos/inner_life/*.py mnemos/modulation/*.py mnemos/soak/*.py
```

---

## License

MIT
