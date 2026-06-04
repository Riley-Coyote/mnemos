# Mnemos

**Connect MCP. Get continuity.**

Mnemos is local-first memory for AI agents. Connect the MCP server and an agent
immediately gets durable continuity: startup context, capture, recall,
correction, and maintenance without OpenRouter, OpenClaw, crons, manual
database setup, tags, or `agent_id` plumbing.

The full Mnemos architecture is still here: scoped continuity, hypomnema,
durable engrams, reconsolidation, decay, connection discovery, beliefs,
substrate work, and cross-agent layers. Simple mode hides that machinery behind
five tools so normal agents can use it safely.

SQLite-backed. No external services required for baseline memory. Dedicated
model providers are optional for richer deep maintenance.

---

## Quick Start

From a checkout:

```bash
pip install -e ".[mcp]"
mnemos doctor
mnemos mcp install generic
```

When published, install the distribution package:

```bash
pipx install "mnemos-memory[mcp]"
mnemos mcp install claude --write
```

Then restart your MCP client. The agent should call `mnemos_context` at the
start of a session and can immediately capture, recall, correct, and maintain
continuity.

### Claude Desktop

```bash
mnemos mcp install claude --write
```

Or print the config without writing:

```bash
mnemos mcp install claude
```

### Codex

```bash
mnemos mcp install codex
```

The command prints a `codex mcp add ...` command using simple mode.

### Cursor / Generic Clients

```bash
mnemos mcp install cursor
mnemos mcp install generic
```

These print MCP JSON snippets you can paste into the client config.

---

## Simple MCP Mode

`mnemos serve` starts simple mode by default.

Simple mode exposes only five user-facing tools:

| Tool | Purpose |
|------|---------|
| `mnemos_context` | Startup continuity packet. Auto-creates local storage and runs lightweight maintenance. |
| `mnemos_capture` | Capture durable preferences, decisions, project state, workflows, and context. |
| `mnemos_recall` | Search scoped continuity and durable memory with natural language. |
| `mnemos_correct` | Update, supersede, or archive stale memory. |
| `mnemos_maintain` | Run the best available maintenance without requiring setup. |

Agents do not need to pass tags, memory kinds, confidence, source types, or
agent IDs. Mnemos resolves scope once from CLI flags, environment, config, and
reasonable defaults.

Example MCP server command:

```bash
mnemos serve
```

With explicit scope:

```bash
MNEMOS_AGENT_ID=nova MNEMOS_PERSON_ID=riley MNEMOS_PROJECT_SCOPE=mnemos \
  mnemos serve
```

Or:

```bash
mnemos serve --agent-id nova --person-id riley --project-scope mnemos
```

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
- maintenance during normal tool calls

If a dedicated model provider is configured, `mnemos_maintain(deep=true)` can
also run richer model-mediated passes such as softening, belief review, and
reflection. Dedicated providers are optional and never required for baseline
continuity.

Future MCP sampling support can let Mnemos ask the host client's model for
in-band compression/classification during an active tool call. Background model
work still needs a dedicated provider or scheduler because MCP clients do not
guarantee standalone server-initiated model calls.

---

## Advanced Mode

Advanced mode exposes the full operator/admin surface in addition to the simple
tools:

```bash
mnemos serve --mode advanced
```

Advanced tools include:

| Tool | Description |
|------|-------------|
| `mnemos_setup` | Legacy guided setup and seeding flow |
| `mnemos_remember` | Encode a memory with explicit fields |
| `mnemos_ingest` | Ingest external knowledge with provenance |
| `mnemos_recall` | Retrieve memories |
| `mnemos_inspect` | View full memory details |
| `mnemos_status` | Show memory system statistics |
| `mnemos_beliefs` | List current beliefs |
| `mnemos_shared` | Read shared memory pool entries |
| `mnemos_hypomnema_write` | Write scoped continuity manually |
| `mnemos_hypomnema_search` | Search scoped continuity manually |
| `mnemos_hypomnema_revise` | Revise a continuity entry |
| `mnemos_hypomnema_supersede` | Replace an active continuity entry |
| `mnemos_hypomnema_candidates` | List promotion-ready continuity |
| `mnemos_hypomnema_promote` | Promote continuity into a durable engram |
| `mnemos_forget` | Archive a memory |
| `mnemos_consolidate` | Trigger explicit consolidation |

Use advanced mode for debugging, migration, research, and direct control. Use
simple mode for normal agent continuity.

---

## CLI

```bash
mnemos doctor                         # Verify simple-mode readiness
mnemos serve                          # Start simple MCP server
mnemos serve --mode advanced          # Start advanced MCP server
mnemos mcp install generic            # Print MCP config
mnemos mcp install claude --write     # Merge Claude Desktop config

mnemos init                           # Initialize a database
mnemos stats                          # Memory statistics
mnemos search "debugging strategies"  # Search memories
mnemos inspect <engram-id>            # Inspect memory details
mnemos consolidate                    # Local deterministic maintenance
mnemos consolidate --deep             # Deep maintenance when a provider exists
mnemos bootstrap --agent-name Nova --workspace ~/nova
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

Optional environment variables:

```bash
MNEMOS_AGENT_ID=nova
MNEMOS_PERSON_ID=riley
MNEMOS_PROJECT_SCOPE=mnemos
MNEMOS_DB_PATH=~/.mnemos/nova.db
```

Optional dedicated model providers:

```bash
MNEMOS_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
MNEMOS_MODEL=anthropic/claude-sonnet-4-5
```

or:

```bash
MNEMOS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
```

or:

```bash
MNEMOS_LLM_PROVIDER=openai
OPENAI_API_KEY=...
```

These are upgrades for richer maintenance, not prerequisites.

---

## Architecture

Mnemos still operates in layered form:

```text
Simple MCP Surface      context · capture · recall · correct · maintain
Continuity Layer        scoped notes · revisions · supersession · promotion
Mnemos Core             engrams · connections · beliefs · reconsolidation
Substrate               decay · softening · reflection · modulators · events
Cross-Agent Layer       shared pool · bridge · federation · attestation
```

The working ladder is:

```text
functional memory -> scoped continuity -> durable Mnemos graph
```

Simple mode uses the same architecture; it just keeps the ontology out of the
agent's normal tool choices.

See [docs/architecture.md](docs/architecture.md) for the full architecture.
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

The package distribution name is `mnemos-memory` because `mnemos` is already
occupied on PyPI. The import package and CLI command remain `mnemos`.

---

## License

MIT
