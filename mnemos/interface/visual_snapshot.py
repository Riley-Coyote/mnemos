"""Inline visual snapshots for Mnemos memory state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .belief_render import format_belief_summary_line
from ..store.sqlite_store import READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def build_memory_visual_snapshot(
    store: "EngramStore",
    *,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str = "",
    max_items: int = 6,
) -> str:
    """Return an operational Markdown/Mermaid snapshot for inline chat.

    Review queues are represented by counts/source IDs only; pending prose
    stays behind explicit review packet surfaces. Visible beliefs include their
    deterministic challenge state.
    """
    stats = store.get_stats(
        agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=READ_VISIBILITY_OPERATIONAL,
    )
    functional = store.load_functional_memories(
        "",
        session_id=session_id or None,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        exclude_needs_confirmation=True,
        limit=max_items,
    )
    hypomnema = store.search_hypomnema(
        "",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        exclude_promotion_candidates=True,
        limit=max_items,
    )
    engrams = store.get_active_engrams(agent_id=agent_id, limit=max_items)
    beliefs = store.get_beliefs(agent_id=agent_id, active_only=True)[:max_items]
    review = store.load_functional_memories(
        "",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        needs_confirmation_only=True,
        limit=max_items,
    )
    candidates = store.get_hypomnema_promotion_candidates(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_items,
        read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
    )
    proposals = store.list_proposals(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        status="pending_review",
        limit=max_items,
    )
    review_functional_count = store.get_functional_stats(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
    )["functional_needs_confirmation"]
    review_hypomnema_count = store.get_hypomnema_stats(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
    )["hypomnema_promotion_candidates"]
    review_proposal_count = store.count_proposals(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        status="pending_review",
    )

    diagram = _build_mermaid(
        stats,
        functional,
        hypomnema,
        engrams,
        review_count=review_functional_count
        + review_hypomnema_count
        + review_proposal_count,
    )
    lists = [
        _format_items("Functional Memory", functional, "memory_type"),
        _format_items("Hypomnema", hypomnema, "domain"),
        _format_engrams(engrams),
        _format_beliefs(beliefs),
        _format_review(
            review,
            candidates,
            proposals,
            functional_count=review_functional_count,
            candidate_count=review_hypomnema_count,
            proposal_count=review_proposal_count,
        ),
    ]
    scope = f"`{agent_id}` / `{person_id}` / `{project_scope}`"
    if session_id:
        scope += f" / session `{session_id}`"
    return (
        f"## Mnemos Visual Snapshot\n\n"
        f"Scope: {scope}\n\n"
        f"{diagram}\n\n" + "\n\n".join(section for section in lists if section)
    )


def _build_mermaid(
    stats: dict[str, Any],
    functional: list[dict[str, Any]],
    hypomnema: list[dict[str, Any]],
    engrams: list[Any],
    *,
    review_count: int,
) -> str:
    fm_count = stats.get("functional_active", len(functional))
    hyp_count = stats.get("hypomnema_active", len(hypomnema))
    engram_count = stats.get("engrams_active", len(engrams))
    belief_count = stats.get("beliefs_active", 0)
    return f"""```mermaid
flowchart LR
  Human["Human + conversation"] --> FM["Functional memory<br/>{fm_count} active"]
  FM --> H["Hypomnema<br/>{hyp_count} scoped entries"]
  H --> M["Mnemos graph<br/>{engram_count} engrams"]
  M --> I["Identity profile<br/>{belief_count} active beliefs"]
  M --> S["Substrate<br/>decay, reflection, consolidation"]
  R["Review queue<br/>{review_count} items"] --> FM
  R --> H
  H -. explicit promotion .-> M
```"""


def _format_items(title: str, items: list[dict[str, Any]], label_key: str) -> str:
    if not items:
        return f"### {title}\n- Empty for this scope."
    lines = [f"### {title}"]
    for item in items:
        content = item.get("content", "")
        if len(content) > 140:
            content = content[:137] + "..."
        label = item.get(label_key, "item")
        lines.append(f"- {content} [{label}]")
    return "\n".join(lines)


def _format_engrams(engrams: list[Any]) -> str:
    if not engrams:
        return "### Mnemos Engrams\n- Empty for this agent."
    lines = ["### Mnemos Engrams"]
    for engram in engrams:
        content = engram.impact or engram.content
        if len(content) > 140:
            content = content[:137] + "..."
        lines.append(f"- {content} [{engram.kind}]")
    return "\n".join(lines)


def _format_beliefs(beliefs: list[Any]) -> str:
    if not beliefs:
        return "### Identity Signals\n- No active beliefs yet."
    lines = ["### Identity Signals"]
    for belief in beliefs:
        lines.append(format_belief_summary_line(belief))
    return "\n".join(lines)


def _format_review(
    functional: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    *,
    functional_count: int | None = None,
    candidate_count: int | None = None,
    proposal_count: int | None = None,
) -> str:
    functional_total = len(functional) if functional_count is None else functional_count
    candidate_total = len(candidates) if candidate_count is None else candidate_count
    proposal_total = len(proposals) if proposal_count is None else proposal_count
    if not functional_total and not candidate_total and not proposal_total:
        return "### Review Queue\n- Clear."
    lines = ["### Review Queue"]
    if functional_total:
        lines.append(
            f"- {functional_total} functional memory item(s) need confirmation "
            "(review-only; prose withheld)."
        )
        for item in functional:
            lines.append(f"  - source_id={item['id']} type={item['memory_type']}")
    if candidate_total:
        lines.append(
            f"- {candidate_total} hypomnema promotion candidate(s) need review "
            "(review-only; prose withheld)."
        )
        for item in candidates:
            lines.append(
                f"  - source_id={item['id']} domain={item['domain']} "
                f"source={item['source']}"
            )
    if proposal_total:
        lines.append(
            f"- {proposal_total} proposal candidate(s) need review "
            "(review-only; prose withheld)."
        )
        for item in proposals:
            lines.append(f"  - source_id={item['id']}")
    return "\n".join(lines)
