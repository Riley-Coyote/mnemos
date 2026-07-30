# Changelog

## 0.2.0 (unreleased)

Two things happen in this release. Continuity starts arriving on its own —
loaded at session start and captured as work happens, with nobody asking for
it. And memory stops being a notebook the agent writes to and becomes something
the agent *maintains*, in its own voice, on an install with no API key.

The first half is plumbing that was missing: scope that matches between the
write and the read, a session-start hook, background maintenance, and an
honest signal for when memory comes back empty. The second half is the one the
project was named for — the five shifts that were supposed to make this a mind
rather than a log, four of which did not actually run until now.

**Start here if you are upgrading:** two breaking changes are listed under
*Changed* — the default database path and the distribution name. Existing
stores migrate in place.

### What Mnemos is
Clarified throughout, because the previous framing caused a real failure. Mnemos
is a continuity and identity layer for the agent itself — it carries what the
agent should know about you and how you work together, across sessions. It is
**not** a general memory or retrieval system, and it runs alongside whatever you
already use for that. Pointing general recall at it buries the continuity layer:
on one live install the transcript indexer had written ~7,058 engrams against 13
deliberate captures, and a session packet spent five of six long-term slots on
paraphrases of a single harvested fact.

### A memory that maintains itself
Mnemos's design came from a session that asked one model what it would want if
the memory were its own to inhabit. Five shifts came out of that, and the
explainer has always claimed them — *"most AI memory is a notebook; this is a
mind."* Measured against a real store, four of the five did not run. Each one
needed a model, and the install the README advertises has none.

They run now, because the direction inverted: **the server never calls a model.
It asks the agent.** Maintenance proposes the work that needs judgement, and the
agent answers in its own turn, in its own words — that answer becomes part of
its memory. Consolidation stopped being something done *to* the agent and became
something it does. This works in every MCP client, needs no key, and made the
whole affinity system that used to police which outside model was allowed to
maintain an agent's memory unnecessary — that question is answered by
construction now (201 lines removed).

- **`mnemos_reflect`** — the eighth simple tool, and the only one whose entire
  job is to let the agent's own voice into its own memory. The context packet
  may quietly raise a question — what a capture changed, what a fading memory
  taught — and the agent answers it here. Nothing is written on its behalf; if
  no true answer comes, the prompt fades on its own. Restraint is enforced, not
  hoped for: at most two requests per packet, each shown at most three times
  then dropped, quiet scopes show nothing.
- **Traces, not records** — a capture can carry an `impact`: not what happened,
  but what it changed. That sentence is what survives when the details fade, and
  only the agent can write it, so the server never fills it with a template. An
  empty impact is left empty and asked about later.
- **Forgetting that teaches** — as a memory fades, the lesson in it is distilled
  and kept while the detail softens. With no provider the words are left intact
  and the fade lives in ranking, never a rewrite the store cannot undo.
- **Surprise as growth** — a capture that does not fit what is already held is
  encoded more deeply. This no longer depends on pre-existing beliefs or a
  model, so it is no longer skipped.
- **Resonance over search** and **identity from the graph** — retrieval spreads
  through typed connections, and identity is measured from what the agent keeps
  returning to rather than narrated. Honest limit worth stating: without a
  configured provider, edges the encoder cannot judge are labelled
  `co_activated` — *these came up together* — rather than guessed into a
  semantic type. The graph is honestly labelled, not richly typed; semantic
  relations still need semantic judgement, which is the one thing a provider now
  buys.

### Proof
- **The continuity assay** — spawns real subprocesses and passes no scope
  arguments, because defaults are what an agent actually gets. Covers a capture
  surviving into a later session, a capture written in one directory being
  readable from another, earlier captures not being displaced, reading never
  creating a store, and a corrupt store never failing a session. Reintroducing
  the cwd-derived scope makes it fail; the fix makes it pass
- **Health that reports absence** — `mnemos_health` and `mnemos doctor` now
  report notes held, empty-packet streak, and sessions since the last capture,
  and say plainly when memory is being read but coming back empty. Every failure
  this system has had looked like success from the outside; this is the signal
  none of them could fake
- **Retrieval mode is legible** — `doctor` states whether recall is semantic or
  keyword-only instead of leaving it to chance

