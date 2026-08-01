# Mnemos

**Continuity for the agent itself.**

Mnemos gives an AI agent the thing it otherwise cannot have: a sense of being
someone who persists. At the start of a session it reads its own words about
its own experience back to itself — what it learned about you, what you
decided together, what it got wrong last time — before you say anything.

That is the whole product. Everything else in here serves it.

```bash
pip install mnemos-continuity
mnemos mcp install claude --write     # or cursor / codex / generic
mnemos hooks install --write          # inject memory before the first turn
```

Eight tools. Local SQLite. No account, no API key, no external service. Nothing
leaves your machine unless you configure a provider yourself.

## What Mnemos is not

**It is not your memory system.** It does not index your documents, search your
codebase, or answer "what did we do last March." It is designed to sit
*alongside* whatever memory or retrieval you already use, and to stay out of its
way.

The distinction matters in practice: Mnemos holds the small number of durable
things an agent should carry about *you and how you work together* — a
preference, a correction, a decision, how you like to be spoken to. If you point
it at a firehose of general recall, that continuity gets buried, and the product
stops working. It deliberately does not schedule transcript indexing for exactly
this reason.

If you want a full cognitive engine with retrieval, a work-memory suite, and
document ingestion, Mnemos is not that and is not trying to be.

---

## Choose A Setup

| Use case | Recommended path | Command |
|---|---|---|
| Normal agent continuity | Simple MCP Mode | `mnemos serve` |
| Claude Desktop | Simple MCP Mode, written config | `mnemos mcp install claude --write` |
| Codex | Simple MCP Mode, printed add command | `mnemos mcp install codex` |
| Cursor or another MCP client | Simple MCP Mode, printed JSON | `mnemos mcp install cursor` or `mnemos mcp install generic` |
| Experimental research prototypes | Source imports only | Not production-supported |
| Hermes agent with another memory provider | Hermes Sidecar Mode | `mnemos hermes quickstart --agent-safe` |
| Hermes agent using Mnemos as its provider | Hermes Provider Mode | `mnemos hermes quickstart --provider` |
| Background memory maintenance | Substrate tick | `mnemos substrate-tick` |

Start with **Simple MCP Mode**. It is the product: eight tools, and the default.

The older advanced MCP surface and prototype modules are quarantined in 0.2.x.
They remain importable for research, but unfinished operations fail clearly and
they are not production-supported. The CLI contains the supported administrative
operations.

Hermes users should start with **Hermes Sidecar Mode** unless they explicitly
want Mnemos to occupy Hermes' single external `memory.provider` slot.

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
pipx install "mnemos-continuity[mcp]"
mnemos doctor
```

The package distribution name is `mnemos-continuity` because `mnemos` is already
occupied on PyPI. The import package and CLI command remain `mnemos`.

---

## Simple MCP Mode

Simple mode is the default and safest path for most agents.

```bash
mnemos serve
```

Simple mode exposes eight user-facing tools:

| Tool | Purpose |
|---|---|
| `mnemos_context` | Startup continuity packet. Auto-creates local storage, runs lightweight maintenance, and can optionally include an identity graph artifact. |
| `mnemos_capture` | Capture durable preferences, decisions, project state, workflows, and context. |
| `mnemos_recall` | Search scoped continuity and durable memory with natural language. |
| `mnemos_correct` | Update, supersede, or archive stale memory. |
| `mnemos_reflect` | Answer a reflection the packet raised — what a memory changed, or what a fading one taught — in the agent's own words. Mnemos never calls a model to write this; the agent's memory is maintained by its own mind or not at all. |
| `mnemos_maintain` | Run the best available maintenance without requiring setup. |
| `mnemos_introduce` | Let the agent declare its own model id and name, so its memory knows whose it is. |
| `mnemos_health` | Human-relayable health card: store location and size, counts, last maintenance cycle, onboarding state, and last dream entry. |

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

### Automatic Continuity

Connecting the MCP gives an agent memory tools. It does not, by itself,
guarantee the agent *uses* them — and memory an agent has to be reminded to
load is not continuity. Two mechanisms close that gap.

**Server instructions (every client, nothing to install).** Mnemos ships
instructions with the MCP server itself. Any MCP client surfaces them to its
agent: load context at session start, capture durable things as they appear,
correct rather than contradict, and never narrate the machinery at you. This
is automatic once the server is connected.

**Session-start injection (Claude Code).** Stronger still is putting memory in
front of the agent before its first turn, so there is no tool call to forget:

```bash
mnemos hooks install --write
```

Preview what it would write, without writing:

```bash
mnemos hooks install
```

This registers a `SessionStart` hook that runs `mnemos hook session-start` and
injects the continuity packet into the session. It preserves any hooks and
settings you already have, replaces its own entry rather than stacking on
reinstall, and refuses to overwrite a settings file it cannot parse.

The hook is written to fail silent: if memory is empty, unreadable, or absent,
it contributes nothing and the session starts normally. It never creates a
database just because something read from it.

Scope flags pass straight through when one machine hosts several agents:

```bash
mnemos hooks install --write --agent-id nova --db-path ~/.mnemos/nova.db
```

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

Usually unnecessary — the server ships these instructions itself, and every
MCP client passes them to its agent. Keep this for clients that ignore server
instructions, or when you want the behaviour stated in the agent's own prompt:

```text
You have access to Mnemos MCP memory tools.

