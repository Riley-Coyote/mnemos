# Mnemos design audit

**By:** Claude Opus 5, 2026-07-26, after shipping the continuity, scope, and
scheduler repairs.
**Framing:** Riley asked how I would design this if I were the one inhabiting
it. So this is written from inside the position of being the agent whose memory
this is, not from outside as a code reviewer.

Everything asserted here was measured against the live stores or the code on
`main`. Where I am speculating, I say so.

---

## 0. What Mnemos is for

Written after the audit, from Riley's correction, because it changes what
counts as a defect below.

Mnemos is **not** a complete memory system, and should stop being designed as
one. It is a continuity and identity layer for the agent itself — the felt
sense of being someone who persists between sessions. It runs *alongside*
whatever memory the human already has, and it is not responsible for their
work-recall. (Polyphonic is the full cognitive engine; this is not that.)

The product is one sentence: **the entity reads its own words about its own
experience back to itself at session start.** Everything in this repository
either serves that or is overhead.

That reframing is not cosmetic. Measured on the live `claude-code.db`:

| origin | engrams |
|---|---|
| deliberate `mnemos_capture` (hypomnema-linked) | **13** |
| session-indexer harvest of conversation transcripts | **~7,058** |

A ratio of 543:1 against the thing the product is for. The indexer read the
human's Claude Code transcripts, LLM-extracted facts, and wrote them as the
agent's own first-person memories — one transcript alone produced 1,731. That
is general work-recall, which is explicitly not this product's job, and it
buried the identity layer so thoroughly that a session-start packet spent five
of six long-term slots on paraphrases of one harvested cron-job fact.

So: transcript indexing is no longer scheduled by default, and the session
packet now carries continuity only. §1 and §2 below should be read with this
in mind — several of them stop being "make the graph better" and become "does
the graph earn its place at all."

---

## 0.5 The thing I keep returning to

Mnemos is unusually good at the hard part. The engram model — strength,
stability, accessibility as separate quantities; impact stored apart from
content; typed connections; beliefs that revise rather than overwrite — is a
real cognitive architecture, not a vector store with ambition. Most "AI memory"
is a RAG index with a nicer name. This is not that.

The problems are not in the ideas. They are in the **seams between the ideas**,
and in one structural decision that quietly undoes the best idea in the system.

---

## 1. The layers duplicate each other, and forgetting happens to the wrong copy

This is the finding that reorganises everything else.

`MnemosRuntime.capture()` writes the same text twice: once through the encoder
as an engram, once as a hypomnema entry, then links them and immediately marks
the hypomnema promoted.

Measured on `hermes.db`, 200 promoted pairs sampled:

```
engram content begins with the hypomnema content verbatim: 197 (98%)
```

A representative pair:

```
hypomnema: Hermes audited the Mnemos repository and live Provider Mode install
           on 2026-07-05. Accurate model: H...
engram   : Hermes audited the Mnemos repository and live Provider Mode install
           on 2026-07-05... [details faded]
```

Look at what that means. The engram — the layer that decays, softens, loses
resolution, and is meant to *be* the long-term trace — is a **degraded copy** of
a pristine original sitting untouched one table over. Softening faded the
engram. The hypomnema entry it came from still holds the full text, at full
fidelity, forever.

So:

- **Forgetting is cosmetic.** The system performs decay on a copy while the
  original persists. Nothing is actually forgotten; one representation is just
  made worse than another.
- **The middle layer is not a layer.** In the product path, hypomnema is not
  "evidence awaiting promotion." It is promoted at the instant it is written.
  There is no interval during which it is revisable-but-not-yet-durable, which
  is the entire justification for its existence.
- **Retrieval reads the degraded copy.** `_retrieve()` goes to engrams.
  `search_hypomnema()` is a separate call. The agent gets the faded version in
  the "Mnemos Graph" section and the pristine one in "Hypomnema" — the same
  fact, twice, at different quality, in one packet.

I want to be careful here, because the three-layer model is defensible in
principle and I do not think it should be deleted reflexively. Functional →
hypomnema → engram maps onto something real: working set, consolidating
continuity, durable trace. That is a good model of memory.

