# Mnemos Hermes Integration

Mnemos for Hermes is a complementary identity-continuity layer. It gives Hermes agents durable self-model continuity, scoped corrections, hypomnema persistence, session handoffs, startup context, and continuity reports without replacing Hermes built-in `MEMORY.md` / `USER.md`.

Hermes built-in memory is always active. Hermes also supports one active external memory provider in `memory.provider`, so Mnemos has two supported modes:

- Provider Mode: `memory.provider=mnemos`; automatic identity continuity through Hermes memory-provider lifecycle hooks.
- Sidecar Mode: `memory.provider` is left unchanged; Mnemos identity continuity is exposed through MCP/tools for users already running Honcho, Supermemory, Mem0, Hindsight, or another external provider.

The implementation follows the current Hermes memory-provider contract:

- one external provider selected through `memory.provider`
- built-in memory remains active alongside the selected external provider
- user-installed providers discovered from `$HERMES_HOME/plugins/<name>/` in the local Hermes loader, with a docs-compatible shim also written to `$HERMES_HOME/plugins/memory/<name>/`
- provider lifecycle methods for startup recall, per-turn sync, built-in memory mirroring, session end, session switching, pre-compression preservation, and delegation handoffs
- profile-local storage and configuration through the active `HERMES_HOME`

Reference Hermes docs:

- https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin
- https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin
- https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers
- https://hermes-agent.nousresearch.com/docs/user-guide/features/personality
- https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files
- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp

## Fastest Safe Install

For a Hermes agent installing Mnemos for itself, use quickstart in agent-safe Sidecar Mode:

```bash
mnemos hermes quickstart --agent-safe
```

Agent-safe quickstart is noninteractive, preserves any existing `memory.provider`, installs Mnemos only as an MCP sidecar, runs a doctor check, and prints exactly what changed and what was preserved. It never overwrites `SOUL.md`, `MEMORY.md`, `USER.md`, `AGENTS.md`, or project context files.

If a user wants to hand the repo URL to a Hermes agent, use the prompt in [`../HERMES_INSTALL.md`](../HERMES_INSTALL.md). Provider Mode must be explicit:

```bash
mnemos hermes quickstart --provider
```

## Provider Mode

Use Provider Mode when Mnemos should be the active external provider for automatic Hermes identity continuity:

```bash
mnemos hermes install --mode provider --activate
```

This writes small shims into both supported Hermes provider layouts:

```text
$HERMES_HOME/plugins/mnemos/
$HERMES_HOME/plugins/memory/mnemos/
```

The first path matches the current local Hermes loader for user-installed memory
providers. The second path matches the public memory-provider docs. Both
directories import the installed Mnemos package; the durable implementation is
not duplicated.

and sets:

```yaml
memory:
  provider: mnemos
```

Provider Mode enables:

- compact startup identity-continuity packet
- automatic scoped recall before turns
- capture of preferences, corrections, decisions, self-model updates, and
  session handoffs, with high-blast identity/foundational claims held for
  review when Mnemos policy requires it
- non-destructive mirroring of Hermes built-in memory writes into Mnemos
- pre-compression preservation of identity-critical facts
- session-end distillation and continuity reports

Provider Mode is exclusive with other external memory providers because Hermes has one `memory.provider` slot. It works with Hermes built-in `MEMORY.md` / `USER.md`; it cannot be active alongside Honcho, Supermemory, Mem0, Hindsight, or another external provider in that same slot.

If you prefer to enable manually:

```bash
mnemos hermes install --mode provider
hermes config set memory.provider mnemos
```

## Sidecar Mode

Use Sidecar Mode when another external provider should stay active:

```bash
mnemos hermes install --mode sidecar
```

Sidecar Mode leaves `memory.provider` untouched and adds a Hermes MCP server entry for Mnemos:

```yaml
mcp_servers:
  mnemos:
    command: "mnemos"
    args: ["serve", "--mode", "simple"]
```

The feasible sidecar path today is MCP/tools. Hermes general plugins and memory providers are separate systems, and adding Mnemos as a second external memory provider would violate Hermes' one-provider rule. Through MCP, Hermes can still call:

- `mnemos_context`
- `mnemos_capture`
- `mnemos_recall`
- `mnemos_correct`
- `mnemos_maintain`
- `mnemos_introduce`
- `mnemos_health`

