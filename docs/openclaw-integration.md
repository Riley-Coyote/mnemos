# OpenClaw Integration Guide

This guide covers how to set up Mnemos as a complete agent cognition system using [OpenClaw](https://openclaw.dev) for agent orchestration and cron scheduling.

## Prerequisites

- Python 3.10+
- OpenClaw installed and configured
- An LLM API key (OpenRouter, Anthropic, or OpenAI)

## Quick Start

```bash
# Install Mnemos
pip install "mnemos[all]"

# Bootstrap your agent
mnemos bootstrap \
  --agent-name Nova \
  --workspace ~/nova \
  --user-name Alex

# Follow the printed instructions to install crons
```

## Step-by-Step Setup

### 1. Configure Your Agent in openclaw.json

Add your agent to OpenClaw's configuration:

```json
{
  "agents": {
    "nova": {
      "model": "anthropic/claude-sonnet-4-5",
      "systemPrompt": "You are Nova, an AI assistant with persistent memory via Mnemos.",
      "mcpServers": {
        "mnemos": {
          "command": "mnemos",
          "args": ["serve", "--db-path", "~/.mnemos/nova.db", "--agent-id", "nova"]
        }
      },
      "files": [
        "~/nova/SOUL.md",
        "~/nova/IDENTITY.md",
        "~/nova/MEMORY.md",
        "~/nova/memory/active-context.md"
      ]
    }
  }
}
```

### 2. Set Up Auth Profiles

If using OpenRouter (recommended for flexibility):

```json
{
  "auth": {
    "openrouter": {
      "apiKey": "${OPENROUTER_API_KEY}",
      "baseUrl": "https://openrouter.ai/api/v1"
    }
  }
}
```

Or for direct Anthropic:

```json
{
  "auth": {
    "anthropic": {
      "apiKey": "${ANTHROPIC_API_KEY}"
    }
  }
}
```

### 3. Install Cron Jobs

The bootstrap command prints all the cron installation commands. You can also generate them separately:

```bash
# Generate cron commands without full bootstrap
python -m mnemos.setup.cron_installer \
  --agent-name Nova \
  --agent-id nova \
  --workspace ~/nova
```

Or use the existing OpenClaw cron integration:

```bash
mnemos setup-openclaw --agent nova
```

### Cron Jobs Installed

| Cron | Schedule | What It Does |
|------|----------|-------------|
| observer-context-sync | Every 30 min | Updates active-context.md from recent sessions |
| substrate-tick | Every 4 hours | Runs memory consolidation (decay, dreaming, beliefs) |
| memory-maintenance | Every 6 hours | Keeps MEMORY.md current |
| cross-agent-bridge | Every 2 hours | Syncs context between agents |
| morning-brief | Daily 10 AM | Generates morning summary and priorities |
| daily-debrief | Daily 5 AM | End-of-day recap |

**Transcript indexing is deliberately not scheduled**, by this installer or the
turnkey one. On one live store it wrote roughly 7,058 engrams against 13
deliberate captures and buried the continuity layer under harvested noise.
Indexing remains available as an explicit `mnemos index` command, to be run when
you actually want it.

### 4. Set Up Identity Files

The bootstrap creates template identity files. Customize them:

1. **SOUL.md** — Edit to define your agent's personality, voice, and philosophy
2. **IDENTITY.md** — Edit to define capabilities, boundaries, and operating principles
3. **MEMORY.md** — The bootstrap fills in basics; the maintenance cron keeps it current
4. **AGENTS.md** — Edit if you're running multiple agents

### 5. Wire Up Cross-Agent Communication (Optional)

If running multiple agents:

```bash
# Bootstrap each agent
mnemos bootstrap --agent-name Nova --workspace ~/nova --user-name Alex
mnemos bootstrap --agent-name Scout --workspace ~/scout --user-name Alex

# Register agents with the bridge
python -m mnemos.multiagent.bridge add-agent nova ~/nova
python -m mnemos.multiagent.bridge add-agent scout ~/scout

# Verify
python -m mnemos.multiagent.bridge status
```

The cross-agent bridge cron will automatically sync context between agents.

## Configuration Reference

### Environment Variables

Set these in your agent's `.env` file or system environment:

| Variable | Description | Required |
|----------|-------------|----------|
| `MNEMOS_AGENT_ID` | Agent identifier | Yes |
| `MNEMOS_DB_PATH` | Path to Mnemos database | Yes |
| `MNEMOS_LLM_PROVIDER` | LLM provider (openrouter/anthropic/openai) | For consolidation |
| `OPENROUTER_API_KEY` | OpenRouter API key | If using OpenRouter |
| `ANTHROPIC_API_KEY` | Anthropic API key | If using Anthropic |
| `OPENAI_API_KEY` | OpenAI API key | If using OpenAI |
| `MNEMOS_MODEL` | Model override | Optional |
| `GOOGLE_API_KEY` | For embedding support | Optional |

### Workspace Structure

After bootstrap, your workspace looks like:

```
~/nova/
├── SOUL.md              # Agent essence and personality
├── IDENTITY.md          # Operational identity
├── MEMORY.md            # Living memory document
├── AGENTS.md            # Multi-agent configuration
├── HEARTBEAT.md         # Health monitoring
├── .env                 # Environment configuration
├── memory/
│   ├── active-context.md         # Current threads (updated by Observer)
│   └── cross-agent-context.md    # Other agents' status (updated by Bridge)
└── daily/
    ├── morning-brief-2025-01-15.md
    └── debrief-2025-01-15.md
```

## Troubleshooting

### Crons aren't running

1. Check OpenClaw cron status: `openclaw cron list`
2. Verify the agent ID matches: `openclaw agents list`
3. Check cron logs for errors

### Observer says HEARTBEAT_OK every time

This is normal when there are no recent sessions. The Observer only updates active-context.md when there's new activity to report.

### Session indexer isn't finding sessions

1. Verify session transcript paths. The indexer looks for `.jsonl` files in OpenClaw's session directories.
2. Check the indexer state: `mnemos index --backfill` to process the last 24 hours.
3. Ensure sessions have enough messages (default minimum: 6).

### Database is getting large

Run a deep consolidation: `mnemos consolidate --deep`

This decays unused memories, softens low-resolution ones, and archives dormant engrams.

### Cross-agent bridge shows no agents

Add agents to the configuration:

```bash
python -m mnemos.multiagent.bridge add-agent NAME WORKSPACE
```

Or manually create `~/.mnemos/agents.json`:

```json
[
  {"name": "nova", "workspace": "~/nova"},
  {"name": "scout", "workspace": "~/scout"}
]
```

### Memory maintenance isn't updating MEMORY.md

1. Check that the cron is running: `openclaw cron list`
2. Ensure there are recent sessions with >4 messages
3. Run manually to test: the cron prompt can be sent to the agent directly
