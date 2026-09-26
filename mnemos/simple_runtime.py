"""Simple-mode continuity runtime for Mnemos.

This module is intentionally MCP-agnostic so the product path can be tested
without a running client. It exposes the real Mnemos stack through nine simple
operations, including agent-written handoff and reflection.
"""

from __future__ import annotations

import functools
import json
import hashlib
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
    from_same_session,
    handoff_framing,
    harness_session,
    note_signature,
    resolve_author_model,
    signature,
)
from .code_version import MAINTENANCE_CODE_VERSION, OLDER_CODE_FIX, OLDER_CODE_MESSAGE
from .config.loader import load_config
from .consolidation.daemon import ConsolidationDaemon
from .core.types import SourceType
from .dream_journal import DREAM_JOURNAL_TAG, fetch_active_dream_entry
from .encoding.encoder import Encoder
from .identity_svg import build_timeline, render_identity_svg, short_label
from .interface.context_packet import format_other_handoffs
from .retrieval.reactive import ReactiveRetriever
# Re-exported: MnemosScope and resolve_scope moved to simple_scope but
# remain importable from here for existing consumers.
from .simple_scope import MnemosScope, resolve_scope  # noqa: F401
from .store.embedding_index import EmbeddingIndex
from .core.engram import Engram
from .core.placeholders import TEMPLATED_IMPACTS, is_templated
from .store.fts import distinctive_terms, fts_words, meaningful_words, or_query
from .store.sqlite_store import EngramStore, ReadOnlyEngramStore


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