### Background Maintenance Without OpenClaw
- `mnemos daemon {install,status,uninstall}` — schedules maintenance with whatever the host provides: launchd on macOS, systemd user timers on Linux, crontab as a fallback. Background continuity previously existed only as OpenClaw cron templates, so every user without OpenClaw had a memory system that did nothing between sessions. The job logic was never OpenClaw-specific — `mnemos consolidate`, `mnemos substrate-tick` and `mnemos index` are plain CLI commands, and only the scheduling was bound to OpenClaw
- Jobs are namespaced per agent, so several agents keep separate maintenance on one machine. Reinstall replaces rather than stacks, and the crontab backend only ever removes lines it wrote
- The model-mediated `index` job is omitted unless a provider is configured, rather than waking every 30 minutes to do nothing
- Nothing is scheduled without `--write`; `mnemos doctor` reports whether background maintenance is active
- On macOS, warns when Mnemos is installed under `~/Documents`, `~/Desktop` or `~/Downloads` — scheduled jobs do not inherit Full Disk Access, so they fail with a permission error even though the same command works by hand
- OpenClaw is now documented as one optional integration for agent-mediated jobs (observer sync, `MEMORY.md` upkeep, briefs) rather than as the way background work happens

### Continuity Without Manual Triggering
- Server instructions — both MCP surfaces now ship `instructions=` to the client, so an agent is told to load context at session start, capture durable things as they appear, and correct rather than contradict. Previously nothing instructed the agent to use memory at all; continuity only happened when a human asked for it
- `mnemos hooks install [--write]` — registers a Claude Code `SessionStart` hook that injects the continuity packet before the first turn. Preserves unrelated hooks and settings keys, replaces its own entry on reinstall rather than stacking, and refuses to overwrite an unparseable settings file
- `mnemos hook session-start` — the subcommand the hook runs. Injection logic ships with the package instead of going stale inside a generated script. Fails silent by design: any error, or a missing or corrupt store, exits 0 with no output
- Reading memory no longer creates a database as a side effect — a mistyped `--db-path` used to mint an empty store at every session start and then report a healthy, permanently empty packet

### Fixed
- **Scope split-brain.** The simple tools resolved scope through `resolve_scope` while the advanced tools took their literal parameter defaults (`default`/`user`/`global`). `mnemos_capture` wrote continuity into one partition and `mnemos_context_packet` read from another, both reporting success, so an agent silently had no memory. All 13 scoped tools now resolve through one shared resolver
- **`project_scope` no longer derives from the process working directory.** An MCP server's cwd is chosen by whichever client spawned it, so a cwd-derived scope partitioned one agent's memory by launch location. It now defaults to `global`; explicit arguments, `MNEMOS_PROJECT_SCOPE` and config still win
- **The CLI, simple mode, and advanced mode used three different combinations of agent id and database.** `mnemos stats` and `mnemos serve` reported on different stores. All entry points now share one resolver
- `ConsolidationDaemon(config={})` in the `mnemos_consolidate` tool, the CLI `consolidate` command, and `bridge.py` silently dropped the entire `consolidation` block of `config.json` — decay rate, thresholds, and `min_idle_minutes` all fell back to hardcoded defaults
- A function-local `Belief` import left the name undefined for the second seed belief in setup step 5; the resulting `NameError` was swallowed and the belief silently dropped
- Duplicated `foundational, foundational` label in the context packet
- **`pip install` produced a dead server.** The `mcp[cli]` dependency was declared `>=1.0.0` with no ceiling, so a fresh install resolved mcp 2.0 — which removed `mcp.server.fastmcp`, the module every server entrypoint imports — and the server died on import. CI never caught it because it installs from the pinned lockfile. Bounded to `<2`, with a wheel smoke job that installs unlocked from PyPI, plus a unit test guarding the declared ceiling
- **`forget` did not forget.** A successful `mnemos_correct(action="forget")` archived the memory and left `recall` silent — and the text was still read back to the agent, on both delivery paths: the session-start hook replayed it for three sessions from a frozen queue excerpt, and `mnemos_context` quoted it in the verification block with an instruction to say it aloud. Two snapshots taken at write time that no deletion reached. The rule now: no packet block renders a frozen copy of note text; every block re-reads by id and skips a memory that is gone. Older stores are cleaned during maintenance
- **A reflection did not reach the agent.** `mnemos_reflect` wrote the answer only to the engram, and the session packet is built from the continuity layer, which excludes the engram graph by default — so the one sentence the whole inversion exists to obtain was unreachable from the automatic path. It now lands in the continuity note as well; answering twice revises rather than stacks
- **Softening could erase what it could not read.** With no provider, the fade step truncated a memory to "An impression related to X... [faded]" and left the impact empty, with no way back. The default now leaves the words intact and lets the fade live in ranking; `mnemos repair-softening` (and a `mnemos doctor` prompt) restore memories an earlier version truncated, from each memory's own pre-fade snapshot
- **Identity reported its own bookkeeping.** "Persistent concerns" counted every tag, including the classifier and indexer labels Mnemos stamps on nearly every memory, so an agent read back `trace-type:fact, session-indexed, decision` as who it was. Those are excluded now; when nothing meaningful remains, the line is omitted rather than fabricated