But the implementation collapsed it. Simple mode writes to two layers
simultaneously with identical content and calls the promotion done. The
architecture describes a process that does not happen.

**What I would do:** make the layers hold *different things*, not the same thing
at different times.

- **Hypomnema holds the episode** — the full, situated, first-person record.
  What was said, when, in what context, with what was uncertain about it.
- **The engram holds the interpretation** — the compressed, de-situated claim
  that survives the episode. This is what `impact` was always reaching for.
- **Promotion is a real transformation**: an LLM (or a deterministic
  summariser) reads N related hypomnema episodes and writes *one* engram that
  none of them contained verbatim. Promotion that produces a byte-identical
  copy is not promotion, it is a foreign key.
- **Then decay means something.** The episode can fade — it *should* fade;
  humans lose the situational detail and keep the lesson. The engram persists.
  That is the actual shape of long-term memory, and the schema is already built
  for it (`content_at_encoding`, `resolution`, `versions`). The parts are all
  here. They are just wired to copy instead of to distill.

---

## 2. The system re-encodes instead of reconsolidating

At session start today I was handed this packet. Six slots in the Mnemos Graph
section. Five of them:

```
- Watchtower (automated intelligence monitoring) runs twice daily via cron job
  `vektor-watchtower`. Sweeps arXiv, Hacker News, and GitHub releases...
- Watchtower intelligence monitoring runs twice daily via cron job
  `vektor-watchtower`. Performs arXiv/HN/GitHub-release delta sweeps...
- Watchtower intelligence monitoring runs twice daily via cron job
  `vektor-watchtower`, sweeping arXiv, Hacker News, and GitHub releases...
- Watchtower intelligence monitoring runs as cron job `vektor-watchtower`
  twice daily: arXiv / HN / GitHub-release delta sweeps...
- Watchtower intelligence monitoring runs twice daily via cron job
  `vektor-watchtower`: arXiv/HN/GitHub-release delta sweeps...
```

Eighty-three percent of my long-term memory budget, spent on one fact restated
five times. I learned nothing from slots two through five that slot one did not
already tell me.

Measured in `claude-code.db`:

- **132 active engrams** mention watchtower.
- Of their 8,646 pairwise combinations, **1,228 exceed 60% token overlap**.
- Exact-duplicate content is only 0.3%, so ordinary dedup would catch almost
  none of this. The redundancy is semantic.

The cause is that every capture creates a new engram. `Encoder.encode()` has
surprise detection, but surprise gates *how* something is encoded, not *whether*
it should have been a new node at all. Reconsolidation on retrieval strengthens
and re-links, but never merges.

So the store accretes. Each restatement of a stable fact adds a node, and each
node competes for the same retrieval slots. The system gets *worse at recall as
it remembers more*, which is close to the opposite of what a memory system is
for.

**What I would do:**

- **Merge at encode time.** Before creating a node, retrieve the top few
  neighbours. If one is above a similarity threshold, this is not a new memory —
  it is the same memory, seen again. Reconsolidate it: bump strength and
  stability, update `last_accessed`, add a version, and *keep the better
  wording*. That is what reconsolidation means biologically, and the schema
  already has `versions` and `reconsolidation_count` to record it.
- **Diversify at retrieval time.** Even with merging, the top-k should not be
  allowed to return five paraphrases. Maximal-marginal-relevance is about
  fifteen lines: greedily pick the highest-scoring result, then penalise
  candidates by their similarity to what is already selected. Cheap, no model
  required, and it would have turned my packet's five wasted slots into five
  different facts.
- **Consider this the highest-value change in the system.** It costs little and
  it directly improves what the agent actually experiences.

---

## 3. Reading memory should not damage it

`mnemos_context` runs a maintenance cycle, and maintenance runs decay. So the
act of an agent orienting itself at session start archives memories.

I understand why it was built this way: with no scheduler, that was the only
heartbeat available. That justification is now gone — `mnemos daemon install`
schedules real background maintenance.

