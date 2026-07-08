"""
Memory-enhanced prompt builder with token budget management.

Constructs the memory section that gets injected into an agent's system
prompt. Retrieves operational-context memories, organizes them by type, and
formats them within a specified token budget.

Token budget strategy:
1. Identity + beliefs always included (highest priority)
2. Retrieve memories relevant to the current cue
3. Fill remaining budget with scored memories, highest score first
4. Truncate individual entries if needed to fit
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.belief import Belief
from ..core.identity import AgentIdentity
from ..retrieval.reactive import ReactiveRetriever, RetrievalResult
from .belief_render import format_belief_summary_line

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


# Approximate characters per token (rough heuristic)
_CHARS_PER_TOKEN = 4


class PromptBuilder:
    """Builds memory-enhanced prompt sections within a token budget.

    The prompt builder retrieves operational-context beliefs and engrams and
    formats them into a structured text block that can be injected into the
    agent's system prompt. Beliefs render with launch-minimal challenge state.
    Review-only and audit-only rows require explicit review/admin surfaces,
    not this operating prompt path. Rendered memories are marked as
    non-fitting-eligible citations for Step 1 instrumentation.

    Usage:
        builder = PromptBuilder(store=store)
        memory_section = builder.build(
            cue="Tell me about the project architecture",
            agent_id="nova",
            token_budget=2000,
        )
    """

    def __init__(self, store: EngramStore, shared_store: Any | None = None) -> None:
        self._store = store
        self._shared_store = shared_store

    def build(
        self,
        cue: str,
        agent_id: str = "default",
        token_budget: int = 2000,
    ) -> str:
        """Build a memory-enhanced prompt section within the token budget.

        Args:
            cue: The retrieval cue (usually the user's latest message).
            agent_id: Which agent's memories to retrieve.
            token_budget: Maximum approximate token count for the section.

        Returns:
            Formatted string containing the memory section.
            Returns empty string if no relevant content found.
        """
        sections: list[str] = []
        remaining_tokens = token_budget

        # 1. IDENTITY + BELIEFS (always included, highest priority)
        identity = self._store.get_identity(agent_id)
        beliefs = self._store.get_beliefs(agent_id, active_only=True)

        identity_section = _format_identity(identity, beliefs)
        identity_tokens = _estimate_tokens(identity_section)

        if identity_section and identity_tokens <= remaining_tokens:
            sections.append(identity_section)
            remaining_tokens -= identity_tokens

        # 2. RETRIEVE MEMORIES relevant to cue
        if cue and remaining_tokens > 50:  # Need at least 50 tokens for memories
            retriever = ReactiveRetriever(self._store, shared_store=self._shared_store)
            emotional_state = self._store.get_latest_emotional_state(agent_id)
            results = retriever.retrieve(
                cue=cue,
                agent_id=agent_id,
                max_results=20,
                emotional_state=emotional_state,
            )

            # 3. FILL BUDGET with scored memories
            if results:
                memory_lines: list[str] = []
                for result in results:
                    line = _format_engram(result)
                    line_tokens = _estimate_tokens(line)
                    if line_tokens <= remaining_tokens:
                        memory_lines.append(line)
                        remaining_tokens -= line_tokens
                        _mark_retrieval_citation(
                            self._store,
                            result,
                            surface="prompt_builder",
                            metadata={
                                "agent_id": agent_id,
                                "cue": cue,
                                "tier": "rendered",
                                "fitting_eligible": False,
                            },
                        )
                    else:
                        break  # Budget exhausted

                if memory_lines:
                    memory_section = "### Relevant Memories\n" + "\n".join(memory_lines)
                    sections.append(memory_section)

        if not sections:
            return ""

        return "## Your Memory\n\n" + "\n\n".join(sections)


def _format_identity(
    identity: AgentIdentity | None,
    beliefs: list[Belief],
) -> str:
    """Format identity and beliefs into a prompt section.

    Shift 5: Uses computed identity profile (from graph topology)
    rather than LLM-generated narrative.
    """
    parts: list[str] = []

    if identity and identity.epoch_state.self_summary:
        parts.append(f"### Identity\n{identity.epoch_state.self_summary}")

    # Beliefs are already part of the identity profile summary,
    # but we include top beliefs separately for emphasis
    if beliefs:
        belief_lines = []
        for b in beliefs[:5]:
            belief_lines.append(format_belief_summary_line(b))
        if belief_lines:
            parts.append("### Beliefs\n" + "\n".join(belief_lines))

    return "\n\n".join(parts)


def _format_engram(result: RetrievalResult) -> str:
    """Format a single retrieval result as a prompt line.

    Prefers impact (the lesson/trace) over content (what happened).
    This is the key Shift 1 behavior in the prompt — the agent sees
    lessons and insights, not raw events.
    """
    engram = result.engram
    # Prefer impact (the lesson) over content (what happened)
    display = engram.impact if engram.impact else engram.content
    if len(display) > 200:
        display = display[:197] + "..."

    confidence_pct = int(engram.source.confidence * 100)
    why = result.retrieval_why.get("path") or result.retrieval_path
    return f"- {display} [{engram.kind}, confidence: {confidence_pct}%, why: {why}]"


def _estimate_tokens(text: str) -> int:
    """Rough token count estimate (4 chars ≈ 1 token)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _mark_retrieval_citation(
    store: "EngramStore",
    result: RetrievalResult,
    *,
    surface: str,
    metadata: dict[str, Any],
) -> None:
    try:
        store.mark_retrieval_citation(
            event_id=result.retrieval_event_id,
            engram_id=result.engram.id,
            surface=surface,
            metadata=metadata,
        )
    except Exception:
        try:
            store.record_instrumentation_failure("retrieval_citations")
        except Exception:
            pass
