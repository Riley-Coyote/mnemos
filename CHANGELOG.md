# Changelog

## 0.3.1 (unreleased)

### Beliefs change only for real reasons

A belief is something the agent said yes to. Mnemos guessed instead: any
answer to a belief question formed a belief, a declined one included; the
answer's first letters decided the rest, so "Now more than ever" retired a
belief and "No, they don't contradict" recorded a contradiction; a "no" to a
contradiction question deleted every link from one memory to the other; and
without a model, a capture sharing one word with a belief and containing
"not" (which also matches "note") lowered it by 0.05 and linked the capture as
contradicting it. Nothing could raise a belief: a reaffirmation question could
never be asked, because the answered question that formed the belief held its
place in the queue.

- `mnemos_reflect` takes a `verdict`, and the verdict alone decides what
  happens. The words are kept as written and never read for a yes or a no.
  - "Is that a belief you hold?": `hold` forms it from the agent's words at
    0.4; `decline` forms nothing; `not_now` leaves the question open.
  - "Still true?": `hold` raises it by 0.05 (never past 0.99); `retire` sets
    it to 0 and stops it shaping context, keeping the belief; each is a
    revision entry carrying the agent's words. `decline` leaves it as it is;
    `not_now` leaves the question open. Nothing is deleted.
  - A contradiction: `contradicts` leaves exactly one CONTRADICTS link between
    the two memories and changes nothing else. It no longer lowers the
    earlier memory's strength: the verdict says the two conflict, not which
    one is wrong. `compatible` removes only a contradiction link from this
    memory to the other, and every other link stays; `unsure` changes
    nothing.
  - A lesson or what a memory changed: `answer`, or `skip` to close the
    question and leave the memory as it is.
- Without a verdict, a belief or contradiction answer is kept on its
  question, which stays open, and nothing is formed, retired or linked. The
  result says which verdicts the question takes. A lesson or impact answer
  without one is still its answer. Hosts calling `reflect` through the host
  mutation protocol pass `verdict` in its arguments to act on belief and
  contradiction questions.
- Without a model, a capture never lowers a belief and never writes a
  CONTRADICTS link. The model-configured path is unchanged.
- A belief the agent has not formed or reaffirmed for 30 days may be asked
  about again ("Still true?"), at most once a month, as its own kind of
  question (`reaffirm`), under the packet's usual two-question cap. A theme
  the agent declined is not asked about again.
- Schema v11. SQLite cannot widen a CHECK in place, so opening a v10 store
  rebuilds its reflection queue with every row kept, after a verified backup
  (`backups/<db>.pre-v11-<stamp>.db`). The queue's unique index keeps its
  name, so an older Mnemos still opens the store.
- `MAINTENANCE_CODE_VERSION` is 2. Older code leaves every belief,
  reaffirmation and contradiction question open, whatever the verdict, and
  keeps the agent's words and verdict as a signed note.

### Old sessions stop maintaining a memory newer code has moved on from

A Claude Code session keeps the Mnemos code it imported when it started, and a
session can run for days. Every server started before an upgrade went on
maintaining the same store by the old rules (decay, linking, softening,
lessons, questions, beliefs, identity) after newer code had replaced them, and
nothing in the store could tell it to stop. `mnemos doctor` also wrote to the
store it was checking: it built a context packet, which ran maintenance and
used up the showings of pending questions. On a copy of a real store, one
doctor run logged a maintenance cycle, used two question showings and moved
the session counter.

- Each store records the newest maintenance code version that has opened it
  (`min_code_version` in its meta table). A server raises it when it starts
  and never lowers it.
- Code older than the store records the agent's words and applies no rules
  to them. It takes the agent's own captures, handoffs, reflections and
  corrections, because refusing those would lose memories, but:
  - it runs no maintenance, whether through a session or `mnemos consolidate`;
  - recall and the context packet still return what they find, but a return
    changes nothing: no access count, strength, version row or co-activation
    link;
  - a capture or a correction is saved in its own shape (classification,
    full-text index, vector) with no links to other memories and no weighing
    against beliefs. Maintenance under current code links it later: on a copy
    of a real store, one pass linked five such captures;
  - a correction lands on the memory it names, but never lowers or retires a
    belief, and gets no placeholder where its meaning would go;
  - belief and contradiction questions wait for a current session. The packet
    shows none and spends no showings. An answer given anyway is kept as a
    signed continuity note that names the question, which stays open. An
    answer about what a memory taught still lands on that memory; filing it
    as a lesson waits for current code;
  - a handoff replaces only its own session's note and retires no other
    session's.

  Old code never moves a belief by any path.
