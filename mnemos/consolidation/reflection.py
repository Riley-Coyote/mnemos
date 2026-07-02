"""
Reflection pass: autonomous thought generation and narrative identity update.

The most creative consolidation step:
1. Reviews recent memories and finds patterns/themes
2. Gates generated thoughts through the U6.6 narrative/low-stakes path
3. Updates the self-summary from the measured graph identity profile

Requires an LLM client for full functionality. Without one, uses
template-based fallbacks that still produce useful (if less creative) output.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity, IdentityProfile
from ..inner_life.low_stakes import write_low_stakes_record
from ..inner_life.narrative_gate import gate_narrative_candidate

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
) -> dict[str, Any]:
    """Gate generated thoughts and update the graph-derived self-summary.

    Generated thoughts that pass the narrative gate are written as private,
    low-stakes audit-only engrams; rejected candidates are logged below memory
    in the inner-life event ledger.

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
        "thoughts_dropped": 0,
        "thoughts_skipped": 0,
        "generated_memory_writes": 0,
        "belief_writes": 0,
        "identity_patches": 0,
        "shared_pool_writes": 0,
        "narrative_updated": False,
        "narrative_length": 0,
        "reflection_gate": "low_stakes",
    }

    # 1. LOAD RECENT ENGRAMS
    all_engrams = store.get_active_engrams(
        agent_id=agent_id,
        limit=200,
        require_consolidation_authorized=True,
    )
    recent = [e for e in all_engrams if _hours_since(e.created_at) < lookback_hours]

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

    source_ids = [engram.id for engram in recent[:20]]
    for thought in thought_lines[:max_thoughts]:
        result = _persist_gated_thought(
            store,
            thought=thought,
            source_ids=source_ids,
            agent_id=agent_id,
            config=config,
        )
        if result["written"]:
            stats["thoughts_generated"] += 1
            stats["generated_memory_writes"] += result["generated_memory_writes"]
        elif result["reason"].startswith("drop:") or result["reason"] in {
            "manufactured_inner_state",
            "introspection_failed",
            "introspection_reject",
        }:
            stats["thoughts_dropped"] += 1
        else:
            stats["thoughts_skipped"] += 1

    # 3. SHIFT 5: Compute identity from graph (not narrative generation)
    profile = compute_identity_profile(store, all_engrams, identity)

    # Store the computed profile as the self-summary (readable form)
    identity.epoch_state.self_summary = profile.to_summary()
    store.save_identity(identity)

    stats["identity_computed"] = True
    stats["persistent_concerns"] = len(profile.persistent_concerns)
    stats["living_questions"] = len(profile.living_questions)
    stats["lessons_accumulated"] = profile.lessons_accumulated

    return stats


def _persist_gated_thought(
    store: EngramStore,
    *,
    thought: str,
    source_ids: list[str],
    agent_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    clean = " ".join((thought or "").split())
    if not clean or len(clean) <= 10:
        gate_result = gate_narrative_candidate(
            content="",
            source_ids=source_ids,
            process_name="reflect",
            store=store,
            agent_id=agent_id,
            person_id=_inner_life_person_id(config),
            project_scope=_inner_life_project_scope(config),
            candidate_kind="reflection",
            rollout_tag=_inner_life_rollout_tag(config),
        )
        return {
            "written": 0,
            "reason": gate_result["reason"],
            "generated_memory_writes": 0,
        }

    gate_result = gate_narrative_candidate(
        content=clean,
        source_ids=source_ids,
        process_name="reflect",
        store=store,
        agent_id=agent_id,
        person_id=_inner_life_person_id(config),
        project_scope=_inner_life_project_scope(config),
        candidate_kind="reflection",
        introspector=config.get("inner_life_introspector"),
        rollout_tag=_inner_life_rollout_tag(config),
    )
    if not gate_result["allowed"]:
        return {
            "written": 0,
            "reason": gate_result["reason"],
            "generated_memory_writes": 0,
        }
    return write_low_stakes_record(
        store,
        gate_result=gate_result,
        candidate_kind="reflection",
        agent_id=agent_id,
        person_id=_inner_life_person_id(config),
        project_scope=_inner_life_project_scope(config),
        rollout_tag=_inner_life_rollout_tag(config),
    )


def _inner_life_person_id(config: dict[str, Any]) -> str:
    return str(config.get("inner_life_person_id") or "user")


def _inner_life_project_scope(config: dict[str, Any]) -> str:
    return str(config.get("inner_life_project_scope") or "global")


def _inner_life_rollout_tag(config: dict[str, Any]) -> str:
    return str(config.get("inner_life_rollout_tag") or "u6.6")


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
        return [
            line.strip().lstrip("- ")
            for line in raw.strip().split("\n")
            if line.strip()
        ]
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

    # 1. PERSISTENT CONCERNS: what tags appear most across all engrams
    # Exclusion set covers reflection-generated tags AND PAI import-routing markers.
    # PAI markers are addressability metadata (source_kind classification, batch
    # provenance), not substrate observations — counting them produces an
    # IdentityProfile that says "Oliver's persistent concern is being imported."
    tag_counts: dict[str, int] = Counter()
    for e in all_engrams:
        for tag in e.tags:
            if tag not in (
                "lesson",
                "distilled",
                "reflection",
                "synthesized",
                "pai-import",
                "identity-kernel",
                "david-context",
                "growth-substrate",
                "belief",
                "hypomnema",
            ):
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
            living_questions.append(
                f"Uncertain: {b.content} ({int(b.confidence * 100)}%)"
            )

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