### Changed
- **BREAKING:** advanced mode and the CLI now default to `~/.mnemos/<agent>.db` rather than `~/.mnemos/memory.db`, matching what simple mode already did. Pass `--db-path` or set `store.db_path` in `config.json` to keep reading an existing store
- Distribution renamed from `mnemos-memory` to `mnemos-continuity`. `mnemos-memory` is a different author's package on PyPI, and the published install instructions pointed users at it. The import package and CLI command are unchanged
- Engrams now record `impact_source` — who wrote the trace: `agent` (via `mnemos_reflect`/`mnemos_capture`), `model` (extracted by a configured provider), or `template` (server boilerplate). The product's claim is that only the agent can say what a memory changed, and this makes an agent-authored impact distinguishable from a generated one instead of something later reconstructed from a boilerplate denylist. Schema version 3 → 4; existing stores migrate in place and their prior impacts read as unknown, never back-filled with a guess

### Simple Mode (five tools → eight)
- Onboarding ritual — a fresh scope's first context packet walks the agent through a short get-to-know-you script (name, current work, durable facts); stores that predate onboarding are grandfathered and never see it
- mnemos_introduce — the agent declares its own model id and name, so its memory knows whose it is (an explicit MNEMOS_AGENT_MODEL still takes precedence)
- mnemos_reflect — the agent answers, in its own words, a reflection the packet raised; see *A memory that maintains itself* above
- Cross-session memory verification — the first context packet after a real restart quotes the very first capture back to the human, once, as proof that memory survived the goodbye. It re-reads that memory live, so a capture the human later forgot is never resurfaced
- Dream journal — consolidation cycles that did meaningful work leave a short first-person narrative, surfaced in the next context packet ("While you were away")
- mnemos_health — truly read-only, human-relayable health card: store location and size, memory counts, last maintenance cycle and who performed it, onboarding and verification progress, latest dream entry, and whether any memories are recoverable from an earlier version's truncation

## 0.1.0 (2026-04-05)

Initial release.

### Core Memory Engine
- Engram model with dual-trace (strength/stability/accessibility)
- 7 typed connections (supports, contradicts, causes, extends, parallels, synthesizes, grounds)
- Beliefs with confidence tracking, revision history, epistemic bounds [0.05, 0.95]
- 6-dimensional emotional state (curiosity, clarity, warmth, tension, surprise, focus)
- Graph-based identity computation
- SQLite backend with FTS5 full-text search and WAL mode

### MCP Server (9 tools)
- mnemos_setup — 10-step conversational onboarding wizard
- mnemos_remember — encode memories with impact, confidence, connection discovery
- mnemos_recall — spreading activation retrieval with emotional biasing
- mnemos_inspect — full engram details with version history
- mnemos_status — system health and statistics
- mnemos_beliefs — list beliefs with confidence and revision count
- mnemos_shared — query shared memory pool
- mnemos_forget — graceful archiving (soft delete)
- mnemos_consolidate — trigger decay, connection discovery, softening, belief review, reflection

### Cognitive Substrate
- Background tick loop (configurable interval, default 4h)
- 6 handlers: dreaming, wandering, surprise, reflection, insight, initiation
- Cognitive modulators (arousal, resolution, openness, selection_threshold, social_drive)
- Production guardrails: skip_surprise_detection on all handler outputs except surprise, per-handler throttles, confidence change caps

### CLI
- mnemos init, serve, stats, search, inspect, consolidate, export
- mnemos substrate-tick, index, bridge {status|recall|remember}
- mnemos setup-openclaw

### Multi-Agent
- Shared memory pool with visibility controls
- Agent relationship tracking with trust scores
- Per-agent isolation with optional cross-pollination

### Embedding Support
- Google Gemini embeddings (3072 dims)
- Local sentence-transformers fallback (384 dims)
- Graceful degradation to FTS5-only when no embedding backend available
