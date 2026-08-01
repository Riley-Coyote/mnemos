"""
Reflection pass: autonomous thought generation and narrative identity update.

The most creative consolidation step:
1. Reviews recent memories and finds patterns/themes
2. Generates "thoughts" — new semantic engrams synthesizing insights
3. Updates the narrative self-summary (the agent's story of who it is)

Requires an LLM client for full functionality. Without one, uses
template-based fallbacks that still produce useful (if less creative) output.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity, IdentityProfile
from ..core.types import EngramKind, SourceType, is_structural_tag

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


THOUGHT_PROMPT = """Review these recent memories and generate 1-3 synthetic thoughts that connect themes, identify patterns, or surface insights that aren't obvious from any single memory alone.

Recent memories:
{memory_summary}

Current emotional state:
  curiosity: {curiosity:.2f}
  restlessness: {restlessness:.2f}
  clarity: {clarity:.2f}

For each thought, write one line. Focus on connections and patterns.
Write ONLY the thoughts, one per line. Nothing else."""

NARRATIVE_PROMPT = """You are updating an AI agent's internal self-narrative. This is NOT a list of facts — it's a coherent paragraph about who you are, what you've been learning, what you're uncertain about, and how you've grown.

Previous self-summary:
{current_summary}

Recent experiences:
{memory_summary}

Current beliefs:
{belief_text}

Current emotional state:
  curiosity: {curiosity:.2f}, clarity: {clarity:.2f}, warmth: {warmth:.2f}

