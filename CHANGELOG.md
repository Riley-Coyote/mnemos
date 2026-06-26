# Changelog

## Unreleased

### PAI Importer (U3a / U3b / U3c)
- Schema v4 (U3a) — adds `voice_exemplar_eligible`, `softening_protected`, `decay_protected`, `consolidation_authorized`, `original_substrate`, `original_timestamp` to `engrams`; `tier`, `needs_review`, `confidence_pending_review` to `beliefs`; `original_timestamp` to `hypomnema_entries`; new `pai_import_row_map` table for idempotent re-runs and repair
- Schema v5 (U3b hardening) — extends `pai_import_row_map` with `content_at_last_import`, `tombstone_at`, source metadata; adds `pai_import_events` audit table and AFTER DELETE tombstone triggers
- `mnemos pai-import preview` / `apply` — operator workflow that loads a JSON source manifest, splits each source into deterministic target rows (identity-kernel, david-context, growth-substrate, beliefs, hypomnema), previews against `pai_import_row_map`, and on apply takes an integrity-checked SQLite backup before writing. Refuses the default live `~/.mnemos/memory.db` unless `--allow-live-db` is passed.
- `mnemos pai-import watch-preview` / `watch-apply` / `watch-once` / `watch-plist` (U3c) — dual-life watcher that polls source SHA-256 fingerprints, replays preview/apply only for changed sources, advances state only after a successful apply, and can write a launchd plist that runs `watch-once` on a fixed interval. Manifest source paths are constrained to the manifest directory; `EngramStore(read_only=True)` opens the SQLite DB via `file:…?mode=ro` so previews cannot mutate state.
- Consolidation and substrate passes now honor the new flags: decay, softening, connection discovery, and the substrate tick all skip rows where `decay_protected = 1`, `softening_protected = 1`, or `consolidation_authorized = 0`, and voice exemplars are filtered by `voice_exemplar_eligible`. The substrate tick decay/softening SQL is now agent-scoped.
- `IdentityProfile` reflection excludes PAI routing tags (`pai-import`, `identity-kernel`, `david-context`, `growth-substrate`, `belief`, `hypomnema`) from persistent-concern counts so an import does not surface as "Oliver's persistent concern is being imported."

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
