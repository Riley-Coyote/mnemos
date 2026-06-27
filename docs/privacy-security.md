# Privacy and Security Boundaries

Mnemos is designed to be local-first by default. Simple mode should give agents
continuity without requiring users to send memory data to a third-party model or
configure an external provider.

## Baseline Simple Mode

With no dedicated provider configured, Mnemos:

- stores memory in a local SQLite database
- uses local full-text search and deterministic maintenance
- scopes memory by agent, person, and project
- avoids OpenRouter, Anthropic, OpenAI, and OpenClaw requirements
- does not read arbitrary files or browser history
- does not transmit memory data over the network

Simple mode tools have these local side effects:

- `mnemos_context` can create the database and log maintenance
- `mnemos_context(include_graph=true)` can return a scoped SVG identity graph
  artifact and structured graph data
- `mnemos_capture` writes continuity and durable memories
- `mnemos_recall` can reconsolidate access metadata
- `mnemos_correct` can archive, revise, or supersede memory
- `mnemos_maintain` runs consolidation and bookkeeping
- `mnemos_introduce` writes the agent's self-declared model/name for affinity
  checks
- `mnemos_health` is read-only

Tool annotations describe these risks to MCP clients, but annotations are only
hints. They are not a security boundary.

## Host-Model Sampling

When an MCP client supports sampling, Mnemos may ask the host client's model for
in-band assistance during an active tool call. The client controls whether that
request is allowed.

Sampling requests should be:

- optional
- tied to the originating client request
- concise
- resilient when declined or unsupported
- free of secrets unless the user intentionally supplied them as memory content

Mnemos must always continue to work without sampling.

## Dedicated Providers

Dedicated model providers are optional. Mnemos should only use them when the
user explicitly configures provider environment variables or Mnemos model
configuration.

Provider keys enable richer maintenance, but they may send selected memory
content to that provider. This must remain an opt-in upgrade path, not a
baseline requirement.

## Scope Isolation

Every memory operation should resolve a scope:

```text
agent_id / person_id / project_scope
```

This prevents multiple agents on the same machine from accidentally sharing
continuity through the same database. Shared memory and federation are advanced
features and should stay opt-in.

## Visual Artifacts

Identity graph artifacts are generated from the same scoped local memory data
used by `mnemos_context`. They should not include raw database paths, provider
keys, or unscoped cross-agent memories. Hosts that render images may display
the SVG inline; hosts that do not can ignore it and continue using the text and
structured content.

## PAI Importer

The `mnemos pai-import` operator workflow replays a JSON source manifest
(identity-kernel, david-context, growth-substrate, beliefs, hypomnema) into a
Mnemos store. It is opt-in and intended for operators bringing a pre-existing
PAI-shaped corpus into a fresh agent — not for end users.

Safety boundaries enforced by the importer:

- `preview` and `watch-preview` open the DB read-only and never mutate state.
- `apply` and `watch-apply` take an integrity-checked SQLite backup before
  writing, into the configured `--backup-dir`.
- Every DB-using subcommand refuses the default live database
  (`~/.mnemos/memory.db`) and other databases under `~/.mnemos` unless
  `--allow-live-db` is passed. The guard is inode-based so case-insensitive
  path variants cannot bypass it.
- Manifest source paths must stay inside the manifest directory after
  resolution; absolute paths are allowed only when they still resolve inside
  that directory, and `..`-escaping paths are rejected at load.
- The dual-life watcher (`watch-once` / `watch-plist`) advances its state only
  after a successful apply. Preview mode leaves state untouched. Missing source
  files are treated as an empty current snapshot so removed sections become
  explicit tombstone, deactivate, or review actions instead of silent drift.
- Imported rows carry `decay_protected`, `softening_protected`, and
  `consolidation_authorized` flags so the consolidation and substrate passes
  cannot silently rewrite imported identity material.
- Imported beliefs carry `confidence_pending_review` and are excluded from
  default belief consumers until belief review clears the flag.
- Enforcement links: `mnemos/importer/operator.py`, `mnemos/importer/watcher.py`,
  `mnemos/importer/review_gate.py`, `tests/test_u3b_pai_operator.py`,
  `tests/test_u3c_pai_watch_doctor.py`, and
  `tests/test_u3c_pai_review_gate.py`.

## Correction and Forgetting

Mnemos favors audited correction over hard deletion:

- corrections can archive old engrams
- continuity notes can be revised or superseded
- audit trails remain available to advanced/admin tools

Future user-facing forget flows should make the difference between archive,
supersede, and hard deletion explicit.

## Release Review Checklist

Before a release:

- verify simple mode works with no provider keys
- verify simple mode exposes only seven tools
- verify advanced mode preserves admin tools
- verify `mnemos doctor` does not leak secrets
- verify package artifacts include templates and simple-mode modules
- verify MCP sampling failures do not break tool calls
- verify agent/person/project scope isolation
- verify docs say provider keys are optional