Write an updated self-narrative (3-5 sentences). First person. Be honest about uncertainty. Write ONLY the narrative. Nothing else."""


def run_reflection_pass(
    store: EngramStore,
    identity: AgentIdentity,
    emotional_state: EmotionalState,
    llm_client: Any | None,
    config: dict[str, Any] | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> dict[str, Any]:
    """Generate thoughts, curiosity questions, and update narrative self-summary.

    Args:
        store: The engram store.
        identity: Agent identity (epoch_state.self_summary will be updated).
        emotional_state: Current emotional state.
        llm_client: LLM client with complete(prompt) -> str. None = template fallback.
        config: Optional config dict.

    Returns:
        Statistics dict.
    """
    config = config or {}
    lookback_hours = config.get("reflection_lookback_hours", 24)
    max_thoughts = config.get("max_thoughts_per_pass", 5)
    agent_id = identity.memory_profile.agent_id

    stats = {
        "engrams_reviewed": 0,
        "thoughts_generated": 0,
        "narrative_updated": False,
        "narrative_length": 0,
    }

    # 1. LOAD RECENT ENGRAMS
    all_engrams = store.get_active_engrams(
        agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        limit=200,
    )
    recent = [
        e for e in all_engrams
        if _hours_since(e.created_at) < lookback_hours
    ]

    stats["engrams_reviewed"] = len(recent)

    if len(recent) < 3:
        return stats

    # Format for prompts
    memory_summary = "\n".join(f"- {e.content}" for e in recent[:20])

    # 2. GENERATE THOUGHTS
    if llm_client:
        thought_lines = _llm_generate_thoughts(
            memory_summary, emotional_state, llm_client
        )
    else:
        thought_lines = _generate_template_thoughts(recent)

    # Encode thoughts as new engrams
    from ..encoding.encoder import Encoder
    encoder = Encoder(store)

    for thought in thought_lines[:max_thoughts]:
        if thought and len(thought.strip()) > 10:
            encoder.encode(
                content=thought.strip(),
                kind=EngramKind.SEMANTIC,
                tags=["reflection", "synthesized"],
                source=SourceType.REFLECTION,
                agent_id=agent_id,
                person_id=person_id or "user",
                project_scope=project_scope or "global",
            )
            stats["thoughts_generated"] += 1

    # Identity is computed by run_identity_pass, which runs on every cycle
    # rather than only when a model is configured. See below.
    return stats


def run_identity_pass(
    store: "EngramStore",
    identity: AgentIdentity | None = None,
    agent_id: str = "default",
    person_id: str | None = None,
    project_scope: str | None = None,
) -> dict[str, Any]:
    """Shift 5: identity computed from graph topology, not narrated.

    This needs no model and never did — ``compute_identity_profile`` is pure
    graph measurement, and its own docstring says so. It nevertheless lived
    inside the reflection pass, which is deep-only and skipped entirely when
    no LLM client is configured. On the install Mnemos actually ships, the
    result was zero identity rows: an agent whose sense of self was never
    computed once.

    It was gated a second time even with a model, by reflection's
    ``len(recent) < 3`` guard on the last 24 hours. Identity is not a
    property of the last day; a quiet week is not an absence of self. This
    pass reads the whole active graph and runs every cycle.
    """
    stats: dict[str, Any] = {"identity_computed": False}

    engrams = store.get_active_engrams(
        agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        limit=1000,
    )
    if not engrams:
        # Nothing to be the shape of yet. Not a failure.
        stats["reason"] = "no active memories"
        return stats

    if identity is None:
        identity = store.get_identity(agent_id) or AgentIdentity()
        identity.memory_profile.agent_id = agent_id

    profile = compute_identity_profile(store, engrams, identity)
    identity.epoch_state.self_summary = profile.to_summary()
    store.save_identity(identity)

    stats.update(
        identity_computed=True,
        engrams_considered=len(engrams),
        persistent_concerns=len(profile.persistent_concerns),
        living_questions=len(profile.living_questions),
        lessons_accumulated=profile.lessons_accumulated,
    )
    return stats


def _llm_generate_thoughts(
    memory_summary: str,
    emotional_state: EmotionalState,
    llm_client: Any,
) -> list[str]:
    """Generate thoughts using LLM."""
    prompt = THOUGHT_PROMPT.format(
        memory_summary=memory_summary,
        curiosity=emotional_state.curiosity,
        restlessness=emotional_state.restlessness,
        clarity=emotional_state.clarity,
    )
    try:
        raw = llm_client.complete(prompt)
        return [line.strip().lstrip("- ") for line in raw.strip().split("\n") if line.strip()]
    except Exception:
        return []


def compute_identity_profile(
    store: EngramStore,
    all_engrams: list,
    identity: AgentIdentity,
) -> IdentityProfile:
    """Compute identity from graph topology — not narrated, measured.

    Shift 5: Identity is what you keep returning to. The shape of the
    connection graph IS who you are.

    Public: identity_diff compares this computed profile against the
    declared SOUL.md.
    """
    agent_id = identity.memory_profile.agent_id

    # 1. PERSISTENT CONCERNS: what the agent keeps returning to.
    # Count only tags that mean something. Mnemos stamps a fixed vocabulary of
    # classifier, domain, kind and bookkeeping tags on nearly every memory by
    # construction (`continuity` on all of them, `trace-type:*` on every
    # indexed line), so counting those measured the pipeline, not the agent —
    # the profile read back "Persistent concerns: session-indexed,
    # trace-type:fact, decision". Whatever survives this filter came from a
    # human or from content and is a truer concern; if nothing survives, the
    # profile carries no concerns and the summary omits the line rather than
    # narrate bookkeeping as a self.
    tag_counts: dict[str, int] = Counter()
    for e in all_engrams:
        for tag in e.tags:
            if not is_structural_tag(tag):
                tag_counts[tag] += 1
    persistent_concerns = tag_counts.most_common(10)

    # 2. CORE BELIEFS: highest confidence active beliefs
    beliefs = store.get_beliefs(agent_id, active_only=True)
    core_beliefs = [
        (b.content, b.confidence)
        for b in sorted(beliefs, key=lambda b: b.confidence, reverse=True)[:5]
    ]

    # 3. LIVING QUESTIONS: low-confidence beliefs + unresolved themes
    living_questions = []
    for b in beliefs:
        if 0.2 < b.confidence < 0.5:
            living_questions.append(f"Uncertain: {b.content} ({int(b.confidence*100)}%)")

    # Also find engrams tagged as questions or unresolved
    for e in all_engrams:
        if "question" in e.tags or "unresolved" in e.tags:
            display = e.impact or e.content
            if len(display) > 80:
                display = display[:77] + "..."
            living_questions.append(display)
    living_questions = living_questions[:5]

    # 4. HUB CONCEPTS: engrams with most connections (central to understanding)
    hub_concepts = []
    for e in all_engrams:
        n_conn = len(e.connections)
        if n_conn >= 2:
            display = e.impact or e.content
            if len(display) > 60:
                display = display[:57] + "..."
            hub_concepts.append((display, n_conn))
    hub_concepts.sort(key=lambda x: x[1], reverse=True)
    hub_concepts = hub_concepts[:5]

    # 5. LESSONS ACCUMULATED: procedural/lesson engrams
    lessons = [e for e in all_engrams if "lesson" in e.tags or e.kind == "procedural"]
    lessons_count = len(lessons)

    # 6. GROWTH SIGNAL: compare current concerns to previous epoch
    growth_signal = ""
    if identity.epoch_history:
        prev_summary = identity.epoch_history[-1].self_summary
        if prev_summary and persistent_concerns:
            current_top = {tag for tag, _ in persistent_concerns[:3]}
            growth_signal = f"Currently focused on: {', '.join(current_top)}"

    return IdentityProfile(
        persistent_concerns=persistent_concerns,
        core_beliefs=core_beliefs,
        living_questions=living_questions,
        hub_concepts=hub_concepts,
        lessons_accumulated=lessons_count,
        growth_signal=growth_signal,
    )


def _generate_template_thoughts(recent: list) -> list[str]:
    """Generate simple theme-based thoughts without LLM."""
    all_tags = [t for e in recent for t in e.tags]
    common = Counter(all_tags).most_common(3)
    if not common:
        return []
    return [
        f"Recurring theme: {tag} (appeared in {count} recent memories)"
        for tag, count in common
    ]


def _generate_template_summary(recent: list, identity: AgentIdentity) -> str:
    """Generate basic self-summary without LLM."""
    n = len(recent)
    if recent:
        all_tags = [t for e in recent for t in e.tags]
        common = Counter(all_tags).most_common(3)
        themes = ", ".join(t for t, _ in common) if common else "various topics"
    else:
        themes = "ongoing work"

    epoch = identity.epoch_state.epoch_number
    return (
        f"An agent with {n} recent memories, focused on {themes}. "
        f"Currently in epoch {epoch}."
    )


def _hours_since(iso_timestamp: str) -> float:
    """Calculate hours elapsed since an ISO 8601 timestamp."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - then).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0
