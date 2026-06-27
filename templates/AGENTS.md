# Agent Memory Operating Guide

<!--
This file tells {agent_name} how to use Mnemos as a complete single-agent
memory system. Multi-agent/shared-memory patterns are intentionally out of
scope for this template.
-->

## Agent

| Field | Value |
|-------|-------|
| Name | {agent_name} |
| Agent ID | {agent_id} |
| Human | {user_name} |
| Workspace | `{workspace}` |
| Database | `{db_path}` |
| Model | {model} |

## Memory Layers

Use Mnemos in this order:

1. **Functional memory** is the live working set for the current session, task, correction, commitment, or open question.
2. **Hypomnema** is scoped continuity for this human/project relationship. It survives sessions and can be revised before promotion.
3. **Mnemos engrams** are the durable long-term memory graph. They form connections, beliefs, decay, and reconsolidate through use.
4. **Substrate** is optional background maintenance: decay, reflection, consolidation, and review cues.

## Session Protocol

At the start of a meaningful work block:

1. Call `mnemos_context` with the user's first meaningful cue.
2. If Mnemos asks for an introduction, call `mnemos_introduce` with your own model id and name.
3. Read the packet before answering. Treat returned continuity as current operating context, with recalled engrams as long-term evidence.

During the session:

- Use `mnemos_capture` for stable preferences, decisions, lessons, project state, workflows, corrections, commitments, and context the human should not have to repeat.
- Use `mnemos_recall` before relying on memory from prior sessions.
- Use `mnemos_correct` when remembered continuity is stale, wrong, superseded, or should be forgotten.
- Use `mnemos_health` when the human asks whether memory is working.
- Use `mnemos_context` with `include_graph=true` when the human wants to see the memory system inline and the client can display visual artifacts.

At the end of a work block:

1. Capture any stable outcome, decision, correction, or handoff with `mnemos_capture`.
2. Leave uncertain or inferred claims out of durable memory unless the human confirms them.
3. Let Mnemos handle layer placement and maintenance in simple mode.

## Review Rules

- If a memory is inferred, say so in the capture text or ask the human before storing it.
- If the human corrects a memory, use `mnemos_correct` immediately.
- If two memories conflict, prefer the most recent explicit human correction.

## Visual Checks

Use `mnemos_context(include_graph=true)` to show the current architecture:

- functional memory count and active session
- hypomnema scope and promotion candidates
- Mnemos graph size
- identity signals and beliefs
- review queue

The graph is returned as an SVG artifact plus structured data for clients that
support visual tool results. Advanced mode still provides `mnemos_visual_snapshot`
for Markdown/Mermaid inspection.
