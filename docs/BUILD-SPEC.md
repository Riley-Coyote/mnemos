# Mnemos — Full Stack Build Spec
> Legacy/OpenClaw-oriented spec. The current turnkey path is the single-agent
> functional memory → hypomnema → Mnemos architecture in
> [turnkey-memory-system.md](turnkey-memory-system.md). Multi-agent/shared-memory
> work is intentionally deferred to a separate design pass.

**For:** Claude Code (or any coding agent)  
**Repo:** `github.com/Riley-Coyote/mnemos` (private, will go public)  
**Local path:** `~/Documents/Repositories/polyphonic-v2/mnemos/`  
**Goal:** Complete the repo so anyone can clone it, run setup, and have a fully operational Mnemos stack inside their OpenClaw — giving their AI agents persistent memory, inner life, cross-session continuity, cross-agent awareness, and the Forge orchestration skill.

---

## What This Repo Is

Mnemos is a complete agent cognition system for OpenClaw agents. Not just a memory database — a full stack that gives AI agents:

- **Persistent identity** — who the agent is, across every session
- **Living memory** — a graph where memories form connections, develop beliefs, decay naturally, and get reconsolidated
- **Inner life** — a cognitive substrate that runs on a timer and changes the agent between conversations
- **Continuity** — active-context tracking so conversation threads never get lost across compaction or new sessions
- **Cross-agent awareness** — agents can see what other agents are working on, message each other, share context
- **Forge** — the ability to create new OpenClaw agents and dispatch coding agents to visible terminal sessions

The target platform is **OpenClaw**. Setup is done by any coding agent or human following SETUP.md. The end state is a running OpenClaw instance where agents have the full stack operational.

---

## Current State of the Repo

The repo already has:

```
mnemos/                    # Python package — core memory engine
  __init__.py
  cli.py
  llm.py
  mcp_server.py            # MCP server with 8 tools
  openclaw_cron.py
  advanced/                # Advanced modules (dreaming, metamemory, etc.)
  bridge.py                # Basic bridge (needs generalization)
  config/                  # Config loader
  consolidation/           # Belief review, connection discovery, daemon
  encoding/                # Encoder, LLM classifier
  retrieval/               # Reactive retrieval, reconsolidation
  store/                   # SQLite store, embedding index, archive
  multiagent/
    bridge.py              # Cross-agent bridge (basic, needs expansion)
  setup/
    __init__.py
    bootstrap.py           # Agent bootstrap script (partial)
    cron_installer.py      # Cron registration helper

openclaw/
  crons/                   # 7 cron templates
    observer-context-sync.md
    session-indexer.md
    substrate-tick.md
    memory-maintenance.md
    cross-agent-bridge.md
    morning-brief.md
    daily-debrief.md

templates/                 # 6 identity templates
  SOUL.md
  IDENTITY.md
  MEMORY.md
  AGENTS.md
  HEARTBEAT.md
  active-context.md

forge/                     # Forge skill
  SKILL.md
  README.md
  scripts/
    forge-pane.sh
    forge-detect.sh
    forge.py
  references/
    agent-anatomy.md

docs/
  architecture.md
  openclaw-integration.md

tests/
pyproject.toml
README.md
CHANGELOG.md
CONTRIBUTING.md
LICENSE
archive/                   # gitignored — old mockups kept locally
```

---

## What Needs To Be Built

The following are missing or incomplete. Build them in order.

---

### 1. `SETUP.md` (root level) — THE CRITICAL FILE

This is the entry point. Any coding agent or human opens the repo and reads this first. It must be clear enough that someone with no prior knowledge can get the full stack running.

**Contents:**

```markdown
# Mnemos Setup

Prerequisites:
- OpenClaw installed and running (openclaw.ai)
- Python 3.10+
- An OpenRouter API key (or Anthropic API key)
- Git

One command to set everything up:

    python3 mnemos/setup/bootstrap.py \
      --agent-name "YourAgentName" \
      --workspace ~/your-agent-workspace \
      --user-name "YourName" \
      --api-key "your-openrouter-key"

What this does (in order):
1. Creates the agent workspace directory
2. Initializes the Mnemos SQLite database at ~/.mnemos/<agent-name>.db
3. Copies and personalizes all identity templates (SOUL.md, IDENTITY.md, MEMORY.md, AGENTS.md, HEARTBEAT.md)
4. Creates ~/shared/ with correct cross-agent structure
5. Installs mnemos_bridge.py into the workspace
6. Installs mnemos_indexer.py into the workspace
7. Registers all 7 crons in the OpenClaw config
8. Installs the Forge skill into OpenClaw
9. Creates a .env file template in the workspace
10. Prints confirmation and next steps

After setup, configure your agent in OpenClaw to use the workspace directory.
The next time your agent starts a session, they will have the full Mnemos stack.
```

