"""Context packet assembly for turnkey Mnemos agent integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ..retrieval.reactive import ReactiveRetriever, RetrievalResult
from ..store.sqlite_store import READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW
from ..store.read_visibility import is_hypomnema_promotion_candidate

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


_CHARS_PER_TOKEN = 4
PACKET_MODE_OPERATIONAL = "operational"
PACKET_MODE_REVIEW = "review"
PacketMode = Literal["operational", "review"]
_VALID_PACKET_MODES = {PACKET_MODE_OPERATIONAL, PACKET_MODE_REVIEW}


def normalize_packet_mode(packet_mode: str = PACKET_MODE_OPERATIONAL) -> PacketMode:
    """Validate and normalize context packet visibility mode."""
    mode = (packet_mode or PACKET_MODE_OPERATIONAL).strip().lower()
    if mode not in _VALID_PACKET_MODES:
        raise ValueError(
            "packet_mode must be one of: "
            f"{PACKET_MODE_OPERATIONAL}, {PACKET_MODE_REVIEW}"
        )
    return mode  # type: ignore[return-value]


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
    packet_mode: str = PACKET_MODE_OPERATIONAL,
    max_functional: int = 10,
    max_hypomnema: int = 8,
    max_engrams: int = 6,
) -> dict[str, Any]:
    """Build the complete memory packet an agent should read before acting.

    The packet orders memory from most immediately actionable to most durable:
    functional memory, hypomnema continuity, then Mnemos engrams and beliefs.
    ``packet_mode="operational"`` withholds review prose and returns only
    review counts/source IDs; ``packet_mode="review"`` exposes review-only
    candidate prose with labels for explicit review. Retrieved engrams included
    in the packet are marked as rendered citations with
    ``fitting_eligible=False``; citation logging is record-only.
    """
    mode = normalize_packet_mode(packet_mode)
    identity = store.get_identity(agent_id)
    beliefs = store.get_beliefs(agent_id, active_only=True)
    session = store.get_memory_session(session_id) if session_id else None
    functional = store.load_functional_memories(
        query,
        session_id=session_id or None,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        exclude_needs_confirmation=mode == PACKET_MODE_OPERATIONAL,
        limit=max_functional,
    )
    hypomnema = store.search_hypomnema(
        query,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_hypomnema,
        exclude_promotion_candidates=mode == PACKET_MODE_OPERATIONAL,
    )
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
        read_visibility=(READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW),
    )
    review_proposals = store.list_proposals(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        status="pending_review",
        limit=6,
    )
    review_proposal_count = store.count_proposals(
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        status="pending_review",
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
    engrams: list[dict[str, Any]] = []
    if query.strip():
        retriever = ReactiveRetriever(store)
        emotional_state = store.get_latest_emotional_state(agent_id)
        results = retriever.retrieve(
            cue=query,
            agent_id=agent_id,
            max_results=max_engrams,
            emotional_state=emotional_state,
        )
        for result in results:
            serialized = _serialize_retrieval_result(result)
            engrams.append(serialized)
            _mark_retrieval_citation(
                store,
                result,
                surface="context_packet",
                metadata={
                    "packet_mode": mode,
                    "agent_id": agent_id,
                    "person_id": person_id,
                    "project_scope": project_scope,
                    "session_id": session_id,
                    "tier": "rendered",
                    "fitting_eligible": False,
                },
            )

    stats_read_visibility = (
        (READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW)
        if mode == PACKET_MODE_REVIEW
        else READ_VISIBILITY_OPERATIONAL
    )
    stats = store.get_stats(
        agent_id,
        person_id=person_id,
        project_scope=project_scope,
        read_visibility=stats_read_visibility,
    )
    packet: dict[str, Any] = {
        "packet_mode": mode,
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
        "review_queue": _build_review_queue(
            mode,
            review_functional,
            review_hypomnema,
            review_proposals,
            functional_count=review_functional_count,
            candidate_count=review_hypomnema_count,
            proposal_count=review_proposal_count,
        ),
        "stats": stats,
    }
    packet = _normalize_packet_visibility(packet, mode)
    if include_prompt:
        packet["prompt"] = format_context_packet(
            packet,
            token_budget=token_budget,
            packet_mode=mode,
        )
    return packet


def format_context_packet(
    packet: dict[str, Any],
    *,
    token_budget: int = 3000,
    packet_mode: str | None = None,
) -> str:
    """Format a context packet as an agent-readable prompt section.

    Operational formatting redacts review-only prose. Review formatting must be
    given an unredacted review packet; redacted operational references cannot be
    escalated back into prose by the formatter.
    """
    if packet_mode is not None:
        mode = normalize_packet_mode(packet_mode)
        packet = _normalize_packet_visibility(packet, mode)
    else:
        mode = normalize_packet_mode(packet.get("packet_mode", PACKET_MODE_OPERATIONAL))
        packet = _normalize_packet_visibility(packet, mode)
    if mode == PACKET_MODE_OPERATIONAL:
        return _format_sections_preserving_prefix(
            [
                "## Mnemos Context Packet",
                _format_scope(packet),
                _format_review(packet),
            ],
            [
                _format_operating_instructions(),
                _format_identity(packet),
                _format_functional(packet),
                _format_hypomnema(packet),
                _format_engrams(packet),
            ],
            max(800, token_budget * _CHARS_PER_TOKEN),
        )
    sections = [
        "## Mnemos Context Packet",
        _format_scope(packet),
        _format_operating_instructions(),
        _format_identity(packet),
        _format_functional(packet),
        _format_hypomnema(packet),
        _format_engrams(packet),
        _format_review(packet),
    ]
    text = _join_sections(sections)
    max_chars = max(800, token_budget * _CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80].rstrip() + "\n\n[context packet truncated to token budget]"


def _join_sections(sections: list[str]) -> str:
    return "\n\n".join(section for section in sections if section.strip())


def _format_sections_preserving_prefix(
    protected_sections: list[str],
    truncatable_sections: list[str],
    max_chars: int,
) -> str:
    protected_text = _join_sections(protected_sections)
    truncatable_text = _join_sections(truncatable_sections)
    text = _join_sections([protected_text, truncatable_text])
    if len(text) <= max_chars:
        return text

    suffix = "\n\n[context packet truncated to token budget]"
    separator_chars = 2 if protected_text and truncatable_text else 0
    available = max_chars - len(protected_text) - separator_chars - len(suffix)
    if available <= 0 or not truncatable_text:
        return protected_text.rstrip() + suffix

    truncated = truncatable_text[:available].rstrip()
    if not truncated:
        return protected_text.rstrip() + suffix
    return _join_sections([protected_text, truncated]) + suffix


def _normalize_packet_visibility(
    packet: dict[str, Any],
    packet_mode: PacketMode,
) -> dict[str, Any]:
    packet = dict(packet)
    packet["packet_mode"] = packet_mode
    if packet_mode == PACKET_MODE_REVIEW:
        review_queue = dict(packet.get("review_queue") or {})
        review_queue["packet_mode"] = packet_mode
        packet["review_queue"] = review_queue
        return packet

    packet["functional_memory"] = [
        item for item in packet.get("functional_memory") or []
        if not item.get("needs_confirmation")
    ]
    packet["hypomnema"] = [
        entry for entry in packet.get("hypomnema") or []
        if not _is_hypomnema_promotion_candidate(entry)
    ]

    review = packet.get("review_queue") or {}
    review_functional = review.get("functional_needs_confirmation") or []
    review_candidates = review.get("hypomnema_promotion_candidates") or []
    review_proposals = review.get("proposal_candidates") or []
    review_queue = _build_review_queue(
        PACKET_MODE_OPERATIONAL,
        review_functional,
        review_candidates,
        review_proposals,
    )
    if "functional_needs_confirmation_count" in review:
        review_queue["functional_needs_confirmation_count"] = int(
            review.get("functional_needs_confirmation_count") or 0
        )
    if "hypomnema_promotion_candidate_count" in review:
        review_queue["hypomnema_promotion_candidate_count"] = int(
            review.get("hypomnema_promotion_candidate_count") or 0
        )
    if "proposal_candidate_count" in review:
        review_queue["proposal_candidate_count"] = int(
            review.get("proposal_candidate_count") or 0
        )
    packet["review_queue"] = review_queue
    return packet


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


def _format_hypomnema(packet: dict[str, Any]) -> str:
    entries = packet.get("hypomnema") or []
    if not entries:
        return "### Hypomnema\n- No scoped continuity entries matched."
    lines = ["### Hypomnema"]
    for entry in entries:
        marker = "foundational, " if entry.get("foundational") else ""
        lines.append(
            f"- {entry['content']} "
            f"[{marker}{entry['domain']}, confidence {float(entry['confidence']):.2f}, "
            f"salience {float(entry['salience']):.2f}]"
        )
    return "\n".join(lines)


def _format_engrams(packet: dict[str, Any]) -> str:
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


def _is_hypomnema_promotion_candidate(entry: dict[str, Any]) -> bool:
    return is_hypomnema_promotion_candidate(
        active=entry.get("active", True),
        graduated_to_engram_id=entry.get("graduated_to_engram_id"),
        confidence=entry.get("confidence", 0),
        salience=entry.get("salience", 0),
        revision_count=entry.get("revision_count", 0),
        foundational=entry.get("foundational", False),
        domain=entry.get("domain", ""),
    )


def _build_review_queue(
    packet_mode: PacketMode,
    functional: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    proposals: list[dict[str, Any]] | None = None,
    *,
    functional_count: int | None = None,
    candidate_count: int | None = None,
    proposal_count: int | None = None,
) -> dict[str, Any]:
    proposals = proposals or []
    queue: dict[str, Any] = {
        "packet_mode": packet_mode,
        "functional_needs_confirmation_count": (
            len(functional) if functional_count is None else functional_count
        ),
        "hypomnema_promotion_candidate_count": (
            len(candidates) if candidate_count is None else candidate_count
        ),
        "proposal_candidate_count": (
            len(proposals) if proposal_count is None else proposal_count
        ),
    }
    if packet_mode == PACKET_MODE_REVIEW:
        queue["functional_needs_confirmation"] = functional
        queue["hypomnema_promotion_candidates"] = candidates
        queue["proposal_candidates"] = proposals
        return queue

    queue["functional_needs_confirmation"] = [
        _functional_review_reference(item) for item in functional
    ]
    queue["hypomnema_promotion_candidates"] = [
        _hypomnema_review_reference(item) for item in candidates
    ]
    queue["proposal_candidates"] = [
        _proposal_review_reference(item) for item in proposals
    ]
    return queue


def _functional_review_reference(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "source_id": item["id"],
        "memory_type": item["memory_type"],
        "agent_id": item["agent_id"],
        "person_id": item["person_id"],
        "project_scope": item["project_scope"],
        "read_visibility": item.get("read_visibility", READ_VISIBILITY_REVIEW),
    }


def _hypomnema_review_reference(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "source_id": item["id"],
        "domain": item["domain"],
        "source": item["source"],
        "agent_id": item["agent_id"],
        "person_id": item["person_id"],
        "project_scope": item["project_scope"],
        "read_visibility": item.get("read_visibility", READ_VISIBILITY_REVIEW),
    }


def _proposal_review_reference(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "source_id": item["id"],
        "read_visibility": item.get("read_visibility", READ_VISIBILITY_REVIEW),
    }


def _format_review(packet: dict[str, Any]) -> str:
    review = packet.get("review_queue") or {}
    mode = normalize_packet_mode(
        review.get("packet_mode") or packet.get("packet_mode", PACKET_MODE_OPERATIONAL)
    )
    functional = review.get("functional_needs_confirmation") or []
    candidates = review.get("hypomnema_promotion_candidates") or []
    proposals = review.get("proposal_candidates") or []
    functional_count = int(
        review.get("functional_needs_confirmation_count", len(functional)) or 0
    )
    candidate_count = int(
        review.get("hypomnema_promotion_candidate_count", len(candidates)) or 0
    )
    proposal_count = int(
        review.get("proposal_candidate_count", len(proposals)) or 0
    )
    if not functional_count and not candidate_count and not proposal_count:
        return "### Review Queue\n- Nothing needs review right now."
    lines = ["### Review Queue"]
    if mode == PACKET_MODE_OPERATIONAL:
        if functional_count:
            lines.append(
                f"- {functional_count} functional memory item(s) need confirmation "
                "(review-only; prose withheld)."
            )
            for item in functional:
                lines.append(
                    f"  - source_id={item['source_id']} type={item['memory_type']}"
                )
        if candidate_count:
            lines.append(
                f"- {candidate_count} hypomnema promotion candidate(s) need review "
                "(review-only; prose withheld)."
            )
            for item in candidates:
                lines.append(
                    f"  - source_id={item['source_id']} domain={item['domain']} "
                    f"source={item['source']}"
                )
        if proposal_count:
            lines.append(
                f"- {proposal_count} proposal candidate(s) need review "
                "(review-only; prose withheld)."
            )
            for item in proposals:
                lines.append(f"  - source_id={item['source_id']}")
        return "\n".join(lines)

    if mode == PACKET_MODE_REVIEW and any(
        "content" not in item for item in [*functional, *candidates]
    ):
        raise ValueError(
            "review packet cannot be formatted from redacted operational references; "
            "rebuild context packet with packet_mode='review'"
        )
    if mode == PACKET_MODE_REVIEW and any(
        "payload" not in item for item in proposals
    ):
        raise ValueError(
            "review packet cannot be formatted from redacted operational proposal references; "
            "rebuild context packet with packet_mode='review'"
        )

    for item in functional:
        lines.append(
            f"- confirm [review-only id={item['id']} source={item.get('source', '')}]: "
            f"{item['content']} [{item['memory_type']}]"
        )
    for item in candidates:
        lines.append(
            f"- promotion candidate [review-only id={item['id']} source={item['source']}]: "
            f"{item['content']} [{item['domain']}]"
        )
    for item in proposals:
        provenance = ", ".join(item.get("provenance_ids") or []) or "none"
        lines.append(
            "- proposal "
            f"[review-only id={item['id']} authority={item['source_authority']} "
            f"kind={item['kind']} domain={item['domain']} "
            f"target={item['target_surface']} blast={item['blast_radius']} "
            f"status={item['status']} provenance={provenance}]: "
            f"{_format_proposal_payload(item)}"
        )
    return "\n".join(lines)


def _format_proposal_payload(item: dict[str, Any]) -> str:
    payload = item.get("payload") or {}
    if isinstance(payload, dict) and payload.get("content"):
        return str(payload["content"])
    return str(payload)


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
    retrieval_why = dict(result.retrieval_why)
    retrieval_why.pop("event_id", None)
    return {
        "id": engram.id,
        "display": display,
        "content": engram.content,
        "impact": engram.impact,
        "kind": engram.kind,
        "score": result.score,
        "confidence": engram.source.confidence,
        "retrieval_path": result.retrieval_path,
        "retrieval_why": retrieval_why,
    }


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
