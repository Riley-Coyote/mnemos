# Mnemos 0.3: Agent-Written Session Handoffs

## Promise

The agent leaves a private note in its own words. The next session receives it
automatically before other continuity and continues without announcing the
memory system.

## Public Contract

- `mnemos_handoff(text)` stores the supplied text exactly.
- One handoff is active per agent/person/project scope.
- A new handoff atomically supersedes the active one; history remains
  recoverable.
- A handoff does not expire, decay, promote, summarize, or rewrite itself.
- `mnemos_correct` is the removal path.
- Startup context shows the handoff first under:
  “From your previous session, in your own words.”
- The packet includes its age and never repeats it as ordinary continuity.

Agents refresh the handoff after meaningful progress or a changed plan, when
unresolved work remains, and before pausing, ending, delegating, or changing
context. They do not refresh it after every ordinary turn.

## Authorship Boundary

Schema v7 gives every hypomnema row:

- `entry_kind`
- `authored_by`
- `author_id`
- `last_surfaced_at`
- `surface_count`

Classifications are:

- agent captures and handoffs: `agent`
- deterministic maintenance: `system`
- jointly formed notes: `coauthored`
- ambiguous historical writing: `unknown`

Migration never guesses that legacy prose belongs to the agent. Legacy dream
text is preserved byte-for-byte but displayed as system-generated material.
New maintenance reports use neutral language.

## Delivery

- Claude Code: `SessionStart` hook, including compaction.
- Codex: `mnemos hooks install codex --write`, matching startup, resume,
  clear, and compact. Existing hook configuration is preserved and Codex's
  normal `/hooks` review remains required.
- Claude Desktop, Cursor, and generic MCP clients: server instructions are the
  portable fallback. MCP has no universal lifecycle hook, so pre-turn delivery
  cannot honestly be guaranteed there.

## Storage And Migration

- Schema version: 7.
- A verified online backup is created before any older store is changed.
- A partial unique SQLite index prevents two active handoffs in one scope.
- Writers use one immediate transaction for deactivation, insertion, and audit
  linking.
- Handoffs and maintenance reports are excluded from promotion candidates.
- Existing memories, corrections, reflections, backups, and integrations are
  preserved.

## Production Gates

- exact-text save, restart, startup injection, correction, supersession, and
  backup/restore
- agent/person/project isolation
- simultaneous writers and one-active-row invariant
- handoff-first ordering with no duplicate rendering
- neutral system maintenance and conservative legacy classification
- Claude and Codex hook preservation, idempotency, invalid JSON refusal,
  paths with spaces, compaction, and fail-safe startup
- independent installed-wheel session A → handoff → fresh session B audit
- Python 3.10–3.13
- installed wheel on Linux, macOS, and Windows
- coverage, lint, package metadata, dependency audit, code security, CodeQL,
  migration, offline, and release workflow checks

## Release Sequence

1. Publish verified 0.2.1 unchanged.
2. Branch 0.3.0 from the released tag.
3. Implement and independently audit this contract.
4. Publish 0.3.0 only after every production gate passes.