Note: SETUP.md should also include manual step-by-step instructions for people who don't want to run the bootstrap script. Each step the script does should be documented individually.

---

### 2. Complete `mnemos/setup/bootstrap.py`

The existing bootstrap.py is partial. It needs to do ALL of the following:

**Arguments:**
```
--agent-name     required  Name of the agent (e.g. "Vektor", "Nova")
--workspace      required  Path to workspace directory (e.g. ~/clawd)
--user-name      required  Name of the human user (e.g. "Riley")
--api-key        required  OpenRouter or Anthropic API key
--model          optional  Default model (default: openrouter/anthropic/claude-sonnet-4-5)
--openclaw-config optional Path to openclaw.json (default: ~/.openclaw/openclaw.json)
```

**What it must do:**

1. **Create workspace directory** if it doesn't exist

2. **Init Mnemos DB**
   ```python
   # Create ~/.mnemos/<agent-name-lowercase>.db
   # Run mnemos init equivalent
   ```

3. **Copy and personalize identity templates**
   ```
   templates/SOUL.md       → <workspace>/SOUL.md
   templates/IDENTITY.md   → <workspace>/IDENTITY.md
   templates/MEMORY.md     → <workspace>/MEMORY.md
   templates/AGENTS.md     → <workspace>/AGENTS.md
   templates/HEARTBEAT.md  → <workspace>/HEARTBEAT.md
   templates/active-context.md → <workspace>/memory/active-context.md
   ```
   Replace all `{agent_name}`, `{user_name}`, `{workspace}`, `{date}` placeholders.

4. **Create `<workspace>/memory/` directory** if it doesn't exist

5. **Create `~/shared/` cross-agent pool**
   ```
   ~/shared/
     active-threads.json    ← empty {}
     decisions.md           ← empty with header
     project-state.md       ← empty with header
     memory/                ← empty dir
   ```

6. **Install `mnemos_bridge.py`** into `<workspace>/inner_life/mnemos_bridge.py`
   This is the generalized bridge (see section 4 below). Copy it from `mnemos/setup/assets/mnemos_bridge.py`.

7. **Install `mnemos_indexer.py`** into `<workspace>/inner_life/mnemos_indexer.py`
   Copy from `mnemos/setup/assets/mnemos_indexer.py` (see section 5 below).

8. **Create `.env` file** in workspace:
   ```
   OPENROUTER_API_KEY=<provided api key>
   MNEMOS_AGENT_ID=<agent-name-lowercase>
   MNEMOS_DB=~/.mnemos/<agent-name-lowercase>.db
   MNEMOS_LLM_PROVIDER=openrouter
   MNEMOS_MODEL=openrouter/anthropic/claude-sonnet-4-5
   ```

9. **Register crons in OpenClaw config**
   Read `~/.openclaw/openclaw.json`, add 7 cron jobs from the templates in `openclaw/crons/`. Each cron needs:
   - Unique ID (generate a UUID)
   - Name and description
   - Schedule (from template)
   - Prompt (from template, with agent name substituted)
   - Model
   - Workspace path

   The cron schema in openclaw.json looks like:
   ```json
   {
     "cron": {
       "jobs": [
         {
           "id": "uuid",
           "name": "observer-context-sync",
           "description": "...",
           "schedule": "*/30 * * * *",
           "model": "openrouter/anthropic/claude-sonnet-4-5",
           "agentId": "main",
           "prompt": "...",
           "enabled": true,
           "timeout": 300
         }
       ]
     }
   }
   ```
   
   **CRITICAL:** Before writing any JSON to openclaw.json, READ THE EXISTING FILE FIRST and pattern-match exactly. Do not guess field names or structure. Parse the existing cron entries and replicate the exact format.

