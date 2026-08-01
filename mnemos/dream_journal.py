"""Neutral, system-authored reports from deterministic consolidation.

Maintenance is invisible by default — connections form, details soften,
memories settle into the archive, and nobody hears about it. The dream
journal turns a maintenance cycle that did meaningful work into a short
mechanical account for the next session. It never speaks as the agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .simple_scope import MnemosScope
    from .store.sqlite_store import EngramStore

DREAM_JOURNAL_TAG = "dream-journal"
DREAM_DOMAIN = "situational"
MAX_NARRATIVE_CHARS = 700


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def compose_dream_narrative(
    cycle_stats: dict,
    belief_deltas: list[dict] | None = None,
    promoted: int = 0,
) -> str | None:
    """Render a consolidation cycle in neutral system language.

    Returns None when the cycle did nothing worth telling. Deep cycles are
    always worth telling, even when everything held steady.
    """

    discovery = cycle_stats.get("connection_discovery") or {}
    decay = cycle_stats.get("decay") or {}
    softening = cycle_stats.get("softening") or {}
    reflection = cycle_stats.get("reflection") or {}

    connections_created = int(discovery.get("connections_created", 0) or 0)
    engrams_archived = int(decay.get("engrams_archived", 0) or 0)
    engrams_dormant = int(decay.get("engrams_dormant", 0) or 0)
    engrams_softened = int(softening.get("engrams_softened", 0) or 0)
    lessons_created = int(softening.get("lessons_created", 0) or 0)
    lessons_reinforced = int(softening.get("lessons_reinforced", 0) or 0)
    thoughts_generated = int(reflection.get("thoughts_generated", 0) or 0)

    is_deep = cycle_stats.get("cycle_type") == "deep"
    noteworthy = is_deep or (
        connections_created + engrams_archived + lessons_created + promoted
    ) > 0
    if not noteworthy:
        return None

    sentences: list[str] = []
    if is_deep:
        sentences.append("Mnemos ran deep consolidation while the agent was away.")
    if connections_created > 0:
        sentences.append(
            f"Mnemos connected {connections_created} "
            f"{_plural(connections_created, 'memory', 'memories')} that belong together."
        )
    if engrams_softened > 0:
        kept = "their lessons" if (lessons_created + lessons_reinforced) > 0 else "what mattered"
        sentences.append(
            f"Mnemos softened {engrams_softened} stale "
            f"{_plural(engrams_softened, 'detail', 'details')} and kept {kept}."
        )
    if engrams_archived > 0:
        sentences.append(
            f"Mnemos moved {engrams_archived} faded "
            f"{_plural(engrams_archived, 'memory', 'memories')} into the archive."
        )
    elif engrams_dormant > 0:
        sentences.append(
            f"{engrams_dormant} {_plural(engrams_dormant, 'memory', 'memories')} "
            "went dormant, ready to wake if needed."
        )
    for delta in (belief_deltas or [])[:3]:
        old = float(delta.get("old_confidence", 0.0))
        new = float(delta.get("new_confidence", 0.0))
        verb = "strengthened" if new >= old else "softened"
        content = str(delta.get("content", ""))[:60]
        sentences.append(
            f'Mnemos recorded that the belief "{content}" {verb} '
            f"({old:.2f} -> {new:.2f})."
        )
    if promoted > 0:
        sentences.append(
            f"Mnemos promoted {promoted} continuity "
            f"{_plural(promoted, 'note', 'notes')} into durable memory."
        )
    if thoughts_generated > 0:
        sentences.append(
            f"Maintenance surfaced {thoughts_generated} new "
            f"{_plural(thoughts_generated, 'thought', 'thoughts')}."
        )

    # A deep cycle with nothing to report still deserves one true sentence.
    has_work = bool(sentences) and not (is_deep and len(sentences) == 1)
    if not has_work:
        sentences = [
            "Mnemos checked the stored continuity; no mechanical changes were needed."
        ]

    narrative = " ".join(sentences)
    if len(narrative) > MAX_NARRATIVE_CHARS:
        narrative = narrative[: MAX_NARRATIVE_CHARS - 1].rstrip() + "…"
    return narrative


def collect_belief_deltas(
    store: EngramStore,
    agent_id: str,
    since_iso: str,
    limit: int = 3,
) -> list[dict]:
    """Belief confidence shifts since a cycle started, largest first.

    Never raises: an unreadable timestamp or store hiccup yields [].
    """

    try:
        since = datetime.fromisoformat(since_iso)
    except (TypeError, ValueError):
        return []
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        deltas: list[dict] = []
        for belief in store.get_beliefs(agent_id, active_only=True):
            for revision in reversed(belief.revision_history or []):
                try:
                    moment = datetime.fromisoformat(revision.timestamp)
                except (TypeError, ValueError):
                    continue  # skip unparseable, keep looking back
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if moment >= since:
                    deltas.append(
                        {
                            "belief_id": belief.id,
                            "content": belief.content,
                            "old_confidence": revision.old_confidence,
                            "new_confidence": revision.new_confidence,
                            "reason": revision.reason,
                        }
                    )
                break  # newest parseable revision decides; older ones predate it
        deltas.sort(
            key=lambda d: abs(float(d["new_confidence"]) - float(d["old_confidence"])),
            reverse=True,
        )
        return deltas[:limit]
    except Exception:
        return []


def fetch_active_dream_entry(store: EngramStore, scope: MnemosScope) -> dict[str, Any] | None:
    """Return the active dream-journal note for a scope, if one exists."""

    entries = store.search_hypomnema(
        "",
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
        limit=50,
    )
    for entry in entries:
        if DREAM_JOURNAL_TAG in (entry.get("tags") or []):
            return entry
    return None


def write_dream_entry(store: EngramStore, scope: MnemosScope, narrative: str) -> str:
    """Store a dream narrative, superseding any prior entry for this scope."""

    prior = fetch_active_dream_entry(store, scope)
    if prior:
        return store.supersede_hypomnema_entry(
            prior["id"],
            narrative,
            reason="dream journal: newer consolidation entry",
            agent_id=scope.agent_id,
            person_id=scope.person_id,
            project_scope=scope.project_scope,
        )
    return store.write_hypomnema_entry(
        narrative,
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
        source="synthesized",
        entry_kind="maintenance_report",
        authored_by="system",
        author_id="mnemos",
        domain=DREAM_DOMAIN,
        tags=[DREAM_JOURNAL_TAG],
        confidence=0.55,
        salience=0.4,
        foundational=False,
    )


def polish_dream_entry(
    store: EngramStore,
    scope: MnemosScope,
    note_id: str,
    polished: str,
) -> None:
    """Apply a host-model rewrite to an existing dream note in place."""

    store.revise_hypomnema_entry(
        note_id,
        polished,
        reason="host-model polish of dream narrative",
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
    )
