"""The session-start briefing: one builder for the hook and ``mnemos_context``.

The briefing is where this memory does its work. An agent reads it at the start
of every session; recall is called far less often. It holds what matters and
nothing else, in the order the reader needs it, and a section with nothing to
say is left out rather than announced empty:

1. Where you left off: the reader's own handoff first, whole, then up to two
   notes other sessions left in the last three days, each signed with its
   model and age.
2. Who you're with: first what the agent marked standing, how the human wants
   it to work in every session: the newest mark first, up to five, one line
   each in the memory's own words, with its id, and how to list the rest.
   Then the durable few foundational notes.
3. What you're carrying: up to three notes or lessons, ranked by the words they
   share with the folder and repository the session works in, then by recency.
   One of them is a concrete, dated episode whenever there is one.
4. Beliefs: each once, with its confidence and its id.
5. One question: at most one thing the agent's memory is waiting on it for.
6. While you were away: the latest maintenance report, when it changed
   something.

The SessionStart hook injects this text and ``mnemos_context`` returns it, so the
two can no longer disagree. Building it never runs maintenance. It stays under
``PACKET_MAX_CHARS``: the reader's handoff is never cut, notes are cut at a
sentence boundary with their id, and ``mnemos_recall(<id>)`` returns a note
whole.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import code_version
from ..authorship import (
    clean_model_id,
    clean_session_id,
    display_name,
    from_same_session,
    lesson_signature,
    note_signature,
    same_model,
)
from ..dream_journal import DREAM_JOURNAL_TAG, changed_something, latest_dream_entry
from ..retrieval.reactive import ReactiveRetriever, RetrievalResult
from ..store.fts import distinctive_terms, is_common

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


_CHARS_PER_TOKEN = 4

# A briefing longer than this is no longer read closely. The reader's handoff
# is never cut, so a handoff longer than this alone makes a longer packet.
PACKET_MAX_CHARS = 6000
DEFAULT_TOKEN_BUDGET = PACKET_MAX_CHARS // _CHARS_PER_TOKEN

# At most this many questions per packet. A packet that asks for work every
# time becomes a chore list appended to every conversation.
PACKET_QUESTIONS = 1

_FOUNDATIONAL_SHOWN = 3
_CARRYING_SHOWN = 3
_BELIEFS_SHOWN = 6

# What the agent marked standing: how the human wants it to work in every
# session, not just now. A standing rule is obeyed, not recalled, so no usage
# signal (reinforcement, recency, recall) can be trusted to bring it here; the
# agent's own mark does. The newest marks open "Who you're with", this many,
# one line each, and the line says how to list the rest.
STANDING_SHOWN = 5
# How long a standing line may run, cut at a sentence boundary. Each is a rule
# to follow in this session, so it is not shortened to make room: when the
# packet must lose something, these are the last lines to go.
STANDING_CHARS = 320
STANDING_LABEL = "Standing, how the human wants you to work in every session:"
STANDING_LIST_CALL = 'mnemos_recall(query="", standing=true)'
# A capture keeps its context after its words, after a blank line. A standing
# line is the words themselves.
_CONTEXT_MARK = "\n\nContext: "

# How long a note may run before it is cut, tried in turn until the packet fits.
# A typical note on a real store is about a thousand characters.
_NOTE_CHARS = (420, 320, 240, 160, 100)
# Other sessions' handoffs are one short line each: enough to tell which thread
# it is and who left it. Shown whole, three handoffs would fill most of the
# packet (a typical one is about 2,000 characters).
_OTHER_HANDOFF_CHARS = 280

_HEADER = "## Mnemos Context Packet"
COLLEAGUE_LINE = (
    "A colleague's note is theirs: take what's useful and don't claim its work as yours."
)

# Whose note is it, as the reader should take it.
_OWN = "own"
_COLLEAGUE = "colleague"
_UNPLACED = "unplaced"


def build_context_packet(
    store: "EngramStore",
    query: str = "",
    *,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    session_id: str = "",
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    include_prompt: bool = True,
    include_engrams: bool = True,
    include_reflections: bool = True,
    mark_surfaced: bool = True,
    max_functional: int = 10,
    max_hypomnema: int = 8,
    max_engrams: int = 6,
    reader_model: str = "",
    reader_session: str = "",
    workdir: str = "",
    older_than_store: bool | None = None,
) -> dict[str, Any]:
    """Build the briefing an agent reads before its first turn.

    ``reader_model`` and ``reader_session`` are the model and harness session
    about to read it, when the harness says: they decide whether a handoff is
    the reader's own or a colleague's. ``workdir`` is the folder the session
    works in. Its name and its repository's name rank what the reader is
    carrying; they never choose whose memory this is.

    ``older_than_store`` says whether this code is older than the store (the
    runtime passes its own check). Code older than the store shows no question
    and spends no showing. Left as None, the store's minimum decides.

    ``query`` is used only with ``include_engrams``: long-term graph recall for
    that cue, appended after the briefing. ``session_id``, ``max_functional``
    and ``max_hypomnema`` are accepted for existing callers; the briefing has no
    functional-memory section and fixed section sizes.
    """
    del session_id, max_functional, max_hypomnema  # accepted, not used
    scope = {"agent_id": agent_id, "person_id": person_id, "project_scope": project_scope}
    reader_model = clean_model_id(reader_model)
    reader_session = clean_session_id(reader_session)
    if older_than_store is None:
        older_than_store = _older_than(store)

    handoffs = store.live_handoffs(**scope, reader_session=reader_session)
    handoff = handoffs[0] if handoffs else None

    # Every standing memory in scope, the newest mark first. Each is said once,
    # as its standing line: its note is not a foundational note or carried too.
    standing = _standing(store, scope)
    standing_ids = {item["id"] for item in standing}
    notes = [
        entry for entry in store.search_hypomnema(
            "", **scope, limit=_MAX_NOTES, exclude_kinds=("handoff", "maintenance_report"),
        )
        if DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
    ]
    standing_notes: dict[str, list[str]] = {}
    for entry in notes:
        if entry.get("graduated_to_engram_id") in standing_ids:
            standing_notes.setdefault(entry["graduated_to_engram_id"], []).append(entry["id"])
    notes = [entry for entry in notes if entry.get("graduated_to_engram_id") not in standing_ids]
    foundational = sorted(
        (entry for entry in notes if entry.get("foundational")),
        key=lambda entry: (
            float(entry.get("confidence") or 0) + float(entry.get("salience") or 0),
            entry.get("created_at") or "",
        ),
        reverse=True,
    )[:_FOUNDATIONAL_SHOWN]
    shown_ids = {entry["id"] for entry in foundational}
    place = place_words(workdir)
    carrying = _carrying(
        [_note_item(entry, place) for entry in notes if entry["id"] not in shown_ids]
        + [
            _lesson_item(row, place) for row in _lessons(store, **scope)
            if row["id"] not in standing_ids
        ]
    )

    beliefs = [_serialize_belief(b) for b in store.get_beliefs(agent_id, active_only=True)]
    beliefs = beliefs[:_BELIEFS_SHOWN]

    questions: list[dict[str, Any]] = []
    if include_reflections and not older_than_store:
        try:
            questions = store.pending_reflections(**scope, limit=PACKET_QUESTIONS)
        except Exception:
            # A packet must never fail because of the reflection queue.
            questions = []

    report = latest_dream_entry(store, **scope)
    if report is not None and not changed_something(report):
        report = None

    found: list[RetrievalResult] = []
    if include_engrams and query.strip():
        found = _graph_recall(store, query, scope, max_engrams)

    packet: dict[str, Any] = {
        "include_engrams": include_engrams,
        "reader_model": reader_model,
        "reader_session": reader_session,
        "workdir": workdir,
        "place_words": sorted(place),
        "older_than_store": older_than_store,
        "scope": dict(scope),
        "query": query,
        "handoff": handoff,
        "other_handoffs": handoffs[1:],
        "standing": standing,
        "standing_notes": standing_notes,
        "foundational": foundational,
        "carrying": carrying,
        "hypomnema": foundational + [item["entry"] for item in carrying if item["kind"] == "note"],
        "beliefs": beliefs,
        "reflections": questions,
        "maintenance_report": report,
        "maintenance_reports": [report] if report else [],
        "mnemos_engrams": [_serialize_retrieval_result(result) for result in found],
    }

    max_chars = _max_chars(token_budget)
    text, shown = _render(packet, max_chars=max_chars)
    packet["shown"] = shown
    # What the briefing rendered is what it showed: the ids a later section
    # must not repeat, and nothing it selected but cut for room.
    text, kept = _append_graph(packet, text, max_chars)
    packet["mnemos_engrams"] = kept
    if mark_surfaced:
        _record_delivery(store, scope, shown)
    if kept:
        # Recall for a cue is a use, and only what the reader was shown is
        # reinforced: once per session, never by code older than the store
        # (see ReactiveRetriever.reinforce). The briefing's own sections
        # reinforce nothing.
        shown_ids_kept = {entry["id"] for entry in kept}
        ReactiveRetriever(store).reinforce(
            [result for result in found if result.engram.id in shown_ids_kept],
            query,
            agent_id=agent_id,
            session=reader_session or None,
        )
    if include_prompt:
        packet["prompt"] = text
    return packet


def format_context_packet(
    packet: dict[str, Any], *, token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """The briefing text for a packet ``build_context_packet`` returned."""
    max_chars = _max_chars(token_budget)
    text, shown = _render(packet, max_chars=max_chars)
    return _append_graph({**packet, "shown": shown}, text, max_chars)[0]


def carried_count(packet: dict[str, Any]) -> int:
    """How many handoffs, standing memories, notes, lessons and reports the
    packet showed after its budget: 0 means the session started from
    nothing."""
    shown = packet.get("shown") or {}
    return (
        len(shown.get("handoffs") or [])
        + len(shown.get("standing") or [])
        + len(shown.get("notes") or [])
        + int(bool(shown.get("report")))
    )


def shown_ids(packet: dict[str, Any]) -> set[str]:
    """The ids of everything the packet rendered: handoffs, standing memories
    and the notes paired with them, notes, lessons (an engram's id) and the
    report. Only what survived the budget counts; a note selected but cut for
    room was not shown. A caller appending more to the packet leaves these
    out, so nothing is shown twice."""
    shown = packet.get("shown") or {}
    ids = set(shown.get("handoffs") or []) | set(shown.get("notes") or [])
    paired = packet.get("standing_notes") or {}
    for engram_id in shown.get("standing") or []:
        ids.add(engram_id)
        ids.update(paired.get(engram_id) or [])
    if shown.get("report"):
        ids.add(shown["report"])
    return ids


def fit_section(
    heading: str, groups: list[tuple[str, list[tuple[Any, str]]]], room: int,
) -> tuple[str, list[Any]]:
    """A section of whole entries that fits in ``room`` characters.

    ``groups`` are ``(label, [(key, entry text), ...])`` in rank order; a label
    is printed only above an entry kept under it ("" prints none). An entry
    that doesn't fit is dropped whole, never cut, and later ones may still
    fit. Returns the text and the keys of the entries kept, or ``("", [])``
    when none fits.
    """
    lines = [heading]
    used = len(heading)
    kept: list[Any] = []
    for label, entries in groups:
        labelled = not label
        for key, text in entries:
            cost = len(text) + 1 + (0 if labelled else len(label) + 1)
            if used + cost > room:
                continue
            if not labelled:
                lines.append(label)
                labelled = True
            lines.append(text)
            used += cost
            kept.append(key)
    return ("\n".join(lines), kept) if kept else ("", [])


def room_after(text: str, max_chars: int) -> int:
    """How long a section appended after ``text`` may be, separator included,
    for the whole to stay under ``max_chars``."""
    return max_chars - 1 - len(text) - (2 if text else 0)


# ── Whose note is it ──


def whose_handoff(
    note: dict[str, Any], reader_model: str = "", reader_session: str = "",
) -> tuple[str, str]:
    """How a handoff is introduced to its reader, and whose it is.

    Returns the label ("Yours (Opus 5.5), from another session, 5 hours ago")
    and one of ``own``, ``colleague`` or ``unplaced``.

    A note the reader's own model left is the reader's, from this session or
    another one: several sessions of one model often work in parallel, and
    each is the same agent (Riley's decision, 2026-09-26). A note a different
    model left is a colleague's, even in this session, where it means the
    model was switched. When the reader's model isn't known, or the note isn't
    signed, the label says so and the reader is left to judge.
    """
    author = clean_model_id(note.get("author_model") or "")
    reader = clean_model_id(reader_model)
    session = from_same_session(reader_session, note.get("author_session"))
    age = _age_text(note.get("created_at"))
    name = display_name(author)
    where = {True: "from this session", False: "from another session"}.get(session)

    if author and reader and not same_model(author, reader):
        during = ", earlier in this session" if session is True else ""
        return f"From {name}, a colleague{during}, {age}", _COLLEAGUE
    if author and (reader or session is True):
        return _joined(f"Yours ({name})", where, age), _OWN
    if not author and session is True:
        return f"Yours, from this session, {age} (unsigned)", _OWN
    elsewhere = " in another session" if session is False else ""
    if author:
        return f"From {name}{elsewhere}, {age} (yours if you are {name})", _UNPLACED
    return f"Unsigned{elsewhere}, {age} (maybe yours, maybe a colleague's)", _UNPLACED


def _joined(*parts: str | None) -> str:
    return ", ".join(part for part in parts if part)


# ── Where the session works: ranking only ──


def place_words(workdir: str) -> set[str]:
    """The distinctive words of a folder's name and of its repository's name.

    Used only to rank what the reader is carrying. Scope never comes from the
    working directory: an MCP server's cwd belongs to whichever client spawned
    it. A worktree is named for its repository, so a session in
    ``/tmp/wt-mnemos-r04`` ranks by "mnemos" as one in the checkout does.
    """
    if not workdir:
        return set()
    try:
        folder = Path(workdir).expanduser()
        names = [folder.name]
        repository = _repository(folder)
        if repository is not None:
            names.append(repository.name)
    except (OSError, ValueError, RuntimeError):
        return set()
    words: set[str] = set()
    for name in names:
        words |= distinctive_terms(name)
    return words


def _repository(folder: Path) -> Path | None:
    """The repository a folder is in: the main checkout for a worktree."""
    for candidate in (folder, *folder.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate
        if marker.is_file():
            try:
                pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return candidate
            gitdir = pointer.removeprefix("gitdir:").strip()
            if "/.git/worktrees/" in gitdir:
                return Path(gitdir.split("/.git/worktrees/", 1)[0])
            return candidate
    return None


# ── What the reader is carrying ──

# Every note in scope is a candidate; a few hundred is typical.
_MAX_NOTES = 5000

# An episode names someone or something and something specific: a date or a
# number, a path, code, a link or a quotation. Lessons and summaries are not
# episodes. Compression pulls toward the generic, and a mind fed only lessons
# feels well-informed while forgetting what happened (psiclaude's finding 4).
_NAME = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")
_SPECIFIC = re.compile(
    r"\d"
    r"|[\w.~-]+/[\w./-]+"
    r"|`[^`]+`"
    r"|\"[^\"]{2,}\"|“[^”]{2,}”"
)
_NOT_NAMES = frozenset(
    "yes today yesterday tonight tomorrow something maybe perhaps please "
    "sometimes often usually note notes lesson context".split()
)


def _is_episode(text: str) -> bool:
    names = [
        word for word in _NAME.findall(text)
        if not is_common(word) and word.lower() not in _NOT_NAMES
    ]
    return bool(names) and _SPECIFIC.search(text) is not None


def _note_item(entry: dict[str, Any], place: set[str]) -> dict[str, Any]:
    text = entry.get("content") or ""
    summary = entry.get("authored_by") == "system" or entry.get("source") == "synthesized"
    date, shown = _dated(text, entry.get("created_at"))
    return {
        "kind": "note",
        "id": entry["id"],
        "text": text,
        "shown": shown,
        "date": date,
        "created_at": entry.get("created_at") or "",
        "by": note_signature(entry),
        "shared": len(place & distinctive_terms(text)),
        "episode": not summary and _is_episode(text),
        "entry": entry,
    }


def _lesson_item(row: dict[str, Any], place: set[str]) -> dict[str, Any]:
    text = row.get("content") or ""
    date, shown = _dated(text, row.get("created_at"))
    return {
        "kind": "lesson",
        "id": row["id"],
        "text": text,
        "shown": shown,
        "date": date,
        "created_at": row.get("created_at") or "",
        "by": lesson_signature(row),
        "shared": len(place & distinctive_terms(text)),
        "episode": False,
    }


# Many notes begin with their own date ("2026-09-26: Riley said…"), the writer's
# local day. The line then carries that date once instead of two dates.
_LEADING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\b[,:;]?\s+")


def _dated(text: str, created_at: str | None) -> tuple[str, str]:
    stripped = text.lstrip()
    match = _LEADING_DATE.match(stripped)
    if match:
        return match.group(1), stripped[match.end():]
    return _date(created_at), text


def _lessons(store: "EngramStore", **scope: str) -> list[dict[str, Any]]:
    rows = store._get_conn().execute(
        """
        SELECT id, content, created_at, author_kind FROM engrams
        WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ?
          AND state = 'active'
          AND (tags LIKE '%"lesson"%' OR tags LIKE '%"distilled"%')
        """,
        (scope["agent_id"], scope["person_id"], scope["project_scope"]),
    ).fetchall()
    return [dict(row) for row in rows]


def _standing(store: "EngramStore", scope: dict[str, str]) -> list[dict[str, Any]]:
    """The standing memories in scope, the newest mark first. A packet never
    fails because of them: a store that cannot say has none here."""
    try:
        return store.standing_engrams(**scope)
    except Exception:
        return []


def _carrying(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Up to three, by shared words with the place, then newest first. When none
    of them is an episode, the best-ranked episode takes the last place."""
    ranked = sorted(
        candidates, key=lambda item: (item["shared"], item["created_at"]), reverse=True,
    )
    chosen = ranked[:_CARRYING_SHOWN]
    if chosen and not any(item["episode"] for item in chosen):
        episode = next((item for item in ranked if item["episode"]), None)
        if episode is not None:
            chosen = [*chosen[: _CARRYING_SHOWN - 1], episode]
    return chosen


# ── Rendering within the budget ──


def _max_chars(token_budget: int) -> int:
    return max(800, int(token_budget) * _CHARS_PER_TOKEN)


def _render(packet: dict[str, Any], *, max_chars: int) -> tuple[str, dict[str, Any]]:
    """The briefing text, and what it shows.

    Notes are cut shorter until the packet fits; then, if it still doesn't,
    whole items are left out, least needed first. The reader's handoff is
    never cut. What comes back as shown is what was rendered.
    """
    dropped: set[tuple[str, str]] = set()
    for chars in _NOTE_CHARS:
        text, shown = _compose(packet, chars, dropped)
        if len(text) < max_chars:
            return text, shown
    for victim in _drop_order(packet):
        dropped.add(victim)
        text, shown = _compose(packet, _NOTE_CHARS[-1], dropped)
        if len(text) < max_chars:
            break
    return text, shown


def _drop_order(packet: dict[str, Any]) -> list[tuple[str, str]]:
    """What leaves the packet first when it does not fit: other sessions'
    notes, then what the reader is carrying, the foundational notes, the
    report, the episode, the question and the beliefs. The standing lines go
    last, the oldest mark first: they say how to work in this session."""
    carrying = packet.get("carrying") or []
    episodes = [item for item in carrying if item["episode"]]
    others = [item for item in carrying if not item["episode"]]
    standing = (packet.get("standing") or [])[:STANDING_SHOWN]
    return [
        *(("other", entry["id"]) for entry in reversed(packet.get("other_handoffs") or [])),
        *(("carrying", item["id"]) for item in reversed(others)),
        *(("who", entry["id"]) for entry in reversed(packet.get("foundational") or [])),
        ("report", ""),
        *(("carrying", item["id"]) for item in episodes),
        ("question", ""),
        *(("belief", belief["id"]) for belief in reversed(packet.get("beliefs") or [])),
        *(("standing", item["id"]) for item in reversed(standing)),
    ]


def _compose(
    packet: dict[str, Any], chars: int, dropped: set[tuple[str, str]],
) -> tuple[str, dict[str, Any]]:
    shown: dict[str, Any] = {
        "handoffs": [], "standing": [], "notes": [], "questions": [], "report": None,
    }
    sections = [
        _format_left_off(packet, chars, dropped, shown),
        _format_who(packet, chars, dropped, shown),
        _format_notes(
            "What you're carrying", packet.get("carrying") or [],
            "carrying", chars, dropped, shown,
        ),
        _format_beliefs(packet, dropped),
        _format_question(packet, dropped, shown),
        _format_away(packet, dropped, shown),
    ]
    body = "\n\n".join(section for section in sections if section)
    return (f"{_HEADER}\n\n{body}" if body else ""), shown


def _append_graph(
    packet: dict[str, Any], text: str, max_chars: int,
) -> tuple[str, list[dict[str, Any]]]:
    """The briefing with opted-in graph recall appended in the room left under
    the budget, leaving out what the briefing already showed: the text, and
    the graph entries kept."""
    if not packet.get("include_engrams"):
        return text, []
    shown = shown_ids(packet)
    entries = [entry for entry in packet.get("mnemos_engrams") or [] if entry["id"] not in shown]
    prefix = text or _HEADER
    section, kept = fit_section(
        "### Mnemos Graph",
        [("", [(entry, _graph_line(entry)) for entry in entries])],
        room_after(prefix, max_chars),
    )
    if not section:
        return text, []
    return f"{prefix}\n\n{section}", kept


def _format_left_off(
    packet: dict[str, Any], chars: int, dropped: set[tuple[str, str]], shown: dict[str, Any],
) -> str:
    handoff = packet.get("handoff")
    if not handoff:
        return ""
    reader_model = packet.get("reader_model") or ""
    reader_session = packet.get("reader_session") or ""
    label, whose = whose_handoff(handoff, reader_model, reader_session)
    relations = {whose}
    lines = ["### Where you left off", f"{label}:", handoff["content"]]
    shown["handoffs"].append(handoff["id"])
    others = [
        entry for entry in packet.get("other_handoffs") or []
        if ("other", entry["id"]) not in dropped
    ]
    if others:
        lines.extend(["", "Other notes from the last three days:"])
        for entry in others:
            label, whose = whose_handoff(entry, reader_model, reader_session)
            relations.add(whose)
            lines.append(f"- {label}: {_cut_with_id(entry, min(chars, _OTHER_HANDOFF_CHARS))}")
            shown["handoffs"].append(entry["id"])
    if relations & {_COLLEAGUE, _UNPLACED}:
        lines.append(COLLEAGUE_LINE)
    return "\n".join(lines)


def _format_who(
    packet: dict[str, Any], chars: int, dropped: set[tuple[str, str]], shown: dict[str, Any],
) -> str:
    """Who you're with: what the agent marked standing, then the foundational
    notes.

    The standing lines come first, the newest mark first, at most
    ``STANDING_SHOWN``, each the memory's own words on one line, cut at a
    sentence boundary, with the id that unmarks it or reads it whole. The
    rest are counted, with the call that lists them all. Without a standing
    memory the section is the foundational notes alone, as before.
    """
    standing = packet.get("standing") or []
    lines: list[str] = []
    if standing:
        kept = [
            item for item in standing[:STANDING_SHOWN]
            if ("standing", item["id"]) not in dropped
        ]
        lines.append(STANDING_LABEL)
        for item in kept:
            lines.append(f"- {standing_words(item['content'])} ({item['id']})")
            shown["standing"].append(item["id"])
        more = len(standing) - len(kept)
        if more and kept:
            lines.append(f"And {more} more: {STANDING_LIST_CALL}")
        elif more:
            lines.append(f"{more} left out for room: {STANDING_LIST_CALL}")
    notes = [
        _note_item(entry, set()) for entry in packet.get("foundational") or []
        if ("who", entry["id"]) not in dropped
    ]
    if notes and lines:
        lines.append("Other notes:")
    for item in notes:
        lines.append(f"- {item['date']}, {item['by']}: {_cut_with_id(item, chars, key='shown')}")
        shown["notes"].append(item["id"])
    return "\n".join(["### Who you're with", *lines]) if lines else ""


def standing_words(content: str) -> str:
    """A standing memory as one line: its own words, without the context a
    capture keeps after them, cut at a sentence boundary."""
    words = (content or "").split(_CONTEXT_MARK, 1)[0]
    return cut_at_sentence(words, STANDING_CHARS)[0]


def _format_notes(
    heading: str,
    items: list[dict[str, Any]],
    section: str,
    chars: int,
    dropped: set[tuple[str, str]],
    shown: dict[str, Any],
) -> str:
    lines = []
    for item in items:
        if (section, item["id"]) in dropped:
            continue
        lines.append(f"- {item['date']}, {item['by']}: {_cut_with_id(item, chars, key='shown')}")
        shown["notes"].append(item["id"])
    return "\n".join([f"### {heading}", *lines]) if lines else ""


def _format_beliefs(packet: dict[str, Any], dropped: set[tuple[str, str]]) -> str:
    """Each belief once, with its confidence and its id.

    A belief changes only when a correction names it by id, so the reader
    needs the id to correct or retire one. The id is for the reader; the
    instructions keep it from the human.
    """
    lines = [
        f"- {belief['content']} "
        f"({int(round(float(belief['confidence']) * 100))}%, {belief['id']})"
        for belief in packet.get("beliefs") or []
        if ("belief", belief["id"]) not in dropped
    ]
    return "\n".join(["### Beliefs", *lines]) if lines else ""


def _format_question(
    packet: dict[str, Any], dropped: set[tuple[str, str]], shown: dict[str, Any],
) -> str:
    items = packet.get("reflections") or []
    if not items or ("question", "") in dropped:
        return ""
    shown["questions"].extend(item["id"] for item in items)
    return format_questions(items)


def format_questions(items: list[dict[str, Any]]) -> str:
    """The question section: what the memory asks, and the call that answers it.

    A question that takes a verdict shows the call with its verdicts, so an
    agent copying it gives the verdict that decides the question: answered
    without one, a belief or contradiction question forms nothing.
    """
    if not items:
        return ""
    # The verdicts live with the runtime that applies them. Imported here, not
    # at the top: the runtime imports this module.
    from ..simple_runtime import verdict_call_lines

    heading = "One question" if len(items) == 1 else "Questions"
    lines = [f"### {heading}"]
    for item in items:
        excerpt = item.get("excerpt") or ""
        if len(excerpt) >= _EXCERPT_CHARS:
            excerpt = excerpt.rsplit(" ", 1)[0] + " […]"
        lines.append(f'About: "{excerpt}"')
        lines.append(_MARKER.sub("", item["prompt"]).strip())
        call = verdict_call_lines(item)
        if call is None:
            lines.append(f'mnemos_reflect(target_id="{item["target_id"]}", text="…")')
        else:
            lines.extend(call)
    lines.append(
        "Answer in your own words if one comes. If nothing true does, leave it; "
        "it fades on its own."
    )
    return "\n".join(lines)


# The store gives a question the first 160 characters of its memory, cut
# wherever they end.
_EXCERPT_CHARS = 160

# The queue's own markers ("[theme:room]", "[belief:<id>]", "[ref:<id>]") say
# which theme or memory a question is about. They are for Mnemos, not the reader.
_MARKER = re.compile(r"\s*\[(?:theme|belief|ref):[^\]]*\]")


def _format_away(
    packet: dict[str, Any], dropped: set[tuple[str, str]], shown: dict[str, Any],
) -> str:
    report = packet.get("maintenance_report")
    if not report or ("report", "") in dropped:
        return ""
    shown["report"] = report["id"]
    written = _age_text(report.get("last_revised_at") or report.get("created_at"))
    return (
        "### While you were away\n"
        f"{' '.join(report['content'].split())}\n"
        f"(Mnemos's upkeep wrote this {written}; these aren't your words.)"
    )


def _graph_line(item: dict[str, Any]) -> str:
    confidence = int(float(item["confidence"]) * 100)
    return (
        f"- {item['display']} "
        f"[{item['kind']}, score {float(item['score']):.2f}, confidence {confidence}%]"
    )


# ── Cutting a note ──

_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*(?=\s)")


def cut_at_sentence(text: str, limit: int) -> tuple[str, bool]:
    """``text`` on one line, cut to ``limit`` at a sentence boundary: the text,
    and whether it was cut.

    When the last sentence that fits would leave less than half the room (a
    short first sentence before a long one), the cut falls on a word boundary
    instead. A note never ends mid-word.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed, False
    ends = [
        match.end() for match in _SENTENCE_END.finditer(collapsed[: limit + 1])
        if match.end() <= limit
    ]
    if ends and ends[-1] >= limit // 2:
        head = collapsed[: ends[-1]]
    else:
        head = collapsed[:limit].rsplit(" ", 1)[0]
    return f"{head.rstrip()} […]", True


def _cut_with_id(item: dict[str, Any], limit: int, *, key: str = "content") -> str:
    text, cut = cut_at_sentence(item.get(key) or "", limit)
    if cut:
        return f'{text} Whole note: mnemos_recall("{item["id"]}")'
    return text


# ── Small helpers ──


def _older_than(store: "EngramStore") -> bool:
    """Whether this code is older than the store, read from the store itself."""
    try:
        minimum = store.min_code_version()
    except Exception:
        return False
    return minimum is not None and minimum > code_version.MAINTENANCE_CODE_VERSION


def _record_delivery(store: "EngramStore", scope: dict[str, str], shown: dict[str, Any]) -> None:
    """Count what was actually shown: a handoff as delivered, a question as
    one showing spent."""
    for handoff_id in shown["handoffs"]:
        store.mark_handoff_surfaced(handoff_id, **scope)
    if shown["questions"]:
        try:
            store.mark_reflections_surfaced(shown["questions"])
        except Exception:
            pass
    if shown["handoffs"] or shown["notes"] or shown.get("standing"):
        store.set_meta(
            f"simple:{scope['agent_id']}:{scope['person_id']}:{scope['project_scope']}"
            ":last_context_delivery_at",
            datetime.now(timezone.utc).isoformat(),
        )


def _graph_recall(
    store: "EngramStore", query: str, scope: dict[str, str], max_engrams: int,
) -> list[RetrievalResult]:
    """Graph recall for ``query``. Finding changes nothing: the caller
    reinforces only what it goes on to show."""
    retriever = ReactiveRetriever(store)
    results = retriever.retrieve(
        cue=query,
        **scope,
        max_results=max_engrams,
        emotional_state=store.get_latest_emotional_state(scope["agent_id"]),
        reconsolidate_results=False,
    )
    # A defensive scope check, although retrieval filters before traversal.
    return [
        result for result in results
        if store.engram_visible_in_scope(result.engram.id, **scope)
    ]


def _date(timestamp: str | None) -> str:
    try:
        return datetime.fromisoformat(timestamp or "").date().isoformat()
    except (TypeError, ValueError):
        return "undated"


def _age_text(timestamp: str | None) -> str:
    try:
        moment = datetime.fromisoformat(timestamp or "")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    except (TypeError, ValueError):
        return "at an unknown time"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


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
