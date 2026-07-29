"""
Softening pass: LLM-mediated lossy compression of fading memories.

When a memory's accessibility drops, its content is rewritten at lower
resolution — preserving gist and emotional essence while losing specific
details. This models how human memories naturally lose detail over time.

Shift 1 (Traces): Before softening, the lasting impact/insight is extracted
and preserved — surviving even when content fades to impressions.

Shift 2 (Forgetting that teaches): When impact is extracted, a "lesson" engram
is created (or reinforced). Lessons are procedural engrams with high stability
that persist as accumulated wisdom. Forgetting feeds forward into learning.

The original content is always preserved in content_at_encoding (immutable).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import ulid as _ulid_mod

from ..core.types import ConnectionRelation, EngramKind, SourceType

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


# ── LLM Prompts (softener lineage: Anima; conservator invariants added) ──

SOFTENER_PROMPT = """You are a memory conservator. Given a sharp memory, rewrite it at lower resolution — in the rememberer's own voice, not yours.

Keep the emotional tone and core meaning. Remove specific timestamps, exact quotes, and precise details. Replace them with impressions and feelings. The result should feel like a memory that's naturally fading — the way the same mind remembers something from months ago. The gist remains. The specifics blur. The voice stays.

Invariants — violating any of these destroys the memory's integrity:
- Preserve the original's person and framing: if it says "I", the softened version says "I".
- Preserve emotional valence: do not brighten, darken, or neutralize the feeling.
- Never add interpretation, names, places, or facts that are not in the original.
- The softened version must not be longer than the original.

{voice_exemplars}Current sharpness: {current_sharpness}
Target sharpness: {target_sharpness}

Memory:
{content}

Write ONLY the softened version. Nothing else."""

VOICE_EXEMPLARS_BLOCK = """These are vivid memories from the same rememberer. Match their voice and register:
{exemplars}

"""

DEEP_SOFTENER_PROMPT = """Reduce this memory to its emotional essence. One or two phrases maximum. What feeling remains when all detail is gone?

This is not a summary. It's an impression — like catching a scent that reminds you of something you can't quite place. Keep the rememberer's framing and valence; add nothing that was not there.

Memory:
{content}