# Hypomnema ids are uuid4 strings. Recall treats a query of exactly this shape
# as an id before it treats it as words.
_ENTRY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
        # The model that introduced itself in this session, if one did. Kept
        # per runtime, not per scope: several models can share one scope, and
        # the last introduction must not sign every other model's notes.
        self._session_author = ""
        self._session_id: int | None = None
        self.last_dream_note_id: str | None = None
        self.last_dream_narrative: str | None = None
        self._host_mutation_active = False
        # How deep this runtime is in tool calls, so only the outermost one
        # ends its result with the older-code notice.
        self._notice_depth = 0

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
            self._store = EngramStore(self.scope.db_path)
            self._announce_code_version()
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

    def _announce_code_version(self) -> None:
        """Raise the store's minimum code version to this code's, at startup.

        From then on, a server still running older code stops maintaining the
        store (see ``maintain``). The raise never lowers the value. If another
        process holds the write lock right now, this server still starts; the
        next one to open the store raises it.
        """
        assert self._store is not None
        try:
            self._store.raise_min_code_version(MAINTENANCE_CODE_VERSION)
        except sqlite3.OperationalError:
            pass

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

        rows = self._store._get_conn().execute(
            """
            SELECT e.id, e.content
            FROM engrams e
            JOIN hypomnema_entries h ON h.related_engram_id = e.id
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.active = 1 AND e.state = 'active'
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
    def reflect(self, target_id: str, text: str, verdict: str = "") -> str:
        """Record the agent's own reflection on one of its memories.

        ``verdict`` is what the agent decided (see ``_VERDICTS``), and it alone
        decides what happens. The words are kept exactly as written and never
        read for a yes or a no. A lesson or impact question asks for the words
        themselves, so without a verdict they are its answer, as before. A
        belief or contradiction question answered without one keeps the words
        and stays open, and nothing is formed, retired or linked.
        """
        answer = (text or "").strip()
        if not answer:
            return "Nothing recorded: the reflection was empty."
        decided = (verdict or "").strip().lower().replace("-", "_").replace(" ", "_")
        if decided and decided not in VERDICTS:
            return f"Nothing recorded: {verdict.strip()!r} is not a verdict. {_VERDICT_GUIDE}"

        self._ensure_init()
        assert self._store is not None

        scope = {
            "agent_id": self.scope.agent_id,
            "person_id": self.scope.person_id,
            "project_scope": self.scope.project_scope,
        }
        target = target_id.strip()
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
            return self._keep_answer_open(ask, answer, decided)
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
            self._carry_reflection_into_note(engram.id, answer)
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
            self._carry_reflection_into_note(engram.id, answer)
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

            lesson_id = _create_or_reinforce_lesson(engram, self._store, {})
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

    def _keep_answer_open(self, ask: dict[str, Any], answer: str, verdict: str = "") -> str:
        """Keep an answer older code cannot apply, without spending its question.

        The words become a signed continuity note that names the question
        (its id and its text) and the verdict given, if any, so they are
        neither lost nor spent. The ask itself is left exactly as it was:
        pending, its showings unchanged.
        """
        assert self._store is not None
        author = self.author_model()
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
            domain=domain,
            tags=sorted({"reflection", "open-question", *_simple_tags(answer)}),
            confidence=confidence,
            salience=salience,
        )
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
            return (
                "Kept. The belief stands a little more firmly "
                f"({before:.0%} to {belief.confidence:.0%})."
            )

        belief.revise(0.0, f"retired by the agent: {answer}")
        belief.superseded_by = "retired"
        self._store.save_belief(belief)
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

    def _carry_reflection_into_note(self, engram_id: str, answer: str) -> str | None:
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
            author = self.author_model()
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

    def _reflection_block(self, limit: int = 2) -> str | None:
        """The quiet ask, shown only when there is genuinely something to sit with."""
        items = self.pending_reflections(limit=limit)
        if not items:
            return None

        assert self._store is not None
        self._store.mark_reflections_surfaced([i["id"] for i in items])

        lines = ["Something of yours is waiting on you:"]
        for item in items:
            lines.append(f'  "{item["excerpt"]}"')
            lines.append(f"    {item['prompt']}")
            call = verdict_call_lines(item)
            if call is None:
                lines.append(f"    mnemos_reflect(target_id=\"{item['target_id']}\", ...)")
            else:
                lines.extend(f"    {line}" for line in call)
        lines.append(
            "  Answer in your own words if one comes. If nothing does, leave it — "
            "this fades on its own."
        )
        return "\n".join(lines)

    def _identity_summary(self) -> str | None:
        """The agent's own computed self-summary, if there is one yet."""
        self._ensure_init()
        assert self._store is not None
        try:
            identity = self._store.get_identity(self.scope.agent_id)
        except Exception:
            return None
        if identity is None:
            return None
        summary = (getattr(identity.epoch_state, "self_summary", "") or "").strip()
        return summary or None

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

    def author_model(self) -> str:
        """The model signing what this session writes, or ``""`` if unknown."""

        return resolve_author_model(self._session_author)

    def _signed_line(self, author: str) -> str:
        if author:
            return f"Signed: {signature(author)}"
        return (
            "Unsigned: Mnemos couldn't tell which model you are. Call "
            "mnemos_introduce with your exact model id so your notes carry "
            "your name."
        )

    @_notice_when_older
    def introduce(self, agent_model: str, agent_name: str = "") -> str:
        """Record the agent's self-declared model so maintenance stays kin.

        The declaration also signs everything this session writes.
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
    def context(self, query: str = "", max_results: int = 5) -> str:
        """Return the startup continuity packet for an agent."""

        self._ensure_init()
        assert self._store is not None

        # Onboarding guard runs before maintenance so the grandfather check
        # reads the store exactly as the session found it.
        status = self._onboarding_status()
        self._current_session()
        maintenance = self.maintain(auto=True)
        stats = self._stats()
        # Several sessions can work this scope at once, each with its own
        # handoff. This session's own note comes first (after compaction it
        # is the thread it was in), then other sessions' recent notes.
        reader_session = harness_session()
        handoffs = self._store.live_handoffs(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            reader_session=reader_session,
        )
        handoff = handoffs[0] if handoffs else None
        # Fetch extras so the dedicated maintenance section never reduces the
        # number of ordinary continuity notes. Handoffs are excluded from the
        # search itself; each one would otherwise cost a slot.
        all_continuity = self._store.search_hypomnema(
            query,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_results + 4,
            exclude_kinds=("handoff",),
        )
        all_continuity = _filter_continuity(query, all_continuity)
        maintenance_reports = [
            entry for entry in all_continuity
            if entry.get("entry_kind") == "maintenance_report"
            and DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        ][:3]
        continuity = [
            entry for entry in all_continuity
            if entry.get("entry_kind") not in {"handoff", "maintenance_report"}
            and DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        ][:max_results]
        memories = self._retrieve(query, max_results=max_results) if query else []
        self._note_context_outcome(
            len(continuity) + len(maintenance_reports) + int(handoff is not None)
        )

        lines = [
            "Mnemos continuity packet",
            f"Scope: agent={self.scope.agent_id} person={self.scope.person_id} project={self.scope.project_scope}",
            "Storage: local SQLite store ready",
            (
                "Status: "
                f"{stats.get('engrams_active', 0)} memories, "
                f"{stats.get('hypomnema_active', 0)} continuity notes, "
                f"{stats.get('connections', 0)} connections"
            ),
            "",
        ]

        reader = self.author_model()
        if handoff:
            heading, guidance = handoff_framing(
                handoff.get("author_model") or "",
                _age_text(handoff["created_at"]),
                reader,
                same_session=from_same_session(reader_session, handoff.get("author_session")),
            )
            lines.extend([
                "",
                heading,
                handoff["content"],
                guidance,
            ])
            others = format_other_handoffs(
                handoffs[1:], reader_session=reader_session, heading_prefix="",
            )
            if others:
                lines.extend(["", *others.splitlines()])
            for delivered in handoffs:
                self._store.mark_handoff_surfaced(
                    delivered["id"],
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                )

        lines.extend([
            "",
            "Use this at the start of a session. Capture important preferences, decisions, project state, corrections, and durable context as the conversation unfolds.",
            "",
            "Maintenance:",
            _indent(maintenance),
        ])

        # Shift 5: identity is measured from the shape of the graph — what
        # this agent keeps returning to. It is computed on every maintenance
        # cycle and is worth showing, because an agent reading its own
        # concerns back is closer to the point of Mnemos than any count of
        # memories is.
        identity_summary = self._identity_summary()
        if identity_summary:
            signers = self._store.hypomnema_signers(
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if len(signers) > 1:
                names = ", ".join(display_name(model) for model in signers)
                heading = (
                    "What this shared memory keeps returning to, across notes "
                    f"signed by {names}:"
                )
            else:
                heading = "Who you have been, measured from what you keep returning to:"
            lines.extend(["", heading, f"  {identity_summary}"])

        # Quiet and occasional by design: at most a couple of items, only when
        # something genuinely needs the agent's own judgement, and each one
        # stops being shown after a few sessions. A packet that asks for work
        # every time becomes a chore list appended to every conversation.
        #
        # Code older than the store shows none and spends no showings: the
        # questions wait for a current session, which can take the answer as
        # current rules need it.
        reflection = self._reflection_block() if self._older_than_store() is None else None
        if reflection:
            lines.extend(["", reflection])

        block = self._onboarding_block(status)
        if block:
            lines.extend(["", block])

        verification = self._verification_block()
        if verification:
            lines.extend(["", verification])

        dream = fetch_active_dream_entry(self._store, self.scope)
        if dream:
            lines.extend([
                "",
                "While you were away:",
                _indent(dream["content"]),
                "  (System-generated maintenance report, not your own words. Legacy reports may use first-person wording.)",
            ])

        if maintenance_reports:
            lines.extend(["", "System-generated continuity checks:"])
            for report in maintenance_reports:
                lines.append(_indent(report["content"]))
            lines.append(
                "  (Mnemos mechanically produced these checks. They are not your own words.)"
            )

        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        else:
            lines.extend(["", "Continuity notes: none yet. Capture durable context when the user gives it."])

        if memories:
            lines.extend(["", "Relevant memories:"])
            lines.extend(_format_memory(result) for result in memories)

        return "\n".join(lines)

    @_notice_when_older
    def handoff(self, text: str) -> str:
        """Save the agent's exact private note for the next session."""

        if not text.strip():
            return "Nothing saved: handoff text was empty."
        self._ensure_init()
        assert self._store is not None
        author = self.author_model()
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

    def _recall_handoff(self, handoff_id: str) -> str:
        """A handoff read whole by its id, signed, or ``""`` if it isn't one."""

        if not _ENTRY_ID.fullmatch(handoff_id):
            return ""
        assert self._store is not None
        note = self._store.get_hypomnema_entry(
            handoff_id,
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
        )
        if not note or note.get("entry_kind") != "handoff":
            return ""
        heading, guidance = handoff_framing(
            note.get("author_model") or "",
            _age_text(note["created_at"]),
            self.author_model(),
            same_session=from_same_session(harness_session(), note.get("author_session")),
        )
        lines = [heading, note["content"], guidance]
        if not note.get("active"):
            lines.append("This note is no longer active: a newer one replaced it or it was forgotten.")
        return "\n".join(lines)

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
        engrams = self._store.get_active_engrams(
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            limit=max_nodes,
            load_connections=False,
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
    def capture(
        self,
        content: str,
        context: str = "",
        importance: str | float = "auto",
        impact: str = "",
        impact_source: str = "agent",
    ) -> str:
        """Capture durable continuity without exposing Mnemos internals.

        ``impact_source`` records who wrote the impact. It defaults to 'agent'
        because the product caller is `mnemos_capture`, which the agent invokes
        mid-conversation with an impact in its own words. Server-internal
        captures that pass a boilerplate impact set this to 'template' so the
        two never blur — the whole point of the field is that an agent-authored
        trace can be told from a generated one after the fact."""

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
        author = self.author_model()
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

        engram = self._encoder.encode(
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
        )
        note_id = self._store.write_hypomnema_entry(
            content.strip(),
            agent_id=self.scope.agent_id,
            person_id=self.scope.person_id,
            project_scope=self.scope.project_scope,
            source="observed",
            entry_kind="continuity",
            authored_by="agent",
            author_id=self.scope.agent_id,
            author_model=author,
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=domain in {"foundational", "identity"},
            related_engram_id=engram.id,
        )
        self._store.mark_hypomnema_promoted(note_id, engram.id)
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
    def recall(self, query: str, max_results: int = 5) -> str:
        """Recall relevant continuity and durable memories."""

        if not query.strip():
            return "Recall needs a query."

        self._ensure_init()
        assert self._store is not None

        # The packet shows other sessions' handoffs as short lines, each with
        # its id. Recalling that id returns the note whole.
        whole = self._recall_handoff(query.strip())
        if whole:
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

        if not continuity and not memories:
            return "No relevant continuity found."

        lines = [f"Mnemos recall for: {query.strip()}"]
        if continuity:
            lines.extend(["", "Continuity notes:"])
            lines.extend(_format_continuity(entry) for entry in continuity)
        if memories:
            lines.extend(["", "Durable memories:"])
            lines.extend(_format_memory(result) for result in memories)
        return "\n".join(lines)

    def _maybe_correct_belief(self, correction: str, query: str, action: str) -> str:
        """Retire or downweight an agent-authored belief the correction names.

        Matches the correction text against active beliefs the agent itself
        stated (``source == 'agent'``) by token overlap, above a conservative
        threshold so a memory correction that merely brushes a belief does not
        move it. Forget/archive → supersede (hidden from every read path);
        otherwise → erode confidence toward the floor. Returns a note if it
        acted, else ''.
        """
        assert self._store is not None
        text = query.strip() or correction.strip()
        terms = _query_terms(text)
        if not terms:
            return ""
        beliefs = [
            b for b in self._store.get_beliefs(self.scope.agent_id, active_only=True)
            if b.source == "agent"
        ]
        best, best_overlap = None, 0.0
        for belief in beliefs:
            bterms = _query_terms(belief.content)
            # Half the words in common is not enough when the half is "what"
            # and "she": the belief must hold the words that mean something.
            if not bterms or not _named_by(text, belief.content):
                continue
            overlap = len(terms & bterms) / len(terms)
            if overlap > best_overlap:
                best, best_overlap = belief, overlap
        if best is None or best_overlap < 0.5:
            return ""

        if action in {"forget", "archive", "remove", "delete"}:
            self._store.supersede_belief(best.id, reason=f"agent correction: {text[:80]}")
            return (
                f'Retired the belief "{best.content[:80]}". '
                "It will no longer shape your context."
            )
        new_conf = max(0.05, best.confidence - 0.3)
        self._store.revise_belief(
            best.id, new_conf, reason=f"agent correction: {text[:80]}"
        )
        return (
            f'Lowered confidence in the belief "{best.content[:80]}" '
            f"to {int(new_conf * 100)}%."
        )

    @_notice_when_older
    def correct(
        self,
        correction: str,
        target_id: str = "",
        query: str = "",
        action: str = "update",
        impact: str = "",
    ) -> str:
        """Correct, supersede, or archive stale memory.

        ``impact`` is what the corrected memory means now, in the agent's own
        words. Left empty, the replacement keeps what the memory it replaces
        meant, and the result says so.
        """

        if not correction.strip() and action not in {"forget", "archive", "remove", "delete"}:
            return "Correction needs replacement text or a forget/archive action."

        self._ensure_init()
        assert self._store is not None
        assert self._encoder is not None

        action = action.strip().lower() or "update"
        target = target_id.strip()

        # Belief correction (agent-authored only). Checked before the memory
        # paths, on the free-text / query form, so "forget that I believe X"
        # can retire a belief the agent stated — not only a note. Restricted to
        # source=='agent' so a seed or model belief can't be erased by mistake.
        # This is one of the few deliberate downward moves in a graph whose
        # stability otherwise only ratchets up.
        #
        # Code older than the store skips it: which belief a correction names
        # is decided by token overlap, a rule newer code may have replaced, and
        # older code never moves a belief by any path. The correction still
        # lands on the memory it names, below, saved without links to other
        # memories and without a placeholder where its meaning would go.
        older = self._older_than_store() is not None
        if not target and not older:
            belief_note = self._maybe_correct_belief(correction, query, action)
            if belief_note:
                return belief_note

        if target:
            hypo = self._store.get_hypomnema_entry(
                target,
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if hypo is not None:
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        target,
                        reason=f"simple correction action={action}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = hypo.get("related_engram_id") or hypo.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    return f"Archived continuity note {target}."

                corrector = self.author_model()
                self._store.revise_hypomnema_entry(
                    target,
                    correction,
                    reason="simple correction",
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    confidence=0.92,
                    salience=0.75,
                    author_model=corrector,
                    revised_by=corrector,
                )
                if (impact or "").strip():
                    # A note is revised in place and has no impact of its own,
                    # so a meaning given here would otherwise vanish silently.
                    return (
                        f"Updated continuity note {target}.\n"
                        "The impact was not saved: a continuity note does not "
                        "hold one. To change what a memory means, correct it "
                        "by its memory ID."
                    )
                return f"Updated continuity note {target}."

            engram = self._store.get_engram_in_scope(
                target, agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
            )
            if engram is not None:
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_for_engram(
                        engram.id,
                        reason=f"simple correction action={action}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                if action in {"forget", "archive", "remove", "delete"} and not correction.strip():
                    return f"Archived memory {target}."
                meaning, meaning_source, kept = _replacement_impact(
                    impact, "" if older else "Correction to earlier continuity.", engram
                )
                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact=meaning,
                    impact_source=meaning_source,
                    kind=_classify_kind(correction),
                    tags=["continuity", "correction"],
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                    discover_connections=not older,
                )
                return (
                    f"Archived memory {target} and captured correction {replacement.id}.\n"
                    f"Correction: {correction.strip()}"
                    + (f"\n{kept}" if kept else "")
                )

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
                agent_id=self.scope.agent_id,
                person_id=self.scope.person_id,
                project_scope=self.scope.project_scope,
                limit=10,
                exclude_kinds=("handoff",),
            ), lambda note: note.get("content") or "")
            if matches:
                match = matches[0]
                if action in {"forget", "archive", "remove", "delete"}:
                    self._store.archive_hypomnema_entry(
                        match["id"],
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                    )
                    related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                    if related_engram_id:
                        related = self._store.get_engram(related_engram_id)
                        if related is not None:
                            self._store.archive_engram(related, reason=f"simple_correction_{action}")
                    maintenance = self.maintain(auto=True)
                    return (
                        f"Archived closest continuity note {match['id']}.\n"
                        "Maintenance:\n"
                        f"{_indent(maintenance)}"
                    )

                note_id = match["id"]
                corrector = self.author_model()
                if action in {"supersede", "replace"}:
                    note_id = self._store.supersede_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction action={action}; query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        author_model=corrector,
                    )
                else:
                    self._store.revise_hypomnema_entry(
                        match["id"],
                        correction,
                        reason=f"simple correction query={query_text}",
                        agent_id=self.scope.agent_id,
                        person_id=self.scope.person_id,
                        project_scope=self.scope.project_scope,
                        confidence=0.92,
                        salience=0.75,
                        author_model=corrector,
                        revised_by=corrector,
                    )

                # The note's memories, newest first: each correction points
                # graduated_to_engram_id at its replacement, while
                # related_engram_id stays on the memory first captured.
                meaning, meaning_source, kept = _replacement_impact(
                    impact,
                    "" if older else "Corrected continuity for future interactions.",
                    *(
                        self._store.get_engram(engram_id)
                        for engram_id in (
                            match.get("graduated_to_engram_id"),
                            match.get("related_engram_id"),
                        )
                        if engram_id
                    ),
                )

                related_engram_id = match.get("related_engram_id") or match.get("graduated_to_engram_id")
                if related_engram_id:
                    related = self._store.get_engram(related_engram_id)
                    if related is not None:
                        self._store.archive_engram(related, reason=f"simple_correction_{action}")

                replacement = self._encoder.encode(
                    content=correction.strip(),
                    impact=meaning,
                    impact_source=meaning_source,
                    kind=_classify_kind(correction),
                    tags=sorted(set(["continuity", "correction", *_simple_tags(correction)])),
                    source=SourceType.SESSION,
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                    override_confidence=0.92,
                    skip_surprise_detection=True,
                    discover_connections=not older,
                )
                self._store.mark_hypomnema_promoted(note_id, replacement.id)
                maintenance = self.maintain(auto=True)
                return (
                    f"Updated closest continuity note {note_id}.\n"
                    f"Memory ID: {replacement.id}\n"
                    + (f"{kept}\n" if kept else "")
                    + "Maintenance:\n"
                    + _indent(maintenance)
                )

        if action in {"forget", "archive", "remove", "delete"}:
            matches = _named_matches(
                search_text,
                self._retrieve(search_text, max_results=1) if search_text else [],
                lambda r: f"{r.engram.content or ''} {r.engram.impact or ''}",
            )
            if matches:
                engram = matches[0].engram
                self._store.archive_hypomnema_for_engram(
                    engram.id,
                    reason=f"simple correction action={action}",
                    agent_id=self.scope.agent_id,
                    person_id=self.scope.person_id,
                    project_scope=self.scope.project_scope,
                )
                self._store.archive_engram(engram, reason=f"simple_correction_{action}")
                return f"Archived closest matching memory {engram.id}."
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
        )
        if query_text:
            return (
                f'No continuity note matched "{query_text}" closely enough, so none was '
                f"changed; the correction was captured as new continuity.\n{captured}"
            )
        return captured

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
                "memories_active": stats.get("engrams_active", 0),
                "memories_archived": stats.get("archived", 0),
                "continuity_notes_active": stats.get("hypomnema_active", 0),
                "continuity_notes_foundational": stats.get("hypomnema_foundational", 0),
                "connections": stats.get("connections", 0),
                "beliefs_active": stats.get("beliefs_active", 0),
            },
            "last_cycle": last_cycle,
            "identity": {
                "declared_model": self._get_meta("agent_model"),
                "declared_name": self._get_meta("agent_name"),
            },
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
            # Memory held in this file that no read path reaches. Without this
            # the card counted only the scoped rows, and a store holding
            # thousands of quarantined memories reported a healthy few hundred.
            "legacy": self.legacy_counts(),
            "semantic": self.semantic_status(),
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
            # Code older than the store returns what it finds and changes
            # none of it: how a return strengthens a memory is a rule.
            reconsolidate_results=self._older_than_store() is None,
        ))
        return [
            result for result in results
            if self._engram_visible_in_current_scope(result.engram.id)
        ]

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
            )
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
    return (
        f"- [{result.score:.2f}] {display}\n"
        f"  id={engram.id} kind={engram.kind} confidence={engram.source.confidence:.2f}"
    )


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


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
        f"{counts['indexer']:,} from the transcript indexer" if counts.get("indexer") else "",
    ]
    return (
        f"{counts['hidden']:,} older memories from before scoping never reach recall "
        f"({', '.join(part for part in parts if part)}); see 'mnemos adopt-legacy'"
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
        line(
            "Memories",
            f"{counts['memories_active']} active, "
            f"{counts['memories_archived']} archived",
        ),
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