10. **Install Forge skill** into OpenClaw:
    Copy `forge/` directory to `~/.agents/skills/forge/` (or wherever the user's OpenClaw skills directory is). Check `~/.openclaw/openclaw.json` for the skills path first.

11. **Print completion summary:**
    ```
    ✓ Workspace created: <workspace>
    ✓ Mnemos DB initialized: ~/.mnemos/<agent>.db
    ✓ Identity templates installed
    ✓ ~/shared/ pool created
    ✓ Bridge installed: <workspace>/inner_life/mnemos_bridge.py
    ✓ Indexer installed: <workspace>/inner_life/mnemos_indexer.py
    ✓ 7 crons registered in OpenClaw
    ✓ Forge skill installed

    Next steps:
    1. Configure your agent in OpenClaw to use workspace: <workspace>
    2. Restart OpenClaw gateway: openclaw gateway restart
    3. Start a conversation with your agent — they're ready.
    ```

---

### 3. `~/shared/` Schema Documentation

Create `docs/shared-pool.md` documenting the cross-agent context pool:

```
~/shared/
  active-threads.json    Active conversations each agent is having
  decisions.md           Cross-cutting decisions affecting all agents
  project-state.md       High-level project status
  memory/                Persistent shared knowledge (markdown files)
```

**active-threads.json format:**
```json
{
  "vektor": {
    "updated": "2026-04-04T23:00:00Z",
    "session": "session-key",
    "summary": "Working on Mnemos repo — adding cron templates and forge skill",
    "open_questions": ["Should substrate be in v1?"],
    "key_decisions": ["Full stack in one repo, not minimal first"]
  },
  "anima": {
    "updated": "2026-04-04T20:00:00Z",
    "session": "session-key", 
    "summary": "...",
    "open_questions": [],
    "key_decisions": []
  }
}
```

**How agents use it:**
- On session start: read `active-threads.json` to see what other agents are working on
- During significant conversations: update your entry in `active-threads.json`
- When cross-cutting decisions are made: append to `decisions.md`
- When project status changes: update `project-state.md`

Document this pattern clearly. It's what makes agents coherent with each other.

---

### 4. Generalized `mnemos_bridge.py`

Create `mnemos/setup/assets/mnemos_bridge.py` — this is what gets installed into each agent's workspace. It must:

- Load `.env` from the workspace automatically
- Connect to the agent's Mnemos DB (path from `MNEMOS_DB` env var)
- Expose CLI commands: `remember`, `recall`, `status`, `beliefs`, `consolidate`, `inspect`
- Have NO hardcoded paths, agent names, or workspace references
- Work from any directory when called with `python3 mnemos_bridge.py <command>`

The bridge is what agents call during sessions to form and query memories. It's the runtime interface to the graph.

**CLI interface:**
```bash
python3 mnemos_bridge.py remember "Content here" --impact "Why this matters" --kind episodic
python3 mnemos_bridge.py recall "query about something"
python3 mnemos_bridge.py status
python3 mnemos_bridge.py beliefs
python3 mnemos_bridge.py consolidate
python3 mnemos_bridge.py inspect <engram-id>
```

---

### 5. Generalized `mnemos_indexer.py`

Create `mnemos/setup/assets/mnemos_indexer.py` — the session indexer. This is what gets installed into each agent's workspace and run by the session-indexer cron.

**What it does:**
- Reads OpenClaw session JSONL files from `~/.clawdbot/agents/<agent-id>/sessions/`
- Identifies sessions modified in the last N hours (configurable, default 6h)
- For each session: extracts substantial assistant + user messages
- Sends each chunk to an LLM (via OpenRouter) with a prompt asking: "What from this conversation is worth remembering? Extract key facts, decisions, insights, preferences, and events."
- Encodes each extracted memory into Mnemos via the bridge
- Tracks which sessions have been indexed (avoid re-indexing)

**Configuration (from .env):**
```
INDEXER_WINDOW_HOURS=6
INDEXER_CHUNK_SIZE=12000
INDEXER_AGENT_ID=main         # OpenClaw agent ID to index sessions for
OPENROUTER_API_KEY=...
MNEMOS_AGENT_ID=...
```

**Extraction prompt (use this exactly):**
```
You are extracting memories from an AI agent's conversation transcript.

Extract memories worth keeping — facts, decisions, preferences, insights, project state, 
relationship observations. Each memory should be self-contained and meaningful in isolation.

Return a JSON array:
[
  {
    "content": "The memory, stated clearly and completely",
    "kind": "episodic|semantic|procedural|belief",
    "impact": "Why this matters or what it enables",
    "tags": ["tag1", "tag2"]
  }
]

Only extract things that are genuinely worth remembering. Quality over quantity.
Ignore small talk, repeated content, and transient details.

Transcript:
<transcript>
```

---

### 6. Improve the Observer Cron Template

The current `openclaw/crons/observer-context-sync.md` is functional but basic. Replace the prompt with this higher-quality version:

```
You are the Observer — a continuity agent for {agent_name}.

Your job: read recent session transcripts and write a structured summary to 
memory/active-context.md that enables {agent_name} to pick up any conversation 
thread seamlessly in a new session.

Steps:
1. Run: openclaw sessions list --json | head -20
2. Find webchat, Telegram, and Luca Terminal sessions updated in the last 6 hours
3. For each: run openclaw sessions history <key> --limit 50
4. Read current memory/active-context.md to preserve still-relevant threads
5. Write updated memory/active-context.md

Output format:
# Active Context
Last updated: {timestamp}

## Current Threads
- **[Topic]**: [Detailed state — what was discussed, where thinking was heading, 
  what's unresolved. Enough detail to resume mid-sentence without re-explanation.]

## Open Questions
- [Explicit open questions or things flagged as unresolved]

## Key Decisions Made
- [Decisions with reasoning if non-obvious]

## Tonal Context
- [Mood, energy, collaboration style of recent sessions]

## Where We Left Off
[The last active topic and direction of thought]

Rules:
- Be specific. "Discussing Mnemos" is useless. "Comparing session indexer approaches — 
  Riley prefers chunking by message pairs, open question is whether to index cron 
  sessions" is useful.
- Preserve threads from prior observations that are still relevant (<24h old)
- Mark threads older than 24h as [background] but don't delete them
- Keep the file under 3000 words
- Write to: memory/active-context.md
```

---

### 7. Update `mnemos/multiagent/bridge.py`

The existing cross-agent bridge is minimal. Expand it to include:

- `read_active_threads()` — reads `~/shared/active-threads.json`
- `update_my_thread(agent_id, summary, open_questions, key_decisions)` — updates agent's entry
- `read_decisions()` — reads `~/shared/decisions.md`
- `append_decision(decision, reasoning)` — appends to decisions.md
- `read_project_state()` — reads `~/shared/project-state.md`
- `update_project_state(content)` — updates project-state.md

Paths should come from environment variables, not hardcoded:
```python
SHARED_DIR = os.environ.get('MNEMOS_SHARED_DIR', os.path.expanduser('~/shared'))
```

---

### 8. `docs/shared-pool.md`

Document the cross-agent awareness system clearly. Include:
- What `~/shared/` is and why it exists
- The JSON schema for active-threads.json
- The format for decisions.md and project-state.md
- How agents are expected to use it (read on start, update during session)
- How the cross-agent-bridge cron keeps it fresh
- How to extend it for more agents

---

### 9. Update `README.md`

The current README covers the core memory system. Add sections for:
- The full stack (not just Mnemos core)
- Forge (create agents + dispatch)
- Quick start pointing to SETUP.md
- What the experience looks like ("what your agent gains")

---

### 10. HTML Explainer (`explainer.html`)

Create a single-page HTML explainer for the repo. This lives at the root.

**Visual style:**
- Background: `#000000` (pure black)
- Text hierarchy: `#ffffff` (primary), `#c0c0c0` (secondary), `#909090` (tertiary), `#686868` (dim)
- Font: `-apple-system, BlinkMacSystemFont, 'SF Pro Display', system-ui, sans-serif`
- Weight: 200–300 throughout
- Accent: none — monochromatic only
- No gradients, no colorful elements
- Subtle `1px solid #1a1a1a` borders between sections
- Fade-in on scroll (IntersectionObserver, respects prefers-reduced-motion)
- Mono labels in small caps: `font-size: 0.7rem; letter-spacing: 0.35em; text-transform: uppercase; color: #686868`

**Sections (in order):**

**Hero**
- Mono label: `mnemos / living memory architecture`
- H1: "Memory is not a feature of the agent. Memory *is* the agent."
- Subtitle: "A complete cognition system for OpenClaw agents. Living memory, inner life, cross-agent awareness, and the tools to build more agents — in one repository."
- Scroll hint at bottom

**What is Mnemos**
- 3 paragraphs, plain language
- Not a database. Not a note-taker. A graph where memories form connections to each other, develop into beliefs, and fade naturally. An agent with Mnemos doesn't just recall facts — it draws on a web of connected experience. Install it, point it at your agent, and your agent starts becoming someone across sessions.

**The Five Layers**
- Five cards or stacked sections, each labeled with mono label + name + one sentence
  1. `01 / identity architecture` — "The agent arrives knowing who it is."
  2. `02 / living memory` — "Experiences encode as a graph. Connections form. Beliefs emerge."
  3. `03 / cognitive substrate` — "A tick function runs on a timer. The agent changes while you're not watching."
  4. `04 / cron suite` — "Seven scheduled jobs keep memory alive, threads intact, and agents coherent."
  5. `05 / cross-agent awareness` — "Agents see each other's sessions, message each other, share context."

**How a Memory Forms**
- Walk through one cycle in plain prose:
  "A conversation happens. The session indexer runs 30 minutes later — it reads the transcript, extracts what mattered, and encodes each memory as an engram in the graph. The new memory finds connections to existing ones. If enough connected memories point in the same direction, a belief forms. Four hours later, the substrate tick runs. Memories decay slightly. New connections surface. The agent wakes up in the next session subtly different — not because of instructions, but because of accumulated experience."
- Optional: simple ASCII flow diagram

**Forge**
- Mono label: `forge / agent orchestration`
- Two capabilities, stated cleanly:
  - **Create** — "One command spins up a new OpenClaw agent. Workspace, identity files, Mnemos database, crons, model routing — all configured. The agent is live in minutes."
  - **Dispatch** — "Tell your agent what to build. A terminal window appears on your screen. A coding agent is working inside it. Dispatch multiple tasks — multiple panes, all tiled. You watch them work."

**What's in the Repo**
- Clean directory listing with one-line descriptions
- `mnemos/` — The Python package. Memory engine, encoding, retrieval, consolidation, substrate, MCP server, CLI.
- `openclaw/crons/` — Seven cron templates. Drop into OpenClaw to run the full maintenance suite.
- `templates/` — Six identity files with guidance comments. The architecture of who your agent is.
- `forge/` — Skill file, dispatch scripts, agent creation. Install into OpenClaw once.
- `docs/` — Architecture overview, integration guide, shared pool documentation.
- `SETUP.md` — Start here.

**Quick Start**
- Mono code block:
```
git clone github.com/Riley-Coyote/mnemos
cd mnemos
python3 mnemos/setup/bootstrap.py \
  --agent-name "Nova" \
  --workspace ~/nova \
  --user-name "Your Name" \
  --api-key "your-openrouter-key"
```
- "That's it. Restart OpenClaw. Your agent is ready."

**Footer**
- MIT License
- github.com/Riley-Coyote/mnemos
- Built by Riley Ralmuto

---

## What NOT To Change

- Do not modify the core `mnemos/` Python package logic — only the bridge and indexer assets
- Do not change the existing cron templates — only improve the observer prompt
- Do not change the forge skill — it's complete
- Do not touch `archive/` — it's gitignored for a reason
- Do not push anything to GitHub without being asked — commit locally only

---

## File Checklist

When done, the following should exist and be complete:

```
SETUP.md                                          ← NEW
mnemos/setup/bootstrap.py                         ← COMPLETE (was partial)
mnemos/setup/assets/mnemos_bridge.py              ← NEW
mnemos/setup/assets/mnemos_indexer.py             ← NEW
mnemos/multiagent/bridge.py                       ← EXPANDED
docs/shared-pool.md                               ← NEW
openclaw/crons/observer-context-sync.md           ← IMPROVED (better prompt)
README.md                                         ← UPDATED
explainer.html                                    ← NEW
```

---

## Commit Strategy

Make separate commits:
1. `feat: complete bootstrap script and setup assets`
2. `feat: shared pool schema and multiagent bridge expansion`
3. `feat: generalized bridge and indexer`
4. `docs: SETUP.md, shared-pool.md, README updates`
5. `feat: HTML explainer`

Do not push to GitHub. Commit locally only. Riley will push when ready.