Sidecar Mode is best when Hermes already uses Honcho, Supermemory, Mem0, Hindsight, or another external provider for broader memory, and Mnemos should provide a local identity-continuity surface beside it.

If an existing `config.yaml` is present and the installer cannot safely edit YAML in the current Python environment, it will leave the file untouched and report a warning rather than risk damaging the existing provider configuration.

## Diagnostics

Check setup:

```bash
mnemos hermes doctor
```

Doctor reports whether Mnemos is in Provider Mode, Sidecar Mode, provider-shim-installed-but-inactive mode, or not configured. It also reports the active `memory.provider`, whether an MCP sidecar entry exists, the Mnemos config path, and the local database path.

Quickstart combines install and diagnostics:

```bash
mnemos hermes quickstart --agent-safe
```

It also reports the `mnemos` command path Hermes will use and whether a Hermes restart is likely needed after install.

## Configuration

Provider and sidecar settings can live in:

```text
$HERMES_HOME/mnemos.json
```

Example:

```json
{
  "db_path": "$HERMES_HOME/mnemos/mnemos.db",
  "agent_id": "coder",
  "person_id": "riley",
  "project_scope": "mnemos",
  "auto_recall": true,
  "auto_capture": true,
  "auto_bootstrap": true,
  "auto_session_distill": true,
  "mirror_builtin_memory": true,
  "deep_maintenance": false,
  "max_recall_results": 4,
  "max_context_chars": 2200
}
```

If no IDs are configured, Mnemos derives them from Hermes runtime context:

- `agent_id`: Hermes profile or `agent_identity`
- `person_id`: gateway user name, user ID, chat ID, or `user`
- `project_scope`: configured scope, chat name, or current git/root directory

Default storage is:

```text
$HERMES_HOME/mnemos/mnemos.db
```

## Provider Tools

In Provider Mode, Hermes receives four provider tools:

- `mnemos_identity_capture`: save stable preferences, corrections, decisions,
  self-model facts, and handoffs, or queue high-blast/uncertain material for review
- `mnemos_identity_recall`: recall scoped continuity for the active agent/person/project
- `mnemos_identity_correct`: revise, supersede, archive, or forget stale continuity
- `mnemos_identity_report`: inspect compact context packets, graph data, review inbox, status, and maintenance

The provider also uses Hermes lifecycle hooks:

- `system_prompt_block`: injects a compact startup continuity packet
- `prefetch`: recalls relevant continuity before a turn
- `sync_turn`: captures durable identity-continuity outcomes without requiring network access
- `on_memory_write`: mirrors built-in Hermes memory writes into Mnemos without editing built-in files
- `on_pre_compress`: preserves identity-critical facts before context compression
- `on_session_end`: distills durable session outcomes
- `on_delegation`: records handoffs without impersonating the child agent

## Identity Bootstrap

On first run, Mnemos can seed scoped continuity from:

- `$HERMES_HOME/SOUL.md`
- project context files such as `.hermes.md`, `HERMES.md`, or `AGENTS.md`
- Hermes runtime metadata such as profile and platform

Bootstrap is read-only. Mnemos never overwrites `SOUL.md`, `AGENTS.md`, `MEMORY.md`, or `USER.md`.

Disable bootstrap with:

```json
{
  "auto_bootstrap": false
}
```

## Review Inbox

Uncertain identity claims are queued as low-confidence, review-only hypomnema
entries instead of becoming high-confidence identity automatically or entering
startup recall as operating context. Inspect them with:

```json
{
  "kind": "inbox"
}
```

through `mnemos_identity_report`.

## Privacy

- Storage is local SQLite by default.
- The baseline works without network access or model provider keys.
- Model-assisted deep maintenance is disabled unless `deep_maintenance` is enabled and a supported provider key is configured.
- Provider Mode writes only the provider shim and optional Mnemos/Hermes config.
- Sidecar Mode leaves `memory.provider` unchanged and only adds an MCP entry when it can do so safely.
- Uninstall Provider Mode by removing `$HERMES_HOME/plugins/mnemos/` and `$HERMES_HOME/plugins/memory/mnemos/`, then clearing `memory.provider`.
- Uninstall Sidecar Mode by removing the `mcp_servers.mnemos` entry from `$HERMES_HOME/config.yaml`.