- A store's `schema_version`, like its `min_code_version`, only ever rises.
  Every open used to stamp the opener's own version, so older code lowered
  what newer code had recorded. Opening a store that is already up to date
  no longer changes the file at all.
- Every result it returns (capture, recall, context, reflect, correct, handoff,
  introduce) ends with one line: "This session runs older Mnemos code than
  the store expects. Restart the session." The agent inside a stale session
  is the only one who can see it. With current code nothing changes.
- `mnemos_health` and `mnemos doctor` show the running version and the
  store's minimum. When a session is older they say so, and what to do:
  restart the session, and if it still says so, update Mnemos or reset the
  minimum.
- `mnemos repair min-code-version` shows both versions and, with
  `--set N --write`, lowers or raises the minimum. It first makes a verified
  backup of the store exactly as it found it
  (`backups/<db>.pre-repair-min-code-version-<stamp>.db`). It refuses N below
  1, and only a human runs it.
- `mnemos doctor` now opens the store read-only and changes nothing. It no
  longer prints a context packet.

Servers started before this change have no gate: they keep maintaining by
their old rules until they are restarted. The gate protects every later bump.
Each change to how memory is written or maintained raises
`MAINTENANCE_CODE_VERSION` in `mnemos/code_version.py`, and from then on an
older server stops maintaining any store the newer code has opened.

### Parallel sessions keep their own handoffs

There was one handoff slot per scope, so when several sessions worked the same
scope at once, each session's handoff replaced whatever another session had
left, and the next session in either thread was handed the other thread's note
first. On one real store, 61 of 208 handoff replacements came from a different
session, and 18 of those replaced a note no session had read. Agents had
started writing combined handoffs to carry each other's threads.

- A handoff now belongs to the session that wrote it. Claude Code gives every
  MCP server `CLAUDE_CODE_SESSION_ID` and gives the SessionStart hook the same
  id, so nothing is asked of the agent or the human. A new handoff replaces
  only the note its own session left before; other sessions' notes stay.
  Clients that don't identify their session keep one shared note, as before.
- The packet hands a session its own note first (after compaction or a resume
  it gets its own thread back), otherwise the newest note, whole. Up to two
  other sessions' notes from the last three days follow as one short, signed
  line each, with the id to read one whole through `mnemos_recall`.
- A note from another session is framed as a colleague's even when the same
  model wrote it, and "the same model as you… Carry on from it" is said only of
  a note this session left.
- At most eight sessions' notes stay active per scope; beyond that the oldest
  is retired, with its prose kept in history.
- Handoffs no longer take continuity slots: the packet, `mnemos_context` and
  `mnemos_recall` leave them out of the ranked continuity search.
- Fixed: `mnemos_correct` with a `query` could land on the active handoff and
  overwrite its exact text with the correction, or forget it. A correction
  found by searching now never touches a handoff.

Schema v10 adds `hypomnema_entries.author_session` and lets the handoff index
hold one active note per session. The store upgrades itself on open, after a
verified backup (`backups/<db>.pre-v10-<stamp>.db`); on a 162 MB real store
that took two seconds. Existing notes keep an empty session and stay as they
are. An older Mnemos can still open a v10 store: the index keeps its name, and
while an unidentified note is active an older writer replaces that one rather
than a session's. A session opened before the upgrade runs the older code until
it restarts.

### Memories the scope migration hid

Schema v6 (0.2.1) gave every engram a person and a project. A legacy engram
linked to exactly one continuity scope was backfilled into it; every other one
was left unscoped and quarantined from scoped reads, so it could never be shown
to the wrong person. The quarantine is right and stays the default. What was
missing was any sign of it and any way out. Unscoped rows never reach recall,
never seed or carry spreading activation, and are skipped by scoped maintenance,
while every count reported only the scoped rows. On one real store the health
card read "186 active" over a file holding about 7,000 engrams, 105 of them
lessons distilled from experience.

- `mnemos_health` and `mnemos doctor` now say how many older memories never
  reach recall, split into lessons, other memories, and transcript-indexer
  output. Doctor raises ATTENTION only while lessons or other memories remain
  hidden; indexer output left out is the recommended state.