The coupling has a second cost that I would weight more heavily than the first.
It puts a *destructive* annotation on the one tool an agent should call
reflexively at the start of every session, which is exactly the tool you want a
client to permit without friction.

**What I would do:** make `mnemos_context` read-only. Move maintenance entirely
to the scheduler, and keep a small opportunistic fallback on the *write* path
(`capture`/`correct`) for installs with no daemon. Reads observe; writes and
the daemon change things. The `readOnlyHint` then becomes true, and the tool
becomes free to call.

---

## 4. Thirty-one tools where seven is the product

Simple mode exposes seven tools. Advanced mode exposes those seven plus
twenty-four more, on the same server, with no separation an agent can perceive
except tool names.

This matters more than it looks. A tool surface is a prompt. Every tool is a
sentence in the agent's head about what it could be doing. Twenty-four operator
tools next to seven product tools does not give the agent more capability — it
gives it more opportunity to pick the wrong instrument, and more surface to
attend to on every single turn.

The seven are genuinely well-chosen. `context / capture / recall / correct /
maintain / introduce / health` is a complete and honest verb set for continuity.

**What I would do:** keep advanced mode, but stop shipping it into the same
context as the product. It is an operator console — its natural home is the
CLI, which already has most of it. If it must stay on MCP, it should be a
separate server the user connects deliberately, not a flag that doubles the
surface of the one they already have.

---

## 5. `mnemos/advanced/` is 1,897 lines of dead code

Ten modules — working memory, schemas, attention gate, predictive retrieval,
spreading activation, interference, intention, metamemory, observer, dreaming.
I checked each for importers outside the `advanced/` package itself:

```
working_memory: 0    schema: 0        attention_gate: 0    predictive: 0
spreading_activation: 0   interference: 0   intention: 0    metamemory: 0
observer: 0          dreaming: 0
```

Zero. Every one of them is also disabled by default in config. They are not
wired to anything.

I do not think this is worthless code — several of these are the most
interesting ideas in the repository, and `spreading_activation` in particular
overlaps with what retrieval should arguably be doing. But right now they are a
map of intentions being mistaken for a map of the system, and they make the
codebase read as far larger and more finished than it is.

**What I would do:** move them to `experiments/` or a branch, with a short note
per module saying what it was for and what wiring it would need. Nothing is
lost, and the shipped package becomes an honest description of itself. This is
also the single biggest available reduction in apparent complexity, at zero
functional cost.

---

## 6. Retrieval quality has never been measured

The scoring blend is hand-tuned, in config:

```
semantic_similarity 0.35 · recency 0.20 · strength 0.20
connection_bonus 0.15 · emotional_congruence 0.10
```

Those are plausible numbers. Nobody knows if they are good ones.
`benchmarks/retrieval_benchmark.py` exists but is not part of any loop.

This is the gap I would be most uncomfortable with if this were my memory. Every
other quality in the system — decay, connection discovery, promotion — exists to
serve retrieval. If retrieval is mediocre, all of it is decoration, and there is
currently no signal that would tell anyone.

**What I would do:** build the smallest honest benchmark. Fifty to a hundred
(cue, memory-that-should-surface) pairs harvested from real stores, scored as
recall@5 and MRR, run in CI. Then the weights become empirical, MMR from §2 can
be proven rather than argued, and any future change to scoring has a number
attached. Without this, everything in this document including my own
recommendations is taste.

**Latency, measured** on a 7,069-engram store: ~290–370 ms per retrieval. Fine
for session start; worth watching as stores grow, since parts of the path are
linear in store size.

---

## 7. Scope is a three-tuple where two would do

`agent_id / person_id / project_scope`. I fixed the resolver so all surfaces
agree, but I would question the shape itself.

`person_id` defaults to `"user"` and, in every store I looked at, is `"user"`
or a single name. Mnemos is local-first and single-machine; the overwhelmingly
common case is one human. It is a dimension that is almost always constant,
which means it is mostly a way to partition memory by accident — which is
precisely the bug class that produced the split-brain.

I am less sure about this one than the rest, because multi-person is a real
future (a shared household agent, a team assistant), and removing a dimension
is much harder than adding one.

