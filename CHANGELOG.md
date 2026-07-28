# Changelog

## 0.2.0 (unreleased)

The release that makes continuity actually work. Mnemos had memory and an
agent and no reliable path between them; 0.2.0 is that path, plus the
evidence that it holds.

**Start here if you are upgrading:** two breaking changes are listed under
*Changed* — the default database path and the distribution name.

### What Mnemos is
Clarified throughout, because the previous framing caused a real failure. Mnemos
is a continuity and identity layer for the agent itself — it carries what the
agent should know about you and how you work together, across sessions. It is
**not** a general memory or retrieval system, and it runs alongside whatever you
already use for that. Pointing general recall at it buries the continuity layer:
on one live install the transcript indexer had written ~7,058 engrams against 13
deliberate captures, and a session packet spent five of six long-term slots on
paraphrases of a single harvested fact.

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

### Continuity Without Manual Triggering

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

### Changed
- **BREAKING:** advanced mode and the CLI now default to `~/.mnemos/<agent>.db` rather than `~/.mnemos/memory.db`, matching what simple mode already did. Pass `--db-path` or set `store.db_path` in `config.json` to keep reading an existing store
- Distribution renamed from `mnemos-memory` to `mnemos-continuity`. `mnemos-memory` is a different author's package on PyPI, and the published install instructions pointed users at it. The import package and CLI command are unchanged

### Simple Mode Magic UX (5 → 7 tools)
- Onboarding ritual — a fresh scope's first context packet walks the agent through a short get-to-know-you script (name, current work, durable facts); stores that predate onboarding are grandfathered and never see it
- mnemos_introduce — the agent declares its own model id and name; the declaration feeds the substrate-affinity gate so maintenance stays kin (an explicit MNEMOS_AGENT_MODEL still takes precedence)
- Cross-session memory verification — the first context packet after a real restart quotes the very first capture back to the human, once, as proof that memory survived the goodbye
- Dream journal — consolidation cycles that did meaningful work leave a short first-person narrative, surfaced in the next context packet ("While you were away") and optionally polished by the host model via MCP sampling
- mnemos_health — truly read-only, human-relayable health card: store location and size, memory counts, last maintenance cycle and who performed it, affinity verdict, onboarding and verification progress, latest dream entry

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
