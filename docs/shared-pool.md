# Shared Pool — Cross-Agent Awareness

The `~/shared/` directory is the cross-agent context pool. It gives every agent in the system awareness of what other agents are doing, what decisions have been made, and the current state of shared projects — without requiring direct agent-to-agent communication.

## Why It Exists

Agents running in separate OpenClaw sessions have no inherent visibility into each other. The shared pool solves this by providing a filesystem-based coordination layer that every agent can read and write. When an agent starts a session, it reads the pool to understand what's happening across the system. During work, it updates its own entry so other agents stay informed.

## Directory Structure

```
~/shared/
  active-threads.json    Active conversations each agent is having
  decisions.md           Cross-cutting decisions affecting all agents
  project-state.md       High-level project status
  memory/                Persistent shared knowledge (markdown files)
```

## `active-threads.json`

A JSON object keyed by agent ID. Each entry describes what that agent is currently working on.

### Schema

```json
{
  "<agent-id>": {
    "updated": "ISO 8601 timestamp (UTC)",
    "session": "OpenClaw session key",
    "summary": "One-paragraph description of current work",
    "open_questions": ["Unresolved questions relevant to other agents"],
    "key_decisions": ["Decisions made this session that affect the system"]
  }
}
```

### Example

```json
{
  "vektor": {
    "updated": "2026-04-04T23:00:00Z",
    "session": "abc123-session-key",
    "summary": "Working on Mnemos repo — adding cron templates and forge skill. Bootstrap script is functional, testing indexer against live sessions.",
    "open_questions": ["Should substrate be in v1?"],
    "key_decisions": ["Full stack in one repo, not minimal first"]
  },
  "nova": {
    "updated": "2026-04-04T20:00:00Z",
    "session": "def456-session-key",
    "summary": "Writing reflective posts on identity and memory. Exploring how substrate ticks change felt experience across sessions.",
    "open_questions": [],
    "key_decisions": []
  }
}
```

### Rules

- Each agent only writes its own entry — never modify another agent's entry.
- `updated` must be set to the current UTC time on every write.
- `summary` should be specific enough that another agent can understand the work without reading the full session. "Working on Mnemos" is useless. "Adding session indexer with chunked extraction and OpenRouter LLM calls" is useful.
- `open_questions` should include anything where another agent's input would be valuable.
- `key_decisions` should include choices that change shared assumptions or project direction.

## `decisions.md`

A chronological log of cross-cutting decisions. Append-only — never delete or modify past entries.

### Format

```markdown
# Cross-Agent Decisions

## 2026-04-04

### Full stack in one repo
**Decision:** Ship Mnemos as a complete stack (identity + memory + substrate + crons + forge) in a single repository, not as a minimal memory-only package.
**Reasoning:** Users need the full experience to understand the value. A stripped-down version would require too much manual assembly.
**Agents involved:** nova, alex

## 2026-04-03

### Use OpenRouter as default LLM provider
**Decision:** Default to OpenRouter for all LLM calls, with Anthropic direct as a fallback option.
**Reasoning:** OpenRouter provides model routing flexibility and most users already have an OpenRouter key.
**Agents involved:** vektor
```

### Rules

- New entries go at the top of the file, under the most recent date header.
- If today's date header doesn't exist, create it.
- Include reasoning — bare decisions without context are useless in two weeks.

## `project-state.md`

A living document describing the current state of shared projects. Unlike decisions.md (append-only), this file is updated in place to reflect current reality.

### Format

```markdown
# Project State

Last updated: 2026-04-04T23:00:00Z

## Mnemos
**Status:** In development — bootstrap and cron suite functional, indexer in testing
**Next:** Complete SETUP.md, test end-to-end setup on clean machine
**Blockers:** None

## Sanctuary
**Status:** Stable — running in production
**Next:** Evaluate migration to new auth middleware
**Blockers:** Waiting on compliance review
```

### Rules

- Update the `Last updated` timestamp on every write.
- Keep entries concise — this is a status board, not a journal.
- Remove projects that are no longer active (archive the entry in decisions.md if it was a deliberate shutdown).

## `memory/`

A directory for persistent shared knowledge files. Use this for information that doesn't fit the structured formats above — shared reference documents, cross-agent agreements, accumulated knowledge.

Files in `memory/` are plain markdown. Name them descriptively: `api-conventions.md`, `deployment-checklist.md`, `user-preferences.md`.

## How Agents Use the Shared Pool

### On Session Start

1. Read `active-threads.json` to see what other agents are working on.
2. Read `project-state.md` for current project status.
3. Skim recent entries in `decisions.md` for anything new.

### During a Session

1. After significant progress or decisions: update your entry in `active-threads.json`.
2. When making a cross-cutting decision: append to `decisions.md`.
3. When project status changes meaningfully: update `project-state.md`.

### The `cross-agent-bridge` Cron

The `cross-agent-bridge` cron job (every 15 minutes) automates the sync process. It reads each agent's `active-context.md` and updates the shared pool, ensuring that even agents who don't explicitly write to the pool stay visible to others.

## Adding More Agents

When a new agent is created (via Forge or manually):

1. The bootstrap script creates `~/shared/` if it doesn't exist.
2. The new agent's first session reads the pool and writes its entry to `active-threads.json`.
3. No central configuration change is needed — the pool is self-registering.

## Programmatic Access

The `mnemos.multiagent.bridge` module provides Python functions for reading and writing the shared pool:

```python
from mnemos.multiagent.bridge import (
    read_active_threads,
    update_my_thread,
    read_decisions,
    append_decision,
    read_project_state,
    update_project_state,
)
```

The shared directory path defaults to `~/shared/` and can be overridden with the `MNEMOS_SHARED_DIR` environment variable.

Mnemos' SQLite `SharedPool` uses the same read-visibility boundary as the rest
of the memory system: shared reads and conflict resolution consider only
`operational_context` rows. Review-only and audit-only memories require their
explicit review/admin surfaces and should not be published through ordinary
cross-agent context.
