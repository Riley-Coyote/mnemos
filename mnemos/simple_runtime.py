"""Simple-mode continuity runtime for Mnemos.

This module is intentionally MCP-agnostic so the product path can be tested
without a running client. It exposes the real Mnemos stack through nine simple
operations, including agent-written handoff and reflection.
"""

from __future__ import annotations

import functools
import json
import hashlib
import heapq
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .authorship import (
    clean_model_id,
    display_name,
    harness_session,
    note_signature,
    resolve_author_model,
    session_introduction,
    session_introduction_key,
    session_introduction_record,
    signature,
)
from .code_version import MAINTENANCE_CODE_VERSION, OLDER_CODE_FIX, OLDER_CODE_MESSAGE
from .config.loader import load_config
from .consolidation.daemon import ConsolidationDaemon
from .core.types import SourceType
from .dream_journal import DREAM_JOURNAL_TAG, fetch_active_dream_entry
from .encoding.encoder import Encoder
from .identity_svg import build_timeline, render_identity_svg, short_label
from .interface.context_packet import (
    COLLEAGUE_LINE,
    PACKET_MAX_CHARS,
    PACKET_QUESTIONS,
    build_context_packet,
    carried_count,
    fit_section,
    format_questions,
    room_after,
    shown_ids,
    whose_handoff,
)
from .retrieval.reactive import ReactiveRetriever, RetrievalResult
# Re-exported: MnemosScope and resolve_scope moved to simple_scope but
# remain importable from here for existing consumers.
from .simple_scope import MnemosScope, resolve_scope  # noqa: F401
from .store.embedding_index import EmbeddingIndex
from .core.engram import Engram
from .core.placeholders import TEMPLATED_IMPACTS, is_templated
from .store.archive import resharpen
from .store.fts import distinctive_terms, fts_words, meaningful_words, or_query
from .store.sqlite_store import FADED_ARCHIVE_REASONS, EngramStore, ReadOnlyEngramStore


SIMPLE_TOOL_NAMES = (
    "mnemos_context",
    "mnemos_handoff",
    "mnemos_capture",
    "mnemos_recall",
    "mnemos_correct",
    "mnemos_maintain",
    "mnemos_reflect",
    "mnemos_introduce",
    "mnemos_health",
)

HOST_MUTATION_PROTOCOL_VERSION = 1
HOST_MUTATION_OPERATIONS = frozenset({
    "capture",
    "correct",
    "maintain",
    "reflect",
    "introduce",
})
_MAX_HOST_MUTATION_REQUEST_BYTES = 1024 * 1024

# Legacy engrams the v6 scope migration could not place, by what they are
# (see EngramStore.unscoped_engrams). `mnemos adopt-legacy` brings back lessons
# and memories written some other way by default. Transcript-indexer output
# stays out unless it is named: its volume is what buried continuity before
# (docs/vision.md, section II), and on a real store adopting all of it took
# every recall slot for some ordinary questions.
LEGACY_CLASSES = ("lessons", "other", "indexer")
LEGACY_DEFAULT_INCLUDE = ("lessons", "other")

# The one call that reaches the memories an ordinary recall never returns:
# those that faded into the archive. Named on the health card beside their count.
UNREACHABLE_COMMAND = 'mnemos_recall("<its words>", include_archived=true)'


# Hypomnema ids are uuid4 strings and engram ids are ULIDs after "engram_".
# Recall treats a query of exactly either shape as an id before it treats it
# as words.
_ENTRY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ENGRAM_ID = re.compile(r"engram_[0-9A-Za-z]{10,40}")


class HostMutationConflictError(ValueError):
    """An idempotency key was reused for a different mutation request."""


_ONBOARDING_RITUAL = """\
ONBOARDING - first session with a fresh memory
This is the first time you and this human meet with durable memory attached. Before other work, run a short get-to-know-you ritual. Be warm, be brief, ask one question at a time:
1. Ask what they would like you to call them. Capture the answer with mnemos_capture.
2. Ask what they are working on right now. Capture the answer with mnemos_capture.
3. Ask what they want you to always remember. Capture the answer with mnemos_capture.
4. Ask them for one small, true fact about themselves or their world - something they would smile to hear you recall later. Capture it with mnemos_capture. It becomes part of their first proof that your memory is real.
5. Call mnemos_introduce with agent_model set to your own model id. You know what model you are - do not ask the human. Add agent_name if you go by a name.
6. Finish by telling them, in plain words, what you will now remember.
When you talk to the human, never mention tools, databases, scopes, or model ids. Just talk like someone who intends to remember."""


def _dedicated_model_requested() -> bool:
    """Return true only when simple mode has explicit model configuration."""

    explicit_env = (
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_MODEL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    )
    if any(os.environ.get(key) for key in explicit_env):
        return True
    try:
        config = load_config()
    except Exception:
        return False
    llm_config = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    return bool(llm_config.get("provider") or llm_config.get("model"))


def _classify_kind(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("how to", "process", "workflow", "steps", "procedure")):
        return "procedural"
    if any(marker in text for marker in ("todo", "remember to", "next time", "follow up", "should do")):
        return "prospective"
    if any(marker in text for marker in ("decided", "built", "debugged", "met", "changed", "fixed")):
        return "episodic"
    return "semantic"


def _classify_domain(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("identity", "who i am", "who you are", "selfhood")):
        return "identity"
    if any(marker in text for marker in ("always", "preference", "prefers", "principle", "boundary")):
        return "foundational"
    if any(marker in text for marker in ("again", "recurring", "pattern", "usually", "often")):
        return "recurring"
    if any(marker in text for marker in ("roadmap", "long term", "long-term", "arc", "future")):
        return "long-arc"
    if any(marker in text for marker in ("current", "today", "now", "temporary", "session")):
        return "situational"
    return "topical"


def _simple_tags(content: str, context: str = "") -> list[str]:
    text = f"{content} {context}".lower()
    tags = ["continuity"]
    for label, markers in {
        "preference": ("prefer", "preference", "likes", "wants"),
        "decision": ("decided", "decision", "chosen", "agreed"),
        "project": ("project", "repo", "workspace", "build"),
        "identity": ("identity", "agent", "user", "self"),
        "correction": ("correction", "wrong", "update", "forget"),
    }.items():
        if any(marker in text for marker in markers):
            tags.append(label)
    return sorted(set(tags))


# A theme must recur across at least this many memories before the agent is
# asked whether it has become a belief — a belief is a stable pattern, not a
# single mention.
_BELIEF_MIN_MEMORIES = 4

# The marker a belief ask carries, so the answer can be filed under its theme
# and a theme already asked is not asked again.
_THEME_MARKER = re.compile(r"\[theme:([^\]]+)\]")

# Only captures whose encoding registered real surprise (they did not fit what
# was already held) are offered as contradiction candidates. Keeps the ask rare
# and tied to genuine tension, not mere topical overlap.
_CONTRADICTION_MIN_SURPRISE = 0.4

# The marker a reaffirmation carries: which belief it asks about.
_BELIEF_MARKER = re.compile(r"\[belief:(belief_[A-Za-z0-9]+)\]")

# A belief the agent has not held to (formed or reaffirmed) for this long may
# be put to it again: still true? Never more often than that.
_REAFFIRM_AFTER_DAYS = 30

# What the agent can decide about each kind of question: mnemos_reflect's
# verdict. The verdict alone decides what happens. The words are kept as
# written and never read for a yes or a no: "Now more than ever" once retired
# a belief because it starts with "no".
_VERDICTS: dict[str, tuple[str, ...]] = {
    "belief": ("hold", "decline", "not_now"),
    "reaffirm": ("hold", "decline", "retire", "not_now"),
    "contradiction": ("contradicts", "compatible", "unsure"),
    "impact": ("answer", "skip"),
    "lesson": ("answer", "skip"),
}
VERDICTS = frozenset(v for verdicts in _VERDICTS.values() for v in verdicts)

# The questions whose words are themselves the answer asked for: what one
# memory changed or taught, which land on that memory. Without a verdict these
# are answered, and every other kind stays open. Code older than the store
# answers only these: a whitelist, so a kind newer code adds is left open,
# never spent.
_ANSWERED_BY_WORDS = frozenset({"impact", "lesson"})

# What each verdict does, told back to the agent when a question needs one.
_VERDICT_HELP = {
    "belief": (
        "hold (your words become a belief you hold), decline (it is not one) "
        "or not_now (ask again later)"
    ),
    "reaffirm": (
        "hold (it still holds), retire (you no longer hold it), decline "
        "(leave it as it is) or not_now (ask again later)"
    ),
    "contradiction": (
        "contradicts (they conflict: one link says so, and nothing else "
        "changes), compatible (they do not) or unsure (you cannot tell)"
    ),
    "impact": "answer (your words become what it meant) or skip (nothing true comes)",
    "lesson": "answer (your words become what it taught) or skip (nothing true comes)",
}
_VERDICT_GUIDE = (
    "Is it a belief you hold: hold, decline or not_now. Still true: hold, "
    "retire, decline or not_now. A contradiction: contradicts, compatible or "
    "unsure. A lesson or what a memory changed: answer or skip."
)


def _question_kind(ask: Mapping[str, Any]) -> str:
    """What an ask asks. A belief ask naming a belief it asks about again
    (``[belief:<id>]``) is a reaffirmation, whatever kind it was filed under:
    until the queue had its own kind for them, reaffirmations were 'belief'."""
    kind = str(ask.get("kind") or "")
    if kind == "belief" and _BELIEF_MARKER.search(ask.get("prompt") or ""):
        return "reaffirm"
    return kind


def verdict_call_lines(ask: Mapping[str, Any]) -> list[str] | None:
    """How to answer a question that takes a verdict: the call, then its verdicts.

    Both packet builders show these (the runtime's and the session-start
    hook's), so an agent copying the call gives the verdict that decides the
    question: answered without one, a belief or contradiction question forms
    nothing. None for a question whose words are the answer (impact, lesson)
    or of a kind this code does not know; those keep their own template.
    """
    kind = _question_kind(ask)
    verdicts = _VERDICTS.get(kind)
    if verdicts is None or kind in _ANSWERED_BY_WORDS:
        return None
    named = ", ".join(verdicts[:-1]) + f" or {verdicts[-1]}"
    return [
        f'mnemos_reflect(target_id="{ask["target_id"]}", text="…", verdict="…")',
        f"verdict: {named}",
    ]