- `mnemos adopt-legacy` brings them back, and only a human runs it. It is a dry
  run unless `--write`, shows what would return, makes a verified backup
  (`backups/<db>.pre-adopt-legacy-<stamp>.db`) before anything moves, and
  adopts into the resolved scope. By default it brings back lessons and other
  memories, never archived rows, and never transcript-indexer output unless
  named with `--include indexer`. On a copy of the real store, adopting
  everything took all five recall slots for three of eight ordinary questions,
  while lessons and other memories changed two of sixty.
- It will not choose between people: when the agent holds memory for anyone
  other than the target person, it refuses until `--person-id` and
  `--project-scope` are given explicitly.

To recover an older store: `mnemos adopt-legacy --agent-id <agent>` to review,
then the same command with `--write`.

### Signed notes

One scope is often shared by several models. The same `claude-code` store is
written by whichever model the human is running that day, and every handoff
used to arrive as "From your previous session, in your own words", so each new
model inherited the previous one's first person and carried on as if it had
done that work. Every note is now signed with the model that wrote it.

- Schema v9 adds `hypomnema_entries.author_model`. It is added in place on open
  with one verified pre-migration backup; existing notes stay unsigned, and
  nothing is backfilled or guessed.
- Signatures come from `MNEMOS_AGENT_MODEL`, then `mnemos_introduce` in the
  current session, then the harness. Claude Code is detected from the session
  transcript (`CLAUDE_CODE_SESSION_ID`), reading only the tail of the file for
  the latest assistant turn's model id. Otherwise the note is unsigned.
- `mnemos_introduce` now signs the introducing session only. It used to be
  stored once per scope, so the last model to introduce itself stood for all.
- The handoff heading names its author: "Left by Fable 5.1 (claude-fable-5-1),
  18 hours ago." When the reader is known (a `model` field in the SessionStart
  payload, or detection in `mnemos_context`), the packet says whether it is the
  same model. An unsigned handoff says so and asks the reader not to assume it
  wrote it.
- Continuity notes show `by <model>`, `unsigned`, `co-formed`, or `Mnemos`.
  When a scope has notes from more than one model, the identity section says
  it is shared.
- Corrections re-sign the corrected note and record the prior signer. A
  reflection keeps the note's signature and names its own author inline.
- `mnemos_capture` and `mnemos_handoff` answer with the signature they wrote,
  or with how to sign when they could not tell.

Host adapters can now execute durable Core mutations through a versioned,
host-neutral exactly-once contract. An idempotency claim, every canonical
SQLite effect, and the serialized result commit atomically; a retry returns the
original result, while reuse of a key for a different request fails closed.

- Adds `MnemosRuntime.execute_host_mutation()` protocol v1 for `capture`,
  `correct`, `maintain`, `reflect`, and `introduce`.
- Adds the schema-v8 `host_mutations` replay ledger without changing the v7
  handoff model or migration behavior.
- Makes store commits transaction-aware so multi-table Core operations roll
  back completely after exceptions or process interruption.
- Treats embeddings as a rebuildable cache outside the canonical transaction;
  host mutation execution suppresses embedding writes until the transaction is
  complete.
- Adds replay, conflict, concurrent-delivery, upgrade, and failure-injection
  coverage, including crashes after each multi-table capture stage.

## 0.3.0 (2026-08-01)

Agent-written session handoffs. The agent can now leave one exact, private note
for its next session with `mnemos_handoff`. The newest handoff is delivered
first at startup, in full and only once in the packet, with its age and a quiet
instruction to continue naturally.

- One active handoff per agent/person/project scope, enforced in SQLite and
  replaced atomically while prior versions remain recoverable.
- Handoffs are never summarized, rewritten, promoted, decayed, or expired.
- Schema v7 records entry kind, authorship, author identity, last delivery, and
  delivery count. Ambiguous legacy writing remains `unknown`.
- Deterministic maintenance now uses neutral system language and is clearly
  separated from the agent's own words.
- `mnemos_health` reports handoff save/delivery/authorship state and warns
  when stored continuity is not reaching sessions.
- Claude Code startup injection remains supported. Codex now has an idempotent
  `mnemos hooks install codex --write` path covering startup, resume, clear,
  and post-compaction reinjection while preserving unrelated hooks.
- The installed-wheel audit now expects nine tools and performs a real
  session-A → handoff → fresh session-B transition.

## 0.2.1 (2026-08-01)

Production-hardening release. This release closes security, privacy, scope,
recovery, and installation gaps found during the 0.2.0 release audit. The
complete implementation and verification checklist lives in
`docs/production-hardening-0.2.1.md`.

## 0.2.0 (2026-07-31)

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
