# Mnemos Setup

This guide covers the production path: connect the MCP, get continuity, and
only opt into advanced infrastructure when you actually need it.

---

## 1. Install

From a local checkout:

```bash
pip install -e ".[mcp]"
```

When published:

```bash
pipx install "mnemos-memory[mcp]"
```

The distribution package is `mnemos-memory`. The Python import package and CLI
command are still `mnemos`.

---

## 2. Check Readiness

```bash
mnemos doctor
```

Doctor will:

1. Resolve agent/person/project scope.
2. Create local SQLite storage if needed.
3. Run local deterministic maintenance.
4. Print the simple MCP tool list.
5. Show whether a dedicated model provider is configured.

No OpenRouter key is required for baseline continuity.

---

## 3. Connect an MCP Client

### Claude Desktop

```bash
mnemos mcp install claude --write
```

Then restart Claude Desktop.

To preview the config without writing:

```bash
mnemos mcp install claude
```

### Codex

```bash
mnemos mcp install codex
```

Run the printed `codex mcp add ...` command, then restart the Codex session.

### Cursor or Generic Clients

```bash
mnemos mcp install cursor
mnemos mcp install generic
```

Paste the printed JSON into the client's MCP config.

---

## 4. Simple Mode

Simple mode is the default:

```bash
mnemos serve
```

It exposes only:

- `mnemos_context`
- `mnemos_capture`
- `mnemos_recall`
- `mnemos_correct`
- `mnemos_maintain`

The user does not need to set up a database, choose tags, pass agent IDs, learn
engram/hypomnema terminology, configure OpenClaw, or supply a model key.

Optional scope:

```bash
mnemos serve --agent-id nova --person-id riley --project-scope mnemos
```

or:

```bash
MNEMOS_AGENT_ID=nova MNEMOS_PERSON_ID=riley MNEMOS_PROJECT_SCOPE=mnemos mnemos serve
```

---

## 5. Maintenance and Models

Mnemos maintenance has three levels:

1. **Local baseline**: always available. Runs decay, connection discovery,
   bookkeeping, promotion checks, correction handling, and reconsolidation
   updates with no provider key.
2. **Client-assisted**: future MCP sampling path for in-band model help during
   active tool calls when the client supports it.
3. **Dedicated background**: optional provider keys for richer deep maintenance
   and autonomous background work.

Dedicated provider example:

```bash
MNEMOS_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
MNEMOS_MODEL=anthropic/claude-sonnet-4-5
```

Deep maintenance can then be requested with:

```bash
mnemos_maintain(deep=true)
```

or from the CLI:

```bash
mnemos consolidate --deep
```

---

## 6. Advanced Mode

Use advanced mode for debugging, migration, research, or direct control:

```bash
mnemos serve --mode advanced
```

Advanced mode preserves the full tool surface: explicit remember/ingest/recall,
hypomnema management, beliefs, shared memory, inspect, forget, and explicit
consolidation.

OpenClaw, crons, Forge, substrate ticks, and full agent workspaces are advanced
integrations. They are no longer part of the baseline setup.

To bootstrap a full workspace:

```bash
mnemos bootstrap --agent-name Nova --workspace ~/nova --user-name Riley
```

To register OpenClaw crons:

```bash
mnemos setup-openclaw --agent main --dry-run
```

Review generated commands before enabling them.

---

## 7. Safety and Release Notes

- Privacy boundaries: [docs/privacy-security.md](docs/privacy-security.md)
- Release checklist: [docs/release-hardening.md](docs/release-hardening.md)

---

## 8. Troubleshooting

### The MCP client does not show Mnemos tools

1. Run `mnemos doctor`.
2. Confirm the client config points to `mnemos serve`.
3. Restart the client session or desktop app.
4. For simple mode, expect exactly five tools.
5. For advanced mode, use `mnemos serve --mode advanced`.

### The agent has no prior memory

That is normal on first run. The first `mnemos_context` creates storage and
returns an empty continuity packet. Use `mnemos_capture` to store preferences,
decisions, project state, and corrections.

### Deep maintenance says it used local maintenance

No dedicated provider is configured. Baseline continuity still works. Add a
provider key only if you want richer model-mediated maintenance or background
work.

### Multiple agents share a machine

Give each agent a scope:

```bash
MNEMOS_AGENT_ID=nova mnemos serve
MNEMOS_AGENT_ID=vektor mnemos serve
```

or separate DBs:

```bash
mnemos serve --agent-id nova --db-path ~/.mnemos/nova.db
mnemos serve --agent-id vektor --db-path ~/.mnemos/vektor.db
```
