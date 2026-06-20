"""Inline visual snapshots for Mnemos memory state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    """Return a Markdown/Mermaid snapshot that can be shown inline in chat."""
    stats = store.get_stats(agent_id)
    functional = store.load_functional_memories(
        "",
        session_id=session_id or None,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_items,
    )
    hypomnema = store.search_hypomnema(
        "",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
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
    )

    diagram = _build_mermaid(stats, functional, hypomnema, engrams, review, candidates)
    lists = [
        _format_items("Functional Memory", functional, "memory_type"),
        _format_items("Hypomnema", hypomnema, "domain"),
        _format_engrams(engrams),
        _format_beliefs(beliefs),
        _format_review(review, candidates),
    ]
    scope = f"`{agent_id}` / `{person_id}` / `{project_scope}`"
    if session_id:
        scope += f" / session `{session_id}`"
    return (
        f"## Mnemos Visual Snapshot\n\n"
        f"Scope: {scope}\n\n"
        f"{diagram}\n\n"
        + "\n\n".join(section for section in lists if section)
    )


def _build_mermaid(
    stats: dict[str, Any],
    functional: list[dict[str, Any]],
    hypomnema: list[dict[str, Any]],
    engrams: list[Any],
    review: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    fm_count = stats.get("functional_active", len(functional))
    hyp_count = stats.get("hypomnema_active", len(hypomnema))
    engram_count = stats.get("engrams_active", len(engrams))
    belief_count = stats.get("beliefs_active", 0)
    review_count = len(review) + len(candidates)
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
        pct = int(float(belief.confidence) * 100)
        lines.append(f"- {belief.content} [{belief.domain}, {pct}%]")
    return "\n".join(lines)


def _format_review(
    functional: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    if not functional and not candidates:
        return "### Review Queue\n- Clear."
    lines = ["### Review Queue"]
    for item in functional:
        lines.append(f"- confirm: {item['content']} [{item['memory_type']}]")
    for item in candidates:
        lines.append(f"- promote: {item['content']} [{item['domain']}]")
    return "\n".join(lines)
