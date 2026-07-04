# Question for Riley — hypomnema graduation without user review

**Status:** draft for David to send. An agent never sends this (supervision protocol §8 / hard boundary — no messages to anyone but David). David reviews and sends.

**Citation note (T3/F3):** the RFC's original reference to `graduate.ts:95,192` is from an ancestral TypeScript lineage and does **not** exist in Riley-Coyote/mnemos (verified read-only 2026-07-02: the repo is Python; `graduate.ts` returns zero code-search hits). The graduation behavior itself is real and lives in the Python `mnemos/simple_runtime.py` promotion path. So the question below cites the **behavior**, not a file:line, to avoid a wrong reference in an upstream message.

---

**Subject: hypomnema graduation into semantic memory — intended without a review step?**

Riley — a design question about the graduation path in Mnemos.

In the current architecture, a hypomnema continuity entry can graduate into an active semantic engram — it becomes part of operational memory that shapes future retrieval and reasoning. As far as I can see, that promotion happens on confidence/salience/stability thresholds, without an explicit human-review step in between.

Two questions:

1. **Is it intentional that a high-blast-domain hypomnema — identity, relationship, or philosophy content — can graduate into operational semantic memory without a human in the loop?** Or was the promotion path designed with lower-stakes continuity in mind, and high-blast content graduating is more incidental?

2. **What, in your design, prevents a model-generated relational inference from becoming durable substrate by that same path?** A dream, a reflection, or an autonomous consolidation can synthesize a claim like "I am someone who…" or "David is the kind of person who…"; if that synthesized prose can accrue confidence and graduate, a model's inference becomes identity-shaping memory with no author but the model.

Context for why I'm asking: I've been hardening a membrane over the graduation and read paths — harness-stamped authority so a write's provenance comes from its channel rather than its content, and a read-visibility quarantine so pending/high-blast candidates stay out of operational context until reviewed. I want to know whether I'm hardening against a hole you already close upstream (in which case I'd rather adopt your approach), or whether this is a genuine divergence in intent worth talking through.

No rush, and thank you for building the thing — the typed-connection + reconsolidation model has been a pleasure to work in.

— David
