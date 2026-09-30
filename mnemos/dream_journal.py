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

# Reports kept in use in a scope: the newest this many. Writing a report
# retires the ones older than them (``retire_old_reports``): archived, with
# their words and revision trail, never deleted. Before reports were found by
# their tag, each cycle wrote a new one instead of replacing the last, and
# they piled up: 117 in use at once on a copy of the live store (2026-09-29).
REPORTS_KEPT = 5
RETIRED_REASON = f"retired: older than the newest {REPORTS_KEPT} maintenance reports"

# Where the health card reads when a scope's report was last written: the
# runtime's own meta key for it (``MnemosRuntime._meta_key``).
WRITTEN_META = "dream_last_written_at"

# What a deep cycle that changed nothing reports. The packet leaves it out:
# "While I was away" is for when something changed.
NO_CHANGE_NARRATIVE = (
    "Mnemos checked the stored continuity; no mechanical changes were needed."
)


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
    # What recall does with them, and no more: a dormant memory comes back
    # only when a cue matches it well. "Ready to wake if needed" promised a
    # door that recall never opened.
    if engrams_dormant > 0:
        sentences.append(
            f"{engrams_dormant} {_plural(engrams_dormant, 'memory', 'memories')} "
            f"went quiet. A strong match brings {_plural(engrams_dormant, 'it', 'them')} back."
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
        sentences = [NO_CHANGE_NARRATIVE]

    narrative = " ".join(sentences)
    if len(narrative) > MAX_NARRATIVE_CHARS:
        narrative = narrative[: MAX_NARRATIVE_CHARS - 1].rstrip() + "…"
    return narrative


def report_cycle(
    store: EngramStore,
    scope: MnemosScope,
    cycle_stats: dict,
    promoted: int = 0,
) -> tuple[str | None, str | None]:
    """Report one maintenance cycle, under the journal's rule: only when it
    did something worth telling (``compose_dream_narrative``). The report
    supersedes the last one and retires those older than the newest
    ``REPORTS_KEPT`` (``write_dream_entry``), and when it was written is kept
    where the health card reads it. Returns its id and words, or
    ``(None, None)`` when there was nothing worth telling.

    A session's maintenance (``MnemosRuntime.maintain``) and the scheduled
    ``mnemos consolidate`` both report through this. The scheduled job used to
    write none, so its cycles went untold, and the briefing's newest report
    fell behind what had changed."""

    deltas = collect_belief_deltas(store, scope.agent_id, cycle_stats.get("started_at", ""))
    narrative = compose_dream_narrative(cycle_stats, deltas, promoted)
    if not narrative:
        return None, None
    entry_id = write_dream_entry(store, scope, narrative)
    store.set_meta(
        f"simple:{scope.agent_id}:{scope.person_id}:{scope.project_scope}:{WRITTEN_META}",
        datetime.now(timezone.utc).isoformat(),
    )
    return entry_id, narrative


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


def latest_dream_entry(
    store: EngramStore,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
) -> dict[str, Any] | None:
    """The newest active dream-journal report in one scope, found by its tag.

    It used to be looked for among the 50 notes that scored highest, and a
    report scores low, so once a scope held more notes than that it was never
    found again. The packet lost its report, and each cycle wrote a new one
    instead of replacing the last: 117 were active at once on a copy of a real
    store (2026-09-26).
    """

    entries = store.get_hypomnema_entries_by_tag(
        DREAM_JOURNAL_TAG,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=1,
    )
    return entries[0] if entries else None


def fetch_active_dream_entry(store: EngramStore, scope: MnemosScope) -> dict[str, Any] | None:
    """Return the active dream-journal note for a scope, if one exists."""

    return latest_dream_entry(
        store,
        agent_id=scope.agent_id,
        person_id=scope.person_id,
        project_scope=scope.project_scope,
    )


def changed_something(entry: dict[str, Any]) -> bool:
    """Whether a report says the cycle changed anything."""

    return " ".join(str(entry.get("content") or "").split()) != NO_CHANGE_NARRATIVE


def write_dream_entry(store: EngramStore, scope: MnemosScope, narrative: str) -> str:
    """Store a dream narrative, superseding any prior entry for this scope,
    and retire the reports older than the newest ``REPORTS_KEPT``, all in one
    transaction. Returns the new entry's id."""

    with store.transaction():
        prior = fetch_active_dream_entry(store, scope)
        if prior:
            entry_id = store.supersede_hypomnema_entry(
                prior["id"],
                narrative,
                reason="dream journal: newer consolidation entry",
                agent_id=scope.agent_id,
                person_id=scope.person_id,
                project_scope=scope.project_scope,
            )
        else:
            entry_id = store.write_hypomnema_entry(
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
        retire_old_reports(store, scope)
    return entry_id


def retire_old_reports(store: EngramStore, scope: MnemosScope, keep: int = REPORTS_KEPT) -> list[str]:
    """Retire this scope's maintenance reports older than the newest ``keep``
    (newest by the order the briefing's finder reads them in), and return
    their ids.

    Retired is archived (``archive_hypomnema_entry``): taken out of use, with
    its words and revision trail kept and ``RETIRED_REASON`` recorded, never
    deleted, so it can be restored. Only the dream journal's reports: an
    identity report or a note carries another tag or kind."""

    scope_args = {
        "agent_id": scope.agent_id,
        "person_id": scope.person_id,
        "project_scope": scope.project_scope,
    }
    rows = store._get_conn().execute(
        """
        SELECT id FROM hypomnema_entries
        WHERE agent_id = ? AND person_id = ? AND project_scope = ?
          AND entry_kind = 'maintenance_report' AND active = 1
          AND tags_json LIKE ?
        ORDER BY last_revised_at DESC, created_at DESC, rowid DESC
        LIMIT -1 OFFSET ?
        """,
        (*scope_args.values(), f'%"{DREAM_JOURNAL_TAG}"%', max(0, int(keep))),
    ).fetchall()
    retired = [row[0] for row in rows]
    if retired:
        with store.transaction():
            for entry_id in retired:
                store.archive_hypomnema_entry(entry_id, reason=RETIRED_REASON, **scope_args)
    return retired


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
