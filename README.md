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
| `mnemos_context` | Startup continuity packet. Auto-creates local storage, runs lightweight maintenance, and can optionally include an identity graph artifact. |
| `mnemos_capture` | Capture durable preferences, decisions, project state, workflows, and context. |
| `mnemos_recall` | Search scoped continuity and durable memory with natural language. |
| `mnemos_correct` | Update, supersede, or archive stale memory. |
| `mnemos_maintain` | Run the best available maintenance without requiring setup. |
| `mnemos_introduce` | Let the agent declare its own model id and name so memory maintenance stays kin from day one. |
| `mnemos_health` | Human-relayable health card: store location and size, counts, last maintenance cycle, affinity verdict, onboarding state, and last dream entry. |

Agents do not need to pass tags, memory kinds, confidence, source types, or
agent IDs. Mnemos resolves scope once from CLI flags, environment, config, and
reasonable defaults.

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
mnemos mcp install generic --agent-id nova --db-path ~/.mnemos/nova.db
```

### Prompt For A Simple MCP Agent

Paste this into an agent after Mnemos MCP is connected:

```text
You have access to Mnemos MCP memory tools.

At the start of this session, call mnemos_context.
If Mnemos asks you to introduce yourself, call mnemos_introduce with your own model id and name.
Use mnemos_capture for stable preferences, decisions, project state, workflows, corrections, and context I should not have to repeat.
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

Install advanced mode into a client:

```bash
mnemos mcp install generic --mode advanced
mnemos mcp install claude --mode advanced --write
```

Advanced tools include:

| Tool | Description |
|---|---|
| `mnemos_setup` | Legacy guided setup and seeding flow. |
| `mnemos_remember` | Encode a memory with explicit fields. |
| `mnemos_ingest` | Ingest external knowledge with provenance. |
| `mnemos_recall` | Retrieve memories. |
| `mnemos_inspect` | View full memory details. |
| `mnemos_status` | Show memory system statistics. |
| `mnemos_beliefs` | List current beliefs. |
| `mnemos_shared` | Read shared memory pool entries. |
| `mnemos_hypomnema_write` | Write scoped continuity manually. |
| `mnemos_hypomnema_search` | Search scoped continuity manually. |
| `mnemos_hypomnema_revise` | Revise a continuity entry. |
| `mnemos_hypomnema_supersede` | Replace an active continuity entry. |
| `mnemos_hypomnema_candidates` | List promotion-ready continuity. |
| `mnemos_hypomnema_promote` | Promote continuity into a durable engram. |
| `mnemos_forget` | Archive a memory. |
| `mnemos_consolidate` | Trigger explicit consolidation. |

Use simple mode for normal continuity. Use advanced mode when the agent or
operator needs direct access to Mnemos internals.

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
MNEMOS_AGENT_ID=nova MNEMOS_DB_PATH=~/.mnemos/nova.db mnemos substrate-tick
```

The substrate can run local deterministic passes without a model provider.
Configured model providers enable richer deep maintenance, softening, belief
review, reflection, dreaming, and wandering.

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
mnemos inspect <engram-id>            # Inspect memory details
mnemos consolidate                    # Local deterministic maintenance
mnemos consolidate --deep             # Deep maintenance when a provider exists
mnemos substrate-tick                 # Run one substrate cycle
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

Hermes commands:

```bash
mnemos hermes quickstart --agent-safe
mnemos hermes quickstart --provider
mnemos hermes install --mode sidecar
mnemos hermes install --mode provider --activate
mnemos hermes doctor
mnemos hermes shim
```

PAI import (operator workflow for replaying SOUL/-shaped source manifests
into a Mnemos store; only intended for operators bringing a pre-existing
PAI corpus into a fresh agent):

```bash
mnemos pai-import preview --manifest ./pai-manifest.json --db-path ./test.db
mnemos pai-import apply   --manifest ./pai-manifest.json --db-path ./test.db \
                          --backup-dir ./pai-import-backups
mnemos pai-import watch-preview --manifest ./pai-manifest.json --db-path ./test.db
mnemos pai-import watch-once --manifest ./pai-manifest.json --db-path ./test.db \
                          --state ./pai-watch-state.json --apply
mnemos pai-import watch-plist --manifest ./pai-manifest.json --db-path ./test.db \
                          --state ./pai-watch-state.json \
                          --artifact-dir ./pai-watch-artifacts \
                          --backup-dir  ./pai-import-backups \
                          --plist ~/Library/LaunchAgents/com.davidef.mnemos.duallife.plist
```

Every PAI import command refuses the default live database (`~/.mnemos/memory.db`)
unless `--allow-live-db` is passed. `apply` and `watch-apply` take an
integrity-checked SQLite backup before writing. `preview` and `watch-preview`
open the DB read-only and never mutate state.

Global options:

```bash
mnemos --db-path ~/.mnemos/nova.db --agent-id nova stats
```

For `serve`, options can also appear after the command:

```bash
mnemos serve --mode simple --agent-id nova --db-path ~/.mnemos/nova.db
```

---

## Configuration

Mnemos works without a config file. It creates local storage on first use.

Common environment variables:

```bash
MNEMOS_AGENT_ID=nova
MNEMOS_PERSON_ID=alex
MNEMOS_PROJECT_SCOPE=mnemos
MNEMOS_DB_PATH=~/.mnemos/nova.db
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
- durable engram capture
- recall with reconsolidation
- strength, stability, and accessibility updates
- local decay
- lightweight connection discovery
- promotion bookkeeping
- correction, supersession, and archiving
- startup context packet generation
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
python -m py_compile mnemos/simple_runtime.py mnemos/simple_mcp.py mnemos/mcp_server.py mnemos/cli.py
```

---

## License

MIT
