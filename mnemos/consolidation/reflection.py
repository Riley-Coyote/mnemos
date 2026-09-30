"""
Reflection pass: the deep cycle's look over what the agent wrote lately.

It writes no memories. It once wrote "thoughts" as new memories: a configured
model's lines, or without one Mnemos's own template ("Recurring theme:
continuity (appeared in 46 recent memories)", author_kind system). Neither was
the agent's words, and words in memory come only from the agent. A theme the
pass notices may one day be put to the agent as a question in the packet, at
most one; until then it becomes nothing.

The identity pass lives here too (``run_identity_pass``): identity measured
from the graph of what the agent wrote, not narrated.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity, IdentityProfile
from ..core.types import is_structural_tag

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def run_reflection_pass(
    store: EngramStore,
    identity: AgentIdentity,
    emotional_state: EmotionalState,
    llm_client: Any | None,
    config: dict[str, Any] | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> dict[str, Any]:
    """Count the agent's recent memories. Writes nothing, and asks no model.

    Args:
        store: The engram store.
        identity: Agent identity; its memory profile names the agent.
        emotional_state: Kept for the daemon's call; unused.
        llm_client: Kept for the daemon's call; never called. A model's
            thoughts would be its words in the agent's memory, and sending
            the agent's memories out for words that are then kept nowhere
            would spend them for nothing.
        config: Optional config dict (``reflection_lookback_hours``).

    Returns:
        Statistics dict. ``thoughts_generated`` stays 0 and
        ``narrative_updated`` False: the dream journal, the watchdog and the
        CLI read those keys, from this cycle and from older cycles' logs.
    """
    config = config or {}
    lookback_hours = config.get("reflection_lookback_hours", 24)
    agent_id = identity.memory_profile.agent_id

    stats = {
        "engrams_reviewed": 0,
        "thoughts_generated": 0,
        "narrative_updated": False,
        "narrative_length": 0,
    }

    # The agent's own memories only: what a tool or a model wrote is not
    # something the agent keeps returning to. Counted, so their links are
    # not loaded.
    all_engrams = store.get_active_engrams(
        agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        limit=200, author_kind="agent", load_connections=False,
    )
    stats["engrams_reviewed"] = sum(
        1 for e in all_engrams if _hours_since(e.created_at) < lookback_hours
    )
    # Identity is computed by run_identity_pass, which runs on every cycle
    # rather than only when a model is configured.
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

    Only what the agent wrote counts (``author_kind`` 'agent'). On one real
    store 103 of 450 memories were a transcript indexer's model's words, and
    what the agent is must not be measured from them.
    """
    stats: dict[str, Any] = {"identity_computed": False}

    engrams = store.get_active_engrams(
        agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        limit=1000, author_kind="agent",
    )
    if not engrams:
        # Nothing to be the shape of yet. Not a failure.
        stats["reason"] = "no active memories the agent wrote"
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

    Only memories the agent wrote are measured (``author_kind`` 'agent'),
    whoever passes them in.
    """
    agent_id = identity.memory_profile.agent_id
    all_engrams = [
        e for e in all_engrams if getattr(e, "author_kind", "unknown") == "agent"
    ]

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