def _moment(timestamp: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None when unreadable."""
    try:
        moment = datetime.fromisoformat(timestamp or "")
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


_STOPWORDS = {
    "about",
    "after",
    "agent",
    "before",
    "continuity",
    "context",
    "durable",
    "memory",
    "mnemos",
    "note",
    "notes",
    "should",
    "that",
    "this",
    "when",
    "with",
}


def _query_terms(query: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-zA-Z0-9]+", query.lower())
        if len(term) >= 3 and term not in _STOPWORDS
    }


def _has_query_overlap(query: str, text: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    text_terms = _query_terms(text)
    return bool(terms & text_terms)


def _named_terms(text: str) -> set[str]:
    """What a query or a note names: its meaningful words, less the words every
    note here shares ("memory", "note", "continuity")."""
    return meaningful_words(text) - _STOPWORDS


# Words that say what to do with a note, not which note it is: "forget the
# zeppelin schedule" names the zeppelin schedule.
_CORRECTION_VERBS = frozenset(
    "forget archive remove delete update supersede replace correct correction".split()
)

# The actions that make mnemos_correct forget what it names, rather than
# replace it: the note and its memory are archived, and nothing is written.
_FORGET_ACTIONS = frozenset({"forget", "archive", "remove", "delete"})


def _named_by(query: str, text: str) -> int:
    """How many of a correction query's meaningful words a note or memory holds,
    when that is enough for the query to name it: at least half of them, and two
    when the query has two or more. 0 when the query does not name it."""
    wanted = _named_terms(query) - _CORRECTION_VERBS
    if not wanted:
        return 0
    shared = len(wanted & _named_terms(text))
    return shared if shared >= max(min(2, len(wanted)), (len(wanted) + 1) // 2) else 0


def _named_matches(query: str, candidates: list[Any], text_of: Any) -> list[Any]:
    """The candidates a correction query names, closest first: the most of its
    words shared, then in the order the search ranked them."""
    named = [(_named_by(query, text_of(item)), rank, item) for rank, item in enumerate(candidates)]
    return [item for shared, _rank, item in sorted(named, key=lambda n: (-n[0], n[1])) if shared]


def _filter_continuity(query: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not query.strip():
        return entries
    # A note is shown when it shares a word that means something in the query;
    # sharing "her" or "for" put unrelated notes beside the ones asked about.
    terms = _named_terms(query)
    return [
        entry for entry in entries
        if (terms & _named_terms(entry.get("content", "")) if terms
            else _has_query_overlap(query, entry.get("content", "")))
        or float(entry.get("score", 0.0)) >= 0.55
    ]


def _filter_memories(query: str, results: list[Any]) -> list[Any]:
    if not query.strip():
        return results
    filtered = []
    for result in results:
        engram = result.engram
        searchable = " ".join([
            engram.content or "",
            engram.impact or "",
            " ".join(engram.tags or []),
        ])
        if _has_query_overlap(query, searchable) or float(result.score) >= 1.35:
            filtered.append(result)
    return filtered


def _notice_when_older(method: Any) -> Any:
    """End a tool's result with OLDER_CODE_MESSAGE while this code is older
    than the store.

    The agent inside a stale session is the only one who can see it is stale,
    and it reads tool results, not the health card. Only the outermost call
    adds the line (correct can capture), and when the code is current the
    result is returned exactly as the method built it.
    """

    @functools.wraps(method)
    def wrapper(self: "MnemosRuntime", *args: Any, **kwargs: Any) -> Any:
        self._notice_depth += 1
        try:
            result = method(self, *args, **kwargs)
        finally:
            self._notice_depth -= 1
        if self._notice_depth == 0 and isinstance(result, str) and self._stale():
            result = f"{result}\n{OLDER_CODE_MESSAGE}"
        return result

    return wrapper


def _traced(tool: str) -> Any:
    """Record one ``memory_trace`` row for each tool call: the ids it showed or
    returned and the ids it wrote, and who made the call.

    Only the outermost call records one (a correction can capture, a capture
    runs maintenance), and whatever the inner calls read or write goes on that
    row. A call that ended before it opened the store records nothing, so
    asking never creates a store. See ``MnemosRuntime._record_trace``.
    """

    def decorate(method: Any) -> Any:
        @functools.wraps(method)
        def wrapper(self: "MnemosRuntime", *args: Any, **kwargs: Any) -> Any:
            if self._trace is not None:
                return method(self, *args, **kwargs)
            self._trace = {"read": [], "written": [], "author": None}
            try:
                return method(self, *args, **kwargs)
            finally:
                trace, self._trace = self._trace, None
                self._record_trace(tool, trace)

        return wrapper

    return decorate


class MnemosRuntime:
    """High-level continuity interface used by simple MCP mode and tests."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        agent_id: str | None = None,
        person_id: str | None = None,
        project_scope: str | None = None,
        use_dedicated_model: bool = True,
        read_only: bool = False,
    ) -> None:
        self.scope = resolve_scope(
            db_path=db_path,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )
        # A read-only runtime inspects an existing store and cannot change it
        # (`mnemos doctor`). Anything that would write raises instead.
        self._read_only = read_only
        self._store: EngramStore | None = None
        self._encoder: Encoder | None = None
        self._retriever: ReactiveRetriever | None = None
        self._embedding_index: EmbeddingIndex | None = None
        self._llm_client: Any | None = None
        self._use_dedicated_model = use_dedicated_model
        self._agent_model_hint: str | None = None
        # The model that introduced itself in this session, if one did, and
        # the name it goes by. Kept per runtime, not per scope: several models
        # can share one scope, and the last introduction must not sign every
        # other model's notes.
        self._session_author = ""
        self._session_name = ""
        self._session_id: int | None = None
        self.last_dream_note_id: str | None = None
        self.last_dream_narrative: str | None = None
        self._host_mutation_active = False
        # How deep this runtime is in tool calls, so only the outermost one
        # ends its result with the older-code notice.
        self._notice_depth = 0
        # What the tool call under way has read and written, for its
        # memory_trace row (see _traced); None between calls.
        self._trace: dict[str, Any] | None = None

    @property
    def db_path(self) -> Path:
        return Path(self.scope.db_path).expanduser()

    @property
    def has_dedicated_model(self) -> bool:
        self._ensure_init()
        return self._llm_client is not None

    def repair_softening(self, dry_run: bool = False) -> int:
        """Restore memories an earlier version truncated without a model.

        Returns how many were restored, or would be when ``dry_run``. A store
        that does not exist yet has nothing to repair and must not be brought
        into existence by the asking — `doctor` calls this, and a check that
        creates the thing it is checking reports health about itself.
        """
        if not self.db_path.exists():
            return 0
        self._ensure_init()
        assert self._store is not None
        from .consolidation.softening import repair_rule_based_softening

        return repair_rule_based_softening(
            self._store, agent_id=self.scope.agent_id, dry_run=dry_run
        )

    def legacy_counts(self) -> dict[str, int]:
        """How much of this agent's memory the scope migration left unplaced.

        Read-only, and never creates a store. Archived rows are counted
        apart: recall would skip them anyway.
        """
        if not self.db_path.exists():
            return _tally_legacy([])
        self._ensure_init()
        assert self._store is not None
        return _tally_legacy(self._store.unscoped_engrams(self.scope.agent_id))

    def adopt_legacy(
        self,
        *,
        include: tuple[str, ...] = LEGACY_DEFAULT_INCLUDE,
        write: bool = False,
        scope_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Plan, and with ``write`` apply, the return of quarantined memories.

        The v6 migration left every legacy engram it could not tie to one
        continuity scope without a person or project, where no scoped read
        reaches it. That quarantine is the safe default and stays one: this
        moves the chosen classes into the current scope only when a human
        asks, after a verified backup.

        It will not choose between people. When this agent holds memory for
        anyone other than the target person, the caller must confirm the
        target was named explicitly (``scope_confirmed``) or nothing moves.
        A store that does not exist is reported, never created.
        """
        plan: dict[str, Any] = {
            "target": (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            "exists": self.db_path.exists(),
            "include": tuple(include),
            "counts": {},
            "selected": [],
            "other_scopes": [],
            "refused": None,
            "adopted": 0,
            "backup": None,
        }
        if not plan["exists"]:
            return plan
        self._ensure_init()
        assert self._store is not None

        rows = self._store.unscoped_engrams(self.scope.agent_id)
        plan["counts"] = _tally_legacy(rows)
        plan["selected"] = [
            row for row in rows
            if row["state"] != "archived" and row["class"] in include
        ]
        target = (self.scope.person_id, self.scope.project_scope)
        scopes = self._store.engram_scopes_in_use(self.scope.agent_id)
        plan["other_scopes"] = sorted(scope for scope in scopes if scope != target)
        people = {person for person, _ in scopes} | {self.scope.person_id}
        if not write or not plan["selected"]:
            return plan
        if len(people) > 1 and not scope_confirmed:
            others = ", ".join(sorted(people - {self.scope.person_id}))
            plan["refused"] = (
                f"This agent also holds memory for: {others}. Mnemos will not guess "
                "whose these are. Name the target with --person-id and --project-scope."
            )
            return plan

        from .backup import create_backup

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-adopt-legacy-{stamp}.db"
        )
        backup = create_backup(
            self.db_path, destination, source_connection=self._store._get_conn()
        )
        plan["backup"] = backup["path"]
        plan["adopted"] = self._store.adopt_unscoped_engrams(
            [row["id"] for row in plan["selected"]],
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )
        return plan

    def repair_lessons(self, *, write: bool = False) -> dict[str, Any]:
        """Plan, and with ``write`` apply, the removal of lesson links filed wrong.

        Until softening checked that a lesson says what a memory taught, the
        first lesson sharing any word was taken as the same lesson, and
        memories carrying placeholder impacts were filed under placeholder
        lessons. On a real store that was 451 of 631 distilled_into links.
        They still carry recall's light at the weight of real evidence.

        This lists them. It removes them only when a human asks, after a
        verified backup. Lessons themselves are left alone. A memory whose link
        goes is filed again, correctly, the next time it fades.
        """
        plan: dict[str, Any] = {
            "target": (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            "exists": self.db_path.exists(),
            "links": 0,
            "misfiled": [],
            "removed": 0,
            "backup": None,
        }
        if not plan["exists"]:
            return plan
        self._ensure_init()
        assert self._store is not None
        from .consolidation.softening import find_misfiled_distillations

        plan["links"] = self._store._get_conn().execute(
            "SELECT count(*) FROM connections c JOIN engrams s ON s.id = c.source_id "
            "WHERE c.relation = 'distilled_into' AND s.owner_agent_id = ? "
            "AND s.person_id = ? AND s.project_scope = ?",
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchone()[0]
        plan["misfiled"] = find_misfiled_distillations(
            self._store, self.scope.agent_id, self.scope.person_id, self.scope.project_scope,
        )
        if not write or not plan["misfiled"]:
            return plan

        from .backup import create_backup

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-repair-lessons-{stamp}.db"
        )
        backup = create_backup(
            self.db_path, destination, source_connection=self._store._get_conn()
        )
        plan["backup"] = backup["path"]
        plan["removed"] = self._store.remove_connections([
            (item["source_id"], item["target_id"], "distilled_into") for item in plan["misfiled"]
        ])
        return plan

    def repair_min_code_version(
        self, *, set_to: int | None = None, write: bool = False
    ) -> dict[str, Any]:
        """Show, and with ``set_to`` and ``write`` change, the store's minimum.

        Opening a store only ever raises its minimum code version. When code
        newer than what is installed raised it (an unmerged checkout, another
        install), every session here stops maintaining the store and
        restarting does not help. This is the way back, and only a human runs
        it: without ``write`` nothing changes, and a verified backup comes
        before any change. It never sets the minimum below 1.
        """
        if set_to is not None and set_to < 1:
            raise ValueError("The minimum code version is 1 or more.")
        plan: dict[str, Any] = {
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "running": MAINTENANCE_CODE_VERSION,
            "store_minimum": None,
            "set_to": set_to,
            "changed": False,
            "backup": None,
        }
        if not plan["exists"]:
            return plan
        # Read it as the store holds it now. Opening the store for writing
        # records this code's version first, which would hide the value a
        # human is deciding about.
        peek = ReadOnlyEngramStore(self.db_path)
        try:
            plan["store_minimum"] = peek.min_code_version()
        finally:
            peek.close()
        if not write or set_to is None or set_to == plan["store_minimum"]:
            return plan

        from .backup import create_backup

        # The backup comes first, read through a read-only connection, so it
        # is the store exactly as the human found it. Opening the store for
        # writing records this code's version and can migrate the schema.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-repair-min-code-version-{stamp}.db"
        )
        source = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            backup = create_backup(self.db_path, destination, source_connection=source)
        finally:
            source.close()
        plan["backup"] = backup["path"]

        self._ensure_init()
        assert self._store is not None
        self._store.set_min_code_version(set_to)
        plan["changed"] = True
        return plan

    def repair_keyword_contradictions(self, *, write: bool = False) -> dict[str, Any]:
        """Show, and with ``write`` undo, what the removed keyword check wrote.

        Without a model, encoding used to lower a belief by 0.05 and link the
        new memory as contradicting the belief's evidence whenever the two
        shared a word and the memory held a negation ("not" also matched
        "note"). Nothing had judged those. This finds them by the check's own
        signatures (see ``find_keyword_contradictions``), for this agent,
        across its scopes.

        A dry run reads the store read-only and changes nothing. With
        ``write``, a verified backup of the store as found comes first; then,
        in one transaction, the check's links go and each active belief it
        lowered gets back exactly what those revisions took, recorded as a
        new revision that says so. History is never deleted, and revisions
        with any other reason stand. A second run finds nothing. Code older
        than the store refuses to write.
        """
        plan: dict[str, Any] = {
            "agent_id": self.scope.agent_id,
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "older_than_store": False,
            "found": None,
            "removed": 0,
            "restored": 0,
            "backup": None,
        }
        if not plan["exists"]:
            return plan
        from .encoding.encoder import (
            KEYWORD_CONTRADICTIONS_RESTORED,
            find_keyword_contradictions,
        )

        peek = ReadOnlyEngramStore(self.db_path)
        try:
            minimum = peek.min_code_version()
            plan["found"] = find_keyword_contradictions(peek, self.scope.agent_id)
        finally:
            peek.close()
        plan["older_than_store"] = minimum is not None and minimum > MAINTENANCE_CODE_VERSION
        found = plan["found"]
        if not write or plan["older_than_store"] or not (found["links"] or found["beliefs"]):
            return plan

        from .backup import create_backup

        # The backup is the store exactly as the human found it: read through
        # a read-only connection, before opening for writing migrates the
        # schema or records this code's version.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-repair-keyword-contradictions-{stamp}.db"
        )
        source = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            backup = create_backup(self.db_path, destination, source_connection=source)
        finally:
            source.close()
        plan["backup"] = backup["path"]

        self._ensure_init()
        assert self._store is not None
        if self._older_than_store() is not None:
            plan["older_than_store"] = True
            return plan
        # Found again under the writer: the store may have moved on since.
        found = plan["found"] = find_keyword_contradictions(self._store, self.scope.agent_id)
        with self._store.transaction():
            plan["removed"] = self._store.remove_connections([
                (link["source_id"], link["target_id"], "contradicts")
                for link in found["links"]
            ])
            for item in found["beliefs"]:
                belief = self._store.get_belief(item["belief_id"])
                if belief is None:
                    continue
                count = item["revisions"]
                belief.revise(
                    item["after"],
                    f"{KEYWORD_CONTRADICTIONS_RESTORED}undid {count} "
                    f"revision{'s' if count != 1 else ''} "
                    f"(-{item['lowered']:.2f} in all) written by the no-model "
                    "keyword-and-negation check, which lowered a belief whenever "
                    "a note shared a word with it and held a negation. Nothing "
                    "had judged them.",
                )
                self._store.save_belief(belief)
                plan["restored"] += 1
        return plan

    def repair_versions(self, *, write: bool = False) -> dict[str, Any]:
        """Show, and with ``write`` remove, version rows that repeat the one
        before them.

        Every return used to append a full snapshot of the memory to its
        history, although a return changes nothing a version records. This
        finds those copies (``EngramStore.duplicate_versions``) across the
        whole store: rows a return wrote that equal the row just before them.
        The first row of every run stays, and so does every row written for
        another reason, so every state the history recorded is kept.

        A dry run reads the store read-only and changes nothing. With
        ``write``, a verified backup of the store as found comes first; then,
        in one transaction, the copies go and nothing else changes. A second
        run finds nothing. Code older than the store refuses to write.
        """
        plan: dict[str, Any] = {
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "older_than_store": False,
            "found": None,
            "removed": 0,
            "backup": None,
        }
        if not plan["exists"]:
            return plan

        peek = ReadOnlyEngramStore(self.db_path)
        try:
            minimum = peek.min_code_version()
            plan["found"] = peek.duplicate_versions()
        finally:
            peek.close()
        plan["older_than_store"] = minimum is not None and minimum > MAINTENANCE_CODE_VERSION
        if not write or plan["older_than_store"] or not plan["found"]["duplicates"]:
            return plan

        from .backup import create_backup

        # The backup is the store exactly as the human found it: read through
        # a read-only connection, before opening for writing migrates the
        # schema or records this code's version.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-repair-versions-{stamp}.db"
        )
        source = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            backup = create_backup(self.db_path, destination, source_connection=source)
        finally:
            source.close()
        plan["backup"] = backup["path"]

        self._ensure_init()
        assert self._store is not None
        if self._older_than_store() is not None:
            plan["older_than_store"] = True
            return plan
        # Found again under the writer: the store may have moved on since.
        with self._store.transaction():
            found = plan["found"] = self._store.duplicate_versions()
            plan["removed"] = self._store.remove_versions(found["duplicates"])
        return plan

    def repair_quarantine_tool_written(
        self, *, write: bool = False, undo: bool = False,
    ) -> dict[str, Any]:
        """Show, and with ``write`` move, the memories in this scope a tool
        wrote, into the legacy quarantine; or with ``undo``, back out of it.

        A tool's words (``author_kind`` 'tool': on a real store, the transcript
        indexer's model's output and the lessons copied from it) sat in scope
        beside the agent's own, where recall and the packet reached them. This
        moves them where the v6 scope migration left the rows it could not
        place: without a person or project, out of every scoped read, with
        their words, links and history intact. A default adoption leaves them.
        ``undo`` returns exactly the memories this moved out of this scope
        (the store records where each came from); ``mnemos adopt-legacy
        --include indexer`` brings them back with all the other tool-written
        memories the quarantine holds.

        A dry run reads the store read-only and changes nothing, and works on
        a store not yet migrated to record authorship (the migration's own rule
        decides then). With ``write``, a verified backup of the store as found
        comes first; then, in one transaction, they move. Archived memories
        stay where they are, as does one a continuity note points at (opening
        the store would give it back its note's scope). A second run finds
        nothing. Only a human runs it, and code older than the store refuses.
        """
        plan: dict[str, Any] = {
            "target": (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "undo": undo,
            "older_than_store": False,
            "found": [],
            "movable": [],
            "moved": 0,
            "backup": None,
        }
        if not plan["exists"]:
            return plan
        scope = {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }

        def movable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [row for row in rows if row["state"] != "archived" and not row["linked"]]

        def find(store: EngramStore) -> list[dict[str, Any]]:
            if not undo:
                return store.tool_written_engrams(**scope)
            # What this repair moved out of this scope and is still without one.
            here = [scope["agent_id"], scope["person_id"], scope["project_scope"]]
            moved_here = {
                engram_id for engram_id, came_from in store.quarantined_tool_written().items()
                if came_from == here
            }
            return [
                {"id": row["id"], "state": row["state"], "content": row["content"],
                 "linked": False}
                for row in store.unscoped_engrams(scope["agent_id"])
                if row["id"] in moved_here
            ]

        peek = ReadOnlyEngramStore(self.db_path)
        try:
            minimum = peek.min_code_version()
            plan["found"] = find(peek)
        finally:
            peek.close()
        plan["movable"] = movable(plan["found"])
        plan["older_than_store"] = minimum is not None and minimum > MAINTENANCE_CODE_VERSION
        if not write or plan["older_than_store"] or not plan["movable"]:
            return plan

        from .backup import create_backup

        # The backup is the store exactly as the human found it: read through
        # a read-only connection, before opening for writing migrates the
        # schema or records this code's version.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.db_path.parent / "backups"
            / f"{self.db_path.stem}.pre-{'un' if undo else ''}quarantine-tool-written-{stamp}.db"
        )
        source = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            backup = create_backup(self.db_path, destination, source_connection=source)
        finally:
            source.close()
        plan["backup"] = backup["path"]

        self._ensure_init()
        assert self._store is not None
        if self._older_than_store() is not None:
            plan["older_than_store"] = True
            return plan
        # Found again under the writer: the store may have moved on since.
        with self._store.transaction():
            plan["found"] = find(self._store)
            plan["movable"] = movable(plan["found"])
            ids = [row["id"] for row in plan["movable"]]
            plan["moved"] = (
                self._store.unquarantine_engrams(ids) if undo
                else self._store.quarantine_engrams(ids, **scope)
            )
        return plan

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
        self._store = None
        self._encoder = None
        self._retriever = None
        self._embedding_index = None
        self._llm_client = None

    def execute_host_mutation(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        host_namespace: str,
        idempotency_key: str,
        protocol_version: int = HOST_MUTATION_PROTOCOL_VERSION,
    ) -> dict[str, Any]:
        """Execute one durable host request with exactly-once Core effects.

        The idempotency claim, all Core SQLite writes, and the serialized
        result commit in one transaction. A retry with the same namespace/key
        returns the original result. Reusing that key for different operation,
        arguments, protocol, or runtime scope fails closed.

        Embeddings are deliberately outside this guarantee: they are a
        rebuildable retrieval cache stored through a separate connection.
        Host mutations temporarily suppress embedding writes so they cannot
        break or delay the atomic Core transaction.
        """

        if protocol_version != HOST_MUTATION_PROTOCOL_VERSION:
            raise ValueError(
                "Unsupported host mutation protocol version: "
                f"{protocol_version}; expected {HOST_MUTATION_PROTOCOL_VERSION}"
            )
        operation = (operation or "").strip().lower()
        if operation not in HOST_MUTATION_OPERATIONS:
            supported = ", ".join(sorted(HOST_MUTATION_OPERATIONS))
            raise ValueError(f"Unsupported host mutation operation: {operation!r}; {supported}")
        namespace = (host_namespace or "").strip()
        key = (idempotency_key or "").strip()
        if not namespace or len(namespace) > 128:
            raise ValueError("host_namespace must contain 1-128 characters")
        if not key or len(key) > 256:
            raise ValueError("idempotency_key must contain 1-256 characters")
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments must be a mapping")

        scope = {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }
        request = {
            "protocol_version": protocol_version,
            "operation": operation,
            "scope": scope,
            "arguments": dict(arguments),
        }
        try:
            request_json = json.dumps(
                request, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("host mutation arguments must be finite JSON values") from exc
        if len(request_json.encode("utf-8")) > _MAX_HOST_MUTATION_REQUEST_BYTES:
            raise ValueError("host mutation request exceeds 1 MiB")
        request_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        scope_json = json.dumps(scope, sort_keys=True, separators=(",", ":"))

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        # The embedding table shares the database file through another SQLite
        # connection. It is a rebuildable cache, not canonical memory state.
        encoder_embedding = self._encoder._embedding_index
        runtime_embedding = self._embedding_index
        runtime_state = (
            self._session_id,
            self._agent_model_hint,
            self._llm_client,
            self.last_dream_note_id,
            self.last_dream_narrative,
        )
        self._encoder._embedding_index = None
        self._embedding_index = None
        prior_host_mutation_active = self._host_mutation_active
        self._host_mutation_active = True
        try:
            with self._store.transaction():
                prior = self._store.get_host_mutation(namespace, key)
                if prior is not None:
                    if prior["request_sha256"] != request_sha256:
                        raise HostMutationConflictError(
                            "idempotency key already belongs to a different request"
                        )
                    if not prior.get("completed_at") or prior.get("result_json") is None:
                        raise RuntimeError("incomplete host mutation ledger row")
                    result = json.loads(prior["result_json"])
                    replayed = True
                else:
                    self._store.begin_host_mutation(
                        host_namespace=namespace,
                        idempotency_key=key,
                        protocol_version=protocol_version,
                        operation=operation,
                        scope_json=scope_json,
                        request_sha256=request_sha256,
                    )
                    handler = getattr(self, operation)
                    result = handler(**dict(arguments))
                    result_json = json.dumps(
                        result, ensure_ascii=True, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    )
                    if len(result_json.encode("utf-8")) > _MAX_HOST_MUTATION_REQUEST_BYTES:
                        raise ValueError("host mutation result exceeds 1 MiB")
                    self._store.complete_host_mutation(
                        host_namespace=namespace,
                        idempotency_key=key,
                        result_json=result_json,
                    )
                    replayed = False
        except Exception:
            (
                self._session_id,
                self._agent_model_hint,
                self._llm_client,
                self.last_dream_note_id,
                self.last_dream_narrative,
            ) = runtime_state
            self._encoder._llm_client = self._llm_client
            raise
        finally:
            self._host_mutation_active = prior_host_mutation_active
            self._encoder._embedding_index = encoder_embedding
            self._embedding_index = runtime_embedding

        return {
            "protocol_version": protocol_version,
            "operation": operation,
            "idempotency_key": key,
            "replayed": replayed,
            "result": result,
        }

    def _ensure_init(self) -> None:
        if self._store is not None:
            return

        if self._read_only:
            self._store = ReadOnlyEngramStore(self.scope.db_path)
            # The index creates its table when it opens a store without one,
            # so it only gets the path when the table is already there. With
            # no table there are no stored vectors to count anyway.
            has_vectors = self._store._get_conn().execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
            ).fetchone() is not None
            self._embedding_index = EmbeddingIndex(
                db_path=self.scope.db_path if has_vectors else None
            )
        else:
            # Opening the store for writing records this code's version in it
            # (EngramStore._record_code_version), so a server still running
            # older code stops maintaining it.
            self._store = EngramStore(self.scope.db_path)
            self._embedding_index = EmbeddingIndex(db_path=self.scope.db_path)
        # The agent's self-declared model, recorded for the record rather
        # than to gate anything. Read straight from the freshly created
        # store: _get_meta would re-enter init.
        self._agent_model_hint = self._store.get_meta(self._meta_key("agent_model"))
        try:
            from .llm import create_client

            self._llm_client = (
                create_client(agent_model_hint=self._agent_model_hint)
                if self._use_dedicated_model and _dedicated_model_requested()
                else None
            )
        except Exception:
            self._llm_client = None

        # The encoding section of ~/.mnemos/config.json, so what it sets (the
        # bar for links made at save time) is applied rather than silently
        # replaced by the defaults.
        try:
            encoding_config = load_config().get("encoding")
        except Exception:
            encoding_config = None
        self._encoder = Encoder(
            self._store,
            embedding_index=self._embedding_index,
            llm_client=self._llm_client,
            config=encoding_config,
        )
        self._retriever = ReactiveRetriever(
            self._store,
            embedding_index=self._embedding_index,
        )

    def _older_than_store(self) -> int | None:
        """The store's minimum when this code is older than it, else None.

        Read on every call, not only at startup: a server that has run for
        days learns here that a newer Mnemos has opened the store since.
        """
        assert self._store is not None
        minimum = self._store.min_code_version()
        if minimum is not None and minimum > MAINTENANCE_CODE_VERSION:
            return minimum
        return None

    def _stale(self) -> bool:
        """Whether this code is older than the store, asked by a tool result.

        A tool can return before it opens the store (an empty capture, say).
        Then the store is looked at read-only, and a store that does not exist
        is not brought into being by asking.
        """
        if self._store is not None:
            return self._older_than_store() is not None
        if not self.db_path.exists():
            return False
        peek = ReadOnlyEngramStore(self.db_path)
        try:
            minimum = peek.min_code_version()
        except sqlite3.Error:
            return False
        finally:
            peek.close()
        return minimum is not None and minimum > MAINTENANCE_CODE_VERSION

    def code_versions(self) -> dict[str, Any]:
        """The maintenance code version running here, and the store's minimum.

        Read-only, and never creates a store. ``older_than_store`` is true once
        a newer Mnemos has opened this store, which is when this process stops
        maintaining it.
        """
        store_minimum = None
        if self.db_path.exists():
            self._ensure_init()
            assert self._store is not None
            store_minimum = self._store.min_code_version()
        return {
            "running": MAINTENANCE_CODE_VERSION,
            "store_minimum": store_minimum,
            "older_than_store": (
                store_minimum is not None and store_minimum > MAINTENANCE_CODE_VERSION
            ),
        }

    def _stats(self) -> dict[str, Any]:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_stats(
            self.scope.agent_id, person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )

    def _meta_key(self, name: str) -> str:
        return f"simple:{self.scope.agent_id}:{self.scope.person_id}:{self.scope.project_scope}:{name}"

    def _get_meta(self, name: str, default: str | None = None) -> str | None:
        self._ensure_init()
        assert self._store is not None
        return self._store.get_meta(self._meta_key(name), default)

    def _set_meta(self, name: str, value: str) -> None:
        self._ensure_init()
        assert self._store is not None
        self._store.set_meta(self._meta_key(name), value)

    def _current_session(self) -> int:
        """Bump the persisted session counter once per runtime instance."""

        if self._session_id is None:
            counter = int(self._get_meta("session_counter", "0") or 0) + 1
            self._set_meta("session_counter", str(counter))
            self._session_id = counter
        return self._session_id

    def _onboarding_status(self, persist: bool = True) -> dict:
        """Where this scope stands in the first-session onboarding ritual.

        Returns {"stage": str, "introduced": bool, "captured": bool}. Stores
        that predate onboarding are grandfathered: any existing memory marks
        the scope complete so an established agent never sees the ritual.
        """

        stage = self._get_meta("onboarding_stage")
        stats = self._stats()
        if stage is None:
            existing = (
                stats.get("engrams_active", 0)
                + stats.get("engrams_consolidating", 0)
                + stats.get("engrams_dormant", 0)
                + stats.get("engrams_archived", 0)
                + stats.get("archived", 0)
                + stats.get("hypomnema_total", 0)
            )
            if existing > 0:
                stage = "complete"
                if persist:
                    self._set_meta("onboarding_stage", stage)
                    self._set_meta("verified_at", "skipped")
            else:
                stage = "fresh"
                if persist:
                    self._set_meta("onboarding_stage", stage)

        introduced = bool(self._get_meta("agent_model"))
        captured = (
            self._get_meta("first_capture") is not None
            or stats.get("hypomnema_total", 0) > 0
        )
        if stage == "fresh" and introduced and captured:
            stage = "complete"
            if persist:
                self._set_meta("onboarding_stage", stage)
        return {"stage": stage, "introduced": introduced, "captured": captured}

    def _onboarding_block(self, status: dict) -> str | None:
        """Build the onboarding reminder for the context packet, if any."""

        if status["stage"] == "complete":
            return None

        introduced = bool(status["introduced"])
        captured = bool(status["captured"])
        if not introduced and not captured:
            return _ONBOARDING_RITUAL

        lines = ["ONBOARDING - almost done"]
        if not introduced:
            lines.append(
                "- Call mnemos_introduce with agent_model set to your own model id. "
                "You know what model you are - do not ask the human."
            )
        if not captured:
            lines.append(
                "- Ask the human for one small, true fact about themselves and "
                "capture it with mnemos_capture."
            )
        lines.append(
            "Then tell the human what you will remember. This reminder disappears "
            "once setup is complete."
        )
        return "\n".join(lines)

    def _enqueue_impact_reflections(self, limit: int = 2) -> int:
        """Notice captures that recorded what happened but not what it meant.

        Shift 1 asks for a trace of how understanding changed, not a record
        of an event. The server must never write one itself — a phrase it
        picked from a list is exactly the boilerplate that made 76% of a
        live store records rather than traces. It can only notice, and ask.
        """
        self._ensure_init()
        assert self._store is not None

        rows = self._store._get_conn().execute(
            """
            SELECT e.id, e.content, e.impact
            FROM engrams e
            JOIN hypomnema_entries h ON h.related_engram_id = e.id
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.active = 1 AND e.state = 'active'
            ORDER BY e.created_at DESC
            LIMIT 40
            """,
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall()

        # A memory already being asked about as a fading lesson must not also
        # be asked about as a missing impact. Two questions about one memory
        # in one packet reads as nagging, however reasonable each is alone.
        already_asked = {
            r[0] for r in self._store._get_conn().execute(
                """
                SELECT target_id FROM reflection_queue
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND answered_at IS NULL
                """,
                (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            ).fetchall()
        }

        enqueued = 0
        for row in rows:
            if enqueued >= limit:
                break
            if row["id"] in already_asked:
                continue
            impact = (row["impact"] or "").strip()
            if impact and impact not in _TEMPLATED_IMPACTS:
                continue
            if self._store.enqueue_reflection(
                "impact",
                row["id"],
                "What did this change in how you understand things? One sentence.",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                enqueued += 1
        return enqueued

    def _enqueue_lesson_reflections(self, softening_stats: dict, limit: int = 2) -> int:
        """Ask what a fading memory taught, before the detail is gone.

        Shift 2: the loss of detail is the learning. Softening compresses
        deterministically, but what a memory *meant* is the one thing the
        server must not guess at — a lesson assembled from keywords is not
        wisdom, it is a summary wearing wisdom's clothes.
        """
        self._ensure_init()
        assert self._store is not None

        enqueued = 0
        for engram_id in (softening_stats.get("awaiting_impact") or [])[:limit]:
            engram = self._store.get_engram(engram_id)
            if engram is None:
                continue
            # A plain "what did this change?" may already be pending from
            # when the memory was captured. Now that it is actually fading,
            # the lesson question subsumes it — asking both is asking twice.
            self._store._get_conn().execute(
                """
                DELETE FROM reflection_queue
                WHERE target_id = ? AND kind = 'impact' AND answered_at IS NULL
                  AND agent_id = ? AND person_id = ? AND project_scope = ?
                """,
                (engram_id, self.scope.agent_id, self.scope.person_id,
                 self.scope.project_scope),
            )
            self._store._commit()
            if self._store.enqueue_reflection(
                "lesson",
                engram_id,
                "This is fading. What did it teach you? The lesson outlives the details.",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                enqueued += 1
        return enqueued

    def _enqueue_belief_reflections(self, limit: int = 1) -> int:
        """Notice a theme the agent keeps returning to, and ask if it is a belief.

        Belief formation is otherwise absent on a keyless install — nothing
        mints new beliefs. Maintenance can only NOTICE a recurring theme (a
        non-bookkeeping tag across several memories, with no belief covering it
        yet) and ask; the agent states the belief in its own words through
        mnemos_reflect, or leaves it. The server never writes a belief itself.

        Beside it, one belief the agent holds may be due to be put to it
        again (see ``_enqueue_belief_reaffirmation``): the agent keeps or
        retires it by its verdict. Conservative by design (one of each per
        cycle at most), and the packet's own ≤2 cap keeps it from ever
        nagging.
        """
        self._ensure_init()
        assert self._store is not None
        from collections import Counter

        unanswered = self._store._get_conn().execute(
            "SELECT target_id, kind, prompt FROM reflection_queue WHERE agent_id = ? "
            "AND person_id = ? AND project_scope = ? AND answered_at IS NULL",
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall()
        already = {r["target_id"] for r in unanswered}
        # A theme is asked once. Leaving the ask is how the agent declines, and
        # an ask that was shown out or expired still records that it was put.
        # Deduping by target alone let a declined theme return every cycle
        # against a fresh memory — one more row per cycle, without end. An
        # answered ask counts too: a theme the agent declined stays declined.
        asked_themes = set()
        for r in self._store._get_conn().execute(
            "SELECT prompt FROM reflection_queue WHERE agent_id = ? "
            "AND person_id = ? AND project_scope = ? AND kind = 'belief'",
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall():
            m = _THEME_MARKER.search(r["prompt"] or "")
            if m:
                asked_themes.add(m.group(1).strip())

        # Themes are mined only from what the agent wrote: a word a tool's or
        # a model's memories keep using is theirs, not something the agent
        # keeps returning to.
        rows = self._store._get_conn().execute(
            """
            SELECT e.id, e.content
            FROM engrams e
            JOIN hypomnema_entries h ON h.related_engram_id = e.id
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.active = 1 AND e.state = 'active'
              AND e.author_kind = 'agent'
            ORDER BY e.created_at DESC
            LIMIT 200
            """,
            (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
        ).fetchall()

        # A theme is a salient content term recurring across several memories.
        # Tags from a simple capture are all bookkeeping, so cluster on content
        # instead — the agent then judges whether the recurrence is a belief.
        # Salient means distinctive, as for links and lessons: counted over
        # every word, the ask went to "only", "asked", "first" and "into".
        term_engrams: dict[str, list[str]] = {}
        for row in rows:  # newest first
            for term in distinctive_terms(row["content"] or "") - _STOPWORDS:
                # Nearly every note starts with a date, and the tokenizer splits
                # dates and times into digit-led runs ("2026", "24t22", "11pm"),
                # so a year outranked every real theme. A number is not a theme;
                # words, even ones carrying digits like "a11y", lead with a letter.
                if term[0].isdigit():
                    continue
                ids = term_engrams.setdefault(term, [])
                if row["id"] not in ids:
                    ids.append(row["id"])

        existing = " ".join(
            b.content.lower()
            for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
        )

        ranked = sorted(term_engrams.items(), key=lambda kv: len(kv[1]), reverse=True)
        asked = 0
        for theme, ids in ranked:
            if len(ids) < _BELIEF_MIN_MEMORIES:
                break  # descending — nothing else clears the bar
            if theme in asked_themes or theme.lower() in existing:
                continue
            # A belief ask must not share a target with another pending
            # reflection: the tool answers by target_id alone, so a collision
            # would route the agent's belief answer to an impact question.
            target = next((i for i in ids if i not in already), None)
            if target is None:
                continue
            if self._store.enqueue_reflection(
                "belief",
                target,
                f'You keep returning to "{theme}" ({len(ids)} memories). Is that a '
                "belief you now hold? If it is, state it in one line with verdict "
                f"hold; if it is not, decline. Or leave it. [theme:{theme}]",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                already.add(target)
                asked = 1
                break  # one belief ask per cycle, never more

        # A reaffirmation is not left for a cycle with no new theme: on a
        # real store every cycle found one (14 cycles, 14 theme asks in a
        # day), so a belief waiting for a quiet cycle was never asked again.
        return asked + self._enqueue_belief_reaffirmation(already)

    def _enqueue_belief_reaffirmation(self, already: set[str]) -> int:
        """Put one belief the agent holds to it again: still true?

        A belief the agent stated may be asked about again once it has gone
        ``_REAFFIRM_AFTER_DAYS`` without being formed or reaffirmed, and no
        sooner than that after it was last asked. The ask has its own kind,
        so the answered ask that formed the belief no longer blocks it. One
        left unanswered is put again the same way once it has expired, if
        nothing else waits on its memory. ``already`` holds the memories that
        other questions wait on.
        """
        assert self._store is not None
        conn = self._store._get_conn()
        scope = (self.scope.agent_id, self.scope.person_id, self.scope.project_scope)
        now = datetime.now(timezone.utc)
        month = now - timedelta(days=_REAFFIRM_AFTER_DAYS)

        def within_month(timestamp: str | None) -> bool:
            moment = _moment(timestamp)
            return moment is not None and moment > month

        beliefs = [
            b for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
            if b.source == "agent" and b.supporting_engram_ids
        ]
        beliefs.sort(key=lambda b: b.last_challenged)  # least recently held to first
        for belief in beliefs:
            if within_month(belief.last_challenged):
                continue  # formed or reaffirmed this month
            target = belief.supporting_engram_ids[0]
            # Beliefs belong to the agent, not to a scope: ask about one only
            # where the memory it rests on lives, or the packet would show
            # another person's or project's memory.
            if not self._engram_visible_in_current_scope(target):
                continue
            engram = self._store.get_engram(target)
            if engram is None or str(getattr(engram.state, "value", engram.state)) == "archived":
                continue
            content = " ".join((belief.content or "").split())
            if len(content) > 160:
                content = content[:159].rstrip() + "…"
            prompt = (
                f'You hold this belief: "{content}". Still true? Verdict hold if it '
                f"is, retire if you no longer hold it, or leave it. [belief:{belief.id}]"
            )
            asked = conn.execute(
                "SELECT id, prompt, created_at, expires_at, answered_at "
                "FROM reflection_queue WHERE agent_id = ? AND person_id = ? "
                "AND project_scope = ? AND kind = 'reaffirm' AND target_id = ?",
                (*scope, target),
            ).fetchall()
            waiting = next((r for r in asked if r["answered_at"] is None), None)
            if waiting is not None:
                # One reaffirmation waits per memory. It is put again only
                # once it has expired, when it asks about this belief, and
                # when no other question waits on the memory.
                expires = _moment(waiting["expires_at"])
                if expires is None or expires > now:
                    continue
                if f"[belief:{belief.id}]" not in (waiting["prompt"] or ""):
                    continue
                others = conn.execute(
                    "SELECT COUNT(*) FROM reflection_queue WHERE agent_id = ? "
                    "AND person_id = ? AND project_scope = ? AND target_id = ? "
                    "AND answered_at IS NULL AND id != ?",
                    (*scope, target, waiting["id"]),
                ).fetchone()[0]
                if others:
                    continue
                conn.execute(
                    "UPDATE reflection_queue SET prompt = ?, created_at = ?, "
                    "expires_at = ?, surfaced_count = 0 "
                    "WHERE id = ? AND answered_at IS NULL",
                    (prompt, now.isoformat(),
                     (now + timedelta(days=_REAFFIRM_AFTER_DAYS)).isoformat(),
                     waiting["id"]),
                )
                self._store._commit()
                return 1
            if target in already:
                continue  # another question waits on this memory
            if any(
                within_month(r["answered_at"] or r["created_at"])
                for r in asked
                if f"[belief:{belief.id}]" in (r["prompt"] or "")
            ):
                continue  # asked this month, and answered
            if self._store.enqueue_reflection(
                "reaffirm",
                target,
                prompt,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                expires_in_days=_REAFFIRM_AFTER_DAYS,
            ):
                return 1
        return 0

    def _enqueue_contradiction_reflections(self, limit: int = 1) -> int:
        """Ask the agent to judge a genuine tension it just encountered.

        Contradiction is the highest-value edge — it is how wrong memory gets
        corrected — but detecting one needs judgement no keyword heuristic has.
        So maintenance only surfaces a *candidate*: a capture whose encoding
        registered real surprise (it did not fit what was already held) paired
        with its nearest existing neighbour. The agent decides whether they
        actually conflict. Rare by construction — most captures aren't
        surprising — so this is not a chore stream.
        """
        self._ensure_init()
        assert self._store is not None

        already = {
            r[0] for r in self._store._get_conn().execute(
                "SELECT target_id FROM reflection_queue WHERE agent_id = ? "
                "AND person_id = ? AND project_scope = ? AND answered_at IS NULL",
                (self.scope.agent_id, self.scope.person_id, self.scope.project_scope),
            ).fetchall()
        }

        recent = self._store.get_active_engrams(agent_id=self.scope.agent_id, limit=25)
        for engram in recent:
            if engram.id in already:
                continue
            surprise = float(
                getattr(engram.encoding_context, "surprise_level", 0.0) or 0.0
            )
            if surprise < _CONTRADICTION_MIN_SURPRISE:
                continue
            neighbor = self._nearest_neighbor(engram)
            if neighbor is None:
                continue
            # Skip if the pair is already typed as contradicting.
            if any(
                c.target_id == neighbor.id
                and str(getattr(c.relation, "value", c.relation)) == "contradicts"
                for c in engram.connections
            ):
                continue
            excerpt = " ".join((neighbor.content or "").split())[:120]
            if self._store.enqueue_reflection(
                "contradiction",
                engram.id,
                f'This memory surprised you. Does it contradict an earlier one: '
                f'"{excerpt}"? Verdict contradicts, compatible or unsure, and say '
                f"why. [ref:{neighbor.id}]",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ):
                return 1
        return 0

    def _nearest_neighbor(self, engram: Any) -> Any:
        """The most topically-overlapping other active engram, or None.

        Reuses the FTS OR-query neighbour pattern the encoder uses, so a
        contradiction candidate is found the same cheap way connections are.
        """
        assert self._store is not None
        words = fts_words(engram.content or "", min_len=4)
        if not words:
            return None
        query = or_query(words[:8])
        try:
            results = self._store.search_fts(query, limit=5)
        except (ValueError, OSError):
            return None
        for r in results:
            if r.id == engram.id:
                continue
            neighbor = self._store.get_engram(r.id)
            if neighbor is not None and neighbor.owner_agent_id == self.scope.agent_id:
                return neighbor
        return None

    def pending_reflections(self, limit: int = 2) -> list[dict[str, Any]]:
        self._ensure_init()
        assert self._store is not None
        return self._store.pending_reflections(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
        )

    @_notice_when_older
    @_traced("reflect")
    def reflect(
        self, target_id: str, text: str, verdict: str = "", signed_as: str = "",
    ) -> str:
        """Record the agent's own reflection on one of its memories.

        ``verdict`` is what the agent decided (see ``_VERDICTS``), and it alone
        decides what happens. The words are kept exactly as written and never
        read for a yes or a no. A lesson or impact question asks for the words
        themselves, so without a verdict they are its answer, as before. A
        belief or contradiction question answered without one keeps the words
        and stays open, and nothing is formed, retired or linked.

        The answer is the agent's, signed with ``signed_as`` when the agent
        gives its model id: a note it adds to, an answer kept open, and a
        lesson drawn from it all carry that signature.
        """
        answer = (text or "").strip()
        if not answer:
            return "Nothing recorded: the reflection was empty."
        decided = (verdict or "").strip().lower().replace("-", "_").replace(" ", "_")
        if decided and decided not in VERDICTS:
            return f"Nothing recorded: {verdict.strip()!r} is not a verdict. {_VERDICT_GUIDE}"

        self._ensure_init()
        assert self._store is not None
        author = self.author_model(signed_as)
        self._traced_author(author)

        scope = {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }
        target = target_id.strip()
        self._traced_read(target)
        ask = self._store.pending_reflection_for(target, **scope)
        if ask is None:
            return (
                f"Nothing was pending for {target_id}. It may already have been "
                "reflected on, or the id may be wrong."
            )
        kind = _question_kind(ask)
        allowed = _VERDICTS.get(kind)
        if decided and allowed is not None and decided not in allowed:
            return (
                f"Nothing recorded: {decided} does not answer this question. "
                f"It takes {_VERDICT_HELP[kind]}."
            )

        # Code older than the store keeps the agent's words and applies none
        # of its rules to them. What a verdict does to a belief or a link may
        # have changed since, so answering here would spend the question: it
        # stays open for a current session instead. So does a question of a
        # kind this code does not know.
        older = self._older_than_store() is not None
        if kind not in _ANSWERED_BY_WORDS and (older or allowed is None):
            return self._keep_answer_open(ask, answer, decided, author=author)
        if kind not in _ANSWERED_BY_WORDS and decided in ("", "not_now"):
            self._keep_on_question(ask, answer)
            if decided:
                return "Left open for later. Your words are kept on the question."
            return (
                "Recorded your words on the question; it stays open. Nothing was "
                "formed, retired or linked, because no verdict was given.\n"
                f"To decide it, answer again with a verdict: {_VERDICT_HELP[kind]}."
            )

        item = self._store.answer_reflection(
            target, answer, reflection_id=ask["id"], **scope
        )
        if item is None:
            return (
                f"Nothing was pending for {target_id}. It may already have been "
                "reflected on, or the id may be wrong."
            )

        if kind == "belief":
            return self._apply_belief_reflection(item, answer, decided)
        if kind == "reaffirm":
            return self._apply_reaffirmation(item, answer, decided)
        if kind == "contradiction":
            return self._apply_contradiction_reflection(item, answer, decided)

        if decided == "skip":
            return (
                "Skipped. The question is closed and the memory is left as it was. "
                "Your words are kept on the question."
            )

        if item["kind"] == "impact":
            engram = self._store.get_engram(item["target_id"])
            if engram is None:
                return f"Recorded, but the memory {item['target_id']} is no longer there."
            engram.impact = answer
            engram.impact_source = "agent"
            self._store.save_engram(engram)
            self._traced_write(engram.id)
            self._traced_write(self._carry_reflection_into_note(engram.id, answer, author=author))
            return (
                "Reflection recorded.\n"
                f"  Memory: {' '.join((engram.content or '').split())[:80]}\n"
                f"  Now carries: {answer}\n"
                "This is what survives when the details fade."
            )

        if item["kind"] == "lesson":
            engram = self._store.get_engram(item["target_id"])
            if engram is None:
                return f"Recorded, but the memory {item['target_id']} is no longer there."
            engram.impact = answer
            engram.impact_source = "agent"
            self._store.save_engram(engram)
            self._traced_write(engram.id)
            self._traced_write(self._carry_reflection_into_note(engram.id, answer, author=author))
            if older:
                # Filing a lesson matches it against the lessons already held,
                # by rules newer code may have replaced. The memory keeps the
                # words; softening under current code files the lesson from
                # them the next time it reaches this memory.
                return (
                    "Reflection recorded.\n"
                    f"  Memory: {' '.join((engram.content or '').split())[:80]}\n"
                    f"  Now carries: {answer}\n"
                    "Filing it as a lesson waits for current Mnemos."
                )

            # Shift 2: the distilled insight becomes its own durable memory,
            # linked back to the experience it came from. This edge has been
            # absent from every Mnemos store ever built.
            from .consolidation.softening import _create_or_reinforce_lesson

            # Drawn only from the agent's own memory: an answer about a
            # memory a tool wrote stays that memory's impact and no lesson.
            lesson_id = _create_or_reinforce_lesson(
                engram, self._store, {},
                author_model=author, author_session=harness_session(),
            )
            self._traced_write(lesson_id)
            if lesson_id is None and engram.author_kind != "agent":
                return (
                    "Reflection recorded.\n"
                    f"  Memory: {' '.join((engram.content or '').split())[:80]}\n"
                    f"  Now carries: {answer}\n"
                    "No lesson was drawn from it: you didn't write that memory, "
                    "and lessons come only from your own."
                )
            return (
                "Lesson recorded.\n"
                f"  From: {' '.join((engram.content or '').split())[:70]}\n"
                f"  Learned: {answer}\n"
                + (f"  Kept as: {lesson_id}\n" if lesson_id else "")
                + "The details can fade now. This is what stays."
            )

        return f"Reflection recorded for {item['target_id']} ({item['kind']})."

    def _keep_on_question(self, ask: dict[str, Any], answer: str) -> None:
        """Keep the agent's words on a question that stays open.

        The words sit on the ask as its answer so far. It stays pending, its
        showings unchanged, and an answer with a verdict later replaces them.
        """
        assert self._store is not None
        self._store._get_conn().execute(
            "UPDATE reflection_queue SET answer = ? WHERE id = ? AND answered_at IS NULL",
            (answer, ask["id"]),
        )
        self._store._commit()

    def _keep_answer_open(
        self, ask: dict[str, Any], answer: str, verdict: str = "", *, author: str = "",
    ) -> str:
        """Keep an answer older code cannot apply, without spending its question.

        The words become a continuity note signed ``author`` that names the
        question (its id and its text) and the verdict given, if any, so they
        are neither lost nor spent. The ask itself is left exactly as it was:
        pending, its showings unchanged.
        """
        assert self._store is not None
        domain = _classify_domain(answer)
        confidence, salience = _importance_scores("auto", domain)
        note = f'{answer}\n\nIn answer to open question {ask["id"]}: "{ask["prompt"]}"'
        if verdict:
            note += f"\nVerdict: {verdict}"
        note_id = self._store.write_hypomnema_entry(
            note,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            entry_kind="continuity",
            authored_by="agent",
            author_id=self.scope.agent_id,
            author_model=author,
            author_session=harness_session(),
            domain=domain,
            tags=sorted({"reflection", "open-question", *_simple_tags(answer)}),
            confidence=confidence,
            salience=salience,
        )
        self._traced_write(note_id)
        return (
            "Kept your answer as a continuity note; the question stays open "
            "for a current session.\n"
            f"  {answer}\n"
            f"Continuity note ID: {note_id}\n"
            f"{self._signed_line(author)}"
        )

    def _apply_belief_reflection(self, item: dict[str, Any], answer: str, verdict: str) -> str:
        """Form the belief the agent stated, or record that it declined.

        hold forms it from the agent's words, at 0.4, with the asked-about
        memory as its first evidence and the ask's theme as its domain.
        decline forms nothing: the words stay on the answered ask.
        """
        assert self._store is not None
        from .core.belief import Belief

        if verdict == "decline":
            return "Declined. No belief was formed; your words are kept on the question."

        theme = ""
        tm = _THEME_MARKER.search(item.get("prompt") or "")
        if tm:
            theme = tm.group(1).strip()
        belief = Belief(
            agent_id=self.scope.agent_id,
            content=answer,
            confidence=0.4,
            domain=theme or "general",
            supporting_engram_ids=[item["target_id"]],
            source="agent",
        )
        self._store.save_belief(belief)
        self._traced_write(belief.id)
        return (
            "Belief recorded, in your words.\n"
            f"  {answer}\n"
            "It will shape what you notice and can be revised as you learn."
        )

    def _apply_reaffirmation(self, item: dict[str, Any], answer: str, verdict: str) -> str:
        """Keep, retire, or leave as it is a belief the agent was asked about again.

        hold raises its confidence by 0.05, never past 0.99, and restarts the
        month before it is asked again. retire sets its confidence to 0 and
        stops it shaping context; the belief and its history are kept. Each
        is a revision entry carrying the agent's words. decline changes
        nothing. Nothing is deleted.
        """
        assert self._store is not None
        if verdict == "decline":
            return "Left as it is. The belief is unchanged; your words are kept on the question."

        marker = _BELIEF_MARKER.search(item.get("prompt") or "")
        belief = self._store.get_belief(marker.group(1)) if marker else None
        if belief is None:
            return "Recorded, but that belief is no longer there."
        if belief.superseded_by:
            return "Recorded. That belief was already retired, and it stays retired."

        before = belief.confidence
        if verdict == "hold":
            belief.revise(min(0.99, round(before + 0.05, 4)), f"reaffirmed by the agent: {answer}")
            belief.challenge()
            self._store.save_belief(belief)
            self._traced_write(belief.id)
            return (
                "Kept. The belief stands a little more firmly "
                f"({before:.0%} to {belief.confidence:.0%})."
            )

        belief.revise(0.0, f"retired by the agent: {answer}")
        belief.superseded_by = "retired"
        self._store.save_belief(belief)
        self._traced_write(belief.id)
        return (
            "Retired. That belief no longer shapes your context. It is kept, "
            "with your words, in its history."
        )

    def _apply_contradiction_reflection(self, item: dict[str, Any], answer: str, verdict: str) -> str:
        """Record the agent's judgement of a candidate contradiction.

        contradicts leaves exactly one CONTRADICTS edge between the pair, and
        does nothing else: it writes one from this memory to the other, marked
        `agent_reflection` so it is distinguishable and correctable, unless the
        pair already has one either way. The verdict says the two conflict,
        not which one is wrong, so neither memory is weakened. compatible
        removes only the edge the question proposes, a CONTRADICTS edge from
        this memory to the other, if one was written; every other link between
        them stays. unsure changes nothing.
        """
        assert self._store is not None
        from .core.engram import Connection
        from .core.types import ConnectionRelation

        m = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", item.get("prompt") or "")
        if not m:
            return "Recorded."
        other_id = m.group(1)
        source = self._store.get_engram(item["target_id"])
        other = self._store.get_engram(other_id)
        if source is None or other is None:
            return "Recorded, but one of the memories is no longer there."
        if verdict == "unsure":
            return "Noted as unsure. Nothing was changed."

        contradicts = ConnectionRelation.CONTRADICTS.value
        if verdict == "compatible":
            removed = self._store.remove_connections([(source.id, other.id, contradicts)])
            if removed:
                self._traced_write(source.id)
                return (
                    "Noted: not a contradiction. The contradiction link between "
                    "them was removed; every other link stays."
                )
            return "Noted: not a contradiction. No conflict recorded."

        linked = any(
            c.target_id == other.id and str(getattr(c.relation, "value", c.relation)) == contradicts
            for c in self._store.get_connections(source.id)
        ) or any(
            c.target_id == source.id and str(getattr(c.relation, "value", c.relation)) == contradicts
            for c in self._store.get_connections(other.id)
        )
        if not linked:
            self._store.save_connection(
                source.id,
                Connection(
                    target_id=other.id,
                    relation=contradicts,
                    strength=0.7,
                    formed_by="agent_reflection",
                ),
            )
            self._traced_write(source.id)
        return (
            "Contradiction recorded.\n"
            f"  {' '.join((source.content or '').split())[:70]}\n"
            f"  vs {' '.join((other.content or '').split())[:70]}\n"
            "They are linked as contradicting; neither is weakened."
        )

    #: How a reflection is labelled inside a continuity note. Stable, because
    #: re-reflecting has to find and replace the previous one rather than
    #: stack a second copy underneath it.
    _REFLECTION_MARKER = "What this changed:"

    def _carry_reflection_into_note(
        self, engram_id: str, answer: str, *, author: str = "",
    ) -> str | None:
        """Write the agent's reflection into the layer the packet is built from.

        `engram.impact` is the right home for a trace, and it is not enough on
        its own: the session packet is assembled from hypomnema, and the engram
        layer is excluded from it by default. A reflection written only to the
        engram was therefore unreachable from the automatic path — recoverable
        only by a manual recall whose cue happened to match — while the tool
        reported "Reflection recorded."

        The note keeps its original content and gains the sentence. Revising
        preserves the prior version in the entry's revision trail, so nothing
        the human said is overwritten. Returns the note id, or None when the
        memory has no note, which is not an error.
        """
        assert self._store is not None
        try:
            note = self._store.get_hypomnema_entry_for_engram(
                engram_id,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if note is None:
                return None

            base = (note.get("content") or "")
            # Drop an earlier reflection before adding this one, so answering
            # twice revises rather than accumulates.
            head = base.split(f"\n\n{self._REFLECTION_MARKER}")[0].rstrip()
            if not head:
                return None

            # The note stays signed by whoever wrote it; the reflection added
            # to it names its own author, who may be a different model.
            by = f"({display_name(author)}) " if author else ""
            return self._store.revise_hypomnema_entry(
                note["id"],
                f"{head}\n\n{self._REFLECTION_MARKER} {by}{answer.strip()}",
                reason="agent reflection",
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                revised_by=author,
            )
        except Exception:
            if self._host_mutation_active:
                raise
            # The impact write already succeeded. Losing the note update is
            # worth reporting as a partial success, never as a failed
            # reflection the agent might retype.
            return None

    def _reflection_block(self, limit: int = PACKET_QUESTIONS) -> str | None:
        """The question section as the packet shows it, spending one showing
        of each question shown, or None when nothing is waiting.

        The packet's builder shows the same section; this is for a caller that
        wants only the questions.
        """
        items = self.pending_reflections(limit=limit)
        if not items:
            return None

        assert self._store is not None
        self._store.mark_reflections_surfaced([i["id"] for i in items])
        return format_questions(items)

    def _note_context_outcome(self, returned: int) -> None:
        """Record whether this session's packet actually carried anything.

        Every failure this system has had looked identical from the
        outside: a layer reporting success while carrying nothing. A
        scope that did not match, a config never applied, a job
        maintaining a phantom store — all of them logged healthy. The one
        thing none of them could fake is that the packet came back empty,
        session after session. Counting that turns silent amnesia into a
        number someone can read.
        """
        if returned > 0:
            self._set_meta("empty_context_streak", "0")
            self._set_meta(
                "last_context_delivery_at", datetime.now(timezone.utc).isoformat()
            )
            return
        streak = int(self._get_meta("empty_context_streak", "0") or 0)
        self._set_meta("empty_context_streak", str(streak + 1))

    def continuity_signals(self) -> dict[str, Any]:
        """Evidence about whether continuity is actually working here.

        Read-only. Returns counts plus any warnings worth showing a human
        in plain words.
        """
        stats = self._stats()
        notes = int(stats.get("hypomnema_active", 0) or 0)
        session = int(self._get_meta("session_counter", "0") or 0)
        streak = int(self._get_meta("empty_context_streak", "0") or 0)

        last_capture = self._get_meta("last_capture_session")
        sessions_since_capture = (
            session - int(last_capture) if last_capture is not None else None
        )

        warnings: list[str] = []
        if notes == 0:
            warnings.append(
                "Nothing has been captured to this scope yet, so every "
                "session starts from zero. If captures are being made, they "
                "are landing somewhere this packet does not read."
            )
        if streak >= 3:
            warnings.append(
                f"The last {streak} context packets carried no continuity. "
                "Memory is being read but is coming back empty."
            )
        if sessions_since_capture is not None and sessions_since_capture >= 5:
            warnings.append(
                f"No capture has reached this scope in {sessions_since_capture} "
                "sessions. Either nothing durable has come up, or captures are "
                "not arriving."
            )
        last_delivery = self._get_meta("last_context_delivery_at")
        if notes > 0 and last_delivery is None:
            warnings.append(
                "Continuity exists in this scope, but no successful startup "
                "delivery has been recorded yet."
            )
        elif notes > 0 and last_delivery is not None:
            try:
                delivered_at = datetime.fromisoformat(last_delivery)
                if delivered_at.tzinfo is None:
                    delivered_at = delivered_at.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - delivered_at
                if age.days >= 7:
                    warnings.append(
                        f"Continuity has not been delivered for {age.days} days. "
                        "Check that the startup integration is still active."
                    )
            except ValueError:
                warnings.append(
                    "Continuity exists, but its last delivery time is unreadable."
                )
        assert self._store is not None
        active_handoff = self._store.get_latest_handoff(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )
        if active_handoff and int(active_handoff.get("surface_count", 0)) == 0:
            warnings.append(
                "A session handoff is waiting but has not been delivered yet."
            )

        return {
            "notes_active": notes,
            "session": session,
            "empty_context_streak": streak,
            "sessions_since_capture": sessions_since_capture,
            "last_delivered_at": last_delivery,
            "warnings": warnings,
        }

    def _record_first_capture(self, note_id: str, engram_id: str, content: str) -> None:
        """Record the first capture of a fresh scope for later verification."""

        if self._get_meta("first_capture") is not None or self._get_meta("verified_at") is not None:
            return
        # No excerpt. The block that renders this re-reads the note by id, so
        # storing a copy of the text here would put a quotable snapshot of the
        # human's first capture somewhere no deletion path reaches — and that
        # block instructs the agent to say it out loud.
        payload = {
            "note_id": note_id,
            "engram_id": engram_id,
            "session": self._current_session(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        self._set_meta("first_capture", json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def _verification_block(self) -> str | None:
        """One-time MEMORY VERIFIED block when continuity crosses a restart."""

        if self._get_meta("verified_at") is not None:
            return None
        raw = self._get_meta("first_capture")
        if raw is None:
            return None
        try:
            first_capture = json.loads(raw)
            note_id = first_capture["note_id"]
            first_session = int(first_capture["session"])
        except (ValueError, KeyError, TypeError):
            return None
        if self._get_meta("onboarding_stage") != "complete":
            return None
        if self._current_session() <= first_session:
            return None

        # Resolve the text now, from the live note. A pre-fix store may still
        # hold an ``excerpt`` key here; it is deliberately never read, because
        # the whole defect was that a frozen copy outlived the memory it came
        # from. If the note is gone or deactivated, the celebration simply does
        # not happen — a missing celebration is the correct failure, and
        # speaking a forgotten memory aloud is not.
        note = self._store.get_hypomnema_entry(
            note_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            active_only=True,
        )
        if note is None:
            return None
        excerpt = " ".join((note.get("content") or "").split())[:160]
        if not excerpt:
            return None

        self._set_meta("verified_at", datetime.now(timezone.utc).isoformat())
        return (
            "MEMORY VERIFIED - continuity crossed a restart\n"
            f'In an earlier session you captured this about the human: "{excerpt}"\n'
            "You still have it. Tell the human, in your own words, that you remember "
            "this from before, and quote it back to them. Let it be a small celebration: "
            "this is the moment their agent stopped forgetting between goodbyes.\n"
            "(This check fires once and will not appear again.)"
        )

    def author_model(self, signed_as: str = "") -> str:
        """The model signing this write, or ``""`` if unknown.

        Resolved on every write, because the human can switch models in the
        middle of a session: the model the agent signed the write as, the
        operator's MNEMOS_AGENT_MODEL, the model this session last introduced
        itself as, then the harness transcript. Never a guess.
        """

        return resolve_author_model(self._session_introduction(), signed_as=signed_as)

    def session_identity(self) -> tuple[str, str]:
        """The model and name this session last introduced itself as, or
        ``""``s.

        Only this session's own introduction counts. One store is shared by
        many sessions and models, and the store-wide declaration once signed
        every session's writes with whichever model introduced itself last
        (a Grok session's, found live on 2026-09-26). With a harness session
        id, the store keeps each session's latest introduction, so every
        process of the session (a restarted server, a CLI capture, `mnemos
        doctor`) reads the same one; without one, only this process's own
        introduction counts. Reading never creates a store.
        """

        if self._store is None and self.db_path.exists():
            self._ensure_init()
        if harness_session() and self._store is not None:
            model, name = session_introduction(self._store.get_meta)
            if model or name:
                return model, name
        return self._session_author, self._session_name

    def _session_introduction(self) -> str:
        """The model this session last introduced itself as, or ``""``."""

        return self.session_identity()[0]

    def _traced_read(self, *ids: Any) -> None:
        """Note ids the tool call under way showed or returned."""
        if self._trace is not None:
            self._trace["read"].extend(str(i) for i in ids if i)

    def _traced_write(self, *ids: Any) -> None:
        """Note ids the tool call under way wrote."""
        if self._trace is not None:
            self._trace["written"].extend(str(i) for i in ids if i)

    def _traced_author(self, author: str) -> None:
        """The model a write in the tool call under way was signed with."""
        if self._trace is not None and self._trace["author"] is None:
            self._trace["author"] = author

    def _record_trace(self, tool: str, trace: dict[str, Any]) -> None:
        """Write a tool call's memory_trace row, once the call has returned.

        Never fails the call it describes, and never opens a store: a call
        that did not open one leaves no row. Code older than the store writes
        none, since what a row records is newer code's to decide.
        """
        if self._store is None or self._read_only:
            return
        try:
            if self._older_than_store() is not None:
                return
            author = trace["author"]
            if author is None:
                author = self.author_model()
            self._store.record_trace(
                tool=tool,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                session=harness_session(),
                author_model=author,
                read_ids=trace["read"],
                written_ids=trace["written"],
            )
        except Exception:
            pass

    def _signed_line(self, author: str) -> str:
        if author:
            return f"Signed: {signature(author)}"
        return (
            "Unsigned: Mnemos couldn't tell which model you are. Call "
            "mnemos_introduce with your exact model id so your notes carry "
            "your name."
        )

    @_notice_when_older
    @_traced("introduce")
    def introduce(self, agent_model: str, agent_name: str = "") -> str:
        """Record the agent's self-declared model so maintenance stays kin.

        The declaration also signs what this session writes, when a write
        doesn't carry its own ``signed_as``. It is this session's alone: kept
        under the harness session's id when there is one (and in this process
        either way), never as a signature for other sessions.
        """

        model = (agent_model or "").strip()
        if not model:
            return (
                "Introduction needs agent_model: your own model id "
                "(for example claude-opus-5-5), exactly as your system prompt gives it."
            )

        self._set_meta("agent_model", model)
        self._session_author = clean_model_id(model)
        name = agent_name.strip()
        self._session_name = name
        session = harness_session()
        if session:
            # Written even when the id doesn't look like a model's, so an
            # earlier introduction of this session stops signing its writes.
            assert self._store is not None
            self._store.set_meta(
                session_introduction_key(session),
                session_introduction_record(self._session_author, name),
            )
        if name:
            self._set_meta("agent_name", name)

        # Keep this runtime and any outer host transaction alive. Reopening the
        # store here used to split introduction into multiple transactions.
        self._agent_model_hint = model
        try:
            from .llm import create_client

            self._llm_client = (
                create_client(agent_model_hint=model)
                if self._use_dedicated_model and _dedicated_model_requested()
                else None
            )
        except Exception:
            self._llm_client = None
        if self._encoder is not None:
            self._encoder._llm_client = self._llm_client

        lines = [
            "Introduction recorded.",
            f"Agent model: {model}",
            f"Agent name: {name or '(none given)'}",
            "Your memory is maintained by you — Mnemos never calls another "
            "model to do it.",
        ]
        env_model = os.environ.get("MNEMOS_AGENT_MODEL", "").strip()
        if env_model:
            lines.append(
                f"Note: MNEMOS_AGENT_MODEL={env_model} is set in the environment "
                "and takes precedence over this declaration."
            )
        signer = self.author_model()
        if signer:
            lines.append(f"Notes you write in this session are signed {signature(signer)}.")
        else:
            lines.append(
                f"{model!r} doesn't look like a model id, so your notes stay unsigned."
            )
        return "\n".join(lines)

    @_notice_when_older
    @_traced("context")
    def context(self, query: str = "", max_results: int = 5) -> str:
        """Return the session-start briefing, the one the hook injects.

        The shared packet comes first. After it, only what belongs to this
        call: the first-session ritual while onboarding lasts, the one-time
        MEMORY VERIFIED block, and, when ``query`` is given, what else in
        memory matches it (at most ``max_results`` of each kind), in the room
        left under the packet's budget. Building the packet runs no
        maintenance; that rides on captures, corrections and mnemos_maintain.
        """

        self._ensure_init()
        assert self._store is not None

        # Onboarding reads the store exactly as the session found it.
        status = self._onboarding_status()
        self._current_session()
        packet = self._briefing_packet()
        self._note_context_outcome(carried_count(packet))
        shown = packet.get("shown") or {}
        asked = set(shown.get("questions") or [])
        self._traced_read(
            *sorted(shown_ids(packet)),
            *(item["target_id"] for item in packet.get("reflections") or [] if item["id"] in asked),
        )

        parts = [packet["prompt"]] if packet["prompt"] else []
        block = self._onboarding_block(status)
        if block:
            parts.append(block)
        verification = self._verification_block()
        if verification:
            parts.append(verification)
        if query.strip():
            room = room_after("\n\n".join(parts), PACKET_MAX_CHARS)
            section = self._query_results(query, max_results, shown_ids(packet), room)
            if section:
                parts.append(section)
        if not parts:
            parts.append(
                "Nothing has carried over yet: no handoff, notes or beliefs in this "
                "memory. Capture durable context as the conversation gives it."
            )
        return "\n\n".join(parts)

    def _briefing_packet(
        self,
        *,
        workdir: str | None = None,
        reader_model: str | None = None,
        reader_session: str | None = None,
    ) -> dict[str, Any]:
        """The shared packet, built as the session-start hook builds it.

        Each input defaults to what this process can see: the model making the
        call, the Claude Code session (``CLAUDE_CODE_SESSION_ID``) and the
        working folder. Claude Code starts each session's server in the
        session's own folder, so the folder ranks notes the way the hook's
        payload does; it never chooses the scope.
        """
        assert self._store is not None
        return build_context_packet(
            self._store,
            "",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            include_engrams=False,
            reader_model=self.author_model() if reader_model is None else reader_model,
            reader_session=harness_session() if reader_session is None else reader_session,
            workdir=_working_folder() if workdir is None else workdir,
            older_than_store=self._older_than_store() is not None,
        )

    def _query_results(self, query: str, max_results: int, shown: set[str], room: int) -> str:
        """What else matches ``query``, after the packet: notes, then durable
        memories, as recall finds them, leaving out everything the packet
        showed. Whole entries only, in ``room`` characters; ``""`` when none
        fits. Only the memories kept are reinforced."""
        assert self._store is not None
        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results + len(shown),
            exclude_kinds=("handoff", "maintenance_report"),
        )
        continuity = [
            entry for entry in _filter_continuity(query, continuity)
            if entry["id"] not in shown
            and DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        ][:max_results]
        memories = [
            result for result in self._retrieve(query, max_results=max_results + len(shown))
            if result.engram.id not in shown
        ][:max_results]

        heading = f'### For "{query.strip()}"'
        if not continuity and not memories:
            said = f"{heading}\nNothing else in memory matches this."
            return said if len(said) <= room else ""
        section, kept = fit_section(
            heading,
            [
                ("Continuity notes:", [(("note", entry["id"]), _format_continuity(entry)) for entry in continuity]),
                ("Relevant memories:", [(("memory", index), _format_memory(result)) for index, result in enumerate(memories)]),
            ],
            room,
        )
        self._traced_read(*(
            key if kind == "note" else memories[key].engram.id for kind, key in kept
        ))
        self._reinforce_returned(
            query, [memories[index] for kind, index in kept if kind == "memory"],
        )
        return section

    @_notice_when_older
    @_traced("handoff")
    def handoff(self, text: str, signed_as: str = "") -> str:
        """Save the agent's exact private note for the next session, signed
        with ``signed_as`` when the agent gives its model id."""

        if not text.strip():
            return "Nothing saved: handoff text was empty."
        self._ensure_init()
        assert self._store is not None
        author = self.author_model(signed_as)
        self._traced_author(author)
        session = harness_session()
        handoff_id = self._store.write_handoff(
            text,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            author_id=self.scope.agent_id,
            author_model=author,
            author_session=session,
            # Code older than the store replaces only this session's own note.
            # Retiring other sessions' notes by count is a rule; they stay
            # active until current code retires them.
            retire_crowded=self._older_than_store() is None,
        )
        self._traced_write(handoff_id)
        if session:
            lasts = (
                "It replaces only the handoff this session left before; notes "
                "other sessions left stay beside it. It remains active until "
                "this session replaces it or you forget it."
            )
        else:
            lasts = (
                "It will be delivered first in the next session and will remain "
                "active until you replace or forget it."
            )
        return (
            "Session handoff saved exactly as written.\n"
            f"Handoff ID: {handoff_id}\n"
            f"{self._signed_line(author)}\n"
            f"{lasts}"
        )

    def _recall_by_id(self, note_id: str) -> str:
        """A note the packet showed, read whole by its id, or ``""``.

        The packet cuts long notes and gives each cut one its id. A handoff
        comes back framed as the packet framed it, active or not. A continuity
        note comes back only while it is live: one that was forgotten stays
        forgotten. So does a durable memory that was forgotten or replaced by
        a correction. A durable memory read this way is reinforced as a recall
        would reinforce it: a dormant one wakes, and one that faded into the
        archive is brought back first (``resharpen``). Its id is the one way
        to reach a faded memory without asking recall to search the archive.

        A note and its memory are one capture, and a note shares its memory's
        fate, so the note's id reaches the memory too: a note whose memory has
        gone quiet or faded brings it back exactly as the memory's id would.
        The id of a note or memory a correction replaced says so and names
        the version now in use, and never shows the words it replaced.
        """
        assert self._store is not None
        scope = {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }
        if _ENTRY_ID.fullmatch(note_id):
            note = self._store.get_hypomnema_entry(note_id, **scope)
            if not note:
                return ""
            if note.get("entry_kind") == "handoff":
                label, whose = whose_handoff(note, self.author_model(), harness_session())
                lines = [f"{label}:", note["content"]]
                if whose != "own":
                    lines.append(COLLEAGUE_LINE)
                if not note.get("active"):
                    lines.append(
                        "This note is no longer active: a newer one replaced it or it was forgotten."
                    )
                return "\n".join(lines)
            if not note.get("active"):
                return self._replaced_by(note_id, "Note")
            brought = self._bring_back_memory_of(note)
            if brought is None:
                return ""
            lines = [
                f"Note {note_id}, {note_signature(note)}, {_age_text(note['created_at'])}:",
                note["content"],
            ]
            return "\n".join(lines + ([brought] if brought else []))
        if _ENGRAM_ID.fullmatch(note_id):
            engram = self._store.get_engram_in_scope(note_id, **scope)
            if engram is None:
                return ""
            faded = engram.state == "archived"
            if faded:
                # Only decay's door opens back. What the agent forgot, or
                # replaced with a correction, it closed on purpose; a
                # replaced one names what replaced it.
                if self._store.archive_reason(note_id) not in FADED_ARCHIVE_REASONS:
                    return self._replaced_by(note_id, "Memory")
                engram = self._restore_faded(engram)
            quiet = engram.state == "dormant"
            kind = "Lesson" if {"lesson", "distilled"} & set(engram.tags or []) else "Memory"
            lines = [f"{kind} {note_id}, {_age_text(engram.created_at)}:", engram.content]
            if engram.impact and engram.impact != engram.content:
                lines.append(f"What it changed: {engram.impact}")
            if faded:
                lines.append(
                    "It had faded into the archive; recalling it by its id brought it back."
                    if engram.state == "active"
                    else "It has faded into the archive, and this session's code leaves it there."
                )
            elif quiet:
                lines.append("It had gone quiet.")
            # A correction remembers what it replaced, by id: the words stay
            # with the memory it replaced, out of recall.
            if engram.lineage.supersedes:
                lines.append(
                    f"It replaced {_id_list('memory', engram.lineage.supersedes)} "
                    "through a correction."
                )
            # Asking for a memory by its id is a use, as a query that returns
            # it is: reinforced the same way, once a session and never by
            # code older than the store, and a dormant one wakes as it would
            # from a query. A note has no reinforcement.
            if engram.state in ("active", "dormant"):
                self._reinforce_returned(
                    note_id, [RetrievalResult(engram=engram, score=1.0, retrieval_path="id")],
                )
            return "\n".join(lines)
        return ""

    def _bring_back_memory_of(self, note: dict[str, Any]) -> str | None:
        """Read a note's memory as its id would be read, since the note shares
        its fate: one that went quiet wakes, and one that faded into the
        archive is restored, by the rules recall by the memory's id follows
        (never by code older than the store). Returns the line saying what
        happened, ``""`` when the memory is in use or the note has none, and
        None when it was forgotten or replaced, which leaves the note out of
        use with it."""
        assert self._store is not None
        scope = self._scope_args()
        engram = None
        for engram_id in self._store.note_memory_ids(note):
            engram = self._store.get_engram_in_scope(engram_id, **scope)
            if engram is not None:
                break
        if engram is None or engram.state not in ("dormant", "archived"):
            return ""
        faded = engram.state == "archived"
        if faded:
            if self._store.archive_reason(engram.id) not in FADED_ARCHIVE_REASONS:
                return None
            engram = self._restore_faded(engram)
        if engram.state in ("active", "dormant"):
            self._reinforce_returned(
                note["id"], [RetrievalResult(engram=engram, score=1.0, retrieval_path="id")],
            )
        back = self._store.engram_state_in_scope(engram.id, **scope) == "active"
        older = self._older_than_store() is not None
        if faded:
            if back:
                return "Its memory had faded into the archive; recalling the note brought it back."
            return "Its memory has faded into the archive, and this session's code leaves it there."
        if back:
            return "Its memory had gone quiet; recalling the note woke it."
        if older:
            return "Its memory has gone quiet, and this session's code leaves it there."
        return "Its memory has gone quiet."

    def _replaced_by(self, pair_id: str, label: str) -> str:
        """What reading a replaced note or memory by its id says: that a
        correction replaced it, and the id of its version now in use. Never
        its words: what a correction replaced stays out of recall, kept as a
        trace. ``""`` for one that was forgotten, and for one whose line of
        corrections ends in something forgotten."""
        assert self._store is not None
        current = self._latest(pair_id)
        if current == pair_id:
            return ""
        note, engram = self._store.capture_pair(current, **self._scope_args())
        if label == "Note":
            live = note is not None and bool(note.get("active"))
        else:
            live = engram is not None and engram.state != "archived"
        if not live:
            return ""
        return (
            f"{label} {pair_id} was replaced by a correction. Its current version "
            f"is {label.lower()} {current}; recall that id to read it."
        )

    def _restore_faded(self, engram: Engram) -> Engram:
        """Bring a memory that faded into the archive back (``resharpen``), and
        return it as it now stands. Code older than the store leaves it where
        it is: restoring applies this version's rules to what it restores."""
        assert self._store is not None
        if self._older_than_store() is not None:
            return engram
        restored = resharpen(self._store, engram.id)
        if restored is None:
            return engram
        self._traced_write(restored.id)
        return restored

    def _faded_matches(self, query: str, max_results: int) -> list[Engram]:
        """The memories that faded into the archive and that ``query`` names,
        the closest first, then the newest: at least half of its meaningful
        words, and two when it has two or more (the bar a correction's query
        must clear). Every faded memory in the scope is weighed, page by page,
        and only the closest are kept in hand. A memory forgotten or replaced
        by a correction is never among them."""
        assert self._store is not None
        if not _named_terms(query) - _CORRECTION_VERBS:
            return []

        def named() -> Any:
            position = 0
            before: str | None = None
            while True:
                page = self._store.faded_engrams(
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    before_id=before,
                )
                if not page:
                    return
                for engram in page:
                    shared = _named_by(query, " ".join([
                        engram.content or "", engram.content_at_encoding or "", engram.impact or "",
                    ]))
                    if shared:
                        yield shared, position, engram
                    position += 1
                before = page[-1].id

        closest = heapq.nsmallest(max(1, max_results), named(), key=lambda m: (-m[0], m[1]))
        return [engram for _shared, _position, engram in closest]

    def identity_graph(self, max_nodes: int = 18) -> dict[str, Any]:
        """Build a portable identity graph snapshot for visual-capable clients."""

        self._ensure_init()
        assert self._store is not None

        max_nodes = min(max(int(max_nodes or 18), 4), 48)
        stats = self._stats()
        continuity = self._store.search_hypomnema(
            "",
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_nodes,
        )
        continuity = [
            entry for entry in continuity
            if entry.get("entry_kind") == "continuity"
        ]
        # Who the agent is, drawn from what it wrote.
        engrams = self._store.get_active_engrams(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_nodes,
            load_connections=False,
            author_kind="agent",
        )

        domain_counts: dict[str, int] = {}
        for entry in continuity:
            domain = entry.get("domain") or "topical"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        nodes: list[dict[str, Any]] = [
            {
                "id": f"agent:{self.scope.agent_id}",
                "label": self.scope.agent_id,
                "kind": "agent",
                "weight": 1.0,
            }
        ]
        edges: list[dict[str, Any]] = []
        for domain, count in sorted(domain_counts.items(), key=lambda item: (-item[1], item[0])):
            domain_id = f"domain:{domain}"
            nodes.append({
                "id": domain_id,
                "label": domain,
                "kind": "domain",
                "weight": count,
            })
            edges.append({
                "source": f"agent:{self.scope.agent_id}",
                "target": domain_id,
                "relation": "contains",
                "strength": min(1.0, 0.35 + count * 0.12),
            })

        for entry in continuity[:max_nodes]:
            domain = entry.get("domain") or "topical"
            node_id = f"continuity:{entry['id']}"
            nodes.append({
                "id": node_id,
                "label": short_label(entry.get("content", ""), 44),
                "kind": "continuity",
                "domain": domain,
                "confidence": round(float(entry.get("confidence", 0.0)), 3),
                "salience": round(float(entry.get("salience", 0.0)), 3),
                "created_at": entry.get("created_at"),
            })
            edges.append({
                "source": f"domain:{domain}",
                "target": node_id,
                "relation": "anchors",
                "strength": round(float(entry.get("salience", 0.5)), 3),
            })

        for engram in engrams[: max(3, max_nodes // 2)]:
            node_id = f"memory:{engram.id}"
            nodes.append({
                "id": node_id,
                "label": short_label(engram.impact or engram.content, 38),
                "kind": "memory",
                "confidence": round(float(engram.source.confidence), 3),
                "strength": round(float(engram.strength), 3),
                "stability": round(float(engram.stability), 3),
                "accessibility": round(float(engram.accessibility), 3),
                "source_type": engram.source.type,
                "created_at": engram.created_at,
            })
            edges.append({
                "source": f"agent:{self.scope.agent_id}",
                "target": node_id,
                "relation": "encodes",
                "strength": round(float(engram.accessibility), 3),
            })

        timeline = build_timeline(continuity, engrams)
        summary = (
            f"{stats.get('engrams_active', 0)} active memories, "
            f"{stats.get('hypomnema_active', 0)} continuity notes, "
            f"{stats.get('connections', 0)} connections"
        )
        snapshot = {
            "version": 1,
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "summary": summary,
            "stats": {
                "active_memories": stats.get("engrams_active", 0),
                "continuity_notes": stats.get("hypomnema_active", 0),
                "connections": stats.get("connections", 0),
                "archived": stats.get("archived", 0),
            },
            "nodes": nodes,
            "edges": edges,
            "timeline": timeline,
        }
        snapshot["svg"] = render_identity_svg(snapshot)
        return snapshot

    @_notice_when_older
    @_traced("capture")
    def capture(
        self,
        content: str,
        context: str = "",
        importance: str | float = "auto",
        impact: str = "",
        impact_source: str = "agent",
        signed_as: str = "",
    ) -> str:
        """Capture durable continuity without exposing Mnemos internals.

        ``impact_source`` records who wrote the impact. It defaults to 'agent'
        because the product caller is `mnemos_capture`, which the agent invokes
        mid-conversation with an impact in its own words. Server-internal
        captures that pass a boilerplate impact set this to 'template' so the
        two never blur — the whole point of the field is that an agent-authored
        trace can be told from a generated one after the fact.

        The memory and its note are the agent's words (``author_kind``
        'agent'), signed with ``signed_as`` when the agent gives its model id,
        and otherwise as ``author_model`` resolves it, and marked with the
        harness session.

        They are one object: saved in one transaction, the note pointing at
        the memory, so either id reaches both (``EngramStore.capture_pair``)
        and a correction or a forget acts on both."""

        if not content.strip():
            return "Nothing captured: content was empty."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        # Run the onboarding guard before this capture writes anything so an
        # existing store is grandfathered on its prior contents, never on the
        # capture currently being made.
        self._onboarding_status()
        self._current_session()

        full_content = content.strip()
        if context.strip():
            full_content = f"{full_content}\n\nContext: {context.strip()}"

        domain = _classify_domain(full_content)
        kind = _classify_kind(full_content)
        tags = _simple_tags(content, context)
        author = self.author_model(signed_as)
        session = harness_session()
        self._traced_author(author)
        confidence, salience = _importance_scores(importance, domain)
        # Shift 1: a trace is what the memory changed, and only the agent can
        # say that. When it does not, the field stays empty rather than being
        # filled with a phrase the server chose — a template reads as complete
        # while carrying nothing, which is how a store ends up 76% records.
        # Empty is honest, and the reflection queue asks about it later.
        impact = (impact or "").strip()
        # Code older than the store saves the capture in its own shape
        # (classification, index, vector) and applies no rules that reach
        # other memories: no weighing against beliefs, no links.
        older = self._older_than_store() is not None

        # Finding links and weighing surprise read the store and may call a
        # model, so they happen before the pair's transaction opens.
        engram = self._encoder.prepare(
            content=full_content,
            impact=impact,
            impact_source=impact_source if impact else "",
            kind=kind,
            tags=tags,
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            override_confidence=confidence,
            # Shift 3: the moment something does not fit what is already held
            # is the moment worth encoding deeply. This is now reachable
            # without beliefs or a model, so it no longer has to be skipped.
            # Code older than the store skips it all the same: this step also
            # weighs the capture as evidence for or against beliefs, by rules
            # newer code may have replaced, and older code must never move a
            # belief. The capture itself still lands.
            skip_surprise_detection=older,
            # Links to other memories follow rules newer code may have
            # replaced; maintenance's connection discovery makes them later.
            discover_connections=not older,
            author_kind="agent",
            author_model=author,
            author_session=session,
        )
        # One capture, one object: the memory and its note land together or
        # not at all, the note pointing at the memory.
        note_id = self._store.save_capture_pair(
            engram,
            content.strip(),
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            entry_kind="continuity",
            authored_by="agent",
            author_id=self.scope.agent_id,
            author_model=author,
            author_session=session,
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=domain in {"foundational", "identity"},
        )
        # The vector, once the pair has committed (see Encoder.finish).
        self._encoder.finish(engram)
        self._traced_write(engram.id, note_id)
        self._record_first_capture(note_id, engram.id, content)
        # Continuity just arrived, so any run of empty packets is over.
        self._set_meta("last_capture_session", str(self._current_session()))
        self._set_meta("empty_context_streak", "0")
        maintenance = self.maintain(auto=True)

        return (
            "Captured continuity.\n"
            f"Memory ID: {engram.id}\n"
            f"Continuity note ID: {note_id}\n"
            f"Scope: {self.scope.agent_id}/{self.scope.person_id}/{self.scope.project_scope}\n"
            f"{self._signed_line(author)}\n"
            "Maintenance:\n"
            f"{_indent(maintenance)}"
        )

    @_notice_when_older
    @_traced("recall")
    def recall(self, query: str, max_results: int = 5, include_archived: bool = False) -> str:
        """Recall relevant continuity and durable memories.

        A dormant memory comes back when the query matches it well, and
        wakes. With ``include_archived``, memories that faded into the
        archive and that the query names come back too, restored; one the
        agent forgot, or replaced with a correction, never does. A faded
        memory's id reaches it without the flag.
        """

        if not query.strip():
            return "Recall needs a query."

        self._ensure_init()
        assert self._store is not None

        # The packet cuts long notes and shows each one's id. Recalling that
        # id returns the note whole.
        whole = self._recall_by_id(query.strip())
        if whole:
            self._traced_read(query.strip())
            return whole

        continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results,
            exclude_kinds=("handoff",),
        )
        continuity = _filter_continuity(query, continuity)
        memories = self._retrieve(query, max_results=max_results)
        faded = self._faded_matches(query, max_results) if include_archived else []

        if not continuity and not memories and not faded:
            if include_archived:
                return "No relevant continuity found, in memory or in the archive."
            return "No relevant continuity found."

        self._traced_read(
            *(entry["id"] for entry in continuity),
            *(result.engram.id for result in memories),
        )
        lines = [f"Mnemos recall for: {query.strip()}"]
        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        if memories:
            lines.extend(["", "Durable memories:"])
            lines.extend(_format_memory(result) for result in memories)
        returned = list(memories)
        if faded:
            restored = [self._restore_faded(engram) for engram in faded]
            self._traced_read(*(engram.id for engram in restored))
            one = len(restored) == 1
            lines.extend(["", "From the archive:"])
            lines.extend(_format_archived(engram) for engram in restored)
            if all(engram.state == "active" for engram in restored):
                lines.append(
                    "It had faded out of ordinary recall; recalling it brought it back."
                    if one
                    else "These had faded out of ordinary recall; recalling them brought them back."
                )
            else:
                lines.append(
                    f"{'It has' if one else 'These have'} faded out of ordinary recall, "
                    f"and this session's code leaves {'it' if one else 'them'} in the archive."
                )
            returned += [
                RetrievalResult(engram=engram, score=0.0, retrieval_path="archive")
                for engram in restored
                if engram.state == "active"
            ]
        # Everything shown is one return, reinforced together: what came back
        # from the archive is linked with what recall found beside it.
        self._reinforce_returned(query, returned)
        return "\n".join(lines)

    # ── Correcting and forgetting ──
    #
    # A capture is one object: its continuity note and its memory (see
    # EngramStore.save_capture_pair). Every path that corrects or forgets
    # reaches both from whichever one it names, and acts on both, in one
    # transaction, so neither is left saying what the other no longer does.

    def _scope_args(self) -> dict[str, str]:
        return {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }

    def _latest(self, pair_id: str) -> str:
        """The id a line of corrections has reached from ``pair_id``: the
        note or memory now standing where it stood, or ``pair_id`` itself when
        no correction replaced it."""
        assert self._store is not None
        scope = self._scope_args()
        current, seen = pair_id, {pair_id}
        while True:
            later = self._store.successor(current, **scope)
            if not later or later in seen:
                return current
            seen.add(later)
            current = later

    def _current_pair(
        self, pair_id: str,
    ) -> tuple[str | None, dict[str, Any] | None, Engram | None, str]:
        """What a correction's id names: ``(kind, note, memory, id acted on)``.

        ``kind`` is 'note' or 'memory' for the half the id names, 'other' for
        a handoff or a report (never half of a pair, never followed), and
        None when nothing in this scope has the id. A note or memory a
        correction already replaced is followed to its current version, so
        an old id reaches the pair in use now and never leaves a second
        replacement live beside the first.
        """
        assert self._store is not None
        scope = self._scope_args()
        note, engram = self._store.capture_pair(pair_id, **scope)
        if note is None and engram is None:
            return None, None, None, pair_id
        if note is not None and note.get("entry_kind") != "continuity":
            return "other", note, None, pair_id
        kind = "note" if note is not None and note["id"] == pair_id else "memory"
        current = self._latest(pair_id)
        if current != pair_id:
            note, engram = self._store.capture_pair(current, **scope)
        return kind, note, engram, current

    def _pair_members(
        self, note: dict[str, Any] | None, engram: Engram | None,
    ) -> tuple[list[dict[str, Any]], list[Engram]]:
        """Everything holding one pair's words: its note and every active
        note pointing at its memory, its memory and every memory those notes
        point at. A correction or a forget takes them out of use together, so
        none stays live beside the rest (an older correction could leave a
        note pointing at two)."""
        assert self._store is not None
        scope = self._scope_args()
        notes: dict[str, dict[str, Any]] = {}
        memories: dict[str, Engram] = {}
        if note is not None:
            notes[note["id"]] = note
        if engram is not None:
            memories[engram.id] = engram
            for other in self._store.notes_for_engram(engram.id, **scope):
                notes.setdefault(other["id"], other)
        for held in list(notes.values()):
            for engram_id in self._store.note_memory_ids(held):
                if engram_id not in memories:
                    found = self._store.get_engram_in_scope(engram_id, **scope)
                    if found is not None:
                        memories[engram_id] = found
        return list(notes.values()), list(memories.values())

    def _still_held(self, engram: Engram) -> bool:
        """Whether a memory is still there to replace or forget: in use, or
        only faded into the archive. One forgotten or already replaced was
        closed on purpose, and nothing reopens it: its words are never
        carried or copied again."""
        assert self._store is not None
        return (
            engram.state != "archived"
            or self._store.archive_reason(engram.id) in FADED_ARCHIVE_REASONS
        )

    def _retire(
        self,
        notes: list[dict[str, Any]],
        memories: list[Engram],
        *,
        action: str,
        corrector: str,
        session: str,
        query: str = "",
    ) -> tuple[list[str], list[str]]:
        """Take a pair out of use, in the transaction under way: its active
        notes and the memories it still holds. Both are kept, archived, never
        deleted; each note's revision records who retired it. Returns the
        ids of the notes and the memories retired."""
        assert self._store is not None
        reason = f"simple correction action={action}" + (f"; query={query}" if query else "")
        retired_notes = []
        for held in notes:
            if held.get("active"):
                self._store.archive_hypomnema_entry(
                    held["id"],
                    reason=reason,
                    revised_by=corrector,
                    author_session=session,
                    **self._scope_args(),
                )
                retired_notes.append(held["id"])
        retired_memories = [
            engram.id for engram in memories
            if self._store.retire_engram(engram, reason=f"simple_correction_{action}")
        ]
        self._traced_write(*retired_notes, *retired_memories)
        return retired_notes, retired_memories

    def _forget_pair(
        self,
        note: dict[str, Any] | None,
        engram: Engram | None,
        *,
        action: str,
        corrector: str,
        session: str,
        query: str = "",
    ) -> tuple[list[str], list[str]]:
        """Forget acts on the pair: the note and its memory are archived
        together, in one transaction, and nothing is written in their place."""
        assert self._store is not None
        notes, memories = self._pair_members(note, engram)
        with self._store.transaction():
            return self._retire(
                notes, memories, action=action, corrector=corrector, session=session, query=query,
            )

    def _note_head(self, note: dict[str, Any] | None) -> str:
        """A note's own words, without a reflection added to them."""
        if note is None:
            return ""
        content = note.get("content") or ""
        return content.split(f"\n\n{self._REFLECTION_MARKER}")[0].strip()

    def _replacement_note(
        self, note: dict[str, Any] | None, correction: str, corrector: str, session: str,
    ) -> dict[str, Any]:
        """How a correction's note is written: the agent's words, signed, in
        the place the note it replaces held (its domain, tags, and standing
        as foundational), or classified afresh when it replaces no note."""
        domain = (note or {}).get("domain") or _classify_domain(correction)
        return {
            **self._scope_args(),
            "source": "observed",
            "entry_kind": "continuity",
            "authored_by": "agent",
            "author_id": self.scope.agent_id,
            "author_model": corrector,
            "author_session": session,
            "domain": domain,
            "tags": list((note or {}).get("tags") or _simple_tags(correction)),
            "confidence": 0.92,
            "salience": max(0.75, float((note or {}).get("salience") or 0.0)),
            "foundational": (
                bool(note.get("foundational")) if note is not None
                else domain in {"foundational", "identity"}
            ),
            "related_session_id": (note or {}).get("related_session_id"),
        }

    def _replace_pair(
        self,
        note: dict[str, Any] | None,
        engram: Engram | None,
        correction: str,
        impact: str,
        *,
        action: str,
        placeholder: str,
        corrector: str,
        session: str,
        older: bool,
        query: str = "",
    ) -> dict[str, Any]:
        """Retire a pair and write the pair that replaces it, in the agent's
        words, in one transaction.

        Current code also records what was replaced (``record_correction``):
        a ``supersedes`` link and lineage both ways, the old note's successor,
        and a version keeping the old words, signed by whoever corrected,
        written only when the words changed. An impact the agent gives
        becomes a lesson about the mistake. Code older than the store does
        none of that: it records the agent's words, retires what they name,
        and changes nothing else.
        """
        assert self._store is not None
        assert self._encoder is not None
        notes, memories = self._pair_members(note, engram)
        held = [memory for memory in memories if self._still_held(memory)]
        live_notes = [entry for entry in notes if entry.get("active")]
        # What it meant, carried unless the agent says otherwise: this pair's
        # memory first, then any other it still held.
        meanings = sorted(held, key=lambda memory: engram is None or memory.id != engram.id)
        meaning, meaning_source, kept = _replacement_impact(impact, placeholder, *meanings)

        text = correction.strip()
        replacement = self._encoder.prepare(
            content=text,
            impact=meaning,
            impact_source=meaning_source,
            kind=_classify_kind(correction),
            tags=sorted({"continuity", "correction", *_simple_tags(correction)}),
            source=SourceType.SESSION,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            override_confidence=0.92,
            skip_surprise_detection=True,
            discover_connections=not older,
            author_kind="agent",
            author_model=corrector,
            author_session=session,
        )
        # Found before it is archived, what this replaces could be linked to
        # its replacement as merely related. The supersedes link says what
        # joins them.
        replaced = {memory.id for memory in memories}
        replacement.connections = [
            link for link in replacement.connections if link.target_id not in replaced
        ]

        words_before: str | None = None
        resolution_before = 1.0
        if not older:
            # Set on the memory itself, so a later save of it keeps it.
            replacement.lineage.supersedes = [memory.id for memory in held]
            # The words it replaces, taken only from what is still held: a
            # memory already forgotten or replaced is never copied again.
            said = {self._note_head(note), (engram.content or "").strip() if engram else ""}
            if text not in said:
                if engram is not None and engram.id in {memory.id for memory in held}:
                    words_before, resolution_before = engram.content, engram.resolution
                elif note is not None and note["id"] in {entry["id"] for entry in live_notes}:
                    words_before = self._note_head(note)

        lesson_id: str | None = None
        with self._store.transaction():
            retired_notes, retired_memories = self._retire(
                notes, memories, action=action, corrector=corrector, session=session, query=query,
            )
            note_id = self._store.save_capture_pair(
                replacement, text, **self._replacement_note(note, correction, corrector, session),
            )
            if not older:
                self._store.record_correction(
                    note_id=note_id,
                    engram_id=replacement.id,
                    replaced_notes=retired_notes,
                    replaced_engrams=retired_memories,
                    words_before=words_before,
                    resolution_before=resolution_before,
                    author_model=corrector,
                    author_session=session,
                )
                if (impact or "").strip():
                    # A lesson about the mistake, in the agent's own words
                    # (only those make a lesson), drawn from the correction,
                    # which its supersedes link joins to the memory it fixed.
                    from .consolidation.softening import _create_or_reinforce_lesson

                    lesson_id = _create_or_reinforce_lesson(
                        replacement, self._store, {},
                        author_model=corrector, author_session=session,
                    )
        # The vector, once everything above has committed (Encoder.finish).
        self._encoder.finish(replacement)
        self._traced_write(replacement.id, note_id, lesson_id)

        said_lines = []
        if kept:
            said_lines.append(kept)
        if lesson_id:
            said_lines.append(f"What it means now became a lesson about the mistake: {lesson_id}")
        elif (impact or "").strip() and older:
            said_lines.append("Filing it as a lesson waits for current Mnemos.")
        if not retired_notes and not retired_memories:
            said_lines.append("Nothing in use still held the old words, so nothing was archived.")
        return {"note_id": note_id, "engram_id": replacement.id, "lines": said_lines}

    def _correct_other_note(
        self,
        note: dict[str, Any],
        correction: str,
        action: str,
        impact: str,
        *,
        corrector: str,
        session: str,
        query: str = "",
    ) -> str:
        """A handoff or a report a correction names: changed in place, as
        before. Neither is half of a capture's pair."""
        assert self._store is not None
        closest = "closest " if query else ""
        if action in _FORGET_ACTIONS:
            if not note.get("active"):
                return f"Nothing was archived: note {note['id']} is no longer active."
            self._store.archive_hypomnema_entry(
                note["id"],
                reason=f"simple correction action={action}" + (f"; query={query}" if query else ""),
                revised_by=corrector,
                author_session=session,
                **self._scope_args(),
            )
            self._traced_write(note["id"])
            return f"Archived {closest}continuity note {note['id']}."
        self._store.revise_hypomnema_entry(
            note["id"],
            correction,
            reason="simple correction" + (f" query={query}" if query else ""),
            **self._scope_args(),
            confidence=0.92,
            salience=0.75,
            author_model=corrector,
            revised_by=corrector,
            author_session=session,
        )
        self._traced_write(note["id"])
        if (impact or "").strip():
            return (
                f"Updated {closest}continuity note {note['id']}.\n"
                "The impact was not saved: this note does not hold one."
            )
        return f"Updated {closest}continuity note {note['id']}."

    def _correct_belief(
        self,
        belief_id: str,
        correction: str,
        action: str,
        impact: str,
        *,
        signed_as: str,
        older: bool,
    ) -> str:
        """Correct a belief the correction names by its id: the one way a
        correction changes a belief. A word shared with a belief never does.

        Forget retires it; a correction replaces it with the agent's words,
        formed as a belief the agent states is formed (at 40%), the old one
        retired and pointing at it. Only a belief the agent stated can be
        changed this way. Code older than the store changes no belief: the
        words are captured as continuity, and the belief is left as it is.
        """
        assert self._store is not None
        from .core.belief import Belief

        belief = self._store.get_belief(belief_id)
        if belief is None or belief.agent_id != self.scope.agent_id:
            return f"Nothing was changed: no belief {belief_id} is held here."
        shown = " ".join((belief.content or "").split())[:80]
        if belief.superseded_by:
            return f'Nothing was changed: the belief "{shown}" was already retired.'
        if belief.source != "agent":
            return (
                f'Nothing was changed: the belief "{shown}" is not one you '
                "stated, so a correction cannot retire or rewrite it."
            )
        text = correction.strip()
        left = f'The belief "{shown}" is left as it is: this session\'s code changes no belief.'
        if older:
            if not text:
                return left
            captured = self.capture(
                text,
                context=f"Correction of the belief {belief_id}.",
                importance="high",
                impact=impact,
                signed_as=signed_as,
            )
            return f"{left} Your words were captured as continuity.\n{captured}"
        if action in _FORGET_ACTIONS:
            self._store.supersede_belief(
                belief.id,
                reason="retired by the agent's correction" + (f": {text}" if text else ""),
            )
            self._traced_write(belief.id)
            return (
                f'Retired the belief "{shown}". It no longer shapes your '
                "context; it is kept, with its history."
            )
        replacement = Belief(
            agent_id=self.scope.agent_id,
            content=text,
            confidence=0.4,
            domain=belief.domain,
            supporting_engram_ids=list(belief.supporting_engram_ids),
            source="agent",
        )
        belief.revise(0.0, f"corrected by the agent: {text}")
        belief.superseded_by = replacement.id
        with self._store.transaction():
            self._store.save_belief(replacement)
            self._store.save_belief(belief)
        self._traced_write(belief.id, replacement.id)
        lines = [
            f'Replaced the belief "{shown}" with your words: "{text}".',
            f"Belief ID: {replacement.id}. It starts at 40%, as a belief formed "
            "from your words does; the old one is kept, retired, with its history.",
        ]
        if (impact or "").strip():
            lines.append("The impact was not saved: a belief does not hold one.")
        return "\n".join(lines)

    @_notice_when_older
    @_traced("correct")
    def correct(
        self,
        correction: str,
        target_id: str = "",
        query: str = "",
        action: str = "update",
        impact: str = "",
        signed_as: str = "",
    ) -> str:
        """Correct, supersede, or archive stale memory.

        What a capture wrote is one object, its continuity note and its
        memory, and every path reaches both from whichever one it names: the
        note's id, the memory's id, or a query whose words name the note (or,
        to forget, the memory). A correction retires the pair and writes the
        replacement pair in the agent's words; a forget retires the pair and
        writes nothing. Either happens in one transaction. The old pair is
        kept, archived, never deleted. An id a correction already replaced
        reaches its current version, so correcting or forgetting twice never
        leaves two live. A belief changes only when named by its id
        (``belief_...``).

        ``impact`` is what the corrected memory means now, in the agent's own
        words, and it becomes a lesson about the mistake. Left empty, the
        replacement keeps what the memory it replaces meant, and the result
        says so.

        Current code records what a correction replaced: a ``supersedes`` link
        and lineage both ways, the old note's successor, and a version keeping
        the old words, written only when the words changed. Code older than
        the store records the agent's words and retires what they name, and
        changes nothing else.

        The correction is the agent's words: the replacement pair and its
        version are signed with ``signed_as`` when the agent gives its model
        id, and marked with the harness session, as is each retired note's
        revision.
        """

        action = (action or "").strip().lower() or "update"
        forget = action in _FORGET_ACTIONS
        if not correction.strip() and not forget:
            return "Correction needs replacement text or a forget/archive action."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None
        corrector = self.author_model(signed_as)
        session = harness_session()
        self._traced_author(corrector)
        signing = {"corrector": corrector, "session": session}
        target = target_id.strip()
        # Code older than the store records the agent's words and applies no
        # rules: a correction still retires what it names and writes its
        # replacement, without links to other memories, lineage, versions,
        # lessons, or a placeholder where its meaning would go.
        older = self._older_than_store() is not None

        if target.startswith("belief_"):
            return self._correct_belief(
                target, correction, action, impact, signed_as=signed_as, older=older,
            )

        if target:
            kind, note, engram, current = self._current_pair(target)
            if kind == "other":
                return self._correct_other_note(note, correction, action, impact, **signing)
            if kind is not None:
                label = "Note" if kind == "note" else "Memory"
                followed = (
                    f"{label} {target} had already been replaced by a correction; "
                    "this acted on its current version."
                    if current != target else ""
                )
                if forget:
                    retired_notes, retired_memories = self._forget_pair(
                        note, engram, action=action, **signing,
                    )
                    if not retired_notes and not retired_memories:
                        lines = [f"Nothing was archived: {label.lower()} {current} was already archived."]
                    elif kind == "note":
                        lines = [f"Archived continuity note {current}."]
                        if retired_memories:
                            lines.append(f"Its memory {', '.join(retired_memories)} was archived with it.")
                    else:
                        lines = [f"Archived memory {current}."]
                        if retired_notes:
                            lines.append(f"Its continuity note {', '.join(retired_notes)} was archived with it.")
                    return "\n".join(lines + ([followed] if followed else []))

                by_note = kind == "note"
                result = self._replace_pair(
                    note, engram, correction, impact,
                    action=action,
                    # A correction by note id wrote no memory before, so it
                    # never gets the server's placeholder where its meaning
                    # would go: the agent's words, carried, or nothing.
                    placeholder="" if older or by_note else "Correction to earlier continuity.",
                    older=older,
                    **signing,
                )
                if by_note:
                    lines = [
                        f"Updated continuity note {current}.",
                        f"Continuity note ID: {result['note_id']}",
                        f"Memory ID: {result['engram_id']}",
                    ]
                else:
                    lines = [
                        f"Archived memory {current} and captured correction {result['engram_id']}.",
                        f"Correction: {correction.strip()}",
                        f"Continuity note ID: {result['note_id']}",
                    ]
                if followed:
                    lines.insert(1, followed)
                return "\n".join(lines + result["lines"])

        search_text = query.strip() or correction.strip()
        query_text = query.strip()
        if query_text:
            # A handoff is replaced by writing a new one. A correction found
            # by searching must never land on one: it would overwrite the
            # agent's exact note with the correction text, or forget it.
            # And it lands only on a note the query names. The closest note is
            # not close when nothing is: "forget the zeppelin schedule"
            # archived a note about a reading.
            matches = _named_matches(query_text, self._store.search_hypomnema(
                query_text,
                **self._scope_args(),
                limit=10,
                exclude_kinds=("handoff",),
            ), lambda note: note.get("content") or "")
            if matches:
                match = matches[0]
                note, engram = self._store.capture_pair(match["id"], **self._scope_args())
                if note is not None and note.get("entry_kind") != "continuity":
                    said = self._correct_other_note(
                        note, correction, action, impact, query=query_text, **signing,
                    )
                    maintenance = self.maintain(auto=True)
                    return f"{said}\nMaintenance:\n{_indent(maintenance)}"
                if forget:
                    _retired_notes, retired_memories = self._forget_pair(
                        note, engram, action=action, query=query_text, **signing,
                    )
                    maintenance = self.maintain(auto=True)
                    lines = [f"Archived closest continuity note {match['id']}."]
                    if retired_memories:
                        lines.append(f"Its memory {', '.join(retired_memories)} was archived with it.")
                    return "\n".join([*lines, "Maintenance:", _indent(maintenance)])

                result = self._replace_pair(
                    note, engram, correction, impact,
                    action=action,
                    placeholder="" if older else "Corrected continuity for future interactions.",
                    older=older,
                    query=query_text,
                    **signing,
                )
                maintenance = self.maintain(auto=True)
                return "\n".join([
                    f"Updated closest continuity note {match['id']}.",
                    f"Memory ID: {result['engram_id']}",
                    f"Continuity note ID: {result['note_id']}",
                    *result["lines"],
                    "Maintenance:",
                    _indent(maintenance),
                ])

        if forget:
            # Finding a memory in order to forget it is not returning it to
            # anyone, so nothing here is reinforced.
            matches = _named_matches(
                search_text,
                self._retrieve(search_text, max_results=1) if search_text else [],
                lambda r: f"{r.engram.content or ''} {r.engram.impact or ''}",
            )
            if matches:
                engram = matches[0].engram
                retired_notes, _retired_memories = self._forget_pair(
                    None, engram, action=action, **signing,
                )
                lines = [f"Archived closest matching memory {engram.id}."]
                if retired_notes:
                    lines.append(f"Its continuity note {', '.join(retired_notes)} was archived with it.")
                return "\n".join(lines)
            # Forgetting acts only on what the words name, and never captures.
            if not search_text:
                return "Nothing was archived: give the ID as target_id, or a query that names it."
            return (
                f'Nothing was archived: no note or memory matched "{search_text}" '
                "closely enough. To forget one, give its ID as target_id."
            )

        captured = self.capture(
            correction.strip(),
            context=f"Correction supplied through mnemos_correct. Prior query: {query.strip()}",
            importance="high",
            impact=impact,
            signed_as=signed_as,
        )
        if query_text:
            return (
                f'No continuity note matched "{query_text}" closely enough, so none was '
                f"changed; the correction was captured as new continuity.\n{captured}"
            )
        return captured

    @_traced("maintain")
    def maintain(self, deep: bool = False, auto: bool = False) -> str:
        """Run the best available maintenance without requiring setup."""

        self._ensure_init()
        assert self._store is not None

        requested_deep = bool(deep)
        # Code older than the store runs none of the passes (decay, linking,
        # softening, lessons, questions, beliefs, identity), whose rules the
        # newer code has replaced. The agent's own writes do not come through
        # here and still land.
        store_minimum = self._older_than_store()
        if store_minimum is not None:
            # Automatic maintenance is reported inside another tool's result,
            # which ends with the notice itself; say it once, there.
            completed = (
                "no maintenance; this code is older than the store"
                if auto
                else f"no maintenance. {OLDER_CODE_MESSAGE}"
            )
            return "\n".join([
                f"Requested: {'deep' if requested_deep else 'standard'}",
                "Cycle: skipped",
                f"Completed: {completed}",
                f"Code: version {MAINTENANCE_CODE_VERSION}; the store needs {store_minimum} or newer",
                "Passes: none",
            ])

        can_run_deep = requested_deep and self._llm_client is not None
        # config={} meant the whole consolidation block in ~/.mnemos/config.json
        # was never applied — decay_rate, thresholds and min_idle_minutes all
        # silently fell back to hardcoded defaults.
        try:
            daemon_config = load_config()
        except Exception:
            daemon_config = {}
        daemon = ConsolidationDaemon(
            store=self._store,
            config=daemon_config,
            llm_client=self._llm_client if can_run_deep else None,
            embedding_index=self._embedding_index,
            agent_model_hint=self._agent_model_hint,
        )
        # Automatic maintenance rides on reads (context) and writes (capture,
        # correct). Those fire many times a session, so they honour the
        # activity gate; an explicit maintain request always runs.
        stats = daemon.run_cycle(
            deep=can_run_deep,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            respect_gate=auto,
        )
        pass_errors = [key for key in stats if key.endswith("_error")]
        if self._host_mutation_active and pass_errors:
            summary = ", ".join(f"{key}={stats[key]}" for key in pass_errors)
            raise RuntimeError(f"host maintenance failed: {summary}")
        if stats.get("skipped"):
            return "\n".join([
                f"Requested: {'deep' if requested_deep else 'standard'}",
                "Cycle: skipped",
                "Completed: no maintenance needed yet (ran recently)",
                "Passes: none",
            ])
        promoted = self._promote_candidates(limit=3)
        # Maintenance proposes reflections; it never answers them.
        try:
            # Hygiene first: a store written before excerpts were dropped can
            # still hold a frozen copy of a memory the human asked to forget.
            self._store.purge_stale_reflections()
            self._enqueue_lesson_reflections(stats.get("softening") or {})
            self._enqueue_impact_reflections(limit=2)
            # Judgement the agent alone can do, proposed rarely: whether a
            # recurring theme is a belief, and whether a surprising capture
            # contradicts what was already held. The packet's ≤2 cap and the
            # per-cycle limit of 1 each keep these from ever becoming a chore.
            self._enqueue_belief_reflections(limit=1)
            self._enqueue_contradiction_reflections(limit=1)
        except Exception:
            if self._host_mutation_active:
                raise

        # Dream journal: narrate the cycle when it did meaningful work. The
        # import stays local so a journal failure can never break maintenance.
        self.last_dream_note_id = None
        self.last_dream_narrative = None
        dream_status = "skipped (nothing noteworthy)"
        try:
            from .dream_journal import (
                collect_belief_deltas,
                compose_dream_narrative,
                write_dream_entry,
            )

            deltas = collect_belief_deltas(
                self._store, self.scope.agent_id, stats.get("started_at", "")
            )
            narrative = compose_dream_narrative(stats, deltas, promoted)
            if narrative:
                self.last_dream_note_id = write_dream_entry(self._store, self.scope, narrative)
                self.last_dream_narrative = narrative
                self._traced_write(self.last_dream_note_id)
                self._set_meta("dream_last_written_at", datetime.now(timezone.utc).isoformat())
                dream_status = "updated"
        except Exception:
            if self._host_mutation_active:
                raise
            dream_status = "skipped (write failed)"

        if can_run_deep:
            completed = "model-assisted deep maintenance completed"
        elif requested_deep:
            completed = "local deterministic maintenance completed; model-assisted deep pass unavailable"
        else:
            completed = "local deterministic maintenance completed"

        model_note = "dedicated model available" if self._llm_client else "no dedicated model configured"
        if requested_deep and not can_run_deep:
            model_note += "; deep requested, ran local deterministic maintenance"
        elif not requested_deep:
            model_note += "; ran local deterministic maintenance"
        if auto:
            model_note += " during normal use"

        lines = [
            f"Requested: {'deep' if requested_deep else 'standard'}",
            f"Cycle: {stats.get('cycle_type', 'shallow')}",
            f"Completed: {completed}",
            f"Passes: {', '.join(stats.get('passes_run', [])) or 'none'}",
            f"Promoted continuity notes: {promoted}",
            f"Model path: {model_note}",
            f"Dream journal: {dream_status}",
        ]
        errors = [key for key in stats if key.endswith("_error")]
        for key in errors:
            lines.append(f"{key}: {stats[key]}")
        return "\n".join(lines)

    def polish_dream(self, note_id: str, polished: str) -> bool:
        """Apply a host-model polish to a dream note. Returns False on any failure."""

        text = (polished or "").strip()
        if not text or len(text) > 900:
            return False
        try:
            from .dream_journal import polish_dream_entry

            self._ensure_init()
            polish_dream_entry(self._store, self.scope, note_id, text)
            return True
        except Exception:
            return False

    def health(self) -> dict[str, Any]:
        """Read-only snapshot of this scope's memory health.

        Unlike context(), this never runs maintenance, never bumps the
        session counter, and never writes onboarding or verification meta.
        Safe to call any number of times without changing the store.
        """

        # _ensure_init() builds the schema, so calling it here would create a
        # database as a side effect of a tool annotated readOnlyHint=True.
        # On a scope with no store yet, report that instead of creating one.
        if not self.db_path.exists():
            return {
                "scope": {
                    "agent_id": self.scope.agent_id,
                    "person_id": self.scope.person_id,
                    "project_scope": self.scope.project_scope,
                },
                "store": {
                    "db_path": str(self.db_path),
                    "exists": False,
                    "size_bytes": 0,
                },
                "code": self.code_versions(),
                "note": (
                    "No memory store exists for this scope yet. It is created "
                    "on first capture, not by reading health."
                ),
            }

        self._ensure_init()
        assert self._store is not None

        stats = self._stats()
        db_path = self.db_path
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        states = _state_counts(stats)

        last_cycle: dict[str, Any] | None = None
        runs = self._store.get_consolidation_runs(
            "cycle", limit=1, agent_id=self.scope.agent_id,
            person_id=self.scope.person_id, project_scope=self.scope.project_scope,
        )
        if runs:
            row = runs[0]
            cycle_stats = row.get("stats") or {}
            substrate = cycle_stats.get("substrate") or {}
            last_cycle = {
                "completed_at": row.get("completed_at"),
                "cycle_type": cycle_stats.get("cycle_type"),
                "passes_run": list(cycle_stats.get("passes_run") or []),
                "substrate_model": substrate.get("model"),
                "substrate_provider": substrate.get("provider"),
            }

        # persist=False keeps the onboarding probe write-free: it only
        # reads meta and stats, never records a stage transition.
        status = self._onboarding_status(persist=False)

        verified_at_meta = self._get_meta("verified_at")
        if verified_at_meta == "skipped":
            verification_status = "skipped"
            verified_at = None
        elif verified_at_meta is not None:
            verification_status = "verified"
            verified_at = verified_at_meta
        elif self._get_meta("first_capture") is not None:
            verification_status = "pending"
            verified_at = None
        else:
            verification_status = "not-started"
            verified_at = None

        dream = fetch_active_dream_entry(self._store, self.scope)
        dream_last_written_at = self._get_meta("dream_last_written_at")
        dream_excerpt: str | None = None
        if dream:
            dream_excerpt = " ".join(str(dream.get("content", "")).split())[:60]
            if dream_last_written_at is None:
                dream_last_written_at = dream.get("last_revised_at")

        latest_handoff = self._store.get_latest_handoff(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            active_only=False,
        )
        handoff_health = {
            "id": latest_handoff.get("id") if latest_handoff else None,
            "active": bool(latest_handoff.get("active")) if latest_handoff else False,
            "last_saved_at": latest_handoff.get("created_at") if latest_handoff else None,
            "last_surfaced_at": (
                latest_handoff.get("last_surfaced_at") if latest_handoff else None
            ),
            "delivery_count": int(latest_handoff.get("surface_count", 0)) if latest_handoff else 0,
            "authored_by": latest_handoff.get("authored_by") if latest_handoff else None,
            "author_id": latest_handoff.get("author_id") if latest_handoff else None,
            "author_model": latest_handoff.get("author_model") if latest_handoff else None,
        }

        return {
            "scope": {
                "agent_id": self.scope.agent_id,
                "person_id": self.scope.person_id,
                "project_scope": self.scope.project_scope,
            },
            "store": {
                "db_path": str(db_path),
                "size_bytes": int(size_bytes),
            },
            # Which rules this process maintains memory by, and the newest
            # version that has opened the store. A long-running session can
            # be older than the store, and then it no longer maintains it.
            "code": self.code_versions(),
            "counts": {
                **states,
                "continuity_notes_active": stats.get("hypomnema_active", 0),
                "continuity_notes_foundational": stats.get("hypomnema_foundational", 0),
                "connections": stats.get("connections", 0),
                "beliefs_active": stats.get("beliefs_active", 0),
            },
            "last_cycle": last_cycle,
            # This session's own introduction. The scope's last one belongs
            # to whichever session made it (on a real store, a Grok session's)
            # and says nothing about who is asking.
            "identity": self._identity_health(),
            "onboarding": {
                "stage": status["stage"],
                "session": int(self._get_meta("session_counter", "0") or 0),
            },
            "verification": {
                "status": verification_status,
                "verified_at": verified_at,
            },
            "dream": {
                "last_written_at": dream_last_written_at,
                "excerpt": dream_excerpt,
            },
            "handoff": handoff_health,
            "continuity": self.continuity_signals(),
            "unreachable": self.unreachable_memories(),
            # Memory held in this file that no read path reaches. Without this
            # the card counted only the scoped rows, and a store holding
            # thousands of quarantined memories reported a healthy few hundred.
            "legacy": self.legacy_counts(),
            "semantic": self.semantic_status(),
        }

    def _identity_health(self) -> dict[str, Any]:
        """Who this session introduced itself as, for health and doctor."""
        model, name = self.session_identity()
        return {
            "session": harness_session() or None,
            "model": model or None,
            "name": name or None,
        }

    def memory_counts(self) -> dict[str, int]:
        """How many memories this scope holds in each state, as the health
        card counts them (and `mnemos doctor`, from the same numbers).
        Read-only."""
        return _state_counts(self._stats())

    def unreachable_memories(self) -> dict[str, Any]:
        """Memories stored in this scope that an ordinary recall never returns,
        and the one call that does. Read-only.

        Dormant ones are not among them: a strong match brings those back.
        Forgotten or replaced ones are not either: nothing brings those back,
        by the agent's own choice. What is left faded into the archive.
        """
        self._ensure_init()
        assert self._store is not None
        return {
            "count": self._store.count_faded(
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            ),
            "command": UNREACHABLE_COMMAND,
        }

    def semantic_status(self, verify: bool = False) -> dict[str, Any]:
        """Whether recall can seed by meaning in this process, and why not.

        Semantic recall is optional, so an install silently has it or not,
        and a failed import used to look exactly like a missing package.
        This is the one answer both the health card and `mnemos doctor`
        print. ``verify`` embeds a probe first (a model load on first use), so
        "on" reflects a real embedding rather than a successful import.
        """
        self._ensure_init()
        assert self._store is not None
        index = self._embedding_index
        if index is None:
            return {
                "active": False, "backend": None, "model": None,
                "reason": "this runtime has no embedding index",
                "verified": False, "last_error": None,
                "embeddings_stored": 0, "embeddings_usable": 0,
                "embeddings_by_model": {},
            }
        if verify:
            index.verify()
        return index.status(candidate_ids=self._store.active_engram_ids(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        ))

    def _retrieve(self, query: str, max_results: int = 5) -> list[Any]:
        """The memories recall finds for ``query``, after every filter.

        Finding changes nothing. Reconsolidating inside retrieval strengthened
        results these filters then dropped, which no one was ever shown, so a
        caller reinforces only what it returns, with ``_reinforce_returned``.
        """
        assert self._store is not None
        assert self._retriever is not None
        emotional_state = self._store.get_latest_emotional_state(self.scope.agent_id)
        results = _filter_memories(query, self._retriever.retrieve(
            cue=query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            max_results=max(1, max_results),
            emotional_state=emotional_state,
            reconsolidate_results=False,
        ))
        return [
            result for result in results
            if self._engram_visible_in_current_scope(result.engram.id)
        ]

    def _reinforce_returned(self, query: str, memories: list[Any]) -> None:
        """Reconsolidate the memories a result shows the reader, and no others.

        Each is reinforced at most once per session: the Claude Code session
        (``CLAUDE_CODE_SESSION_ID``) when there is one, otherwise this server
        process. Code older than the store reinforces nothing: how a return
        strengthens a memory is a rule newer code may have replaced.
        """
        if not memories or self._older_than_store() is not None:
            return
        assert self._retriever is not None
        self._retriever.reinforce(
            memories,
            query,
            agent_id=self.scope.agent_id,
            session=harness_session(),
        )

    def _engram_visible_in_current_scope(self, engram_id: str) -> bool:
        """Respect hypomnema person/project scope for durable memories.

        Thin wrapper over the store's shared visibility check, so this path and
        ``build_context_packet`` cannot disagree about whose memory an engram
        is. See ``EngramStore.engram_visible_in_scope``.
        """
        assert self._store is not None
        return self._store.engram_visible_in_scope(
            engram_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )

    def _promote_candidates(self, limit: int = 3) -> int:
        assert self._store is not None
        assert self._encoder is not None
        candidates = self._store.get_hypomnema_promotion_candidates(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=limit,
        )
        promoted = 0
        for entry in candidates:
            if entry.get("related_engram_id"):
                self._store.mark_hypomnema_promoted(entry["id"], entry["related_engram_id"])
                promoted += 1
                continue
            # The memory holds the note's words, so it keeps the note's
            # author: the agent's note stays the agent's, Mnemos's stays
            # Mnemos's, and any other kind is not claimed for either.
            engram = self._encoder.encode(
                content=entry["content"],
                impact="Stable continuity promoted during simple maintenance.",
                impact_source="template",
                kind="semantic",
                tags=["continuity", "promoted", *entry.get("tags", [])],
                source=SourceType.BACKGROUND,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                override_confidence=float(entry["confidence"]),
                skip_surprise_detection=True,
                author_kind={"agent": "agent", "system": "system"}.get(
                    entry.get("authored_by") or "", "unknown"
                ),
                author_model=entry.get("author_model") or "",
                author_session=entry.get("author_session") or "",
            )
            self._traced_write(engram.id)
            self._store.mark_hypomnema_promoted(entry["id"], engram.id)
            promoted += 1
        return promoted


def _importance_scores(importance: str | float, domain: str) -> tuple[float, float]:
    if isinstance(importance, (float, int)):
        normalized_score = min(max(float(importance), 0.0), 1.0)
        confidence = min(max(0.55 + (normalized_score * 0.4), 0.55), 0.95)
        salience = min(max(0.35 + (normalized_score * 0.55), 0.35), 0.9)
        return confidence, salience

    normalized = str(importance).strip().lower()
    if normalized in {"low", "minor"}:
        return 0.72, 0.45
    if normalized in {"high", "important", "critical"}:
        return 0.92, 0.82
    if domain in {"foundational", "identity"}:
        return 0.9, 0.8
    if domain in {"recurring", "long-arc"}:
        return 0.86, 0.72
    return 0.82, 0.66


# Phrases the server itself writes into `impact` (mnemos/core/placeholders.py).
# They fill the column but are not traces of how understanding changed, so an
# engram carrying only one of these still needs the agent's own words.
_TEMPLATED_IMPACTS = TEMPLATED_IMPACTS


def _replacement_impact(
    impact: str, placeholder: str, *replaced: Engram | None
) -> tuple[str, str, str]:
    """What a correction's replacement means: (impact, impact_source, note).

    An impact given with the correction is the agent's own words, labelled as
    capture labels them. Without one, the replacement keeps what the memory
    it replaces meant (``replaced`` is newest first), with that meaning's own
    source: a correction usually fixes a detail, not the meaning, and a
    placeholder in its place means no lesson can ever come from it. A
    placeholder is never carried as meaning, so only when there is nothing
    true to carry does the replacement get one. ``note`` is the result line
    saying what was kept, so the agent can notice a meaning that no longer
    holds.
    """
    given = (impact or "").strip()
    if given:
        return given, "agent", ""
    for engram in replaced:
        if engram is None:
            continue
        kept = (engram.impact or "").strip()
        if kept and not is_templated(kept, engram.impact_source):
            shown = " ".join(kept.split())
            end = "" if shown.endswith((".", "!", "?")) else "."
            return kept, engram.impact_source, (
                f'Kept what it meant: "{shown}"{end} '
                "If that has changed, correct it with a new impact."
            )
    return placeholder, "template", ""


def _impact_for(content: str, domain: str) -> str:
    if domain in {"foundational", "identity"}:
        return "Foundational continuity for future interactions."
    if domain == "recurring":
        return "Recurring pattern worth carrying across sessions."
    if domain == "long-arc":
        return "Long-arc context that should shape future work."
    if domain == "situational":
        return "Current working context for continuity."
    if "prefer" in content.lower() or "wants" in content.lower():
        return "Preference to respect in future decisions."
    return "Durable continuity captured from the session."


def _format_continuity(entry: dict[str, Any]) -> str:
    score = entry.get("score", 0.0)
    content = entry["content"].replace("\n", " ")
    if len(content) > 180:
        content = content[:177] + "..."
    return (
        f"- [{score:.2f}] {content}\n"
        f"  id={entry['id']} domain={entry['domain']} confidence={entry['confidence']:.2f} "
        f"{note_signature(entry)}"
    )


def _format_memory(result: Any) -> str:
    engram = result.engram
    display = engram.impact or engram.content
    display = display.replace("\n", " ")
    if len(display) > 180:
        display = display[:177] + "..."
    # A dormant memory the cue matched starts at half the score, which the
    # reader would otherwise have no way to read.
    quiet = " (it had gone quiet)" if engram.state == "dormant" else ""
    return (
        f"- [{result.score:.2f}] {display}\n"
        f"  id={engram.id} kind={engram.kind} confidence={engram.source.confidence:.2f}{quiet}"
    )


def _format_archived(engram: Engram) -> str:
    """A memory recall found in the archive: no score, since recall's
    resonance never reaches the archive; the query named it."""
    display = (engram.impact or engram.content).replace("\n", " ")
    if len(display) > 180:
        display = display[:177] + "..."
    return (
        f"- {display}\n"
        f"  id={engram.id} kind={engram.kind} confidence={engram.source.confidence:.2f}"
    )


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


def _id_list(noun: str, ids: list[str]) -> str:
    """'memory X', or 'memories X and Y'."""
    ids = list(ids)
    if len(ids) == 1:
        return f"{noun} {ids[0]}"
    plural = "memories" if noun == "memory" else f"{noun}s"
    return f"{plural} {', '.join(ids[:-1])} and {ids[-1]}"


def _age_text(timestamp: str) -> str:
    """Render a compact age while remaining safe around legacy timestamps."""

    try:
        moment = datetime.fromisoformat(timestamp)
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


def _working_folder() -> str:
    """This process's working folder, or ``""`` when it has none (deleted)."""

    try:
        return os.getcwd()
    except OSError:
        return ""


def _human_size(num_bytes: int) -> str:
    """Render a byte count in 1024-base units: "0 B", "412 KB", "1.2 MB"."""

    size = float(max(int(num_bytes), 0))
    if size < 1024:
        return f"{int(size)} B"
    unit = "B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            break
    if size >= 100 or size.is_integer():
        return f"{size:.0f} {unit}"
    return f"{size:.1f} {unit}"


def _tally_legacy(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count unscoped rows by kind; archived ones apart, as recall skips them."""

    counts = {"hidden": 0, "archived": 0, **{name: 0 for name in LEGACY_CLASSES}}
    for row in rows:
        if row["state"] == "archived":
            counts["archived"] += 1
            continue
        counts["hidden"] += 1
        counts[row["class"]] += 1
    return counts


def format_legacy_summary(counts: Mapping[str, int] | None) -> str | None:
    """One plain sentence about memory the scope migration hid, or None."""

    if not counts or not counts.get("hidden"):
        return None
    lessons = counts.get("lessons", 0)
    parts = [
        f"{lessons:,} lesson{'' if lessons == 1 else 's'}" if lessons else "",
        f"{counts['other']:,} other" if counts.get("other") else "",
        f"{counts['indexer']:,} written by a tool" if counts.get("indexer") else "",
    ]
    return (
        f"{counts['hidden']:,} older memories from before scoping never reach recall "
        f"({', '.join(part for part in parts if part)}); see 'mnemos adopt-legacy'"
    )


def _state_counts(stats: Mapping[str, Any]) -> dict[str, int]:
    """The memory counts by state out of the store's stats."""
    return {
        "memories_active": int(stats.get("engrams_active", 0) or 0),
        "memories_dormant": int(stats.get("engrams_dormant", 0) or 0),
        "memories_archived": int(stats.get("archived", 0) or 0),
    }


def format_memory_counts(counts: Mapping[str, Any]) -> str:
    """How many memories are active, dormant and archived, in the words the
    health card and `mnemos doctor` both print."""
    return (
        f"{counts.get('memories_active', 0)} active, "
        f"{counts.get('memories_dormant', 0)} dormant, "
        f"{counts.get('memories_archived', 0)} archived"
    )


def format_unreachable_summary(unreachable: Mapping[str, Any] | None) -> str | None:
    """One plain sentence about memories an ordinary recall never returns, and
    the call that does, or None when there are none."""

    count = int((unreachable or {}).get("count") or 0)
    if not count:
        return None
    command = (unreachable or {}).get("command") or UNREACHABLE_COMMAND
    return (
        f"{count:,} faded {'memory is' if count == 1 else 'memories are'} stored in the "
        f"archive, out of ordinary recall; {command} reaches "
        f"{'it' if count == 1 else 'them'}"
    )


def describe_semantic(semantic: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Say whether recall can seed by meaning, in plain words.

    Returns a headline, detail lines, and warnings. Shared by the health card
    and `mnemos doctor`, so the two can never disagree about it.
    """
    if not semantic:
        return "unknown", [], []
    details: list[str] = []
    attention: list[str] = []
    model = semantic.get("model")
    if semantic.get("active"):
        headline = f"on — {semantic.get('backend')} model {model}"
        if "memories" in semantic:
            headline += (
                f", {semantic.get('memories_searchable', 0)} of {semantic['memories']} "
                "active memories searchable by meaning"
            )
        if not semantic.get("verified"):
            details.append(
                "nothing embedded in this process yet; the model loads on first recall or capture"
            )
        others = {
            name: count
            for name, count in (semantic.get("embeddings_by_model") or {}).items()
            if name != model
        }
        if others:
            listed = ", ".join(
                f"{name} ({count:,})"
                for name, count in sorted(others.items(), key=lambda item: -item[1])
            )
            details.append(
                f"{sum(others.values()):,} stored embeddings come from other models and "
                f"are skipped: {listed}"
            )
        if semantic.get("last_error"):
            details.append(f"last embedding attempt failed: {semantic['last_error']}")
    else:
        headline = "OFF — recall finds memories by keyword only"
        reason = semantic.get("reason") or "unknown"
        details.append(f"why: {reason}")
        stored = int(semantic.get("embeddings_stored") or 0)
        if stored:
            attention.append(
                f"semantic recall is off, but this store holds {stored:,} embeddings "
                "it cannot use (why: see Semantic)."
            )
    return headline, details, attention


def describe_identity(identity: Mapping[str, Any] | None) -> str:
    """The identity line of the health card and `mnemos doctor`: whom this
    session introduced itself as, or that it hasn't. Shared, so the two can
    never disagree."""
    identity = identity or {}
    model = str(identity.get("model") or "")
    name = str(identity.get("name") or "")
    if not model and not name:
        return "none this session"
    said = [signature(model)] if model else []
    if name:
        said.append(f"named {name}")
    return f"{', '.join(said)} (introduced this session)"


def describe_code(code: Mapping[str, Any] | None) -> tuple[str, str | None]:
    """The code line of the health card and `mnemos doctor`, and its warning.

    Returns the line, plus the plain fix when this process runs older code
    than the store expects (None otherwise): restart first, and if that does
    not clear it, update or reset.
    """
    code = code or {}
    running = code.get("running", MAINTENANCE_CODE_VERSION)
    minimum = code.get("store_minimum")
    if minimum is None:
        return f"version {running} (the store sets no minimum yet)", None
    headline = f"version {running} (the store needs {minimum} or newer)"
    if not code.get("older_than_store"):
        return headline, None
    return headline, f"{OLDER_CODE_MESSAGE} {OLDER_CODE_FIX}"


def format_health_card(data: dict[str, Any]) -> str:
    """Render a health() snapshot as a human-relayable card."""

    def line(label: str, value: Any) -> str:
        return f"{label + ':':<15}{value}"

    scope = data["scope"]
    store = data["store"]
    if store.get("exists") is False:
        return "\n".join([
            "Mnemos health card",
            line(
                "Scope",
                f"agent={scope['agent_id']} person={scope['person_id']} "
                f"project={scope['project_scope']}",
            ),
            line("Store", f"{store['db_path']} (not created yet)"),
            "",
            data.get("note", "No memory store exists for this scope yet."),
            "",
            "Everything on this card is safe to relay to the human in plain words.",
        ])
    counts = data["counts"]

    last_cycle = data["last_cycle"]
    if last_cycle is None:
        cycle_line = "none yet"
    else:
        substrate = last_cycle["substrate_model"] or "local rules, no model"
        cycle_line = (
            f"{last_cycle['completed_at']} ({last_cycle['cycle_type']}) "
            f"maintained by {substrate}"
        )

    verification = data["verification"]
    verification_line = {
        "verified": f"verified on {verification['verified_at']}",
        "pending": "pending first restart",
        "skipped": "skipped (existing store)",
        "not-started": "not started",
    }.get(verification["status"], verification["status"])

    dream = data["dream"]
    if dream["excerpt"] is None:
        dream_line = "none yet"
    else:
        dream_line = f"{dream['last_written_at']}: \"{dream['excerpt']}\""

    handoff = data.get("handoff") or {}
    if handoff.get("last_saved_at") is None:
        handoff_line = "none yet"
    else:
        state = "active" if handoff.get("active") else "removed"
        signed = signature(handoff.get("author_model") or "") or "unsigned"
        handoff_line = (
            f"{handoff['last_saved_at']} ({state}, {handoff.get('authored_by')}, {signed}, "
            f"delivered {handoff.get('delivery_count', 0)} time(s), "
            f"last {handoff.get('last_surfaced_at') or 'never'})"
        )

    continuity = data.get("continuity") or {}
    warnings = continuity.get("warnings") or []
    if warnings:
        # Amnesia is the failure that looks exactly like success, so it is
        # stated first and in plain words rather than left to be inferred
        # from a count further down the card.
        continuity_lines = ["", "ATTENTION — continuity may not be reaching this agent:"]
        continuity_lines += [f"  - {w}" for w in warnings]
    else:
        streak = continuity.get("empty_context_streak", 0)
        since = continuity.get("sessions_since_capture")
        detail = "carrying continuity"
        if since == 0:
            detail = "carrying continuity (captured this session)"
        elif since is not None:
            detail = f"carrying continuity (last capture {since} session(s) ago)"
        continuity_lines = ["", f"Continuity check: {detail}, {streak} empty packet(s) in a row."]

    legacy = format_legacy_summary(data.get("legacy"))
    legacy_lines = [line("Hidden", legacy)] if legacy else []
    unreachable = format_unreachable_summary(data.get("unreachable"))
    unreachable_lines = [line("Unreachable", unreachable)] if unreachable else []
    semantic_headline, semantic_details, semantic_attention = describe_semantic(
        data.get("semantic") or {}
    )
    semantic_lines = [line("Semantic", semantic_headline)]
    semantic_lines += [f"{'':<15}{detail}" for detail in semantic_details]
    if semantic_attention:
        continuity_lines = [
            "", f"ATTENTION — {semantic_attention[0]}", *continuity_lines,
        ]
    # First, because it is the one with a fix the human can apply right now,
    # and because while it holds, nothing else on the card is being maintained.
    code_headline, code_attention = describe_code(data.get("code"))
    if code_attention:
        continuity_lines = ["", f"ATTENTION — {code_attention}", *continuity_lines]

    return "\n".join([
        "Mnemos health card",
        line(
            "Scope",
            f"agent={scope['agent_id']} person={scope['person_id']} "
            f"project={scope['project_scope']}",
        ),
        line("Store", f"{store['db_path']} ({_human_size(store['size_bytes'])})"),
        line("Code", code_headline),
        line("Identity", describe_identity(data.get("identity"))),
        line("Memories", format_memory_counts(counts)),
        *unreachable_lines,
        *legacy_lines,
        line(
            "Continuity",
            f"{counts['continuity_notes_active']} notes "
            f"({counts['continuity_notes_foundational']} foundational)",
        ),
        line("Connections", counts["connections"]),
        line("Beliefs", f"{counts['beliefs_active']} active"),
        *semantic_lines,
        line("Last cycle", cycle_line),
        line(
            "Onboarding",
            f"{data['onboarding']['stage']} (session {data['onboarding']['session']})",
        ),
        line("Verification", verification_line),
        line("Last handoff", handoff_line),
        line("Last dream", dream_line),
        *continuity_lines,
        "",
        "Everything on this card is safe to relay to the human in plain words.",
    ])