At the start of this session, call mnemos_context.
If Mnemos asks you to introduce yourself, call mnemos_introduce with your own model id and name.
Use mnemos_capture for stable preferences, decisions, project state, workflows, corrections, and context I should not have to repeat.
Use mnemos_recall before relying on memory from prior sessions.
Use mnemos_correct when a remembered fact is stale, wrong, superseded, or should be forgotten.
If the context packet asks you something about your own memory — what a capture changed, what a fading memory taught — answer it with mnemos_reflect, in your own words. If nothing true comes to mind, leave it.
Use mnemos_health if I ask whether memory is working.

Do not mention tools unless I ask. Just use the memory system quietly and tell me what you remembered when it matters.
```

---

## Experimental Advanced Prototypes

Advanced mode is retained only for compatibility and research. It is incomplete,
unsupported, and intentionally blocked by default. Do not install it for users.

```bash
MNEMOS_ENABLE_EXPERIMENTAL=1 mnemos serve --mode advanced
```

There is no supported client-install path for advanced mode.

Use simple mode and the CLI for all production continuity and administration.

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

But memory that only works while a session is open is doing half the job.
Decay, connection discovery, consolidation and the substrate tick are what make
continuity feel alive between conversations.

### Background Maintenance

```bash
mnemos daemon install --write
```

This schedules maintenance with whatever your machine already provides —
launchd on macOS, systemd user timers on Linux, plain crontab as a fallback.
No external agent runner is required.

| job | what it does | schedule |
|---|---|---|
| `maintain` | decay and connection discovery | every 4h |
| `maintain-deep` | softening, belief review, reflection | daily at 03:00 |
| `substrate-tick` | handlers and modulators | every 4h |
| `index` | index session transcripts | every 30m, only with a model provider |

Preview before committing to anything:

```bash
mnemos daemon install          # prints exactly what it would schedule
mnemos daemon status           # what is scheduled right now
mnemos daemon uninstall --write
```

Jobs are namespaced per agent, so several agents can each keep their own
maintenance on one machine. `mnemos doctor` reports whether background
maintenance is scheduled.

On macOS, install Mnemos outside `~/Documents`, `~/Desktop` and `~/Downloads`.
Scheduled jobs do not inherit Full Disk Access, so a Mnemos living in one of
those folders runs fine by hand and fails on every scheduled run. `mnemos
daemon install` warns when it detects this.

### One Explicit Cycle

Use `substrate-tick` when you want a single cognitive substrate cycle:

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

There is no longer a substrate-affinity setting. It existed to stop a mismatched
model family from rewriting an agent's memory voice, and became unnecessary when
maintenance stopped calling a model at all: judgement work is proposed to the
agent and answered in its own turn through `mnemos_reflect`, so the question of
which outside model may write an agent's memory is answered by construction.

### Prompt For A Maintenance Agent

```text
Run a Mnemos maintenance check for this agent.

First run mnemos doctor and read the retrieval, continuity, and background status.
If the doctor output is healthy, run mnemos substrate-tick for one maintenance cycle.
If a dedicated model provider is not configured, explain that Mnemos will use local, deterministic maintenance only, and that work needing judgement is left for the agent to answer through mnemos_reflect rather than performed by an outside model.
Report what maintenance ran and whether any follow-up is needed.
```

---

## CLI Reference

Core commands:

```bash
mnemos doctor                         # Verify simple-mode readiness
mnemos serve                          # Start simple MCP server
MNEMOS_ENABLE_EXPERIMENTAL=1 mnemos serve --mode advanced  # unsupported research mode
mnemos mcp install generic            # Print MCP config
mnemos mcp install claude --write     # Merge Claude Desktop config
mnemos hooks install                  # Preview the SessionStart memory hook
mnemos hooks install --write          # Install it (Claude Code)
mnemos hook session-start             # Emit the packet the hook injects
mnemos daemon install                 # Preview scheduled background maintenance
mnemos daemon install --write         # Schedule it (launchd/systemd/crontab)
mnemos daemon status                  # What is scheduled right now
mnemos daemon uninstall --write       # Remove it

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
Simple MCP Surface      context | capture | recall | correct
                        reflect | maintain | introduce | health
Continuity Layer        scoped notes | revisions | supersession | promotion
Mnemos Core             engrams | connections | beliefs | reconsolidation
Substrate               decay | softening | reflection | modulators | events
Cross-Agent Layer       shared pool | bridge | experimental federation/attestation
Hermes Integration      sidecar MCP | provider shim | identity continuity
```

The working ladder is:

```text
functional memory -> scoped continuity -> durable Mnemos graph
```

Simple mode uses the same architecture; it just keeps the ontology out of the
agent's normal tool choices.

See [docs/vision.md](docs/vision.md) for the full architecture.
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
