"""Context packet assembly for turnkey Mnemos agent integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..dream_journal import DREAM_JOURNAL_TAG
from ..retrieval.reactive import ReactiveRetriever, RetrievalResult

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


_CHARS_PER_TOKEN = 4


def build_context_packet(
    store: "EngramStore",
    query: str,
    *,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str = "",
    token_budget: int = 3000,
    include_prompt: bool = True,
    include_engrams: bool = True,
    include_reflections: bool = True,
    mark_surfaced: bool = True,
    max_functional: int = 10,
    max_hypomnema: int = 8,
    max_engrams: int = 6,
) -> dict[str, Any]:
    """Build the complete memory packet an agent should read before acting.

    The packet orders memory from most immediately actionable to most durable:
    functional memory, hypomnema continuity, then Mnemos engrams and beliefs.

    ``include_engrams=False`` returns continuity only. Mnemos is a continuity
    and identity layer, usually running alongside whatever memory system the
    human already has; the long-term graph accumulates general recall that can
    crowd the scoped continuity out of a session-start packet. On one live
    store the graph section spent five of six slots on paraphrases of a single
    fact. Continuity-only keeps what the agent could not reconstruct from
    anywhere else.
    """
    identity = store.get_identity(agent_id)
    beliefs = store.get_beliefs(agent_id, active_only=True)
    session = store.get_memory_session(session_id) if session_id else None
    functional = store.load_functional_memories(
        query,
        session_id=session_id or None,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_functional,
    )
    # Fetch extra so dropping dream-journal notes still leaves a full section.
    hypomnema = store.search_hypomnema(
        query,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_hypomnema + 4,
    )
    # Dream-journal entries are consolidation diary ("I connected 148 memories
    # that belong together"), not continuity about the human or the work.
    # runtime.context() renders them in their own section and excludes them
    # here; this path was showing them as ordinary continuity notes.
    hypomnema = [
        entry for entry in hypomnema
        if DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
    ][:max_hypomnema]
    review_functional = store.load_functional_memories(
        "",
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        needs_confirmation_only=True,
        limit=6,
    )
    review_hypomnema = store.get_hypomnema_promotion_candidates(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=6,
    )

    engrams: list[dict[str, Any]] = []
    if include_engrams and query.strip():
        retriever = ReactiveRetriever(store)
        emotional_state = store.get_latest_emotional_state(agent_id)
        results = retriever.retrieve(
            cue=query,
            agent_id=agent_id,
            max_results=max_engrams,
            emotional_state=emotional_state,
        )
        engrams = [_serialize_retrieval_result(result) for result in results]

    # Work the agent's memory is waiting on it for. This is the path the
    # SessionStart hook uses, so omitting it here meant the reflection loop
    # existed but never reached the agent that most needs it.
    reflections: list[dict[str, Any]] = []
    if include_reflections:
        try:
            reflections = store.pending_reflections(
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                limit=2,
            )
            if reflections and mark_surfaced:
                store.mark_reflections_surfaced([r["id"] for r in reflections])
        except Exception:
            # A packet must never fail because of the reflection queue.
            reflections = []

    stats = store.get_stats(agent_id)
    packet: dict[str, Any] = {
        "include_engrams": include_engrams,
        "scope": {
            "agent_id": agent_id,
            "person_id": person_id,
            "project_scope": project_scope,
            "session_id": session_id,
        },
        "query": query,
        "session": session,
        "identity": _serialize_identity(identity),
        "beliefs": [_serialize_belief(b) for b in beliefs[:8]],
        "functional_memory": functional,
        "hypomnema": hypomnema,
        "mnemos_engrams": engrams,
        "reflections": reflections,
        "review_queue": {
            "functional_needs_confirmation": review_functional,
            "hypomnema_promotion_candidates": review_hypomnema,
        },
        "stats": stats,
    }
    if include_prompt:
        packet["prompt"] = format_context_packet(packet, token_budget=token_budget)
    return packet


def format_context_packet(packet: dict[str, Any], *, token_budget: int = 3000) -> str:
    """Format a context packet as an agent-readable prompt section."""
    sections = [
        "## Mnemos Context Packet",
        _format_scope(packet),
        _format_operating_instructions(),
        _format_identity(packet),
        _format_functional(packet),
        _format_hypomnema(packet),
        _format_engrams(packet),
        _format_reflections(packet),
        _format_review(packet),
    ]
    text = "\n\n".join(section for section in sections if section.strip())
    max_chars = max(800, token_budget * _CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80].rstrip() + "\n\n[context packet truncated to token budget]"


def _format_scope(packet: dict[str, Any]) -> str:
    scope = packet["scope"]
    session = packet.get("session") or {}
    lines = [
        "### Scope",
        f"- agent: {scope['agent_id']}",
        f"- person: {scope['person_id']}",
        f"- project: {scope['project_scope']}",
    ]
    if scope.get("session_id"):
        title = session.get("title") or "(untitled)"
        lines.append(f"- session: {scope['session_id']} / {title}")
    return "\n".join(lines)


def _format_operating_instructions() -> str:
    return (
        "### How To Use This Memory\n"
        "- Functional memory is the live working set for this session or task.\n"
        "- Hypomnema is scoped continuity: durable enough to carry forward, still easy to revise.\n"
        "- Mnemos engrams are the long-term graph. Promote only stable, repeatedly useful continuity.\n"
        "- If the human corrects a memory, update functional memory first, then revise hypomnema."
    )


def _format_identity(packet: dict[str, Any]) -> str:
    lines = ["### Identity And Beliefs"]
    identity = packet.get("identity") or {}
    if identity.get("self_summary"):
        lines.append(identity["self_summary"])
    beliefs = packet.get("beliefs") or []
    if beliefs:
        for belief in beliefs[:6]:
            pct = int(float(belief["confidence"]) * 100)
            lines.append(f"- {belief['content']} [{belief['domain']}, {pct}%]")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _format_functional(packet: dict[str, Any]) -> str:
    memories = packet.get("functional_memory") or []
    if not memories:
        return "### Functional Memory\n- No active functional memory for this scope."
    lines = ["### Functional Memory"]
    for item in memories:
        flags = []
        if item.get("pinned"):
            flags.append("pinned")
        if item.get("needs_confirmation"):
            flags.append("needs confirmation")
        flag_text = f" / {', '.join(flags)}" if flags else ""
        lines.append(
            f"- {item['content']} "
            f"[{item['memory_type']}, confidence {float(item['confidence']):.2f}, "
            f"salience {float(item['salience']):.2f}{flag_text}]"
        )
    return "\n".join(lines)


# A single continuity note can be thousands of characters. Left whole, two
# or three of them consume the entire packet and the rest — along with any
# section after them — is hard-cut mid-word by the budget clamp. Capping each
# note trades depth in one entry for the breadth the packet exists to give;
# the full text is always one mnemos_recall away.
_MAX_NOTE_CHARS = 420


def _clip(text: str, limit: int = _MAX_NOTE_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    # Cut on a word boundary so a note never ends mid-word.
    head = collapsed[:limit].rsplit(" ", 1)[0]
    return f"{head} […]"


def _format_hypomnema(packet: dict[str, Any]) -> str:
    entries = packet.get("hypomnema") or []
    if not entries:
        return "### Hypomnema\n- No scoped continuity entries matched."
    lines = ["### Hypomnema"]
    for entry in entries:
        # The domain is itself sometimes "foundational" — don't say it twice.
        marker = (
            "foundational, "
            if entry.get("foundational") and entry.get("domain") != "foundational"
            else ""
        )
        lines.append(
            f"- {_clip(entry['content'])} "
            f"[{marker}{entry['domain']}, confidence {float(entry['confidence']):.2f}, "
            f"salience {float(entry['salience']):.2f}]"
        )
    return "\n".join(lines)


def _format_engrams(packet: dict[str, Any]) -> str:
    # Continuity-only packets omit the section rather than announcing an
    # empty one — a heading saying nothing was found is still noise in a
    # block injected at the top of every session.
    if not packet.get("include_engrams", True):
        return ""
    engrams = packet.get("mnemos_engrams") or []
    if not engrams:
        return "### Mnemos Graph\n- No long-term engrams were retrieved for this cue."
    lines = ["### Mnemos Graph"]
    for item in engrams:
        confidence = int(float(item["confidence"]) * 100)
        lines.append(
            f"- {item['display']} "
            f"[{item['kind']}, score {float(item['score']):.2f}, confidence {confidence}%]"
        )
    return "\n".join(lines)


def _format_reflections(packet: dict[str, Any]) -> str:
    """What the agent's own memory is waiting on it for.

    Distinct from the review queue below, which is work for the human.
    This is work only the agent can do: Mnemos never calls a model, so a
    memory that needs judgement asks the mind that made it.
    """
    items = packet.get("reflections") or []
    if not items:
        return ""
    lines = ["### Waiting On You"]
    for item in items:
        lines.append(f'- "{item["excerpt"]}"')
        lines.append(f"  {item['prompt']}")
        lines.append(f'  mnemos_reflect(target_id="{item["target_id"]}", text="…")')
    lines.append(
        "Answer in your own words if one comes. If nothing true does, leave it — "
        "these fade on their own."
    )
    return "\n".join(lines)


def _format_review(packet: dict[str, Any]) -> str:
    review = packet.get("review_queue") or {}
    functional = review.get("functional_needs_confirmation") or []
    candidates = review.get("hypomnema_promotion_candidates") or []
    if not functional and not candidates:
        return "### Review Queue\n- Nothing needs review right now."
    lines = ["### Review Queue"]
    for item in functional:
        lines.append(f"- confirm: {item['content']} [{item['memory_type']}]")
    for item in candidates:
        lines.append(f"- promotion candidate: {item['content']} [{item['domain']}]")
    return "\n".join(lines)


def _serialize_identity(identity: Any | None) -> dict[str, Any]:
    if identity is None:
        return {}
    summary = getattr(identity.epoch_state, "self_summary", "")
    return {
        "self_summary": summary,
        "agent_id": identity.memory_profile.agent_id,
    }


def _serialize_belief(belief: Any) -> dict[str, Any]:
    return {
        "id": belief.id,
        "content": belief.content,
        "confidence": belief.confidence,
        "domain": belief.domain,
    }


def _serialize_retrieval_result(result: RetrievalResult) -> dict[str, Any]:
    engram = result.engram
    display = engram.impact or engram.content
    if len(display) > 240:
        display = display[:237] + "..."
    return {
        "id": engram.id,
        "display": display,
        "content": engram.content,
        "impact": engram.impact,
        "kind": engram.kind,
        "score": result.score,
        "confidence": engram.source.confidence,
        "retrieval_path": result.retrieval_path,
    }
