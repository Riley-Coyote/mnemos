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
- `mnemos_capture` writes continuity and durable memories
- `mnemos_recall` can reconsolidate access metadata
- `mnemos_correct` can archive, revise, or supersede memory
- `mnemos_maintain` runs consolidation and bookkeeping

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
- verify simple mode exposes only five tools
- verify advanced mode preserves admin tools
- verify `mnemos doctor` does not leak secrets
- verify package artifacts include templates and simple-mode modules
- verify MCP sampling failures do not break tool calls
- verify agent/person/project scope isolation
- verify docs say provider keys are optional
