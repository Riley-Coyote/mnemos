# Mnemos 0.2.0 — distribution readiness

A verification pass before public release, run because this codebase's failure
mode is *"a layer reporting success while carrying nothing"* — so readiness had
to be shown behaviorally, on a real install, not asserted from a green suite.

**Verdict: ready.** `pip install mnemos-continuity`, no extras and no API key,
yields a working server whose eight simple tools list over the real stdio
protocol, a capture survives a process restart, and all five philosophical
shifts are alive on a keyless store. What a verification pass is *for*, though,
is finding what's wrong — and it found five real issues, now fixed.

Reproduce any of this with `uv run --extra dev --extra mcp python
scripts/readiness_check.py` (the wheel + mind-state proof) and `pytest -q`.

## What a fresh install actually gives you

- `pip install mnemos-continuity` pulls three small deps (`mcp<2`, `ulid-py`,
  `python-dotenv`); no extras are needed to run. Retrieval is keyword-only
  until you add `[embeddings]`; no hosted model is involved.
- The server starts in **simple mode** by default — eight tools, verified
  listing over real stdio from the *installed wheel*, not just in-process.
- Continuity arrives without being asked: server instructions on every MCP
  client, plus a SessionStart hook that injects the packet before the first
  turn.

## The five shifts, measured on a keyless store

From `scripts/readiness_check.py`, no provider configured:

| Shift | Signal | Result |
|---|---|---|
| 1 · traces, not records | impact coverage (non-templated) | **89%** |
| 2 · forgetting that teaches | `distilled_into` edges | **6** |
| 3 · surprise as growth | prediction error on a novel capture | **0.6** |
| 4 · resonance, not search | edge kinds / `supports` share | **2 kinds / 0% supports** |
| 5 · identity from the graph | identity rows | **1** |

All five are alive with no API key — the claim the explainer makes, now true on
the default install.

## Issues the pass found and fixed

Each landed with a test proven to fail on the prior code (stash/run/restore),
CI green on Python 3.10–3.13.

- **`forget` didn't forget** (#28) — a forgotten memory was read back to the
  agent, on both delivery paths, from frozen copies no deletion reached. One
  rule now: no packet block renders a frozen copy of note text.
- **`reflect` didn't reach the agent** (#29) — the agent's own answer landed
  only on the engram, which the packet excludes; now it lands in the continuity
  note the packet reads.
- **softening could erase** (#30) — with no model it truncated memories with no
  way back; now it leaves the words and fades in ranking, and `mnemos
  repair-softening` restores older damage.
- **identity reported bookkeeping** (#32) — "persistent concerns" were
  classifier/indexer tags; now only genuine concerns surface, or the line is
  omitted.
- **`pip install` produced a dead server** (#31) — `mcp` was unbounded and
  resolved 2.0, which removed the module every entrypoint imports; bounded to
  `<2`, guarded by a unit test and an unlocked wheel-smoke job.

Plus, from this readiness pass specifically (#36, #37):

- **old-store upgrades** — a store from before 0.2 columns raised
  `OperationalError` at open; a column reconciler now upgrades it in place.
- **the hook could hang** — a bare `sys.stdin.read()` blocked session start
  forever if a harness held stdin open; now a non-blocking drain.
- **the packet leaked engrams across person/project** — the advanced
  `mnemos_context` filtered engrams by agent only; both read paths now share one
  scope check.
- **doctor created the store it inspected** — now it reports without minting a
  phantom store.
- **legacy cron scheduled the indexer flood** — removed from both generators,
  matching the turnkey scheduler.

## Honest limits (state these in the launch copy)

- **Without a provider, the graph is honestly labelled, not richly typed.**
  Edges the encoder can't judge are `co_activated` — *these came up together* —
  not guessed into `supports`/`contradicts`. Semantic relations are the one
  thing a configured model still buys.
- **Beliefs and identity are agent-level, not person-level.** Usage is
  solo-per-agent; if one agent is ever shared across people, that scoping is the
  next question.
- **Existing damaged stores don't self-heal silently.** Memories a pre-0.2
  softening truncated are restorable with `mnemos repair-softening`, surfaced by
  `mnemos doctor` — but the user runs it.

## Coverage boundary

Verified: the fresh-wheel install journey over real stdio, the five shifts on a
keyless store, cross-process continuity and cross-agent isolation, old-store
migration, and 332 tests on 3.10 and 3.13. Not exercised here: provider-backed
maintenance, embeddings/semantic recall at scale, and multi-person-per-agent
(out of scope by design). Numbers above are from a small scripted store; they
demonstrate the shifts fire, not production-scale behavior.