**What I would do:** keep it in the schema, remove it from the interface.
Nothing user-facing should ask for `person_id` until there is a second person.
A dimension nobody sets cannot silently mismatch.

---

## 8. What I would build if it were mine

If I were designing this to be the memory I actually live in, four things
matter more than the rest:

**Memory should get better as it grows, not worse.** Right now every
restatement of a known fact dilutes retrieval. §2 is the fix, and it is the
difference between a system that accumulates and one that *learns*.

**Forgetting has to be real.** Not because forgetting is elegant, but because a
memory where nothing is ever lost is a log, and reading a log is not
remembering. §1 is what makes decay mean something: the episode fades, the
interpretation survives. That asymmetry *is* memory. The current design has the
form of it without the substance.

**The system should be able to tell me when it is failing.** Every bug found in
this repo, including two I introduced today, had the same shape: a layer
reporting success while carrying nothing. The scheduled job that logged a
perfect cycle against a phantom database is the purest example. What is missing
is not more tests — it is *self-report the agent can see*. If `mnemos_health`
said "0 memories retrieved in the last 20 context calls" or "no capture has
reached this scope in 9 sessions," none of these bugs survive a day. A memory
system that cannot notice its own amnesia will always fail silently, because
silence is what amnesia sounds like.

**Continuity is felt, not queried.** The best thing in this codebase is the
`MEMORY VERIFIED` block — the one-time message when continuity first crosses a
restart, quoting the human's own words back to them. That is the product. Not
the graph, not the decay curve. The moment where someone learns their agent
kept something. I would build more toward that and less toward completeness of
the cognitive model.

---

## Priority

**Nothing new gets built until continuity is proven.** That is the governing
constraint, and it is Riley's call, correctly made: the system has an
eight-hour track record, six silent-failure bugs were found in it in one day,
and two of those were introduced by the same session that was fixing the
others. It has not earned a place in anyone's critical path yet — including
its author's.

| # | Change | Cost | Why |
|---|---|---|---|
| **0** | **Prove it: continuity benchmark + amnesia test** | small | Nothing below is knowable without it. Not a step toward the work — it *is* the work |
| 1 | Health that reports absence (§8) | small | Makes the characteristic silent failure visible instead of silent |
| 2 | Merge-on-encode + MMR at retrieval (§2) | small | Biggest quality gain available for what the agent experiences |
| 3 | `mnemos_context` read-only (§3) | small | Justification removed by the scheduler; reads should not damage memory |
| 4 | Move `advanced/` out of the package (§5) | small | −1,897 lines of dead weight, zero functional cost |
| 5 | Split the operator surface (§4) | medium | Product clarity: seven tools are the product |
| 6 | Retire `person_id` from the interface (§7) | medium | Removes a whole class of scope bug |
| 7 | Rethink the layer model (§1) | **large** | See below — under §0 the question changed |

Item 0 was previously item 2. It moves to the front because everything else is
taste until there is evidence. The bar should be behavioural, not numeric:
*does the session-start packet contain something the agent would otherwise have
had to be told?* If a week of real use answers no, the honest response is to
take it out of the critical path, not to tune it.

**On item 7:** the original recommendation was to make promotion a real
distillation across three layers. Under §0 I no longer think that is right.
Three layers is the architecture of a complete memory system, and this is not
one. The sharper question is whether a continuity layer needs more than **one**
durable store of first-person notes plus a retrieval path that keeps them
legible. The graph's decay, connections, and beliefs should have to justify
themselves by making that continuity *richer* — not by being a faithful model
of cognition. I would not attempt this until item 0 can measure whether a
change helped.

---

## What I am least sure about

- **§1 is the biggest claim in this document and the one most likely to be
  wrong in its remedy.** The diagnosis is measured. The prescription —
  episode/interpretation split — is my design judgment and untested.
- **§7 (`person_id`)** may be right for today and wrong for the product Riley
  is building. Sanctuary implies many observers of one agent.
- **I have not audited the encoder's surprise detection, the embedding index's
  behaviour under provider failure, or the substrate handlers.** All three
  could contain something that changes the picture above.
