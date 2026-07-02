# Mnemos Architecture

## Overview

Mnemos is a complete agent cognition system, not just a memory library. It operates in five layers that together give an AI agent persistent identity, living memory, autonomous maintenance, and cross-agent awareness.

```
┌─────────────────────────────────────────────────────────┐
│                    Agent Sessions                        │
│              (user conversations, tasks)                 │
├─────────────────────────────────────────────────────────┤
│                  Identity Architecture                   │
│         SOUL.md  IDENTITY.md  MEMORY.md  AGENTS.md      │
├─────────────────────────────────────────────────────────┤
│                     Cron Suite                           │
│     Observer  Indexer  Substrate  Maintenance  Bridge    │
├─────────────────────────────────────────────────────────┤
│                   Mnemos Core                            │
│      Engrams  Connections  Beliefs  Consolidation        │
├─────────────────────────────────────────────────────────┤
│                    Substrate                             │
│     Decay  Dreaming  Reflection  Modulators  Events     │
├─────────────────────────────────────────────────────────┤
│                 Cross-Agent Layer                        │
│        Shared Pool  Bridge  Federation  Attestation      │
└─────────────────────────────────────────────────────────┘
```

## Layer 1: Mnemos Core (Graph Memory)

The foundation. A living memory graph backed by SQLite.

### Engrams

The fundamental unit of memory. Each engram has:

- **Content**: What happened (mutable through reconsolidation)
- **Impact**: Why it matters (the lasting insight)
- **Dual-trace model**: Strength, stability, accessibility — three independent dimensions
- **Kind**: Episodic (experiences), semantic (facts), procedural (how-to), prospective (future-directed)
- **Confidence**: Scored by source reliability (user-explicit → speculative)
- **State lifecycle**: Active → consolidating → dormant → archived
- **Resolution**: High → low (details fade through softening, like human memory)
- **Full version history**: Every reconsolidation is tracked
- **PAI import controls** (schema v4/v5): `decay_protected`, `softening_protected`,
  `consolidation_authorized`, `voice_exemplar_eligible`, `original_substrate`,
  `original_timestamp`. Decay skips `decay_protected` rows, softening skips
  `softening_protected` rows and filters voice exemplars through
  `voice_exemplar_eligible`, and consolidation/substrate mutation paths require
  `consolidation_authorized` so imported identity material is not silently
  rewritten by background maintenance.
- **Afferent Membrane controls** (schema v6/v7): producer-fed rows carry
  `read_visibility` (`operational_context`, `review_only`, or `audit_only`).
  Operational retrieval, packets, consolidation, substrate producers, and
  modulators read only `operational_context` rows by default; review APIs opt
  in explicitly. Stable promotion candidates and identity/foundational live
  hypomnema writes classify to `review_only` by default, while the bare
  hypomnema SQL default stays `operational_context` for legacy compatibility;
  omitted-visibility callers still pass through the write-time classifier
  before ordinary use. Proposed
  durable transitions are tracked in
  `proposal_ledger` with authority, target surface, transition, blast radius,
  visibility, status, reason, gate version, provenance, and payload fields.
- **PAI coordinate guard**: before imported text becomes retrievable rows, the
  splitter strips Strict-B eigenvalue, vivezza, coordinate-target, and
  persona-signature tuple-value lines from any source kind. These values are
  runtime steering substrate; Mnemos imports the surrounding prose, not the
  coordinate values.

### Connections

Typed edges between engrams that form the memory graph:

- `supports`, `contradicts`, `causes`, `extends`, `parallels`, `synthesizes`, `grounds`
- Connections have strength that evolves through co-retrieval and consolidation
- Connection discovery runs automatically during consolidation

### Beliefs

Higher-order knowledge structures extracted from patterns across engrams:

- Confidence tracking with tier-based change detection
- Domain categorization (engineering, social, preferences, etc.)
- Stagnant beliefs get stress-tested during deep consolidation
- Full revision history
- PAI import metadata: `tier` (foundational | operational | tactical),
  `needs_review`, and `confidence_pending_review` mark beliefs whose
  upstream source has changed but has not been re-reviewed by the operator.
  Default `get_beliefs()` consumers exclude pending-confidence beliefs;
  belief review opts in so it can resolve them, clear the pending flags, and
  move approved rows back to operational read visibility.

### Encoding Pipeline

Content → LLM classification → engram creation → connection discovery → embedding (optional)

### Retrieval

Cue → read-visibility prefilter → FTS5 search + embedding similarity → graph
activation/scoring → reconsolidation → results

Operational retrieval uses `read_visibility="operational_context"` before FTS,
embedding hits, graph propagation, scoring, and reconsolidation. Explicit
review callers can request `review_only`; audit-only rows stay out of normal
review flows.

Every retrieval updates the returned visible memory — access count, strength,
new connections. Operational paths return operational rows. Memories are living
traces.

### Operational Context And Review

Context packets have two modes:

- `operational`: the default for agents. It includes normal functional memory,
  hypomnema, beliefs, and engrams, plus review counts/source IDs with prose
  withheld.
- `review`: the explicit operator surface. It can include review-only
  confirmation and promotion-candidate prose with labels and provenance cues.

`proposal_ledger` holds candidate durable transitions separately from the
operational packet so generated or review-shaped material can be audited before
it becomes operating context. Audit-only proposal rows require the explicit
`mnemos_proposal_audit` admin/audit surface and do not appear in ordinary
operational or review packets.

## Layer 2: Substrate (Inner Life / Consolidation)

The "sleeping brain" — autonomous processing that runs between sessions.

