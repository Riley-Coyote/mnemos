# Substrate Tick

Runs the cognitive substrate consolidation cycle — the agent's "subconscious processing."

## Cron Definition

| Field | Value |
|-------|-------|
| **Schedule** | `0 */4 * * *` (every 4 hours) |
| **Model** | default agent model |
| **Timeout** | 300s |
| **Session Target** | `isolated` |

## Purpose

Runs the substrate consolidation cycle which includes:
- **Decay**: Unused memories lose accessibility over time while S0 stays fixed
- **Connection Discovery**: Find new semantic relationships between memories
- **Belief Review**: Challenge stagnant beliefs with new evidence
- **Event Cascade**: Handlers fire for significant events (dreaming, reflection, insight, etc.)
- **Modulator Update**: Recalculate emotional modulators (arousal, openness, resolution, etc.)

This is automated consolidation — it does not encode new memories, just processes existing ones.

## Prompt

```
Run a substrate tick. Execute:
cd {workspace} && python3 -m mnemos.substrate.tick
Then report the summary (events produced, handled, decayed, modulators).
If any handlers fire or beliefs change, note what happened.
This is automated consolidation — do not encode new memories, just run the tick and report.
```

## Notes

- The substrate tick is offset from other crons to avoid resource contention.
- Event handlers may fire based on what the tick discovers:
  - `BELIEF_CONTRADICTED` → reflection handler
  - `MEMORY_SOFTENED` → dreaming handler
  - `CONNECTION_DISCOVERED` → insight handler
  - `SILENCE_EXTENDED` → wandering handler
  - `SALIENCE_ACCUMULATED` → initiation handler
- Modulators (arousal, openness, resolution, selection_threshold, temperature) influence
  subsequent retrieval and encoding behavior.
- The tick has safety limits: `max_engrams_per_tick`, `max_cascade_depth`, throttles for
  dreams and wanderings per week.
