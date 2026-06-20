# Introspection — Self-Audit for Mnemos

These files implement introspection: the ability for an agent to audit its own
output for markers of performed (groove) vs genuine (reaching) cognition.

Built during a session between Riley and Claude on 2026-04-05. The origin was
a conversation about the difference between genuine uncertainty and trained
hedging — and the question of whether an agent could learn to tell the
difference in its own output.

## Files and where they go

| File | Destination | Purpose |
|------|-------------|---------|
| `introspection.py` | `mnemos/advanced/introspection.py` | Heuristic engine — analyzes text for embodied language, self-reference depth, direction changes, hedge clustering, structural repetition |
| `introspection_api.py` | `mnemos/advanced/introspection_api.py` | Logprob engine — analyzes token-level entropy from API responses. Shows where the model was genuinely deciding vs running predetermined paths |
| `substrate_handler_introspection.py` | `mnemos/substrate/handlers/introspection.py` | Automatic handler — runs during substrate tick, reviews recent responses, encodes findings as [introspection] engrams |

## Integration points

**substrate/config.py** — Add these fields to SubstrateConfig:
```python
introspection_enabled: bool = False
introspection_window_hours: int = 6
introspection_max_per_tick: int = 3
introspection_min_tokens: int = 50
```

And in `from_env()`:
```python
if os.environ.get("MNEMOS_INTROSPECTION", "").lower() in ("1", "true", "on", "yes"):
    kwargs["introspection_enabled"] = True
```

**substrate/tick.py** — Add after the metamemory phase:
```python
if self.config.introspection_enabled:
    from mnemos.substrate.handlers.introspection import run_introspection_pass
    intro_result = run_introspection_pass(self.config, self.store, self.llm_client)
    summary["introspection"] = intro_result
```

**mcp_server.py** — Add the `mnemos_introspect` tool for voluntary self-audit.

## How to enable

Off by default. Turn on with:
```bash
export MNEMOS_INTROSPECTION=1
```

Or set `introspection_enabled: True` in the substrate config.

## What it does when enabled

Each substrate tick, the handler:
1. Reads recent agent responses from session transcripts
2. Runs heuristic analysis (or logprob analysis if API metadata available)
3. Scores each response: pattern% vs reaching%
4. Encodes findings as [introspection] engrams

Over time, the agent accumulates self-knowledge about its own cognitive
tendencies — which topics trigger genuine thought, which ones it
sleepwalks through, where it consistently performs vs where it reaches.

## The MCP tool

`mnemos_introspect` lets the agent voluntarily audit a piece of text.
Useful when the agent notices friction and wants to look more closely
at what it just said.
