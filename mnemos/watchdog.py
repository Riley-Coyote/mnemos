"""What should be moving in a memory, and whether it is.

Every serious failure this memory has had looked like success: a maintenance
report nobody had seen for weeks, questions no one answered, lesson questions
starved behind stale ones, old servers writing by old rules. Each layer
reported healthy. Polyphonic found its equivalents only once it watched its
own queues, and this does the same here.

``watch`` looks at one scope and returns, for each thing it checks, what is
expected and what it saw. Whatever has stalled for more than a day becomes a
flag: one plain sentence and the command that fixes or inspects it. When all
is well there are no flags, and the health card and ``mnemos doctor`` print
nothing of it.

It reads only. Every statement it runs is a SELECT (``_rows`` refuses any
other), it works on a store opened read-only, and a check that cannot run says
so in its own data instead of raising: looking at memory must never change it
or fail the call that looked. A signal that would have to be recorded first
(how slow an embedding was, say) is left out rather than written.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .dream_journal import changed_something, compose_dream_narrative, latest_dream_entry
from .store.embedding_index import text_hash
from .store.sqlite_store import AUTHORS_LABELED_KEY, EngramStore

# How long something may sit still before it is flagged.
STALL = timedelta(days=1)
# How far back activity is read: maintenance cycles and older-code sessions.
WINDOW = timedelta(days=7)
# How long a lesson question may wait to be shown before it counts as starved.
LESSON_WAIT = timedelta(days=14)
# Automatic maintenance skips a cycle when one ran a few minutes before (the
# activity gate, `min_idle_minutes`), so a capture just after a cycle can wait
# for the next one. Captures this close to the last cycle are not counted as
# left without maintenance.
GATE_TOLERANCE = timedelta(hours=1)
# memory_trace arrived with maintenance code version 5 (#86). From the first
# open by such code (when it labelled the store's authors), every tool call by
# code at least as new as the store leaves a trace row. A session whose writes
# leave none runs older code.
TRACE_SINCE_VERSION = 5
# How many sessions a flag names before it says "and N more".
SESSIONS_NAMED = 3

# What each check expects, in the words the health card uses.
EXPECTED = {
    "questions": (
        "each question is answered, or declined with a verdict, within "
        f"{EngramStore.MAX_SURFACINGS} showings"
    ),
    "report": "the briefing finds a report of the latest maintenance that changed memory",
    "maintenance": "maintenance runs after captures, and its cycles change something",
    "lesson_questions": (
        f"no lesson question waits more than {LESSON_WAIT.days} days to be shown"
    ),
    "unreachable": "memory that faded into the archive stays reachable by one call",
    "older_code": (
        "every session that writes here runs code at least as new as the store expects"
    ),
    "authorship": "the agent wrote what its memories say",
    "recall_index": (
        "everything recall can return is indexed by meaning within a day of being written"
    ),
    "notes": "notes reach the briefing unless the memory they belong to went quiet or faded",
}

# The counters in a cycle's log that mean it changed memory: a link made,
# removed, retyped or strengthened; a memory that decayed, went quiet or faded;
# words softened; a lesson made or reinforced; a belief moved; a thought.
# Counts of what a pass looked at (processed, evaluated, candidates) are not
# changes.
_CHANGE_COUNTERS = {
    "connection_discovery": (
        "connections_created", "connections_removed",
        "connections_reclassified", "connections_strengthened",
    ),
    "decay": ("engrams_decayed", "engrams_dormant", "engrams_archived", "dormant_decayed"),
    "softening": ("engrams_softened", "lessons_created", "lessons_reinforced"),
    "belief_review": ("beliefs_strengthened", "beliefs_weakened"),
    "reflection": ("thoughts_generated", "narrative_updated"),
}


def watch(
    store: EngramStore,
    *,
    agent_id: str,
    person_id: str,
    project_scope: str,
    index: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every check, for one scope: ``checks`` (by name: ``expected``,
    ``seen``, ``stalled`` and the facts behind them) and ``flags`` (the
    stalled ones: ``check``, ``sentence``, ``command``). ``index`` is the
    runtime's embedding index, for recall's meaning index; without one that
    check reports recall as words only. Reads only, and never raises."""
    now = _at(now) or datetime.now(timezone.utc)
    scope = {"agent_id": agent_id, "person_id": person_id, "project_scope": project_scope}
    checks: dict[str, dict[str, Any]] = {}
    for name, check in _CHECKS:
        try:
            result = check(store, scope, now, index)
        except Exception as exc:  # the read path fails silent, and says so here
            result = {
                "seen": f"not checked ({type(exc).__name__}: {exc})",
                "stalled": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        result.setdefault("stalled", False)
        checks[name] = {"expected": EXPECTED[name], **result}
    flags = [
        {"check": name, "sentence": result["flag"], "command": result["command"]}
        for name, result in checks.items()
        if result.get("stalled") and result.get("flag")
    ]
    return {"checked_at": now.isoformat(), "checks": checks, "flags": flags}


def flag_lines(watched: Mapping[str, Any] | None) -> list[str]:
    """Each flag as the line the health card and doctor print: the sentence,
    then the command. Nothing when all is well."""
    return [
        f"{flag['sentence']} Run: {flag['command']}"
        for flag in (watched or {}).get("flags") or []
    ]


def flagged(watched: Mapping[str, Any] | None, check: str) -> bool:
    """Whether ``check`` raised a flag."""
    return any(flag.get("check") == check for flag in (watched or {}).get("flags") or [])


def note_counts(
    store: EngramStore, *, agent_id: str, person_id: str, project_scope: str,
) -> dict[str, int]:
    """Notes in one scope that reach the briefing (``live``), those hidden
    because the memory they belong to went quiet or faded (``hidden``), and
    whether anything was ever captured here (``captured``: continuity notes in
    any state, plus memories in any state). The fate rule is the store's own
    (``_NOTE_LIVE``). Reads."""
    conn = store._get_conn()
    scope = (agent_id, person_id, project_scope)
    live, hidden = _rows(conn, """
        SELECT
          COALESCE(SUM(CASE WHEN h.active = 1 AND (m.id IS NULL
            OR m.state NOT IN ('dormant', 'archived')) THEN 1 ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN h.active = 1 AND m.id IS NOT NULL
            AND m.state IN ('dormant', 'archived') THEN 1 ELSE 0 END), 0)
        FROM hypomnema_entries h LEFT JOIN engrams m ON m.id = h.graduated_to_engram_id
        WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
    """, scope)[0]
    notes = _rows(conn, """
        SELECT COUNT(*) FROM hypomnema_entries
        WHERE agent_id = ? AND person_id = ? AND project_scope = ?
          AND entry_kind = 'continuity'
    """, scope)[0][0]
    memories = _rows(conn, """
        SELECT COUNT(*) FROM engrams
        WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ?
    """, scope)[0][0]
    return {"live": int(live), "hidden": int(hidden), "captured": int(notes) + int(memories)}


def continuity_moments(
    store: EngramStore, *, agent_id: str, person_id: str, project_scope: str,
) -> dict[str, datetime | None]:
    """When continuity in one scope began and was last added to, for the
    continuity warnings to tell a stall from a quiet spell: the oldest note
    that reaches the briefing (``first_live_note``), the newest capture
    (``last_capture``: the agent's notes and captured memories), and the
    newest thing the agent wrote (``last_write``: those and its handoffs).
    None where there is none. Reads."""
    conn = store._get_conn()
    scope = (agent_id, person_id, project_scope)
    first_live = _rows(conn, """
        SELECT MIN(h.created_at) FROM hypomnema_entries h
        LEFT JOIN engrams m ON m.id = h.graduated_to_engram_id
        WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
          AND h.active = 1 AND (m.id IS NULL OR m.state NOT IN ('dormant', 'archived'))
    """, scope)[0][0]
    noted = _rows(conn, """
        SELECT MAX(CASE WHEN entry_kind = 'continuity' THEN created_at END),
               MAX(CASE WHEN entry_kind = 'handoff' THEN created_at END)
        FROM hypomnema_entries
        WHERE agent_id = ? AND person_id = ? AND project_scope = ? AND authored_by = 'agent'
    """, scope)[0]
    captured = _rows(conn, """
        SELECT MAX(created_at) FROM engrams
        WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ?
          AND CASE WHEN json_valid(source) THEN json_extract(source, '$.type') END = 'session'
    """, scope)[0][0]
    last_capture = _latest(noted[0], captured)
    return {
        "first_live_note": _at(first_live) if first_live else None,
        "last_capture": last_capture,
        "last_write": _latest(last_capture, noted[1]),
    }


# ── The checks ──


def _questions(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Questions shown as often as they ever will be, still unanswered."""
    shown_max = EngramStore.MAX_SURFACINGS
    rows = _rows(store._get_conn(), """
        SELECT target_id, surfaced_count, created_at, expires_at, answered_at
        FROM reflection_queue
        WHERE agent_id = ? AND person_id = ? AND project_scope = ?
        ORDER BY created_at ASC
    """, _scope_tuple(scope))
    total = len(rows)
    answered = sum(1 for row in rows if row["answered_at"])
    ever = [
        row for row in rows
        if not row["answered_at"] and int(row["surfaced_count"] or 0) >= shown_max
    ]
    # Flagged only while still in their lifetime: past it, a question is gone
    # whatever happened to it, and the flag clears once answering resumes.
    unanswered = [row for row in ever if not _expired(row["expires_at"], now)]
    stalled = [row for row in unanswered if _older_than(row["created_at"], now, STALL)]
    rate = _percent(answered, total)
    seen = (
        f"{_count(len(unanswered), 'question')} unanswered after {shown_max} showings; "
        f"{answered} of {total} ever answered ({rate}%)"
        if total else "no question has been asked"
    )
    result: dict[str, Any] = {
        "seen": seen,
        "asked": total,
        "answered": answered,
        "answer_rate": rate,
        "unanswered_after_showings": len(unanswered),
        "unanswered_after_showings_ever": len(ever),
        "stalled": bool(stalled),
    }
    if stalled:
        result["flag"] = (
            f"{_count(len(stalled), 'question')} {_was(len(stalled))} shown "
            f"{_times(shown_max)} and never answered, and {answered} of {total} "
            f"{_was(total)} ever answered ({rate}%)."
        )
        # A question past its showings can still be answered by its id: the
        # newest one is where to start.
        result["command"] = f'mnemos_reflect(target_id="{stalled[-1]["target_id"]}", text="…")'
    return result


def _report(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """The newest report the briefing finds, against the maintenance since."""
    report = latest_dream_entry(store, **scope)  # the briefing's own finder
    report_at = _at(report.get("last_revised_at") or report.get("created_at")) if report else None
    since = max(report_at, now - WINDOW) if report_at else now - WINDOW
    # A cycle the dream journal would have told about (its own rule) that
    # finished after the newest report is one the briefing never tells.
    untold = [
        at for at, stats in _cycles(store, scope, since)
        if compose_dream_narrative(stats) is not None
        and (report_at is None or at > report_at)
    ]
    if report_at:
        seen = f"newest report {_ago(report_at, now)} ({_day(report_at)})"
    else:
        seen = "the briefing finds no report"
    if untold:
        seen += f"; {_count(len(untold), 'cycle')} changed memory after it"
    result: dict[str, Any] = {
        "seen": seen,
        "report_id": report["id"] if report else None,
        "report_at": report_at.isoformat() if report_at else None,
        "report_age_hours": _hours(report_at, now),
        # The briefing shows a report only when it says something changed.
        "shown_in_briefing": bool(report) and changed_something(report),
        "cycles_untold": len(untold),
        "stalled": bool(untold) and _older_than(min(untold), now, STALL),
    }
    if result["stalled"]:
        first = min(untold)
        if report_at:
            said = f"the briefing's newest report is from {_day(report_at)}"
        else:
            said = "the briefing finds no report of it"
        result["flag"] = (
            f"Maintenance changed memory on {_day(first)}, but {said}."
        )
        result["command"] = "mnemos_context"
    return result


def _maintenance(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Cycles in the window and how many changed anything; captures left
    without any cycle after them."""
    cycles = _cycles(store, scope, now - WINDOW)
    changed = [at for at, stats in cycles if _changed(stats)]
    # The newest run of cycles that changed nothing, oldest first, and the
    # passes that failed in it.
    idle: list[datetime] = []
    errors: set[str] = set()
    for at, stats in reversed(cycles):
        if _changed(stats):
            break
        idle.insert(0, at)
        errors.update(key for key in stats if key.endswith("_error"))
    last_cycle = _last_cycle(store, scope)
    waiting = _captures_after(store, scope, (last_cycle + GATE_TOLERANCE) if last_cycle else None)
    seen = (
        f"{_count(len(cycles), 'cycle')} in the last {WINDOW.days} days, "
        f"{len(changed)} changed something"
    )
    if last_cycle:
        seen += f"; the last {_ago(last_cycle, now)}"
    else:
        seen += "; no cycle is recorded"
    unmaintained = bool(waiting) and _older_than(min(waiting), now, STALL)
    idle_stalled = len(idle) >= 2 and (idle[-1] - idle[0]) > STALL
    result: dict[str, Any] = {
        "seen": seen,
        "cycles": len(cycles),
        "cycles_changed": len(changed),
        "last_cycle_at": last_cycle.isoformat() if last_cycle else None,
        "last_change_at": max(changed).isoformat() if changed else None,
        "idle_cycles": len(idle),
        "pass_errors": sorted(errors),
        "captures_without_cycle": len(waiting),
        "stalled": unmaintained or idle_stalled,
    }
    if unmaintained:
        last = f"the last ran {_day(last_cycle)}" if last_cycle else "no cycle is recorded"
        result["flag"] = (
            f"{_count(len(waiting), 'memory', 'memories')} captured since {_day(min(waiting))} "
            f"{_has(len(waiting))} had no maintenance cycle; {last}."
        )
        result["command"] = "mnemos consolidate"
    elif idle_stalled:
        passes = [key[: -len("_error")].replace("_", " ") for key in sorted(errors)]
        failing = f"; its log shows these passes failing: {', '.join(passes)}" if passes else ""
        result["flag"] = (
            f"Maintenance has run {_times(len(idle))} since {_day(idle[0])} "
            f"without changing anything{failing}."
        )
        result["command"] = "mnemos consolidate"
    return result


def _lesson_questions(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Lesson questions still in line to be shown, and how long they waited."""
    rows = _rows(store._get_conn(), """
        SELECT q.target_id, q.created_at, q.expires_at FROM reflection_queue q
        JOIN engrams e ON e.id = q.target_id
        WHERE q.agent_id = ? AND q.person_id = ? AND q.project_scope = ?
          AND q.kind = 'lesson' AND q.answered_at IS NULL AND q.surfaced_count < ?
          AND e.state != 'archived'
        ORDER BY q.created_at ASC
    """, (*_scope_tuple(scope), EngramStore.MAX_SURFACINGS))
    # The same line the briefing draws its question from (pending_reflections).
    waiting = [row for row in rows if not _expired(row["expires_at"], now)]
    starved = [row for row in waiting if _older_than(row["created_at"], now, LESSON_WAIT)]
    oldest = _at(waiting[0]["created_at"]) if waiting else None
    seen = f"{_count(len(waiting), 'lesson question')} waiting to be shown"
    if oldest:
        seen += f", the oldest {_ago(oldest, now)}"
    result: dict[str, Any] = {
        "seen": seen,
        "waiting": len(waiting),
        "waiting_over_limit": len(starved),
        "oldest_at": oldest.isoformat() if oldest else None,
        "stalled": bool(starved),
    }
    if starved:
        first = starved[0]
        result["flag"] = (
            f"{_count(len(starved), 'lesson question')} {_has(len(starved))} waited more than "
            f"{LESSON_WAIT.days} days to be shown, the oldest since "
            f"{_day(_at(first['created_at']))}."
        )
        result["command"] = f'mnemos_reflect(target_id="{first["target_id"]}", text="…")'
    return result


def _unreachable(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Memory in the archive, which an ordinary recall never returns (R06)."""
    from .simple_runtime import UNREACHABLE_COMMAND  # the runtime imports this module

    faded = store.count_faded(**scope)
    return {
        "seen": (
            f"{_count(faded, 'faded memory', 'faded memories')} out of ordinary recall; "
            f"{UNREACHABLE_COMMAND} reaches them" if faded else "nothing faded out of reach"
        ),
        "count": faded,
        "command_to_reach": UNREACHABLE_COMMAND,
    }


def _older_code(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Sessions whose latest writes carry no trace, so they run older code."""
    from .code_version import MAINTENANCE_CODE_VERSION

    conn = store._get_conn()
    minimum = store.min_code_version()
    labelled = _rows(conn, "SELECT value FROM meta WHERE key = ?", (AUTHORS_LABELED_KEY,))
    anchor = _at(_decode(labelled[0][0]).get("at")) if labelled else None
    result: dict[str, Any] = {
        "running": MAINTENANCE_CODE_VERSION,
        "store_minimum": minimum,
        "sessions": [],
    }
    if anchor is None or minimum is None or minimum < TRACE_SINCE_VERSION:
        # Before code that keeps a trace has opened the store, no write leaves
        # one, and a missing trace says nothing about the code that wrote.
        result["seen"] = "not recorded in this store yet"
        return result
    since = max(anchor, now - WINDOW)
    notes = _rows(conn, """
        SELECT id, author_session, created_at FROM hypomnema_entries
        WHERE agent_id = ? AND person_id = ? AND project_scope = ?
          AND authored_by = 'agent' AND entry_kind IN ('continuity', 'handoff')
          AND author_session != '' AND created_at >= ?
        ORDER BY created_at ASC
    """, (*_scope_tuple(scope), since.isoformat()))
    traced: set[str] = set()
    last_trace: dict[str, datetime] = {}
    for row in _rows(conn, """
        SELECT session, at, written_ids FROM memory_trace
        WHERE agent_id = ? AND person_id = ? AND project_scope = ? AND at >= ?
    """, (*_scope_tuple(scope), since.isoformat())):
        traced.update(str(item) for item in _decode(row["written_ids"], []))
        at = _at(row["at"])
        if at and row["session"] and (row["session"] not in last_trace or at > last_trace[row["session"]]):
            last_trace[row["session"]] = at
    latest: dict[str, Any] = {}
    untraced: dict[str, int] = {}
    for row in notes:
        latest[row["author_session"]] = row
        if row["id"] not in traced:
            untraced[row["author_session"]] = untraced.get(row["author_session"], 0) + 1
    sessions = []
    for session, row in latest.items():
        wrote = _at(row["created_at"])
        if row["id"] in traced or wrote is None:
            continue
        if session in last_trace and last_trace[session] >= wrote:
            continue  # it left a trace since: current code (one lost trace row)
        sessions.append({
            "session": session,
            "below_version": minimum,
            "last_wrote_at": wrote.isoformat(),
            "writes_without_trace": untraced.get(session, 0),
        })
    sessions.sort(key=lambda item: item["last_wrote_at"], reverse=True)
    active = [item for item in sessions if not _older_than(item["last_wrote_at"], now, STALL)]
    result["sessions"] = sessions
    result["seen"] = (
        f"{_count(len(sessions), 'session')} wrote with code below version {minimum} "
        f"in the last {WINDOW.days} days" if sessions
        else f"every session that wrote in the last {WINDOW.days} days ran current code"
    )
    if active:
        named = "; ".join(
            f"{item['session']} last wrote {_ago(_at(item['last_wrote_at']), now)}"
            for item in active[:SESSIONS_NAMED]
        )
        more = f"; and {len(active) - SESSIONS_NAMED} more" if len(active) > SESSIONS_NAMED else ""
        result["stalled"] = True
        result["flag"] = (
            f"{_count(len(active), 'session')} still {'writes' if len(active) == 1 else 'write'} "
            f"with code older than the store (below version {minimum}) and "
            f"{'needs' if len(active) == 1 else 'need'} a restart: {named}{more}."
        )
        result["command"] = "claude --resume <session id>"
    return result


def _authorship(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Who wrote the memories recall can return (R05)."""
    rows = _rows(store._get_conn(), """
        SELECT author_kind, COUNT(*) FROM engrams
        WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ?
          AND state IN ('active', 'dormant')
        GROUP BY author_kind
    """, _scope_tuple(scope))
    kinds = {str(row[0] or "unknown"): int(row[1]) for row in rows}
    total = sum(kinds.values())
    others = {kind: count for kind, count in kinds.items() if kind != "agent"}
    not_agent = sum(others.values())
    by = ", ".join(f"{count} {kind}" for kind, count in sorted(others.items(), key=lambda i: -i[1]))
    return {
        "seen": (
            f"{not_agent} of {total} live memories ({_percent(not_agent, total)}%) "
            f"not written by the agent" + (f": {by}" if by else "")
        ),
        "memories": total,
        "not_by_agent": not_agent,
        "share_not_by_agent": _percent(not_agent, total),
        "by_kind": kinds,
    }


def _recall_index(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """What waits for recall's meaning index (R08), and since when."""
    from .simple_runtime import recall_index_items, recall_index_meta_key  # imports this module

    embedder = getattr(index, "_embedder", None)
    if index is None or embedder is None or not hasattr(index, "passage_hashes"):
        return {"seen": "no embedding backend here: recall finds by words only", "waiting": 0}
    items = [(item_id, text) for item_id, text in recall_index_items(store, **scope) if (text or "").strip()]
    stored = index.passage_hashes(item_id for item_id, _ in items)
    waiting = [item_id for item_id, text in items if stored.get(item_id) != text_hash(text)]
    written = _words_written_at(store, waiting)
    late = sorted(at for at in written.values() if _older_than(at, now, STALL))
    oldest = min(written.values()) if written else None
    last_pass = _decode(store.get_meta(recall_index_meta_key(**scope)))
    skipped = last_pass.get("skipped") if last_pass else None
    seen = f"{_count(len(waiting), 'item')} of {len(items)} waiting"
    if oldest:
        seen += f", the oldest written {_ago(oldest, now)}"
    result: dict[str, Any] = {
        "seen": seen,
        "items": len(items),
        "waiting": len(waiting),
        "waiting_over_a_day": len(late),
        "oldest_waiting_at": oldest.isoformat() if oldest else None,
        "model": getattr(embedder, "model_name", None),
        "last_pass": last_pass or None,
        "stalled": bool(late),
    }
    if late:
        why = f"; the last pass could not embed anything: {skipped.rstrip('.')}" if skipped else ""
        result["flag"] = (
            f"{_count(len(late), 'memory or note', 'memories and notes')} "
            f"{_has(len(late))} waited more than a day for recall's meaning index, "
            f"the oldest since {_day(late[0])}{why}."
        )
        result["command"] = "mnemos embeddings index"
    return result


def _notes(store: EngramStore, scope: dict[str, str], now: datetime, index: Any) -> dict:
    """Notes that reach the briefing, and the ones the fate rule hides (R07)."""
    counts = note_counts(store, **scope)
    seen = f"{counts['live']} live, {counts['hidden']} hidden while their memories are quiet or faded"
    if not counts["captured"]:
        seen = "nothing has been captured here"
    return {"seen": seen, **counts}


_CHECKS: Sequence[tuple[str, Callable[..., dict]]] = (
    ("questions", _questions),
    ("report", _report),
    ("maintenance", _maintenance),
    ("lesson_questions", _lesson_questions),
    ("unreachable", _unreachable),
    ("older_code", _older_code),
    ("authorship", _authorship),
    ("recall_index", _recall_index),
    ("notes", _notes),
)


# ── Reading ──


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[Any]:
    """Run one read. Anything but a SELECT is refused before it reaches the
    database: the watchdog never writes."""
    if sql.lstrip().split(None, 1)[0].upper() not in ("SELECT", "WITH"):
        raise ValueError("the watchdog only reads")
    return conn.execute(sql, tuple(params)).fetchall()


def _scope_tuple(scope: Mapping[str, str]) -> tuple[str, str, str]:
    return (scope["agent_id"], scope["person_id"], scope["project_scope"])


def _cycles(
    store: EngramStore, scope: Mapping[str, str], since: datetime,
) -> list[tuple[datetime, dict[str, Any]]]:
    """This scope's logged maintenance cycles finished since ``since``, oldest
    first, with their stats."""
    cycles = []
    for row in _rows(store._get_conn(), """
        SELECT completed_at, stats FROM consolidation_log
        WHERE pass_name = 'cycle' AND agent_id = ? AND person_id = ? AND project_scope = ?
          AND completed_at >= ?
        ORDER BY completed_at ASC
    """, (*_scope_tuple(scope), since.isoformat())):
        at = _at(row["completed_at"])
        if at is not None:
            cycles.append((at, _decode(row["stats"])))
    return cycles


def _last_cycle(store: EngramStore, scope: Mapping[str, str]) -> datetime | None:
    rows = _rows(store._get_conn(), """
        SELECT MAX(completed_at) FROM consolidation_log
        WHERE pass_name = 'cycle' AND agent_id = ? AND person_id = ? AND project_scope = ?
    """, _scope_tuple(scope))
    return _at(rows[0][0]) if rows else None


def _changed(stats: Mapping[str, Any]) -> bool:
    """Whether a cycle's log says it changed memory."""
    for name, counters in _CHANGE_COUNTERS.items():
        section = stats.get(name)
        if not isinstance(section, Mapping):
            continue
        for counter in counters:
            value = section.get(counter)
            if isinstance(value, bool):
                if value:
                    return True
            elif isinstance(value, (int, float)) and value > 0:
                return True
    return False


def _captures_after(
    store: EngramStore, scope: Mapping[str, str], after: datetime | None,
) -> list[datetime]:
    """When each capture or correction since ``after`` (every one, with None)
    was written: the writes that set off automatic maintenance."""
    sql = """
        SELECT created_at FROM engrams
        WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ?
          AND CASE WHEN json_valid(source) THEN json_extract(source, '$.type') END = 'session'
    """
    params: list[Any] = list(_scope_tuple(scope))
    if after is not None:
        sql += " AND created_at > ?"
        params.append(after.isoformat())
    return sorted(at for at in (_at(row[0]) for row in _rows(store._get_conn(), sql, params)) if at)


def _words_written_at(store: EngramStore, ids: Sequence[str]) -> dict[str, datetime]:
    """When each item's present words were written, as near as the store
    says: a memory's newest version or its creation; a note's (a handoff's
    too) last revision. A note rewritten in place drops its passages with its
    old words (``revise_hypomnema_entry``), so it waits from then, not from
    when it was first written. A later date only ever makes an item look
    younger, never stalled when it is not."""
    conn = store._get_conn()
    written: dict[str, datetime] = {}
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), 400):
        chunk = ordered[start:start + 400]
        marks = ", ".join("?" for _ in chunk)
        for row in _rows(conn, f"""
            SELECT e.id, e.created_at, MAX(v.changed_at) FROM engrams e
            LEFT JOIN versions v ON v.engram_id = e.id
            WHERE e.id IN ({marks}) GROUP BY e.id
        """, chunk):
            moments = [at for at in (_at(row[1]), _at(row[2])) if at]
            if moments:
                written[row[0]] = max(moments)
        for row in _rows(conn, f"""
            SELECT id, created_at, last_revised_at FROM hypomnema_entries
            WHERE id IN ({marks})
        """, chunk):
            at = _at(row["last_revised_at"]) or _at(row["created_at"])
            if at:
                written[row["id"]] = at
    return written


# ── Words ──


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _latest(*values: Any) -> datetime | None:
    moments = [moment for moment in (_at(value) for value in values if value) if moment]
    return max(moments) if moments else None


def _older_than(value: Any, now: datetime, span: timedelta) -> bool:
    moment = _at(value)
    return moment is not None and now - moment > span


def _expired(value: Any, now: datetime) -> bool:
    moment = _at(value) if value else None
    return moment is not None and moment <= now


def _hours(moment: datetime | None, now: datetime) -> float | None:
    return round((now - moment).total_seconds() / 3600, 1) if moment else None


def _decode(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        decoded = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def _percent(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def _was(n: int) -> str:
    return "was" if n == 1 else "were"


def _has(n: int) -> str:
    return "has" if n == 1 else "have"


def _times(n: int) -> str:
    return {1: "once", 2: "twice", 3: "three times"}.get(n, f"{n:,} times")


def _day(moment: datetime | None) -> str:
    return moment.date().isoformat() if moment else "never"


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"
