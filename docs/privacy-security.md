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

Simple mode tools still mutate local state:

- `mnemos_context` can create the database and log maintenance
- `mnemos_context(include_graph=true)` can return a scoped SVG identity graph
  artifact and structured graph data
- `mnemos_capture` writes continuity and durable memories
- `mnemos_recall` can reconsolidate access metadata
- `mnemos_correct` can archive, revise, or supersede memory
- `mnemos_maintain` runs consolidation and bookkeeping

Tool annotations describe these risks to MCP clients, but annotations are only
hints. They are not a security boundary.

## Host-Model Sampling

The supported Mnemos tools do not request host-model sampling. Capture stores
the supplied text exactly, and baseline maintenance remains local and
deterministic. Optional dedicated providers run only when explicitly configured.

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
continuity through the same database. Federation is an unsupported experimental
feature and must stay opt-in.

## Visual Artifacts

Identity graph artifacts are generated from the same scoped local memory data
used by `mnemos_context`. They should not include raw database paths, provider
keys, or unscoped cross-agent memories. Hosts that render images may display
the SVG inline; hosts that do not can ignore it and continue using the text and
structured content.

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
- verify simple mode exposes only eight tools
- verify advanced mode stays quarantined unless explicitly enabled for research
- verify `mnemos doctor` does not leak secrets
- verify package artifacts include templates and simple-mode modules
- verify baseline MCP tools make no sampling requests
- verify agent/person/project scope isolation
- verify docs say provider keys are optional