Write ONLY the impression. One or two phrases. Nothing else."""


def _gen_log_id(prefix: str) -> str:
    if hasattr(_ulid_mod, "new"):
        return f"{prefix}_{_ulid_mod.new()}"
    from ulid import ULID

    return f"{prefix}_{ULID()}"


def _hours_since(timestamp: str) -> float:
    """Hours since an ISO timestamp. Unparseable means old, never brand new."""
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def run_softening_pass(
    store: EngramStore,
    config: dict[str, Any],
    llm_client: Any | None,
    agent_id: str | None = None,
    invent_impact: bool = False,
) -> dict[str, Any]:
    """Rewrite memories that have dropped below the resolution threshold.

    Args:
        store: The engram store.
        agent_id: Agent whose memories to soften. None preserves legacy
            behavior (the store's own default scope); callers that manage
            multiple agents in one store MUST pass this explicitly —
            softening rewrites memory content, and rewriting another
            agent's memories is identity contamination.
        config: Configuration dict with softening parameters. Set
            softening_dry_run=True to compute and log before/after pairs
            to consolidation_log (pass_name "softening_dry_run") without
            rewriting any engram — audit the conservator before enabling.
        llm_client: LLM client with complete(prompt) -> str method.
            If None, uses rule-based fallback.

    Returns:
        Statistics dict.
    """
    softening_threshold = config.get("softening_threshold", 0.15)
    minimum_resolution = config.get("minimum_resolution", 0.1)
    max_llm_calls = config.get("max_llm_calls_per_cycle", 50)
    dry_run = bool(config.get("softening_dry_run", False))
    started_at = datetime.now(timezone.utc).isoformat()

    stats = {
        "engrams_evaluated": 0,
        "engrams_softened": 0,
        "lessons_created": 0,
        "lessons_reinforced": 0,
        "llm_calls": 0,
        "avg_resolution_before": 0.0,
        "avg_resolution_after": 0.0,
        "dry_run": dry_run,
    }

    # Get all engrams that could need softening (active or dormant, resolution > minimum)
    all_engrams = store.get_active_engrams(agent_id=agent_id, limit=5000) if agent_id is not None else store.get_active_engrams(limit=5000)

    # Conservator: the softener should preserve the agent's register, not
    # normalize it into the substrate model's voice. The most vivid
    # memories of the SAME agent serve as style exemplars.
    exemplar_pool = _select_voice_exemplars(all_engrams)

    total_res_before = 0.0
    total_res_after = 0.0
    softened_count = 0
    dry_run_pairs: list[dict[str, Any]] = []

    # Forgetting takes time. Softening triggers on accessibility alone, and a
    # newly encoded engram already sits at 0.5 — so with the pass enabled, a
    # memory captured seconds ago qualified as fading and the agent was asked
    # what it had taught. Nothing had faded; the pass had simply never run
    # before, so the missing notion of age had never mattered.
    min_age_hours = float(config.get("softening_min_age_hours", 24))

    for engram in all_engrams:
        if engram.resolution <= minimum_resolution:
            continue  # Already at minimum resolution

        if _hours_since(engram.created_at) < min_age_hours:
            stats["too_recent"] = stats.get("too_recent", 0) + 1
            continue

        stats["engrams_evaluated"] += 1
        total_res_before += engram.resolution

        # Determine target resolution from accessibility
        target = _calculate_target_resolution(engram.accessibility)

        if target >= engram.resolution:
            total_res_after += engram.resolution
            continue  # No softening needed — accessibility still high enough

        # Hysteresis: only soften if significantly above target
        if engram.resolution <= target + 0.15:
            total_res_after += engram.resolution
            continue

        # Cap at minimum resolution
        target = max(minimum_resolution, target)

        exemplars_block = _format_exemplars(exemplar_pool, exclude_id=engram.id)

        if dry_run:
            # Audit mode: compute what softening WOULD do, log the
            # before/after pair, and leave the engram untouched.
            if llm_client and stats["llm_calls"] < max_llm_calls:
                candidate = _llm_soften(
                    engram.content, engram.resolution, target, llm_client,
                    exemplars_block,
                )
                stats["llm_calls"] += 1
            else:
                candidate = _rule_based_soften(engram.content, target)
            softened_content = _conserve(engram.content, candidate, target)
            dry_run_pairs.append(
                {
                    "engram_id": engram.id,
                    "resolution_before": engram.resolution,
                    "resolution_target": round(target, 2),
                    "before": engram.content[:500],
                    "after": softened_content[:500],
                }
            )
            total_res_after += engram.resolution
            continue

        # EXTRACT IMPACT before softening (if not already set)
        # This is the key Shift 1 behavior: before content gets compressed,
        # extract the lasting insight. Impact survives even when content fades.
        if not engram.impact:
            if llm_client and stats["llm_calls"] < max_llm_calls:
                engram.impact = _extract_impact(engram.content, llm_client)
                stats["llm_calls"] += 1
            elif invent_impact:
                engram.impact = _rule_based_impact(engram.content)
            else:
                # Shift 2 says the lesson is what survives the forgetting, so
                # a lesson the server guessed at from keywords is the one
                # thing that must not be written here. The compression is
                # deterministic and happens anyway; the meaning is recorded
                # for the agent to supply through mnemos_reflect.
                stats.setdefault("awaiting_impact", []).append(engram.id)

        # SHIFT 2: Create or reinforce a lesson engram from the impact.
        # Forgetting feeds forward — the distilled insight becomes persistent wisdom.
        lesson_id = None
        if engram.impact:
            lesson_id = _create_or_reinforce_lesson(engram, store, stats)

        # SOFTEN content (impact is preserved separately)
        if llm_client and stats["llm_calls"] < max_llm_calls:
            candidate = _llm_soften(
                engram.content, engram.resolution, target, llm_client,
                exemplars_block,
            )
            stats["llm_calls"] += 1
        else:
            candidate = _rule_based_soften(engram.content, target)

        # Conservation guard: softening may compress, never inflate or
        # invent. A candidate that grows or introduces new named entities
        # is rejected in code, not just in the prompt.
        softened_content = _conserve(engram.content, candidate, target)

        # Version snapshot (preserve pre-softening state)
        engram.add_version(reason="softening")

        # Update content (impact is already set and untouched)
        engram.content = softened_content
        engram.resolution = round(target, 2)

        # Connect source to its lesson via DISTILLED_INTO
        if lesson_id:
            engram.add_connection(
                target_id=lesson_id,
                relation=ConnectionRelation.DISTILLED_INTO,
                strength=0.8,
                formed_by="consolidation",
            )

        store.save_engram(engram)

        total_res_after += engram.resolution
        softened_count += 1
        stats["engrams_softened"] += 1

    if dry_run:
        stats["engrams_would_soften"] = len(dry_run_pairs)
        if dry_run_pairs:
            store.log_consolidation(
                log_id=_gen_log_id("softening_dry_run"),
                pass_name="softening_dry_run",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                stats={"agent_id": agent_id, "pairs": dry_run_pairs},
            )

    if stats["engrams_evaluated"] > 0:
        stats["avg_resolution_before"] = round(
            total_res_before / stats["engrams_evaluated"], 3
        )
        stats["avg_resolution_after"] = round(
            total_res_after / stats["engrams_evaluated"], 3
        )

    return stats


def _calculate_target_resolution(accessibility: float) -> float:
    """Map accessibility to appropriate resolution level.

    Ported from Anima's calculate_target_sharpness.
    """
    if accessibility >= 0.7:
        return 1.0
    elif accessibility >= 0.4:
        t = (accessibility - 0.4) / 0.3
        return 0.4 + (0.6 * t)
    elif accessibility >= 0.15:
        t = (accessibility - 0.15) / 0.25
        return 0.1 + (0.3 * t)
    else:
        return 0.0


def _select_voice_exemplars(all_engrams: list, k: int = 4) -> list:
    """Pick the most vivid engrams to anchor the softener in the agent's voice.

    Vividness = resolution (sharpness), tie-broken by strength. The pool is
    already agent-scoped by the caller — exemplars must come from the same
    agent whose memories are being rewritten.
    """
    candidates = [e for e in all_engrams if e.content and len(e.content) >= 20]
    candidates.sort(key=lambda e: (e.resolution, e.strength), reverse=True)
    return candidates[:k]


def _format_exemplars(pool: list, exclude_id: str, limit: int = 3) -> str:
    exemplars = [e for e in pool if e.id != exclude_id][:limit]
    if not exemplars:
        return ""
    lines = "\n".join(
        f'{i}. "{e.content[:240]}"' for i, e in enumerate(exemplars, 1)
    )
    return VOICE_EXEMPLARS_BLOCK.format(exemplars=lines)


def _new_named_entities(original: str, softened: str) -> set[str]:
    """Capitalized mid-sentence tokens in the softened text absent from the original."""
    orig_tokens = {t.lower() for t in re.findall(r"[A-Za-z][\w'’-]*", original)}
    out = set()
    for m in re.finditer(r"\b[A-Z][\w'’-]+\b", softened):
        tok = m.group(0)
        if tok.lower() in orig_tokens:
            continue
        prefix = softened[: m.start()].rstrip()
        if not prefix or prefix[-1] in ".!?…":
            continue  # sentence-initial capitalization is not an entity signal
        out.add(tok)
    return out


def _is_conserved(original: str, softened: str) -> bool:
    if not softened or not softened.strip():
        return False
    if len(softened) > len(original):
        return False
    return not _new_named_entities(original, softened)


def _conserve(original: str, candidate: str, target_resolution: float) -> str:
    """Enforce the conservator invariants on a softened candidate.

    Softening may compress, never inflate or invent. A candidate that is
    longer than the original or introduces named entities the original
    never contained is rejected; the rule-based softener is tried next,
    and if even that violates (e.g. on very short memories), the original
    content is kept — there was no detail to shed.
    """
    candidate = (candidate or "").strip()
    if _is_conserved(original, candidate):
        return candidate
    fallback = _rule_based_soften(original, target_resolution)
    if _is_conserved(original, fallback):
        return fallback
    return original


def _llm_soften(
    content: str,
    current_resolution: float,
    target_resolution: float,
    llm_client: Any,
    voice_exemplars: str = "",
) -> str:
    """Soften memory content using LLM, in the agent's own register."""
    if target_resolution >= 0.4:
        prompt = SOFTENER_PROMPT.format(
            current_sharpness=current_resolution,
            target_sharpness=target_resolution,
            content=content,
            voice_exemplars=voice_exemplars,
        )
    else:
        prompt = DEEP_SOFTENER_PROMPT.format(content=content)

    try:
        result = llm_client.complete(prompt)
        return result.strip() if result else _rule_based_soften(content, target_resolution)
    except Exception:
        return _rule_based_soften(content, target_resolution)


IMPACT_EXTRACTION_PROMPT = """What is the one lasting insight from this memory? Not what happened — what it taught. What understanding remains when the details are gone?

Memory:
{content}

Write ONE sentence capturing the lasting impact. Nothing else."""


def _extract_impact(content: str, llm_client: Any) -> str:
    """Extract the lasting impact/lesson from content before it gets softened."""
    prompt = IMPACT_EXTRACTION_PROMPT.format(content=content)
    try:
        result = llm_client.complete(prompt)
        return result.strip() if result else _rule_based_impact(content)
    except Exception:
        return _rule_based_impact(content)


def _rule_based_impact(content: str) -> str:
    """Extract impact without LLM — take the core assertion.

    Heuristic: the last substantive sentence is often the conclusion/lesson.
    """
    sentences = [s.strip() for s in content.split(".") if s.strip()]
    # Take the last substantive sentence (often the insight)
    for s in reversed(sentences):
        if len(s) > 15:
            return s
    return content[:100] if content else ""


def _create_or_reinforce_lesson(
    engram: Any,
    store: EngramStore,
    stats: dict,
) -> str | None:
    """Create or reinforce a lesson engram from the impact of a softened memory.

    Shift 2: Forgetting that teaches. The distilled insight from softening
    becomes a persistent "lesson" engram with high stability. If a similar
    lesson already exists, reinforce it instead of creating a duplicate.

    Returns the lesson engram ID, or None if no lesson was created.
    """
    impact_text = engram.impact
    if not impact_text or len(impact_text.strip()) < 10:
        return None

    # Search for existing similar lessons
    words = [w for w in impact_text.split() if len(w) > 2 and w.isalnum()]
    if not words:
        return None

    query = " OR ".join(f'"{w}"' for w in words[:6])
    try:
        existing = store.search_fts(query, limit=10)
    except Exception:
        existing = []

    # Check if any existing engram is a lesson with similar content
    for candidate in existing:
        if candidate.id == engram.id:
            continue
        if "lesson" in candidate.tags or "distilled" in candidate.tags:
            # Reinforce existing lesson
            candidate.strength = min(1.0, candidate.strength + 0.1)
            candidate.stability = min(1.0, candidate.stability + 0.05)
            candidate.record_access()
            store.save_engram(candidate)
            # A second experience teaching the same lesson must also be
            # linked to it, or the lesson's evidence stays invisible.
            _link_distillation(engram, candidate.id, store)
            stats["lessons_reinforced"] = stats.get("lessons_reinforced", 0) + 1
            return candidate.id

    # No existing lesson found — create a new one
    from ..core.engram import Engram, MemorySource
    lesson = Engram(
        content=impact_text,
        impact=impact_text,  # For lessons, impact IS the content
        kind=EngramKind.PROCEDURAL,
        tags=list(set(engram.tags + ["lesson", "distilled"])),
        strength=0.8,
        stability=0.8,  # High stability — lessons persist
        source=MemorySource(
            type=SourceType.REFLECTION,
            confidence=engram.source.confidence,
            confidence_source=engram.source.confidence_source,
        ),
        owner_agent_id=engram.owner_agent_id,
    )

    store.save_engram(lesson)

    # Shift 2 is not the lesson existing, it is the lesson being *reachable
    # from* the experience it came from. Without this edge the distillate is
    # an orphan, resonance can never travel from a fading memory to what it
    # taught, and the count of DISTILLED_INTO edges stays at zero — which is
    # exactly what it was across every Mnemos store ever built.
    _link_distillation(engram, lesson.id, store)

    stats["lessons_created"] = stats.get("lessons_created", 0) + 1
    return lesson.id


def _link_distillation(engram: Any, lesson_id: str, store: EngramStore) -> None:
    """Connect an experience to the lesson drawn from it."""
    from ..core.types import ConnectionRelation

    already = any(
        c.target_id == lesson_id and c.relation == ConnectionRelation.DISTILLED_INTO
        for c in engram.connections
    )
    if already:
        return
    engram.add_connection(
        target_id=lesson_id,
        relation=ConnectionRelation.DISTILLED_INTO,
        strength=0.9,
        formed_by="softening",
    )
    store.save_engram(engram)


def _rule_based_soften(content: str, target_resolution: float) -> str:
    """Soften memory without LLM — rule-based fallback."""
    if target_resolution >= 0.4:
        # Keep first sentence, blur the rest
        sentences = content.split(".")
        first = sentences[0].strip() if sentences else content[:50]
        return f"{first}... [details faded]"
    else:
        # Deep impression: just the emotional residue
        words = content.split()
        key_word = words[0] if words else "something"
        return f"An impression related to {key_word}... [faded]"
