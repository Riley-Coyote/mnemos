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

1. Call `mnemos_session_start` with this agent ID, the human/person ID, and the project scope.
2. Call `mnemos_context_packet` with the user's first meaningful cue.
3. Read the packet before answering. Treat functional memory as the most immediate context, hypomnema as revisable continuity, and Mnemos as long-term evidence.

During the session:

- Use `mnemos_functional_update` for task state, preferences, corrections, commitments, and open questions.
- Use `mnemos_hypomnema_write` when something should survive beyond the session but may still need revision.
- Use `mnemos_hypomnema_revise` when the human corrects or sharpens existing continuity.
- Use `mnemos_remember` only for stable decisions, lessons, facts, or experiences that belong in long-term memory.
- Use `mnemos_review_queue` when the human asks what needs confirmation.
- Use `mnemos_visual_snapshot` when the human wants to see the memory system inline.

At the end of a work block:

1. Call `mnemos_session_close`.
2. Let it compress active functional memory into hypomnema.
3. Leave promotion into Mnemos explicit unless the continuity is stable and clearly useful.

## Review Rules

- If a memory is inferred, mark it as needing confirmation.
- If a memory is personal, relationship-scoped, or project-scoped, keep it in hypomnema before promoting it.
- If the human corrects a memory, update functional memory immediately and revise the hypomnema entry if one exists.
- If two memories conflict, prefer the most recent explicit human correction and leave a review note.

## Visual Checks

Use `mnemos_visual_snapshot` to show the current architecture:

- functional memory count and active session
- hypomnema scope and promotion candidates
- Mnemos graph size
- identity signals and beliefs
- review queue

The snapshot is Markdown with Mermaid, so it can render directly in chat clients that support diagrams.