### Consolidation Cycle

1. **Decay**: Recalculate strength/stability/accessibility. Unused memories fade.
2. **Connection Discovery**: Find new semantic relationships between engrams.
3. **Softening**: LLM-mediated lossy compression. Low-resolution memories get rewritten preserving essence.
4. **Belief Review**: Challenge stagnant beliefs with new evidence.
5. **Reflection**: Generate thoughts, curiosity questions, narrative self-summary.

### Event System

The substrate produces events based on what it discovers:

| Event | Trigger | Handler |
|-------|---------|---------|
| `MEMORY_SOFTENED` | Vividness below threshold | Dreaming |
| `CONNECTION_DISCOVERED` | New semantic link | Insight |
| `BELIEF_CONTRADICTED` | Confidence crosses tier down | Reflection |
| `BELIEF_CONFIRMED` | Confidence crosses tier up | — |
| `SILENCE_EXTENDED` | No memories in 6+ hours | Wandering |
| `SALIENCE_ACCUMULATED` | Multiple high-salience events | Initiation |

### Modulators

Six emotional modulators that influence retrieval and encoding:

- **Arousal**: Overall activation level
- **Openness**: Willingness to form new connections
- **Resolution**: Detail preservation threshold
- **Selection threshold**: How strong a memory must be to surface
- **Temperature**: LLM creativity parameter
- **Surprise sensitivity**: Threshold for surprise detection

Modulators are recalculated every substrate tick based on recent activity.

## Layer 3: Cron Suite (Sensory System)

The agent's autonomous processes — the things that happen in the background to keep the agent alive between sessions.

| Cron | Schedule | Purpose |
|------|----------|---------|
| **Observer** | Every 30 min | Reads session transcripts → updates active-context.md |
| **Session Indexer** | Every 30 min | Extracts memories from conversations → encodes into graph |
| **Substrate Tick** | Every 4 hours | Runs consolidation cycle (decay, dreaming, beliefs) |
| **Memory Maintenance** | Every 6 hours | Reviews sessions → updates MEMORY.md |
| **Cross-Agent Bridge** | Every 2 hours | Syncs context between agents |
| **Morning Brief** | Daily 10 AM | Generates daily summary and priorities |
| **Daily Debrief** | Daily 5 AM | End-of-day recap and handoff |

These crons run as isolated OpenClaw sessions — they don't interfere with active conversations.

### Data Flow

```
User Session → transcript.jsonl
     ↓
Session Indexer (every 30 min)
     ↓ extracts memories
Mnemos Graph ←─── Substrate Tick (every 4h)
     │                    ↓ produces events
     │              Event Handlers (dreaming, reflection, insight)
     │                    ↓ may create new engrams
     ↓
Observer (every 30 min) → active-context.md
     ↓
Memory Maintenance (every 6h) → MEMORY.md
     ↓
Morning Brief (daily) → daily/morning-brief-{date}.md
```

## Layer 4: Identity Architecture (Persistent Self)

The files that define who the agent is. Together they form a complete identity that persists across sessions.

| File | Purpose | Update Frequency |
|------|---------|-----------------|
| **SOUL.md** | Essence, personality, philosophy, voice | Rarely (manual) |
| **IDENTITY.md** | Role, capabilities, boundaries, protocols | Occasionally (manual) |
| **MEMORY.md** | Living memory — facts, projects, patterns | Every 6 hours (cron) |
| **AGENTS.md** | Multi-agent topology and protocols | Rarely (manual) |
| **HEARTBEAT.md** | Health monitoring configuration | Rarely (manual) |
| **active-context.md** | Current threads, open questions, where we left off | Every 30 min (cron) |

### Session Startup

When an agent starts a new session, it loads:

1. **SOUL.md** → knows who it is
2. **IDENTITY.md** → knows what it can do
3. **MEMORY.md** → knows what it knows
4. **active-context.md** → knows what's happening right now
5. **cross-agent-context.md** → knows what other agents are doing

This gives the agent complete continuity across session boundaries.

## Layer 5: Cross-Agent Infrastructure (Multi-Agent Awareness)

Enables multiple agents to work together without direct communication.

### Shared Memory Pool

- Dedicated database (`~/.mnemos/shared.db`) accessible to all agents
- Agents publish memories with visibility controls (private/shared/public)
- Conflict resolution when agents disagree (confidence > strength > recency)
- Relationship and trust tracking between agents

### Cross-Agent Bridge

- Reads each agent's `active-context.md`
- Writes per-agent summaries to shared directory
- Generates combined `cross-agent-context.md`
- Distributes combined context back to each agent's workspace

### Federation (Planned)

- Cross-instance memory synchronization
- Selective memory sharing across network boundaries

### Attestation (Planned)

- Cryptographic provenance for shared memories
- Trust verification across federated instances

## How They Connect

The five layers form a feedback loop:

1. **User talks to agent** → session transcript created
2. **Session Indexer** extracts memories → stored in **Mnemos Core**
3. **Substrate** consolidates memories → events trigger handlers → new insights
4. **Observer** reads transcripts → updates **active-context.md** (Identity layer)
5. **Memory Maintenance** reviews sessions → updates **MEMORY.md** (Identity layer)
6. **Cross-Agent Bridge** syncs context → other agents gain awareness
7. **Next session** loads identity files → agent has full continuity
8. **Morning Brief** synthesizes everything → user starts day with context

The system is designed to be self-maintaining. Once bootstrapped, the cron suite keeps everything current without manual intervention. The agent's memory grows, consolidates, and evolves autonomously.
