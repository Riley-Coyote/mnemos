"""
SQLite-backed engram storage with FTS5 full-text search.

Replaces Anima's JSON file persistence. Key advantages:
- Scales to 100K+ engrams without loading everything into memory
- FTS5 gives free full-text search with no external dependencies
- WAL mode for concurrent reads without locking
- Atomic transactions prevent corruption
- Still local-first, single file, portable

All tables are created on init. Migrations handle schema evolution.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..authorship import AUTHOR_KINDS
from ..code_version import MAINTENANCE_CODE_VERSION
from ..file_security import secure_directory, secure_file
from .embedding_index import PASSAGE_TABLE_SQL
from .fts import is_common
from ..core.engram import Connection, Engram, VersionRef
from ..core.belief import Belief
from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity


# Schema version — increment when tables change. v15: passage_vectors, recall's
# meaning index (see embedding_index.PASSAGE_TABLE_SQL).
SCHEMA_VERSION = 15

# The lowest maintenance code version still allowed to maintain this store,
# raised by each newer version that opens it (see mnemos/code_version.py).
MIN_CODE_VERSION_KEY = "min_code_version"

# Who wrote each memory's words (schema v12; see mnemos/authorship.py). Set
# once, when the memory is first stored: a later save never changes it.
VALID_AUTHOR_KINDS = frozenset(AUTHOR_KINDS)
_AUTHOR_COLUMNS = frozenset({"author_kind", "author_model", "author_session"})
# Set, with the counts, once the memories a store held before v12 have been
# labelled. Its presence is what stops the labelling from running again.
AUTHORS_LABELED_KEY = "engram_authors_labeled"
# Set, with what it linked per scope, once the capture notes a store held from
# before pairs were recorded have been paired with their memories (v13). Its
# presence is what stops the linking from running again.
CAPTURE_PAIRS_LINKED_KEY = "capture_pairs_linked"
# A capture writes its memory and then its note in one call: seconds apart at
# most, even when finding links waits on a model. A note written further from
# a memory than this was not written with it, however alike their words.
CAPTURE_WINDOW_SECONDS = 10
# Where each memory `mnemos repair quarantine-tool-written` moved came from, so
# `--undo` returns exactly those and nothing else the quarantine holds.
QUARANTINED_KEY = "quarantined_tool_written"

# A standing memory (v14) is how the human wants the agent to work in every
# session, not just now. Only the agent marks one, as a typed choice: nothing
# reads it from the words. It opens the briefing and is exempt from decay while
# it is marked. A standing rule is obeyed, not recalled, so every usage signal
# (reinforcement, recency, recall) works against it; the mark is what keeps it.
#
# The mark lives only in these columns, signed by whoever last marked or
# unmarked it, and when. They are not part of ``Engram.to_dict()``, so no save
# of a memory, by this code or by older code, ever writes them: only a capture
# that marks, ``set_standing``, and a correction carrying the mark to the
# memory that replaces it do.
STANDING_COLUMNS = ("standing", "standing_by", "standing_session", "standing_at")

# The link a correction writes from the memory that replaces to the memory it
# replaced (v13). Mechanism-formed, like co_activated: nothing classified it,
# the agent's correction made it. It never carries activation, because what it
# points at is archived and nothing reaches an archived memory through a link.
SUPERSEDES = "supersedes"
# What a correction's version entry says changed: the words it replaced.
CORRECTION_VERSION_REASON = "correction"

# How long memory_trace keeps a row: one per tool call, so "what did the agent
# see" has an answer for as long as anyone is likely to ask it.
TRACE_KEEP_DAYS = 90

VALID_FUNCTIONAL_TYPES = {
    "working",
    "preference",
    "fact",
    "decision",
    "commitment",
    "open_question",
    "correction",
    "profile",
    "project",
}

VALID_SESSION_STATUSES = {"active", "paused", "closed"}

# Upper bound on hypomnema rows considered for ranking. Deliberately far
# above any healthy continuity store: this is a backstop against a
# pathological store, not a relevance filter. See search_hypomnema.
_MAX_HYPOMNEMA_CANDIDATES = 5000

# Handoffs belong to the harness session that wrote them (``author_session``).
# Several sessions often work one scope at once, and each keeps its own note
# instead of replacing whatever another session left. At most this many
# sessions' notes stay active per scope; beyond it the oldest is retired, with
# its prose kept in history.
HANDOFF_SESSIONS_KEPT = 8
# A starting session is handed at most this many notes (its own or the newest
# whole, the rest as short lines), and other sessions' notes only while they
# are this recent. The continuity layer is scarce by design.
PACKET_HANDOFFS = 3
LIVE_HANDOFF_HOURS = 72

# Why a memory is in the archive, when nobody chose it: decay took it there
# ("low_accessibility" is archive_engram's default, and the reason the first
# decay pass wrote). Only these may come back through recall. A memory the
# agent forgot, or replaced with a correction, was put there on purpose, and
# stays until someone who can see it decides otherwise.
FADED_ARCHIVE_REASONS = ("decay_below_threshold", "low_accessibility")

VALID_HYPO_SOURCES = {"observed", "synthesized", "co-formed"}
VALID_HYPO_ENTRY_KINDS = {
    "continuity",
    "handoff",
    "maintenance_report",
}
VALID_HYPO_AUTHORSHIP = {"agent", "system", "coauthored", "unknown"}
VALID_HYPO_DOMAINS = {
    "foundational",
    "identity",
    "recurring",
    "long-arc",
    "topical",
    "situational",
}

# A continuity note's memory, when it has one, for a query that joins it as
# ``m`` to the note as ``h``: the memory it is paired with, which one capture
# wrote together with it (``graduated_to_engram_id``; promotion pairs a note
# with a memory made from its words the same way). ``related_engram_id`` is
# only a reference: a note written through the advanced tools can name a
# memory it interprets or summarises, and that memory is never its pair.
_NOTE_MEMORY_JOIN = "LEFT JOIN engrams m ON m.id = h.graduated_to_engram_id"
# A note shares its memory's fate. It is live while it is active and its
# memory, if it has one, has neither gone quiet nor faded into the archive:
# when decay takes the memory there the note stops showing, and when recall
# wakes it or resharpen restores it the note is back. Nothing is copied from
# one to the other, so the two cannot fall out of step. A note with no memory
# of its own (a handoff, a report, an answer kept open, a note that only
# references a memory) is live while it is active.
_NOTE_LIVE = "h.active = 1 AND (m.id IS NULL OR m.state NOT IN ('dormant', 'archived'))"
# A note's own words, for a query that holds it as ``h``: its content without
# a reflection added to it later (the runtime appends one after a blank line,
# as "What this changed: ...").
_NOTE_WORDS = (
    "CASE WHEN instr(h.content, char(10) || char(10) || 'What this changed:') > 0 "
    "THEN rtrim(substr(h.content, 1, "
    "instr(h.content, char(10) || char(10) || 'What this changed:') - 1)) "
    "ELSE h.content END"
)
# Whether a memory's text (``{text}``) is a note's words as a capture writes
# them: the same words, or the words and the context the capture adds after a
# blank line ("Context: ...").
_CAPTURED_AS = (
    f"(trim({{text}}) = trim({_NOTE_WORDS}) "
    f"OR substr({{text}}, 1, length({_NOTE_WORDS}) + 11) = "
    f"{_NOTE_WORDS} || char(10) || char(10) || 'Context: ')"
)

# Allowed column names for engrams table — prevents SQL injection via to_dict() keys
_ENGRAM_COLUMNS = frozenset({
    "id", "content", "content_at_encoding", "impact", "impact_source",
    "author_kind", "author_model", "author_session",
    "resolution", "kind", "tags",
    "schema_refs", "strength", "stability", "accessibility", "encoding_context",
    "source", "lineage", "owner_agent_id", "person_id", "project_scope",
    "visibility", "state", "created_at",
    "last_accessed", "access_count", "reconsolidation_count",
})

# Set on each VersionRef: the store files that hold it as a version row.
_VERSION_STORED_IN = "_mnemos_stored_in"


def _mark_version_stored(version: VersionRef, store_key: str) -> None:
    stored_in = getattr(version, _VERSION_STORED_IN, frozenset())
    if store_key not in stored_in:
        setattr(version, _VERSION_STORED_IN, stored_in | {store_key})


def _version_stored_in(version: VersionRef, store_key: str) -> bool:
    return store_key in (getattr(version, _VERSION_STORED_IN, None) or ())


# Allowed column names for beliefs table
_BELIEF_COLUMNS = frozenset({
    "id", "agent_id", "content", "confidence", "domain", "created_at",
    "last_revised", "last_challenged", "revision_history", "superseded_by",
    "supporting_engram_ids", "source",
})

# Columns a store from before 0.2 may be missing, added on open by
# `_reconcile_columns`. Every definition (and its default) must match the
# corresponding column in SQL_CREATE_TABLES below. Only columns that carry a
# default are listed — a NOT NULL column without one cannot be added to a table
# that already has rows, and those (id, content, created_at, …) are structural
# originals present in any store that has the table.
_RECONCILABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "engrams": [
        ("impact", "impact TEXT NOT NULL DEFAULT ''"),
        ("impact_source", "impact_source TEXT NOT NULL DEFAULT ''"),
        ("author_kind", "author_kind TEXT NOT NULL DEFAULT 'unknown'"),
        ("author_model", "author_model TEXT NOT NULL DEFAULT ''"),
        ("author_session", "author_session TEXT NOT NULL DEFAULT ''"),
        ("resolution", "resolution REAL NOT NULL DEFAULT 1.0"),
        ("kind", "kind TEXT NOT NULL DEFAULT 'episodic'"),
        ("tags", "tags TEXT NOT NULL DEFAULT '[]'"),
        ("schema_refs", "schema_refs TEXT NOT NULL DEFAULT '[]'"),
        ("strength", "strength REAL NOT NULL DEFAULT 0.5"),
        ("stability", "stability REAL NOT NULL DEFAULT 0.1"),
        ("accessibility", "accessibility REAL NOT NULL DEFAULT 0.5"),
        ("encoding_context", "encoding_context TEXT NOT NULL DEFAULT '{}'"),
        ("source", "source TEXT NOT NULL DEFAULT '{}'"),
        ("lineage", "lineage TEXT NOT NULL DEFAULT '{}'"),
        ("owner_agent_id", "owner_agent_id TEXT NOT NULL DEFAULT 'default'"),
        ("person_id", "person_id TEXT"),
        ("project_scope", "project_scope TEXT"),
        ("visibility", "visibility TEXT NOT NULL DEFAULT 'private'"),
        ("state", "state TEXT NOT NULL DEFAULT 'active'"),
        ("access_count", "access_count INTEGER NOT NULL DEFAULT 0"),
        ("reconsolidation_count", "reconsolidation_count INTEGER NOT NULL DEFAULT 0"),
        ("standing", "standing INTEGER NOT NULL DEFAULT 0"),
        ("standing_by", "standing_by TEXT NOT NULL DEFAULT ''"),
        ("standing_session", "standing_session TEXT NOT NULL DEFAULT ''"),
        ("standing_at", "standing_at TEXT"),
    ],
    "beliefs": [
        ("source", "source TEXT NOT NULL DEFAULT ''"),
    ],
    "consolidation_log": [
        ("agent_id", "agent_id TEXT"),
        ("person_id", "person_id TEXT"),
        ("project_scope", "project_scope TEXT"),
    ],
    "versions": [
        ("author_model", "author_model TEXT NOT NULL DEFAULT ''"),
        ("author_session", "author_session TEXT NOT NULL DEFAULT ''"),
    ],
    "hypomnema_entries": [
        (
            "entry_kind",
            "entry_kind TEXT NOT NULL "
            "CHECK (entry_kind IN ('continuity', 'handoff', 'maintenance_report')) "
            "DEFAULT 'continuity'",
        ),
        (
            "authored_by",
            "authored_by TEXT NOT NULL "
            "CHECK (authored_by IN ('agent', 'system', 'coauthored', 'unknown')) "
            "DEFAULT 'unknown'",
        ),
        ("author_id", "author_id TEXT NOT NULL DEFAULT ''"),
        ("author_model", "author_model TEXT NOT NULL DEFAULT ''"),
        ("author_session", "author_session TEXT NOT NULL DEFAULT ''"),
        ("last_surfaced_at", "last_surfaced_at TEXT"),
        ("surface_count", "surface_count INTEGER NOT NULL DEFAULT 0"),
    ],
}


# What the reflection queue can ask. 'reaffirm' (schema v11) asks whether a
# belief the agent holds is still true; before it, a reaffirmation was filed as
# 'belief' and the unique index below, which counts answered asks, blocked it
# for good behind the answered ask that formed the belief.
REFLECTION_KINDS = ("impact", "lesson", "belief", "contradiction", "reaffirm")

# The reflection queue's table, kept apart from the schema script so that a
# queue from before v11 can be rebuilt from it: SQLite cannot widen a CHECK in
# place (see EngramStore._allow_reaffirm_asks). Must match REFLECTION_KINDS.
_REFLECTION_QUEUE_TABLE = """CREATE TABLE IF NOT EXISTS reflection_queue (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    kind TEXT NOT NULL
        CHECK (kind IN ('impact', 'lesson', 'belief', 'contradiction', 'reaffirm')),
    target_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    -- How many times this has been shown. An agent that has declined to
    -- answer three times is answering; stop asking.
    surfaced_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    answered_at TEXT,
    answer TEXT
)"""
_REFLECTION_QUEUE_COLUMNS = (
    "id, agent_id, person_id, project_scope, kind, target_id, prompt, excerpt, "
    "surfaced_count, created_at, expires_at, answered_at, answer"
)
# Its indexes. The unique one allows one ask per memory and kind, answered or
# not: asking again is nagging. A reaffirmation is asked again about the same
# belief (a month after the last one), so only a waiting one counts for it.
# Both keep their names: an older Mnemos runs CREATE ... IF NOT EXISTS under
# them when it opens a store, which leaves these alone.
_REFLECTION_QUEUE_INDEXES = (
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_unique
    ON reflection_queue(agent_id, person_id, project_scope, kind, target_id)
    WHERE kind != 'reaffirm' OR answered_at IS NULL""",
    """CREATE INDEX IF NOT EXISTS idx_reflection_pending
    ON reflection_queue(agent_id, person_id, project_scope, surfaced_count, created_at)
    WHERE answered_at IS NULL""",
)

SQL_CREATE_TABLES = """
-- Core engram storage
CREATE TABLE IF NOT EXISTS engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
    impact_source TEXT NOT NULL DEFAULT '',
    -- Who wrote the words (v12): agent, tool, system, import or unknown.
    -- No CHECK: SQLite cannot widen one in place, and a store must never
    -- refuse a row a newer Mnemos wrote.
    author_kind TEXT NOT NULL DEFAULT 'unknown',
    author_model TEXT NOT NULL DEFAULT '',
    author_session TEXT NOT NULL DEFAULT '',
    resolution REAL NOT NULL DEFAULT 1.0,
    kind TEXT NOT NULL DEFAULT 'episodic',
    tags TEXT NOT NULL DEFAULT '[]',
    schema_refs TEXT NOT NULL DEFAULT '[]',
    strength REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.1,
    accessibility REAL NOT NULL DEFAULT 0.5,
    encoding_context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '{}',
    lineage TEXT NOT NULL DEFAULT '{}',
    owner_agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT,
    project_scope TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0,
    -- Standing (v14): the agent marked this as how the human wants it to work
    -- in every session. A typed choice, never read from the words; signed by
    -- whoever last marked or unmarked it, and when (NULL: never marked).
    standing INTEGER NOT NULL DEFAULT 0,
    standing_by TEXT NOT NULL DEFAULT '',
    standing_session TEXT NOT NULL DEFAULT '',
    standing_at TEXT
);

-- Full-text search on engram content
CREATE VIRTUAL TABLE IF NOT EXISTS engrams_fts USING fts5(
    content,
    id UNINDEXED
);

-- Typed connections between engrams
CREATE TABLE IF NOT EXISTS connections (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.5,
    formed_at TEXT NOT NULL,
    formed_by TEXT NOT NULL DEFAULT 'encoding',
    PRIMARY KEY (source_id, target_id, relation)
);

-- Reconsolidation version history
CREATE TABLE IF NOT EXISTS versions (
    engram_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content_snapshot TEXT NOT NULL,
    resolution_at_version REAL NOT NULL,
    changed_at TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT 'reconsolidation',
    -- Who made the change, when someone did (v13): a correction's version
    -- entry is signed by whoever corrected. Empty for a change a pass made.
    author_model TEXT NOT NULL DEFAULT '',
    author_session TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (engram_id, version_num)
);

-- Which named session has already reinforced which memory: a memory is
-- reinforced at most once per session (retrieval/reconsolidation.py).
CREATE TABLE IF NOT EXISTS session_reinforcements (
    session_id TEXT NOT NULL,
    engram_id TEXT NOT NULL,
    reinforced_at TEXT NOT NULL,
    PRIMARY KEY (session_id, engram_id)
);

-- Beliefs
CREATE TABLE IF NOT EXISTS beliefs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.3,
    domain TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    last_revised TEXT NOT NULL,
    last_challenged TEXT NOT NULL,
    revision_history TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    supporting_engram_ids TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT ''
);

-- Hypomnema: scoped durable continuity that can revise before promotion
CREATE TABLE IF NOT EXISTS hypomnema_entries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    entry_kind TEXT NOT NULL DEFAULT 'continuity'
        CHECK (entry_kind IN ('continuity', 'handoff', 'maintenance_report')),
    authored_by TEXT NOT NULL DEFAULT 'unknown'
        CHECK (authored_by IN ('agent', 'system', 'coauthored', 'unknown')),
    author_id TEXT NOT NULL DEFAULT '',
    author_model TEXT NOT NULL DEFAULT '',
    author_session TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'observed'
        CHECK (source IN ('observed', 'synthesized', 'co-formed')),
    density REAL NOT NULL DEFAULT 0.5,
    domain TEXT NOT NULL DEFAULT 'topical'
        CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    foundational INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    revisions_json TEXT NOT NULL DEFAULT '[]',
    related_session_id TEXT,
    related_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    graduated_to_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    superseded_by TEXT REFERENCES hypomnema_entries(id),
    created_at TEXT NOT NULL,
    last_revised_at TEXT NOT NULL,
    last_challenged_at TEXT,
    last_surfaced_at TEXT,
    surface_count INTEGER NOT NULL DEFAULT 0
);

-- Functional memory sessions: the active conversational frame
CREATE TABLE IF NOT EXISTS memory_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'mcp',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

-- Functional memory: current working context before it becomes continuity
CREATE TABLE IF NOT EXISTS functional_memories (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES memory_sessions(id) ON DELETE SET NULL,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'working'
        CHECK (memory_type IN (
            'working', 'preference', 'fact', 'decision', 'commitment',
            'open_question', 'correction', 'profile', 'project'
        )),
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    needs_confirmation INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'agent_observed',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    promoted_to_hypomnema_id TEXT REFERENCES hypomnema_entries(id) ON DELETE SET NULL
);

-- Emotional state history
CREATE TABLE IF NOT EXISTS emotional_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL DEFAULT 'default',
    curiosity REAL NOT NULL,
    restlessness REAL NOT NULL,
    warmth REAL NOT NULL,
    clarity REAL NOT NULL,
    creative_flow REAL NOT NULL,
    isolation REAL NOT NULL,
    timestamp TEXT NOT NULL
);

-- Agent identity
CREATE TABLE IF NOT EXISTS agent_identity (
    agent_id TEXT PRIMARY KEY,
    kernel_id TEXT NOT NULL,
    invariants TEXT NOT NULL DEFAULT '{}',
    evolution_rules TEXT NOT NULL DEFAULT '{}',
    epoch_state TEXT NOT NULL DEFAULT '{}',
    epoch_history TEXT NOT NULL DEFAULT '[]',
    memory_profile TEXT NOT NULL DEFAULT '{}'
);

-- Archived engrams (cold storage)
CREATE TABLE IF NOT EXISTS archive (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    kind TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    archived_at TEXT NOT NULL,
    archive_reason TEXT NOT NULL DEFAULT 'low_accessibility',
    final_accessibility REAL NOT NULL DEFAULT 0.0
);

-- Consolidation audit log
CREATE TABLE IF NOT EXISTS consolidation_log (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    person_id TEXT,
    project_scope TEXT,
    pass_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}'
);

-- Reflection queue: work the agent does on its own memory.
--
-- Mnemos never calls a model. Consolidation that needs judgement — what a
-- fading memory taught, whether a pattern is really a belief — is proposed
-- here by maintenance and performed by the agent itself, in its own turn and
-- its own words, through mnemos_reflect. The server proposes; it never
-- invents the answer.
""" + _REFLECTION_QUEUE_TABLE + """;

-- Host mutation replay ledger. The row is committed in the same SQLite
-- transaction as the Core mutation it describes, so a host can safely retry
-- after a timeout or process crash without applying the mutation twice.
CREATE TABLE IF NOT EXISTS host_mutations (
    host_namespace TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    operation TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    result_json TEXT,
    completed_at TEXT,
    PRIMARY KEY (host_namespace, idempotency_key)
);

-- One row per tool call (v12): which tool, in which session, signed by which
-- model, and the ids it showed or returned and the ids it wrote, so "what did
-- the agent actually see" has an answer. Ids only, never text: a forgotten
-- memory leaves no copy here. Rows older than TRACE_KEEP_DAYS are dropped.
CREATE TABLE IF NOT EXISTS memory_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    tool TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    session TEXT NOT NULL DEFAULT '',
    author_model TEXT NOT NULL DEFAULT '',
    read_ids TEXT NOT NULL DEFAULT '[]',
    written_ids TEXT NOT NULL DEFAULT '[]'
);

""" + ";\n".join(_REFLECTION_QUEUE_INDEXES) + """;
CREATE INDEX IF NOT EXISTS idx_host_mutations_completed
    ON host_mutations(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_trace_at ON memory_trace(at);
CREATE INDEX IF NOT EXISTS idx_memory_trace_session ON memory_trace(session, at);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_engrams_state ON engrams(state);
CREATE INDEX IF NOT EXISTS idx_engrams_accessibility ON engrams(accessibility DESC);
CREATE INDEX IF NOT EXISTS idx_engrams_kind ON engrams(kind);
CREATE INDEX IF NOT EXISTS idx_engrams_owner ON engrams(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_engrams_scope
    ON engrams(owner_agent_id, person_id, project_scope, state);
CREATE INDEX IF NOT EXISTS idx_engrams_last_accessed ON engrams(last_accessed);
CREATE INDEX IF NOT EXISTS idx_connections_source ON connections(source_id);
CREATE INDEX IF NOT EXISTS idx_connections_target ON connections(target_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_scope
    ON consolidation_log(agent_id, person_id, project_scope, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_beliefs_domain ON beliefs(agent_id, domain);
CREATE INDEX IF NOT EXISTS idx_hypomnema_scope_revised
    ON hypomnema_entries(agent_id, person_id, project_scope, last_revised_at DESC)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_hypomnema_promotion
    ON hypomnema_entries(agent_id, project_scope, created_at)
    WHERE active = 1 AND graduated_to_engram_id IS NULL;
-- A note records its pair (graduated_to_engram_id) and any memory it only
-- references (related_engram_id); these make the memory-to-note direction a
-- lookup (see EngramStore.capture_pair).
CREATE INDEX IF NOT EXISTS idx_hypomnema_related_engram
    ON hypomnema_entries(related_engram_id);
CREATE INDEX IF NOT EXISTS idx_hypomnema_graduated_engram
    ON hypomnema_entries(graduated_to_engram_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hypomnema_one_active_handoff
    ON hypomnema_entries(agent_id, person_id, project_scope, author_session)
    WHERE active = 1 AND entry_kind = 'handoff';
CREATE INDEX IF NOT EXISTS idx_memory_sessions_scope
    ON memory_sessions(agent_id, person_id, project_scope, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_functional_scope
    ON functional_memories(agent_id, person_id, project_scope, updated_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_functional_session
    ON functional_memories(session_id, updated_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_functional_review
    ON functional_memories(agent_id, person_id, project_scope, updated_at DESC)
    WHERE is_deleted = 0 AND needs_confirmation = 1;
CREATE INDEX IF NOT EXISTS idx_emotional_history_agent ON emotional_state_history(agent_id, timestamp);
"""
# Recall's meaning index (v15), owned by the embedding index: see there.
SQL_CREATE_TABLES += PASSAGE_TABLE_SQL.strip() + ";\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _written_together(note_at: str | None, memory_at: str | None) -> bool:
    """Whether a note and a memory were written within one capture call
    (``CAPTURE_WINDOW_SECONDS`` of each other). An unreadable time proves
    nothing, so it is not."""

    moments = []
    for timestamp in (note_at, memory_at):
        try:
            moment = datetime.fromisoformat(timestamp or "")
        except (TypeError, ValueError):
            return False
        moments.append(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))
    return abs((moments[0] - moments[1]).total_seconds()) <= CAPTURE_WINDOW_SECONDS


def _written_since(timestamp: str | None, cutoff: datetime) -> bool:
    """Whether an ISO timestamp is at or after ``cutoff``; unreadable is not."""

    try:
        moment = datetime.fromisoformat(timestamp or "")
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= cutoff


def handoff_retirement(note: dict[str, Any]) -> str | None:
    """Why a handoff is no longer in use, when recall still searches it:
    ``superseded`` (a newer handoff from its session replaced it) or
    ``retired`` (more sessions left handoffs than are kept). None for one in
    use, and for one taken out of use on purpose: forgotten, or archived by a
    correction. Read from its revision trail, which every retirement writes."""
    if note.get("active"):
        return None
    revisions = note.get("revisions") or []
    reason = str((revisions[-1] or {}).get("reason", "")) if revisions else ""
    if reason.startswith("archived"):
        return None
    if reason.startswith("retired"):
        return "retired"
    if reason.startswith("superseded") or note.get("superseded_by"):
        return "superseded"
    return None


def _new_id() -> str:
    return str(uuid.uuid4())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _split_tags(tags: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    return [str(tag).strip() for tag in tags if str(tag).strip()]


def _tokenize(text: str) -> set[str]:
    clean = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {token for token in clean.split() if len(token) > 2}


def _lexical_score(query: str, text: str) -> float:
    query_terms = _tokenize(query)
    if not query_terms:
        return 0.0
    # Scored on the words that mean something in the query, as recall searches
    # (#78). Counting "for" and "her", a note sharing only those tied with the
    # note the query was about. A query of nothing but common words keeps them.
    query_terms = {term for term in query_terms if not is_common(term)} or query_terms
    text_terms = _tokenize(text)
    if not text_terms:
        return 0.0
    return len(query_terms & text_terms) / max(1, len(query_terms))


def legacy_author_kinds(conn: sqlite3.Connection) -> dict[str, str]:
    """Who wrote the memories a store held before it recorded authorship.

    Only what a row carries decides, and anything it cannot show is left
    ``unknown``, never guessed:

    - ``tool``: tagged ``session-indexed``, the transcript indexer's mark. A
      lesson distilled from indexer output inherits the tag, and it is that
      model's words too: a lesson copies the impact it was drawn from.
    - ``agent``: a capture or correction the agent made through its own
      tools (a ``session`` memory tagged ``continuity``, which the simple
      runtime puts on everything it captures, while the indexer, the bridge
      and the advanced tools do not); and a lesson whose words are exactly
      the impact of a memory it was distilled from, when that impact is the
      agent's (``impact_source`` 'agent') on the agent's own memory, or is
      itself an agent's lesson.

    Returns the ids this labels ``agent`` or ``tool``; every other id is
    ``unknown``. Reads only.
    """

    rows: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id, tags, source, impact, impact_source, content FROM engrams"
    ):
        tags = _decode_json(row[1], [])
        source = _decode_json(row[2], {})
        rows[row[0]] = {
            "tags": {tag for tag in tags if isinstance(tag, str)} if isinstance(tags, list) else set(),
            "type": source.get("type") if isinstance(source, dict) else None,
            "impact": (row[3] or "").strip(),
            "impact_source": row[4] or "",
            "content": (row[5] or "").strip(),
        }

    kinds: dict[str, str] = {}
    for engram_id, row in rows.items():
        if "session-indexed" in row["tags"]:
            kinds[engram_id] = "tool"
        elif row["type"] == "session" and "continuity" in row["tags"]:
            kinds[engram_id] = "agent"

    sources: dict[str, list[str]] = {}
    for source_id, target_id in conn.execute(
        "SELECT source_id, target_id FROM connections WHERE relation = 'distilled_into'"
    ):
        sources.setdefault(target_id, []).append(source_id)
    lessons = [
        engram_id for engram_id, row in rows.items()
        if engram_id not in kinds and row["type"] == "reflection"
        and {"lesson", "distilled"} & row["tags"] and row["content"]
    ]
    # A lesson can be drawn from another lesson, so this runs until nothing
    # more is placed.
    placed = True
    while placed:
        placed = False
        for engram_id in lessons:
            if engram_id in kinds:
                continue
            for source_id in sources.get(engram_id, []):
                source = rows.get(source_id)
                if source is None or kinds.get(source_id) != "agent":
                    continue
                if source["impact"] != rows[engram_id]["content"]:
                    continue
                if source["impact_source"] == "agent" or source["type"] == "reflection":
                    kinds[engram_id] = "agent"
                    placed = True
                    break
    return kinds


class EngramStore:
    """SQLite-backed storage for Mnemos engrams, beliefs, and identity.

    NOT thread-safe. Each thread should use its own EngramStore instance,
    or callers must synchronize access externally. SQLite WAL mode allows
    concurrent reads from separate connections, but writes must be serialized.

    Usage:
        store = EngramStore("~/.mnemos/memory.db")
        store.save_engram(engram)
        results = store.search_fts("debugging python")
        engram = store.get_engram("engram_abc123")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        private_root = Path.home() / ".mnemos"
        try:
            owned_directory = self.db_path.parent == private_root or self.db_path.parent.is_relative_to(private_root)
        except AttributeError:  # Python 3.8 compatibility for downstream imports
            owned_directory = str(self.db_path.parent).startswith(str(private_root))
        secure_directory(self.db_path.parent, force=owned_directory)
        self._conn: sqlite3.Connection | None = None
        self._transaction_depth = 0
        # Versions written in the open transaction, marked stored on commit.
        self._versions_written: list[VersionRef] = []
        self._init_db()
        self._record_code_version()

    def _record_code_version(self) -> None:
        """Raise the store's minimum code version to this code's, on every
        writable open, after the migrations.

        From then on, code older than this stops maintaining the store and
        reinforcing its memories (see mnemos/code_version.py). Only the simple
        runtime used to record it, while the session-start hook, `mnemos
        search`, the bridge, the advanced server and the shared pool wrote
        through their own stores by these rules, so a store could stay marked
        for older code that then never stood down. The raise never lowers the
        value. If another process holds the write lock, the store still opens,
        and the next opener raises it. A read-only store records nothing.
        """
        try:
            self.raise_min_code_version(MAINTENANCE_CODE_VERSION)
        except sqlite3.OperationalError:
            pass

    def _secure_sqlite_files(self) -> None:
        """Keep the database and transient WAL files private."""

        for suffix in ("", "-wal", "-shm"):
            secure_file(f"{self.db_path}{suffix}")

    def _init_db(self) -> None:
        """Initialize database with schema.

        Reconciliation runs **before** the schema script, not after. The script
        creates indexes on engram columns (`state`, `accessibility`, …); on a
        store written by an earlier Mnemos whose `engrams` table lacks those
        columns, `CREATE INDEX` raises at open time — so a bare `ALTER` after
        `executescript` never even runs. Adding the missing columns first lets
        the rest of the script (which is all `IF NOT EXISTS`) apply cleanly.
        """
        conn = self._get_conn()
        self._backup_before_migration(conn)
        self._reconcile_columns(conn)
        self._rebuild_handoff_index(conn)
        self._allow_reaffirm_asks(conn)
        conn.executescript(SQL_CREATE_TABLES)
        self._classify_legacy_hypomnema(conn)
        self._backfill_engram_scopes(conn)
        self._label_engram_authors(conn)
        self._link_older_capture_pairs(conn)
        # Only ever raised. A store newer code has stamped keeps its version
        # when older code opens it, as min_code_version does: stamping this
        # code's version over it would tell the newer code its own migration
        # never ran.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "UPDATE meta SET value = ? "
            "WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
            (str(SCHEMA_VERSION), SCHEMA_VERSION),
        )
        self._commit()
        integrity = [
            row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"]:
            details = "; ".join(integrity[:10]) or "no result"
            raise RuntimeError(
                f"SQLite integrity check failed after migration: {details}"
            )

    def _backup_before_migration(self, conn: sqlite3.Connection) -> None:
        """Create a verified recovery point before altering an older schema."""
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('engrams', 'hypomnema_entries') LIMIT 1"
        ).fetchone()
        if not existing:
            return

        version_row = None
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        try:
            current_version = int(version_row[0]) if version_row else 0
        except (TypeError, ValueError):
            current_version = 0
        if current_version >= SCHEMA_VERSION:
            return
        from ..backup import create_backup

        backup_dir = self.db_path.parent / "backups"

        # One recovery point per migration, not one per opener.
        #
        # Several processes can open the same store at once — the CLI, a
        # session hook, a running agent — and each of them reaches this line
        # while the file is still at the old version, because none of them has
        # migrated it yet. Without this check each writes its own copy of the
        # same bytes; one upgrade here left five identical 146 MB files stamped
        # three seconds apart.
        #
        # An existing file at the final name is already a verified copy:
        # `create_backup` writes to a temp name and only renames after
        # `check_database` passes. So presence alone is enough, and a partly
        # migrated database never overwrites the good pre-migration copy.
        existing_backups = list(
            backup_dir.glob(f"{self.db_path.stem}.pre-v{SCHEMA_VERSION}-*.db")
        )
        if any(path.is_file() and path.stat().st_size > 0 for path in existing_backups):
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = backup_dir / f"{self.db_path.stem}.pre-v{SCHEMA_VERSION}-{stamp}.db"
        create_backup(self.db_path, destination, source_connection=conn)

    @staticmethod
    def _rebuild_handoff_index(conn: sqlite3.Connection) -> None:
        """Allow one active handoff per session instead of one per scope.

        Up to v9 the index kept a single active handoff per scope, so parallel
        sessions replaced each other's notes. The index keeps its name on
        purpose: an older Mnemos still running in a session opened before the
        upgrade (or installed elsewhere) runs ``CREATE UNIQUE INDEX IF NOT
        EXISTS`` under that name when it opens the store, which is a no-op
        while the name exists. Under a new name it would try to build the old
        per-scope index over several active handoffs and fail to open the
        store at all. The schema script recreates it with the session column.
        """

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_hypomnema_one_active_handoff'"
        ).fetchone()
        if row is not None and "author_session" not in (row[0] or ""):
            conn.execute("DROP INDEX IF EXISTS idx_hypomnema_one_active_handoff")

    @staticmethod
    def _allow_reaffirm_asks(conn: sqlite3.Connection) -> None:
        """Let the reflection queue hold reaffirmation asks (schema v11).

        Up to v10 the queue's CHECK allowed four kinds, and SQLite cannot widen
        a CHECK in place, so the table is rebuilt from
        ``_REFLECTION_QUEUE_TABLE`` with every row kept. The old table goes
        with its indexes, and they are made again under their old names in
        the same transaction, the unique one now counting only a waiting
        reaffirmation: no other process ever sees the queue without it, so
        none can slip in a duplicate that would stop the index being built.
        Nothing references the queue (no foreign key, trigger or view), so
        renaming the old table first rewrites nothing else.

        An older Mnemos opening a v11 store finds the table and the indexes by
        name and leaves them alone. A unique index still in the old shape
        (dropped and remade by hand, say) is rebuilt the same way; that shape
        allows no duplicate the new one would refuse.
        """

        def outdated() -> tuple[bool, bool]:
            table = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'reflection_queue'"
            ).fetchone()
            index = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_reflection_unique'"
            ).fetchone()
            return (
                table is not None and "'reaffirm'" not in (table[0] or ""),
                index is not None and "reaffirm" not in (index[0] or ""),
            )

        if not any(outdated()):
            return
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have rebuilt it while this one waited.
            table_is_old, index_is_old = outdated()
            if table_is_old:
                conn.execute("ALTER TABLE reflection_queue RENAME TO reflection_queue_v10")
                conn.execute(_REFLECTION_QUEUE_TABLE)
                conn.execute(
                    f"INSERT INTO reflection_queue ({_REFLECTION_QUEUE_COLUMNS}) "
                    f"SELECT {_REFLECTION_QUEUE_COLUMNS} FROM reflection_queue_v10"
                )
                conn.execute("DROP TABLE reflection_queue_v10")
            if table_is_old or index_is_old:
                conn.execute("DROP INDEX IF EXISTS idx_reflection_unique")
                for statement in _REFLECTION_QUEUE_INDEXES:
                    conn.execute(statement)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _classify_legacy_hypomnema(conn: sqlite3.Connection) -> None:
        """Classify legacy rows without pretending ambiguous prose was agent-written."""

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(hypomnema_entries)")
        }
        if not {"entry_kind", "authored_by", "author_id"}.issubset(columns):
            return
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET entry_kind = 'maintenance_report',
                authored_by = 'system',
                author_id = 'mnemos'
            WHERE authored_by = 'unknown'
              AND tags_json LIKE '%"dream-journal"%'
            """
        )
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET authored_by = 'coauthored'
            WHERE authored_by = 'unknown' AND source = 'co-formed'
            """
        )

    @staticmethod
    def _label_engram_authors(conn: sqlite3.Connection) -> None:
        """Say who wrote the memories stored before authorship was (v12).

        Runs once per store, in one transaction with the record of its
        counts (``AUTHORS_LABELED_KEY``), which is what keeps it from running
        again: from then on a memory's author is set when it is written, and
        never inferred later. A row older code writes afterwards stays
        ``unknown``. The rule is ``legacy_author_kinds``; every row it cannot
        place keeps the column's default, ``unknown``.
        """

        done = "SELECT 1 FROM meta WHERE key = ?"
        if conn.execute(done, (AUTHORS_LABELED_KEY,)).fetchone():
            return
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have labelled the store while this one waited.
            if not conn.execute(done, (AUTHORS_LABELED_KEY,)).fetchone():
                kinds = legacy_author_kinds(conn)
                conn.executemany(
                    "UPDATE engrams SET author_kind = ? "
                    "WHERE id = ? AND author_kind = 'unknown'",
                    [(kind, engram_id) for engram_id, kind in kinds.items()],
                )
                counts = {
                    row[0]: row[1] for row in conn.execute(
                        "SELECT author_kind, COUNT(*) FROM engrams GROUP BY author_kind"
                    )
                }
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (AUTHORS_LABELED_KEY, _encode_json({"at": _utc_now(), "counts": counts})),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _link_older_capture_pairs(conn: sqlite3.Connection) -> None:
        """Pair the capture notes that name their memory only as a reference
        (v13).

        A note and a memory are a pair only when one capture wrote them
        together, and the pair is recorded as ``graduated_to_engram_id``. A
        note with only ``related_engram_id`` may be a capture from before the
        pair was recorded, or a note that interprets or summarises a memory
        (the advanced tools write those), and nothing kept at write time tells
        them apart. What a capture leaves does: such a note is paired with the
        memory it names when its own words are the memory's words as a
        capture writes them (``_CAPTURED_AS``), in the same scope, and the two
        were written within one capture call (``CAPTURE_WINDOW_SECONDS``). A
        note written later with the memory's very words only references it.

        Runs once per store, in one transaction with the record of what it
        linked (``CAPTURE_PAIRS_LINKED_KEY``), which keeps this whole-store
        pass from running again. It does not end the pairing: code from
        before pairs can still take a capture after this code has opened the
        store, and writes only the reference. Each maintenance cycle pairs
        those in its scope by the same rule (``link_capture_pairs``).
        """
        done = "SELECT 1 FROM meta WHERE key = ?"
        if conn.execute(done, (CAPTURE_PAIRS_LINKED_KEY,)).fetchone():
            return
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have linked them while this one waited.
            if not conn.execute(done, (CAPTURE_PAIRS_LINKED_KEY,)).fetchone():
                linked: dict[str, int] = {}
                for _note, _memory, scope in EngramStore._pair_capture_notes(conn):
                    linked[scope] = linked.get(scope, 0) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (CAPTURE_PAIRS_LINKED_KEY, _encode_json({"at": _utc_now(), "linked": linked})),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _pair_capture_notes(
        conn: sqlite3.Connection,
        scope: tuple[str, str, str] | None = None,
        *,
        live_only: bool = False,
    ) -> list[tuple[str, str, str]]:
        """Pair each continuity note that names its memory only as a reference
        with that memory, when one capture wrote the two together: the note's
        words are the memory's words as a capture writes them
        (``_CAPTURED_AS``), both are in one scope, and they were written
        within one capture call (``CAPTURE_WINDOW_SECONDS``). In ``scope``
        (agent, person, project) when given, else the whole store; with
        ``live_only``, only notes whose memory is live (neither quiet nor
        faded). Runs in the caller's transaction and returns what it linked:
        (note id, memory id, "agent/person/project")."""
        where = [
            "h.entry_kind = 'continuity'",
            "h.graduated_to_engram_id IS NULL",
            "e.owner_agent_id = h.agent_id",
            "e.person_id = h.person_id",
            "e.project_scope = h.project_scope",
        ]
        params: list[str] = []
        if scope is not None:
            where.insert(0, "h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?")
            params.extend(scope)
        if live_only:
            where.append("e.state NOT IN ('dormant', 'archived')")
        rows = conn.execute(
            f"""
            SELECT h.id, h.related_engram_id,
                   h.agent_id || '/' || h.person_id || '/' || h.project_scope AS scope,
                   h.created_at, e.created_at
            FROM hypomnema_entries h
            JOIN engrams e ON e.id = h.related_engram_id
            WHERE {' AND '.join(where)}
              AND ({_CAPTURED_AS.format(text='e.content')}
                   OR {_CAPTURED_AS.format(text='e.content_at_encoding')})
            """,
            params,
        ).fetchall()
        rows = [row for row in rows if _written_together(row[3], row[4])]
        conn.executemany(
            "UPDATE hypomnema_entries SET graduated_to_engram_id = ? "
            "WHERE id = ? AND graduated_to_engram_id IS NULL",
            [(row[1], row[0]) for row in rows],
        )
        return [(row[0], row[1], row[2]) for row in rows]

    def link_capture_pairs(
        self, *, agent_id: str, person_id: str, project_scope: str,
    ) -> list[str]:
        """Pair the capture notes in one scope that still name their memory
        only as a reference, by the rule the one-time pass uses
        (``_pair_capture_notes``), and return the notes paired.

        Code from before pairs can still take a capture after this code has
        opened a store (it may save, only not maintain), and it records only
        the reference; the one-time pass has run by then and does not run
        again. Maintenance calls this each cycle. Only notes whose memory is
        live: a quiet or faded memory's note waits until it wakes, which
        keeps the cycle's cost to the few notes that need it.
        """
        with self.transaction() as conn:
            linked = self._pair_capture_notes(
                conn, (agent_id, person_id, project_scope), live_only=True,
            )
        return [note for note, _memory, _scope in linked]

    @staticmethod
    def _backfill_engram_scopes(conn: sqlite3.Connection) -> None:
        """Backfill only legacy engrams with one unambiguous continuity scope.

        Ambiguous or unlinked legacy rows remain unscoped and are therefore
        quarantined from normal scoped reads. Guessing would risk disclosing a
        memory to the wrong person or project.
        """
        rows = conn.execute(
            """
            SELECT e.id, MIN(h.agent_id) AS agent_id,
                   MIN(h.person_id) AS person_id,
                   MIN(h.project_scope) AS project_scope,
                   COUNT(DISTINCT h.agent_id || char(31) || h.person_id || char(31) || h.project_scope) AS scopes
            FROM engrams e
            JOIN hypomnema_entries h
              ON h.related_engram_id = e.id OR h.graduated_to_engram_id = e.id
            WHERE e.person_id IS NULL OR e.person_id = ''
               OR e.project_scope IS NULL OR e.project_scope = ''
            GROUP BY e.id
            HAVING scopes = 1
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE engrams
                   SET owner_agent_id = ?, person_id = ?, project_scope = ?
                   WHERE id = ?""",
                (row["agent_id"], row["person_id"], row["project_scope"], row["id"]),
            )

    @staticmethod
    def _reconcile_columns(conn: sqlite3.Connection) -> None:
        """Add any expected column an older table is missing.

        `CREATE TABLE IF NOT EXISTS` never alters a table that already exists,
        so a database from before a column was added is not upgraded by the
        schema script alone — only `impact`/`impact_source` had one-off `ALTER`
        backfills, and a store missing any of the other 0.2 columns raised
        `OperationalError` in ordinary use. This adds every missing column with
        its schema default. It is idempotent (present columns are skipped), and
        it skips a table that does not exist yet (a fresh database — the schema
        script will create it in full).

        Every default here matches ``SQL_CREATE_TABLES``. Columns with no
        default (the structural originals: id, content, created_at, …) are not
        listed: they exist in any store old enough to have the table at all,
        and SQLite cannot ADD a NOT NULL column without a default to a
        populated table anyway.
        """
        for table, columns in _RECONCILABLE_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table absent — the schema script will create it whole
            for name, add_ddl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {add_ddl}")
                    # Some SQLite builds leave numeric defaults virtual after
                    # ALTER TABLE and then report those legacy rows as NULL
                    # during integrity_check. The column is brand new, so
                    # materialize its declared default for every old row.
                    _, marker, default_sql = add_ddl.partition(" DEFAULT ")
                    if marker:
                        conn.execute(
                            f"UPDATE {table} SET {name} = {default_sql}"
                        )

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create SQLite connection with WAL mode."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.set_trace_callback(
                lambda statement: self._secure_sqlite_files()
                if statement.lstrip().upper().startswith(("COMMIT", "END"))
                else None
            )
            self._secure_sqlite_files()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
        self._secure_sqlite_files()

    @contextmanager
    def transaction(self):
        """Run all nested store writes in one immediate SQLite transaction.

        Store methods historically committed their own small transactions.
        While this context is active those commits are deferred to the outer
        boundary. This is what lets a host mutation and its replay-ledger row
        become durable together.
        """

        conn = self._get_conn()
        outermost = self._transaction_depth == 0
        if outermost:
            self._begin_immediate()
        self._transaction_depth += 1
        try:
            yield conn
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                self._rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                try:
                    self._commit()
                except Exception:
                    self._rollback()
                    raise

    def _begin_immediate(self) -> None:
        """Begin a method-local transaction unless one is already managed."""

        if self._transaction_depth == 0:
            self._get_conn().execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        """Commit a method-local transaction, or defer to the managed one."""

        if self._transaction_depth == 0:
            self._get_conn().commit()
            self._mark_versions_written()

    def _rollback(self) -> None:
        """Roll back a method-local transaction, or let the outer owner do it."""

        if self._transaction_depth == 0:
            self._get_conn().rollback()
            self._versions_written = []

    def _mark_versions_written(self) -> None:
        """Mark the versions this transaction wrote as held by this store,
        now that they are; a rolled-back one is written by the next save."""
        written = getattr(self, "_versions_written", None)
        if written:
            key = self._version_store_key()
            for version in written:
                _mark_version_stored(version, key)
            self._versions_written = []

    def get_host_mutation(
        self, host_namespace: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Return one host replay-ledger row without changing it."""

        row = self._get_conn().execute(
            """SELECT * FROM host_mutations
               WHERE host_namespace = ? AND idempotency_key = ?""",
            (host_namespace, idempotency_key),
        ).fetchone()
        return dict(row) if row else None

    def begin_host_mutation(
        self,
        *,
        host_namespace: str,
        idempotency_key: str,
        protocol_version: int,
        operation: str,
        scope_json: str,
        request_sha256: str,
    ) -> None:
        """Claim an idempotency key inside the caller-owned transaction."""

        if self._transaction_depth == 0:
            raise RuntimeError("host mutation claims require a managed transaction")
        self._get_conn().execute(
            """INSERT INTO host_mutations(
                   host_namespace, idempotency_key, protocol_version, operation,
                   scope_json, request_sha256, result_json, completed_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
            (
                host_namespace,
                idempotency_key,
                protocol_version,
                operation,
                scope_json,
                request_sha256,
            ),
        )

    def complete_host_mutation(
        self,
        *,
        host_namespace: str,
        idempotency_key: str,
        result_json: str,
    ) -> None:
        """Store a host result in the same transaction as its Core effects."""

        if self._transaction_depth == 0:
            raise RuntimeError("host mutation completion requires a managed transaction")
        cursor = self._get_conn().execute(
            """UPDATE host_mutations
               SET result_json = ?, completed_at = ?
               WHERE host_namespace = ? AND idempotency_key = ?
                 AND completed_at IS NULL""",
            (result_json, _utc_now(), host_namespace, idempotency_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("host mutation claim was not active")

    # ── Engram CRUD ──

    def save_engram(self, engram: Engram) -> None:
        """Insert or update an engram.

        All operations (engram table, FTS index, connections, versions) are
        wrapped in a single transaction for atomicity.

        Versions are history, so they are only ever appended: the versions
        this store already holds are never written again, and a version the
        engram gained since it was loaded is numbered after the last one
        stored. A version is written only when the save changes the memory's
        content, impact or resolution (or first stores the memory); a snapshot
        taken without such a change records nothing, and is dropped.

        Who wrote the memory (``author_kind``, ``author_model``,
        ``author_session``) is written with its first save and never changed
        by a later one: authorship is a fact of the writing, not something a
        pass that loads and saves a memory can restate.
        """
        conn = self._get_conn()
        data = engram.to_dict()

        # Validate column names to prevent SQL injection
        safe_data = {k: v for k, v in data.items() if k in _ENGRAM_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in safe_data
            if k != "id" and k not in _AUTHOR_COLUMNS
        )

        try:
            self._begin_immediate()

            before = conn.execute(
                "SELECT content, impact, resolution FROM engrams WHERE id = ?",
                (engram.id,),
            ).fetchone()
            changed = before is None or (
                before["content"] != engram.content
                or (before["impact"] or "") != (engram.impact or "")
                or before["resolution"] != engram.resolution
            )

            conn.execute(
                f"INSERT INTO engrams ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                list(safe_data.values()),
            )

            # Update FTS index (atomic with engram)
            conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram.id,))
            conn.execute(
                "INSERT INTO engrams_fts (id, content) VALUES (?, ?)",
                (engram.id, engram.content),
            )

            # Save connections
            for conn_obj in engram.connections:
                self._save_connection_no_commit(conn, engram.id, conn_obj)

            # Append the versions this store does not hold yet. Every save
            # used to write the whole history again: one maintenance cycle on
            # a copy of a real store rewrote 29,379 version rows.
            self._append_versions_no_commit(conn, engram, changed=changed)

            self._commit()
        except Exception:
            self._rollback()
            raise

    def engram_visible_in_scope(
        self,
        engram_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> bool:
        """Whether an engram belongs to one exact person/project scope."""
        row = self._get_conn().execute(
            """SELECT 1 FROM engrams
               WHERE id = ? AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?""",
            (engram_id, agent_id, person_id, project_scope),
        ).fetchone()
        return row is not None

    def engram_state_in_scope(
        self,
        engram_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> str | None:
        """An engram's state when it belongs to one exact scope, else None.

        Recall's resonance asks this of every memory a connection leads to, to
        stay in scope and to leave quiet memories out (see ReactiveRetriever).
        """
        row = self._get_conn().execute(
            """SELECT state FROM engrams
               WHERE id = ? AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?""",
            (engram_id, agent_id, person_id, project_scope),
        ).fetchone()
        return None if row is None else row[0]

    def active_engram_ids(
        self, *, agent_id: str, person_id: str, project_scope: str
    ) -> set[str]:
        """IDs of the active engrams in one exact scope: what recall may seed from."""
        rows = self._get_conn().execute(
            """SELECT id FROM engrams
               WHERE state = 'active' AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?""",
            (agent_id, person_id, project_scope),
        ).fetchall()
        return {row[0] for row in rows}

    def recall_scopes(self) -> list[dict[str, str]]:
        """Every exact scope this store holds something recall can return in:
        a live memory, a handoff or a note. Rows a migration could not place
        (no person or project) are in none, since no scoped read reaches them.
        Sorted, as ``agent_id``/``person_id``/``project_scope`` dicts."""
        rows = self._get_conn().execute(
            """
            SELECT owner_agent_id, person_id, project_scope FROM engrams
            WHERE state IN ('active', 'dormant')
              AND person_id IS NOT NULL AND project_scope IS NOT NULL
            UNION
            SELECT agent_id, person_id, project_scope FROM hypomnema_entries
            WHERE person_id IS NOT NULL AND project_scope IS NOT NULL
            ORDER BY 1, 2, 3
            """
        ).fetchall()
        return [
            {"agent_id": row[0], "person_id": row[1], "project_scope": row[2]}
            for row in rows
        ]

    def live_engram_ids(
        self, *, agent_id: str, person_id: str, project_scope: str
    ) -> set[str]:
        """IDs of the memories recall may return in one exact scope: active,
        and dormant (found by its cue, and woken). What recall's meaning search
        scores, before it takes its top."""
        rows = self._get_conn().execute(
            """SELECT id FROM engrams
               WHERE state IN ('active', 'dormant') AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?""",
            (agent_id, person_id, project_scope),
        ).fetchall()
        return {row[0] for row in rows}

    def live_memory_texts(
        self, *, agent_id: str, person_id: str, project_scope: str
    ) -> list[tuple[str, str, str]]:
        """``(id, content, state)`` of every memory recall may return in one
        exact scope: the active ones and the dormant ones (a dormant memory is
        found by its cue and wakes). Newest first. Recall's meaning search
        scores exactly these, and its index is built from their words."""
        rows = self._get_conn().execute(
            """SELECT id, content, state FROM engrams
               WHERE state IN ('active', 'dormant') AND owner_agent_id = ?
                 AND person_id = ? AND project_scope = ?
               ORDER BY created_at DESC, id DESC""",
            (agent_id, person_id, project_scope),
        ).fetchall()
        return [(row[0], row[1] or "", row[2]) for row in rows]

    def live_memory_lessons(
        self, *, agent_id: str, person_id: str, project_scope: str
    ) -> dict[str, str]:
        """The lesson each memory recall may return holds in its impact, when
        the agent wrote one that its own words don't already say
        (``written_lesson``): memory id to lesson, newest first, active and
        dormant, in one exact scope. Recall finds a memory by what it taught
        as well as by what happened: these are ranked by their words beside
        the memories', and each is one more passage of its memory in the
        meaning index. A store without the impact columns has none."""
        from ..core.placeholders import written_lesson

        try:
            rows = self._get_conn().execute(
                """SELECT id, content, impact, impact_source FROM engrams
                   WHERE state IN ('active', 'dormant') AND owner_agent_id = ?
                     AND person_id = ? AND project_scope = ? AND impact != ''
                   ORDER BY created_at DESC, id DESC""",
                (agent_id, person_id, project_scope),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        lessons: dict[str, str] = {}
        for engram_id, content, impact, source in rows:
            lesson = written_lesson(content, impact, source or "")
            if lesson:
                lessons[engram_id] = lesson
        return lessons

    def get_engram_in_scope(
        self, engram_id: str, *, agent_id: str, person_id: str, project_scope: str
    ) -> Engram | None:
        """Load an engram only when its complete scope matches."""
        if not self.engram_visible_in_scope(
            engram_id, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope,
        ):
            return None
        return self.get_engram(engram_id)

    # ── Legacy rows without a scope ──

    def unscoped_engrams(self, agent_id: str) -> list[dict[str, Any]]:
        """This agent's engrams that the v6 scope migration could not place.

        ``_backfill_engram_scopes`` leaves them without a person or project,
        so every scoped read and every scoped maintenance pass skips them.
        ``mnemos repair quarantine-tool-written`` moves tool-written memories
        here too. Each row is classified so a human can decide what comes
        back:

        - ``indexer``: words a tool wrote (``author_kind`` 'tool': the
          transcript indexer's output), including rows the indexer itself
          labelled "lesson", and lessons distilled from its output, which
          copy its words. On a store from before authorship was recorded,
          the indexer's ``session-indexed`` tag says the same.
        - ``lessons``: what softening distilled from a fading memory — the
          target of a ``distilled_into`` edge, or tagged ``distilled``.
        - ``other``: anything written through another path.
        """
        authored = self.has_engram_column("author_kind")
        rows = self._get_conn().execute(
            f"""
            SELECT e.id, e.state, e.tags, e.content,
                   {"e.author_kind" if authored else "''"} AS author_kind,
                   EXISTS (
                       SELECT 1 FROM connections c
                       WHERE c.target_id = e.id AND c.relation = 'distilled_into'
                   ) AS distilled
            FROM engrams e
            WHERE e.owner_agent_id = ?
              AND (e.person_id IS NULL OR e.person_id = ''
                   OR e.project_scope IS NULL OR e.project_scope = '')
            ORDER BY e.created_at
            """,
            (agent_id,),
        ).fetchall()
        classified = []
        for row in rows:
            try:
                tags = set(json.loads(row["tags"] or "[]"))
            except (TypeError, ValueError):
                tags = set()
            tool = row["author_kind"] == "tool" if authored else "session-indexed" in tags
            if tool:
                kind = "indexer"
            elif row["distilled"] or "distilled" in tags:
                kind = "lessons"
            else:
                kind = "other"
            classified.append({
                "id": row["id"],
                "state": row["state"],
                "class": kind,
                "content": row["content"],
            })
        return classified

    def engram_scopes_in_use(self, agent_id: str) -> set[tuple[str, str]]:
        """Every person/project pair this agent holds scoped memory under."""
        conn = self._get_conn()
        scopes = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT DISTINCT person_id, project_scope FROM engrams "
                "WHERE owner_agent_id = ? AND person_id <> '' AND project_scope <> ''",
                (agent_id,),
            )
        }
        scopes.update(
            (row[0], row[1])
            for row in conn.execute(
                "SELECT DISTINCT person_id, project_scope FROM hypomnema_entries "
                "WHERE agent_id = ?",
                (agent_id,),
            )
        )
        return scopes

    def adopt_unscoped_engrams(
        self,
        engram_ids: list[str],
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> int:
        """Give quarantined legacy engrams one explicit scope, all or nothing.

        Whatever ids are passed, only rows that are still unscoped, owned by
        ``agent_id`` and not archived are touched. Returns how many moved.
        """
        ids = list(dict.fromkeys(engram_ids))
        adopted = 0
        with self.transaction() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    "UPDATE engrams SET person_id = ?, project_scope = ? "
                    "WHERE owner_agent_id = ? AND state != 'archived' "
                    "AND (person_id IS NULL OR person_id = '' "
                    "     OR project_scope IS NULL OR project_scope = '') "
                    f"AND id IN ({marks})",
                    (person_id, project_scope, agent_id, *chunk),
                )
                adopted += cursor.rowcount
        return adopted

    def has_engram_column(self, name: str) -> bool:
        """Whether the engrams table has this column: a store opened
        read-only is never migrated, so it may predate one."""
        return any(
            row[1] == name
            for row in self._get_conn().execute("PRAGMA table_info(engrams)")
        )

    def tool_written_engrams(
        self, *, agent_id: str, person_id: str, project_scope: str
    ) -> list[dict[str, Any]]:
        """The memories in one exact scope whose words a tool wrote.

        ``author_kind`` 'tool'. On a store opened read-only before it gained
        that column, the rule its migration applies decides instead
        (``legacy_author_kinds``), so a dry run reports what the repair will
        find. Each row says whether a continuity note points at it
        (``linked``): opening a store gives an unscoped memory a note links
        to back its note's scope, so moving one of those would not last.
        """
        conn = self._get_conn()
        scope = (agent_id, person_id, project_scope)
        if self.has_engram_column("author_kind"):
            rows = conn.execute(
                "SELECT id, state, content, created_at FROM engrams "
                "WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ? "
                "AND author_kind = 'tool' ORDER BY created_at, id",
                scope,
            ).fetchall()
        else:
            tools = {
                engram_id for engram_id, kind in legacy_author_kinds(conn).items()
                if kind == "tool"
            }
            rows = [
                row for row in conn.execute(
                    "SELECT id, state, content, created_at FROM engrams "
                    "WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ? "
                    "ORDER BY created_at, id",
                    scope,
                ).fetchall()
                if row["id"] in tools
            ]
        linked = {
            row[0] for row in conn.execute(
                "SELECT related_engram_id FROM hypomnema_entries "
                "WHERE related_engram_id IS NOT NULL "
                "UNION SELECT graduated_to_engram_id FROM hypomnema_entries "
                "WHERE graduated_to_engram_id IS NOT NULL"
            )
        }
        return [
            {
                "id": row["id"],
                "state": row["state"],
                "content": row["content"],
                "linked": row["id"] in linked,
            }
            for row in rows
        ]

    def quarantine_engrams(
        self,
        engram_ids: list[str],
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> int:
        """Move tool-written memories out of one scope into the legacy
        quarantine, all or nothing.

        Only rows still in exactly this scope, written by a tool, not
        archived and not pointed at by a continuity note are touched; whatever
        else is passed is left. They lose their person and project, as the v6
        migration left the rows it could not place. Nothing else about them
        changes: their words, links and history stay. Where each came from is
        recorded (``QUARANTINED_KEY``), so ``unquarantine_engrams`` returns
        exactly these; ``mnemos adopt-legacy --include indexer`` brings them
        back too, with every other tool-written memory the quarantine holds.
        Returns how many moved.
        """
        ids = list(dict.fromkeys(engram_ids))
        moved = 0
        with self.transaction() as conn:
            recorded = self.quarantined_tool_written()
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                where = (
                    "WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ? "
                    "AND author_kind = 'tool' AND state != 'archived' "
                    "AND id NOT IN (SELECT related_engram_id FROM hypomnema_entries "
                    "               WHERE related_engram_id IS NOT NULL) "
                    "AND id NOT IN (SELECT graduated_to_engram_id FROM hypomnema_entries "
                    "               WHERE graduated_to_engram_id IS NOT NULL) "
                    f"AND id IN ({marks})"
                )
                params = (agent_id, person_id, project_scope, *chunk)
                for (engram_id,) in conn.execute(f"SELECT id FROM engrams {where}", params):
                    recorded[engram_id] = [agent_id, person_id, project_scope]
                moved += conn.execute(
                    f"UPDATE engrams SET person_id = NULL, project_scope = NULL {where}",
                    params,
                ).rowcount
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (QUARANTINED_KEY, _encode_json(recorded)),
            )
        return moved

    def quarantined_tool_written(self) -> dict[str, list[str]]:
        """What ``quarantine_engrams`` moved: id -> [agent, person, project]
        it came from. An id stays listed after it comes back."""
        value = _decode_json(self.get_meta(QUARANTINED_KEY), {})
        return {
            str(engram_id): list(scope) for engram_id, scope in value.items()
            if isinstance(scope, list) and len(scope) == 3
        } if isinstance(value, dict) else {}

    def unquarantine_engrams(self, engram_ids: list[str]) -> int:
        """Return memories ``quarantine_engrams`` moved to the scope each came
        from, all or nothing. Only a listed memory still without a scope and
        not archived moves. Returns how many came back."""
        recorded = self.quarantined_tool_written()
        by_scope: dict[tuple[str, str, str], list[str]] = {}
        for engram_id in dict.fromkeys(engram_ids):
            if engram_id in recorded:
                by_scope.setdefault(tuple(recorded[engram_id]), []).append(engram_id)
        back = 0
        with self.transaction():
            for (agent_id, person_id, project_scope), ids in by_scope.items():
                back += self.adopt_unscoped_engrams(
                    ids, agent_id=agent_id, person_id=person_id,
                    project_scope=project_scope,
                )
        return back

    def get_engram(self, engram_id: str) -> Engram | None:
        """Load an engram by ID, including connections and versions."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM engrams WHERE id = ?", (engram_id,)
        ).fetchone()
        if row is None:
            return None

        engram = Engram.from_dict(dict(row))

        # Load connections
        engram.connections = self.get_connections(engram_id)

        # Load versions
        engram.versions = self._get_versions(engram_id)

        return engram

    def get_active_engrams(
        self,
        agent_id: str | None = "default",
        limit: int = 1000,
        load_connections: bool = True,
        person_id: str | None = None,
        project_scope: str | None = None,
        author_kind: str | None = None,
    ) -> list[Engram]:
        """Get all active engrams for an agent, sorted by accessibility.

        Args:
            agent_id: Which agent's engrams to return. If None, returns all
                agents' active engrams (useful for shared DB consolidation).
            load_connections: If True, load connections for each engram.
                Set to False for bulk operations where connections aren't needed
                (e.g., decay pass only needs accessibility/strength fields).
            author_kind: Only engrams whose words this kind of writer wrote
                ('agent' for the passes that make the agent who it is).
        """
        return self.get_engrams_in_states(
            ("active",),
            agent_id=agent_id,
            limit=limit,
            load_connections=load_connections,
            person_id=person_id,
            project_scope=project_scope,
            author_kind=author_kind,
        )

    def get_engrams_in_states(
        self,
        states: tuple[str, ...],
        *,
        agent_id: str | None = "default",
        limit: int = 1000,
        load_connections: bool = True,
        person_id: str | None = None,
        project_scope: str | None = None,
        after_id: str | None = None,
        author_kind: str | None = None,
        standing: bool | None = None,
    ) -> list[Engram]:
        """Engrams in any of ``states`` for an agent, the most accessible
        first, as ``get_active_engrams`` returns active ones.

        With ``after_id``, the ``limit`` engrams after that id in id order
        instead: one page of a walk through every one of them (start it with
        ``after_id=""``). Changes made to the engrams along the way cannot make
        such a walk skip or repeat one. Decay walks dormant memories this way,
        apart from the active ones, so no dormant memory waits behind a limit
        the active ones fill. With ``author_kind``, only engrams whose words
        that kind of writer wrote. With ``standing``, only engrams marked
        standing (True) or only those not marked (False): decay passes over
        the marked ones.
        """
        conn = self._get_conn()
        states = tuple(dict.fromkeys(states))
        if not states:
            return []
        where = [f"state IN ({', '.join('?' * len(states))})"]
        params: list[Any] = list(states)
        if agent_id is not None:
            where.append("owner_agent_id = ?")
            params.append(agent_id)
            if person_id is not None and project_scope is not None:
                where.append("person_id = ? AND project_scope = ?")
                params.extend([person_id, project_scope])
        if author_kind is not None:
            where.append("author_kind = ?")
            params.append(author_kind)
        if standing is not None:
            where.append("standing = ?")
            params.append(1 if standing else 0)
        order = "accessibility DESC"
        if after_id is not None:
            where.append("id > ?")
            params.append(after_id)
            order = "id"
        rows = conn.execute(
            f"SELECT * FROM engrams WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
            (*params, limit),
        ).fetchall()
        engrams = [Engram.from_dict(dict(r)) for r in rows]
        if load_connections:
            for engram in engrams:
                engram.connections = self.get_connections(engram.id)
                engram.versions = self._get_versions(engram.id)
        return engrams

    def delete_engram(self, engram_id: str) -> None:
        """Remove an engram (use archive_engram for soft delete)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM engrams WHERE id = ?", (engram_id,))
        conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram_id,))
        conn.execute(
            "DELETE FROM connections WHERE source_id = ? OR target_id = ?",
            (engram_id, engram_id),
        )
        conn.execute("DELETE FROM versions WHERE engram_id = ?", (engram_id,))
        self._commit()

    def count_engrams(self, agent_id: str | None = "default", state: str = "active") -> int:
        """Count engrams for an agent in a given state.

        Args:
            agent_id: Agent to count for. If None, counts all agents.
            state: Engram state to filter by.
        """
        conn = self._get_conn()
        if agent_id is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE state = ?",
                (state,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?",
                (agent_id, state),
            ).fetchone()
        return row[0] if row else 0

    # ── Full-Text Search ──

    def search_fts(
        self, query: str, limit: int = 50, *, agent_id: str | None = None,
        person_id: str | None = None, project_scope: str | None = None,
    ) -> list[Engram]:
        """Search engrams using FTS5 full-text search."""
        return [e for e, _ in self.search_fts_ranked(
            query, limit, agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        )]

    def search_fts_ranked(
        self, query: str, limit: int = 50, *, agent_id: str | None = None,
        person_id: str | None = None, project_scope: str | None = None,
        state: str = "active",
    ) -> list[tuple[Engram, float]]:
        """search_fts, with how well each engram matched: FTS5's bm25 rank.

        Ranks are negative, and the better the match, the lower the rank. They are
        comparable within one search, not across searches. Searches for one query
        in different states are comparable too: bm25 weighs a match against the
        whole index, whatever state the rows found are in. Recall searches active
        memories, then dormant ones (``state="dormant"``); an archived memory has
        no row in the index.
        """
        conn = self._get_conn()
        scope_sql = ""
        params: list[Any] = [query, state]
        if agent_id is not None and person_id is not None and project_scope is not None:
            scope_sql = " AND e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?"
            params.extend([agent_id, person_id, project_scope])
        params.append(limit)
        rows = conn.execute(
            "SELECT e.*, f.rank AS fts_rank FROM engrams e JOIN engrams_fts f ON e.id = f.id "
            "WHERE engrams_fts MATCH ? AND e.state = ?" + scope_sql +
            " ORDER BY rank LIMIT ?", params,
        ).fetchall()
        ranked = []
        for r in rows:
            d = dict(r)
            rank = d.pop("fts_rank")
            ranked.append((Engram.from_dict(d), rank))
        return ranked

    # ── Connections ──

    def save_connection(self, source_id: str, conn_obj: Connection) -> None:
        """Save a typed connection (with auto-commit)."""
        conn = self._get_conn()
        self._save_connection_no_commit(conn, source_id, conn_obj)
        self._commit()

    def _save_connection_no_commit(
        self, conn: sqlite3.Connection, source_id: str, conn_obj: Connection
    ) -> None:
        """Save a typed connection without committing (for use in transactions)."""
        conn.execute(
            "INSERT OR REPLACE INTO connections "
            "(source_id, target_id, relation, strength, formed_at, formed_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_id,
                conn_obj.target_id,
                conn_obj.relation,
                conn_obj.strength,
                conn_obj.formed_at,
                conn_obj.formed_by,
            ),
        )

    def get_connections(self, engram_id: str) -> list[Connection]:
        """Get all connections FROM an engram."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM connections WHERE source_id = ?", (engram_id,)
        ).fetchall()
        return [
            Connection(
                target_id=r["target_id"],
                relation=r["relation"],
                strength=r["strength"],
                formed_at=r["formed_at"],
                formed_by=r["formed_by"],
            )
            for r in rows
        ]

    def update_connection(self, source_id: str, connection) -> None:
        """Update an existing connection's relation, strength, or formed_by."""
        self._conn.execute(
            """UPDATE connections
               SET relation = ?, strength = ?, formed_by = ?
               WHERE source_id = ? AND target_id = ?""",
            (
                connection.relation.value if hasattr(connection.relation, 'value') else str(connection.relation),
                connection.strength,
                connection.formed_by,
                source_id,
                connection.target_id,
            ),
        )
        self._commit()

    def remove_connections(self, keys: list[tuple[str, str, str]]) -> int:
        """Remove exactly these connections, each named by (source, target, relation),
        in one transaction. Other relations between the same two engrams stay.
        Returns how many rows went."""
        conn = self._get_conn()
        removed = 0
        self._begin_immediate()
        try:
            for source_id, target_id, relation in keys:
                removed += conn.execute(
                    "DELETE FROM connections WHERE source_id = ? AND target_id = ? AND relation = ?",
                    (source_id, target_id, relation),
                ).rowcount
            self._commit()
        except Exception:
            self._rollback()
            raise
        return removed

    def remove_connection(self, source_id: str, target_id: str) -> None:
        """Remove a connection between two engrams."""
        self._conn.execute(
            "DELETE FROM connections WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        )
        self._commit()

    def get_recent_engrams(
        self,
        agent_id: str | None = None,
        since: "datetime | None" = None,
        limit: int = 50,
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> list:
        """Get recently created engrams, optionally filtered by agent and time.

        Args:
            agent_id: Filter by agent ID (optional).
            since: Only return engrams created after this datetime (optional).
            limit: Maximum number to return.

        Returns:
            List of Engram objects, most recent first.
        """
        query = "SELECT * FROM engrams WHERE state = 'active'"
        params: list = []

        if agent_id:
            query += " AND owner_agent_id = ?"
            params.append(agent_id)

        if person_id is not None and project_scope is not None:
            query += " AND person_id = ? AND project_scope = ?"
            params.extend([person_id, project_scope])

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_engram(dict(r)) for r in rows]


    def get_connected_engram_ids(
        self,
        engram_id: str,
        max_depth: int = 2,
    ) -> set[str]:
        """Get IDs of engrams connected within max_depth hops."""
        visited: set[str] = set()
        frontier = {engram_id}

        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT target_id FROM connections WHERE source_id = ? "
                    "UNION SELECT source_id FROM connections WHERE target_id = ?",
                    (eid, eid),
                ).fetchall()
                next_frontier.update(r[0] for r in rows)
            frontier = next_frontier - visited

        visited.discard(engram_id)
        return visited

    # ── Versions ──
    #
    # A version row is history: once written it is never written again. Each
    # VersionRef loaded from a store, or written to it by a committed
    # transaction, carries that store's file in ``_VERSION_STORED_IN``, so a
    # save appends only what the store lacks, whatever list the engram arrived
    # with (a partial engram's is empty).

    def _version_store_key(self) -> str:
        key = getattr(self, "_version_key", None)
        if key is None:
            try:
                key = str(self.db_path.resolve())
            except OSError:
                key = str(self.db_path)
            self._version_key = key
        return key

    def _save_version(self, engram_id: str, version: VersionRef) -> None:
        """Append a version snapshot (with auto-commit)."""
        conn = self._get_conn()
        self._begin_immediate()
        try:
            version.version_num = self._next_version_num(conn, engram_id)
            self._save_version_no_commit(conn, engram_id, version)
            self._commit()
        except Exception:
            self._rollback()
            raise

    def _next_version_num(self, conn: sqlite3.Connection, engram_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(version_num) FROM versions WHERE engram_id = ?", (engram_id,)
        ).fetchone()
        return int(row[0] or 0) + 1

    def _save_version_no_commit(
        self, conn: sqlite3.Connection, engram_id: str, version: VersionRef
    ) -> None:
        """Write one new version row without committing (for use in transactions).

        A plain INSERT: a row already written is never replaced. The version
        counts as held by this store once the transaction commits.
        """
        conn.execute(
            "INSERT INTO versions "
            "(engram_id, version_num, content_snapshot, resolution_at_version, "
            "changed_at, change_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                engram_id,
                version.version_num,
                version.content_snapshot,
                version.resolution_at_version,
                version.changed_at,
                version.change_reason,
            ),
        )
        if getattr(self, "_versions_written", None) is None:
            self._versions_written = []
        self._versions_written.append(version)

    def _append_versions_no_commit(
        self, conn: sqlite3.Connection, engram: Engram, *, changed: bool
    ) -> None:
        """Append the engram's versions this store does not hold yet.

        They are numbered after the last version stored, not by their place
        in the engram's list, which may be partial. When the save changes
        none of content, impact or resolution, they are dropped instead.
        """
        where = self._version_store_key()
        # Written earlier in this still-open transaction: held, once it commits.
        pending = {id(v) for v in getattr(self, "_versions_written", None) or ()}

        def held(version: VersionRef) -> bool:
            return id(version) in pending or _version_stored_in(version, where)

        new = [v for v in engram.versions if not held(v)]
        if not new:
            return
        if not changed:
            engram.versions = [v for v in engram.versions if held(v)]
            return
        next_num = self._next_version_num(conn, engram.id)
        for version in new:
            version.version_num = next_num
            self._save_version_no_commit(conn, engram.id, version)
            next_num += 1

    def _get_versions(self, engram_id: str) -> list[VersionRef]:
        """Get version history for an engram."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM versions WHERE engram_id = ? ORDER BY version_num",
            (engram_id,),
        ).fetchall()
        stored_here = frozenset((self._version_store_key(),))
        versions = [VersionRef.from_dict(dict(r)) for r in rows]
        for version in versions:
            setattr(version, _VERSION_STORED_IN, stored_here)
        return versions

    def duplicate_versions(self) -> dict[str, Any]:
        """Version rows in this store that repeat the row before them.

        Until a return stopped writing versions, every reconsolidation
        appended a full snapshot of a memory that had not changed. A row
        counts only when a return wrote it (reason ``reconsolidation``) and
        its content and resolution equal the row just before it in the same
        memory's history. The first row of every run stays, so every state
        the history recorded is kept, as is every row written for another
        reason (softening, repairs). Read-only.
        """
        conn = self._get_conn()
        total, memories = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT engram_id) FROM versions"
        ).fetchone()
        rows = conn.execute(
            """SELECT engram_id, version_num FROM (
                   SELECT engram_id, version_num, change_reason,
                          content_snapshot, resolution_at_version,
                          LAG(version_num) OVER history AS before_num,
                          LAG(content_snapshot) OVER history AS before_content,
                          LAG(resolution_at_version) OVER history AS before_resolution
                   FROM versions
                   WINDOW history AS (PARTITION BY engram_id ORDER BY version_num)
               )
               WHERE change_reason = 'reconsolidation'
                 AND before_num IS NOT NULL
                 AND content_snapshot = before_content
                 AND resolution_at_version = before_resolution
               ORDER BY engram_id, version_num"""
        ).fetchall()
        duplicates = [(row[0], int(row[1])) for row in rows]
        per_memory: dict[str, int] = {}
        for engram_id, _ in duplicates:
            per_memory[engram_id] = per_memory.get(engram_id, 0) + 1
        return {
            "rows": int(total),
            "memories": int(memories),
            "duplicates": duplicates,
            "memories_with_duplicates": len(per_memory),
            "most_repeated": sorted(
                per_memory.items(), key=lambda item: (-item[1], item[0])
            )[:3],
        }

    def remove_versions(self, keys: list[tuple[str, int]]) -> int:
        """Delete these version rows (engram id, version number), and only
        these. Returns how many went. Used only by ``mnemos repair-versions``."""
        conn = self._get_conn()
        self._begin_immediate()
        try:
            cursor = conn.executemany(
                "DELETE FROM versions WHERE engram_id = ? AND version_num = ?",
                keys,
            )
            removed = cursor.rowcount
            self._commit()
        except Exception:
            self._rollback()
            raise
        return max(0, removed)

    # ── Returns ──

    def claim_return(self, engram_id: str, session_id: str) -> bool:
        """Record that ``session_id`` reinforces this memory now; False if it
        already has. Run it in the transaction that writes the reinforcement,
        so two processes of one session cannot both reinforce."""
        conn = self._get_conn()
        self._begin_immediate()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO session_reinforcements "
                "(session_id, engram_id, reinforced_at) VALUES (?, ?, ?)",
                (session_id, engram_id, _utc_now()),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return cursor.rowcount == 1

    def record_return(self, engram: Engram, links: list[Connection]) -> None:
        """Write what returning a memory changed, in place.

        The access record and trace dynamics on the engram's row, and the
        co-activation links the return formed or strengthened. Never content,
        the text index, other links, or a version: a return changes none of
        what those hold.

        A dormant memory that is returned wakes: it is active again, with the
        accessibility the return gave it (reconsolidation's floor, well above
        where decay makes a memory dormant). Like the rest of a return, this
        happens once per session and never from code older than the store:
        Mnemos comes here only through ``ReactiveRetriever.reinforce``, which
        holds both rules.
        """
        if engram.state == "dormant":
            engram.state = "active"
        conn = self._get_conn()
        self._begin_immediate()
        try:
            conn.execute(
                "UPDATE engrams SET access_count = ?, last_accessed = ?, "
                "reconsolidation_count = ?, strength = ?, stability = ?, "
                "accessibility = ?, "
                "state = CASE WHEN state = 'dormant' THEN 'active' ELSE state END "
                "WHERE id = ?",
                (
                    engram.access_count,
                    engram.last_accessed,
                    engram.reconsolidation_count,
                    engram.strength,
                    engram.stability,
                    engram.accessibility,
                    engram.id,
                ),
            )
            for link in links:
                self._save_connection_no_commit(conn, engram.id, link)
            self._commit()
        except Exception:
            self._rollback()
            raise

    # ── Archive ──

    def archive_engram(self, engram: Engram, reason: str = "low_accessibility") -> None:
        """Move engram to cold storage."""
        conn = self._get_conn()
        from datetime import datetime, timezone

        conn.execute(
            "INSERT OR REPLACE INTO archive "
            "(id, content, content_at_encoding, kind, tags, "
            "archived_at, archive_reason, final_accessibility) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                engram.id,
                engram.content,
                engram.content_at_encoding,
                engram.kind,
                json.dumps(engram.tags),
                datetime.now(timezone.utc).isoformat(),
                reason,
                engram.accessibility,
            ),
        )
        # Remove from active tables
        conn.execute("UPDATE engrams SET state = 'archived' WHERE id = ?", (engram.id,))
        conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram.id,))
        # An unanswered reflection request about an archived memory is a dead
        # end, and pre-fix rows carry a frozen copy of its text. Every path
        # that forgets something ends here, so this is where the request goes.
        conn.execute(
            "DELETE FROM reflection_queue WHERE target_id = ? AND answered_at IS NULL",
            (engram.id,),
        )
        self._commit()

    def search_archive(self, query: str, limit: int = 20) -> list[dict]:
        """Search archived engrams by content (for resharpen)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM archive WHERE content LIKE ? OR content_at_encoding LIKE ? "
            "LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def archive_reason(self, engram_id: str) -> str | None:
        """Why an archived engram is in the archive, or None when it is not."""
        row = self._get_conn().execute(
            "SELECT a.archive_reason FROM archive a JOIN engrams e ON e.id = a.id "
            "WHERE a.id = ? AND e.state = 'archived'",
            (engram_id,),
        ).fetchone()
        return None if row is None else row[0]

    def faded_engrams(
        self,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
        limit: int = 500,
        before_id: str | None = None,
    ) -> list[Engram]:
        """Archived engrams in one scope that faded there (FADED_ARCHIVE_REASONS),
        ``limit`` at a time, newest first by id (a ULID, so by when each was
        written, to the millisecond); with ``before_id``, the page after that
        id. Walking the pages
        reaches every one of them, as ``count_faded`` counts every one, and
        restoring one along the way cannot make the walk skip or repeat one. A
        memory forgotten or replaced by a correction is never among them.
        Which of them a query names is the caller's to judge, word by word:
        SQLite's LIKE folds the case of ASCII letters only."""
        reasons = ", ".join("?" * len(FADED_ARCHIVE_REASONS))
        sql = (
            "SELECT e.* FROM engrams e JOIN archive a ON a.id = e.id "
            "WHERE e.state = 'archived' AND e.owner_agent_id = ? "
            "AND e.person_id = ? AND e.project_scope = ? "
            f"AND a.archive_reason IN ({reasons})"
        )
        params: list[Any] = [agent_id, person_id, project_scope, *FADED_ARCHIVE_REASONS]
        if before_id is not None:
            sql += " AND e.id < ?"
            params.append(before_id)
        rows = self._get_conn().execute(
            sql + " ORDER BY e.id DESC LIMIT ?", (*params, limit),
        ).fetchall()
        return [Engram.from_dict(dict(row)) for row in rows]

    def count_faded(self, *, agent_id: str, person_id: str, project_scope: str) -> int:
        """How many archived engrams in one scope faded there: stored, and out of
        reach of an ordinary recall (see ``faded_engrams``)."""
        reasons = ", ".join("?" * len(FADED_ARCHIVE_REASONS))
        row = self._get_conn().execute(
            "SELECT COUNT(*) FROM engrams e JOIN archive a ON a.id = e.id "
            "WHERE e.state = 'archived' AND e.owner_agent_id = ? "
            "AND e.person_id = ? AND e.project_scope = ? "
            f"AND a.archive_reason IN ({reasons})",
            (agent_id, person_id, project_scope, *FADED_ARCHIVE_REASONS),
        ).fetchone()
        return int(row[0]) if row else 0

    # ── Beliefs ──

    def save_belief(self, belief: Belief) -> None:
        """Insert or update a belief."""
        conn = self._get_conn()
        data = belief.to_dict()

        # Validate column names
        safe_data = {k: v for k, v in data.items() if k in _BELIEF_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in safe_data if k != "id")

        conn.execute(
            f"INSERT INTO beliefs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(safe_data.values()),
        )
        self._commit()

    def get_beliefs(
        self,
        agent_id: str = "default",
        domain: str | None = None,
        active_only: bool = True,
    ) -> list[Belief]:
        """Get beliefs for an agent, optionally filtered by domain."""
        conn = self._get_conn()
        query = "SELECT * FROM beliefs WHERE agent_id = ?"
        params: list[Any] = [agent_id]

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if active_only:
            query += " AND superseded_by IS NULL"

        query += " ORDER BY confidence DESC"
        rows = conn.execute(query, params).fetchall()
        return [Belief.from_dict(dict(r)) for r in rows]

    def get_belief(self, belief_id: str) -> Belief | None:
        """Load a single belief by id, or None."""
        row = self._get_conn().execute(
            "SELECT * FROM beliefs WHERE id = ?", (belief_id,)
        ).fetchone()
        return Belief.from_dict(dict(row)) if row else None

    def revise_belief(
        self, belief_id: str, new_confidence: float, reason: str,
        *, trigger_engram_id: str | None = None,
    ) -> bool:
        """Lower (or raise) a belief's confidence with an audit trail.

        The one deliberate way to erode a belief's confidence from the
        agent-facing side — the graph's stability ratchet otherwise only runs
        upward. Wraps ``Belief.revise`` (which clamps and records the change)
        and persists. Returns False if the belief is gone.
        """
        belief = self.get_belief(belief_id)
        if belief is None:
            return False
        belief.revise(new_confidence, reason, trigger_engram_id=trigger_engram_id)
        self.save_belief(belief)
        return True

    def supersede_belief(self, belief_id: str, *, reason: str = "") -> bool:
        """Retire a belief. It stops appearing in ``get_beliefs(active_only)``.

        Activates the built-but-never-called ``superseded_by`` plumbing: the
        row is kept for provenance but hidden from every read path. Records the
        retirement in the revision history so the reason survives.
        """
        belief = self.get_belief(belief_id)
        if belief is None:
            return False
        if reason:
            belief.revise(belief.confidence, f"superseded: {reason}")
        # A sentinel that reads as "retired" without pointing at a replacement.
        belief.superseded_by = belief.superseded_by or "retired"
        self.save_belief(belief)
        return True

    # ── Functional Memory ──

    def start_memory_session(
        self,
        *,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        title: str = "",
        source: str = "mcp",
    ) -> dict[str, Any]:
        """Start or reopen a functional memory session."""
        now = _utc_now()
        sid = (session_id or "").strip() or _new_id()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory_sessions(
                id, agent_id, person_id, project_scope, title, source,
                status, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                title = CASE
                    WHEN excluded.title != '' THEN excluded.title
                    ELSE memory_sessions.title
                END,
                source = excluded.source,
                status = 'active',
                updated_at = excluded.updated_at,
                closed_at = NULL
            """,
            (
                sid,
                agent_id,
                person_id,
                project_scope,
                title.strip(),
                source.strip() or "mcp",
                now,
                now,
            ),
        )
        self._commit()
        session = self.get_memory_session(sid)
        if session is None:
            raise RuntimeError(f"Failed to start memory session: {sid}")
        return session

    def get_memory_session(self, session_id: str) -> dict[str, Any] | None:
        """Load a functional memory session by ID."""
        row = self._get_conn().execute(
            "SELECT * FROM memory_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def close_memory_session(
        self,
        session_id: str,
        *,
        status: str = "closed",
    ) -> dict[str, Any] | None:
        """Mark a functional memory session closed or paused."""
        if status not in VALID_SESSION_STATUSES:
            raise ValueError(f"Unsupported session status: {status}")
        now = _utc_now()
        closed_at = now if status == "closed" else None
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE memory_sessions
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (status, now, closed_at, session_id),
        )
        self._commit()
        return self.get_memory_session(session_id)

    def write_functional_memory(
        self,
        content: str,
        *,
        memory_id: str | None = None,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        memory_type: str = "working",
        confidence: float = 0.65,
        salience: float = 0.5,
        needs_confirmation: bool = False,
        pinned: bool = False,
        source: str = "agent_observed",
        metadata: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Write or update a functional memory entry.

        Functional memory is the live, revisable working layer. It is useful
        for current task state, open questions, corrections, commitments, and
        preferences that have not yet earned hypomnema or engram status.
        """
        if memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")
        if not content.strip():
            raise ValueError("Functional memory content cannot be empty")

        now = _utc_now()
        fid = (memory_id or "").strip() or _new_id()
        session = (session_id or "").strip() or None
        conn = self._get_conn()
        if session and self.get_memory_session(session) is None:
            self.start_memory_session(
                session_id=session,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                title="Recovered session",
                source=source,
            )

        conn.execute(
            """
            INSERT INTO functional_memories(
                id, session_id, agent_id, person_id, project_scope, content,
                memory_type, confidence, salience, needs_confirmation, pinned,
                source, metadata_json, created_at, updated_at, expires_at,
                is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                content = excluded.content,
                memory_type = excluded.memory_type,
                confidence = excluded.confidence,
                salience = excluded.salience,
                needs_confirmation = excluded.needs_confirmation,
                pinned = excluded.pinned,
                source = excluded.source,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                is_deleted = 0
            """,
            (
                fid,
                session,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                memory_type,
                _clamp(confidence),
                _clamp(salience),
                int(needs_confirmation),
                int(pinned),
                source.strip() or "agent_observed",
                _encode_json(metadata or {}),
                now,
                now,
                expires_at,
            ),
        )
        if session:
            conn.execute(
                "UPDATE memory_sessions SET updated_at = ? WHERE id = ?",
                (now, session),
            )
        self._commit()
        row = conn.execute(
            "SELECT * FROM functional_memories WHERE id = ?",
            (fid,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to write functional memory: {fid}")
        return self._hydrate_functional_row(dict(row))

    def get_functional_memory(
        self,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Load a functional memory by ID."""
        sql = "SELECT * FROM functional_memories WHERE id = ?"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        row = self._get_conn().execute(sql, (memory_id,)).fetchone()
        if row is None:
            return None
        return self._hydrate_functional_row(dict(row))

    def load_functional_memories(
        self,
        query: str = "",
        *,
        session_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        memory_type: str | None = None,
        needs_confirmation_only: bool = False,
        include_deleted: bool = False,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Search functional memories for the current scope/session."""
        if memory_type and memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")

        sql = (
            "SELECT * FROM functional_memories "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if needs_confirmation_only:
            sql += " AND needs_confirmation = 1"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT 200"

        rows = self._get_conn().execute(sql, params).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._hydrate_functional_row(dict(row))
            if query:
                score = (
                    _lexical_score(query, item["content"]) * 0.5
                    + float(item["confidence"]) * 0.2
                    + float(item["salience"]) * 0.25
                    + (0.05 if item["pinned"] else 0.0)
                )
            else:
                score = (
                    float(item["confidence"]) * 0.35
                    + float(item["salience"]) * 0.45
                    + (0.15 if item["pinned"] else 0.0)
                    + (0.05 if item["needs_confirmation"] else 0.0)
                )
            item["score"] = round(score, 4)
            scored.append(item)

        scored.sort(
            key=lambda item: (item["score"], item["pinned"], item["updated_at"]),
            reverse=True,
        )
        return scored[: max(1, limit)]

    def close_session_to_hypomnema(
        self,
        session_id: str,
        *,
        synthesis: str = "",
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any]:
        """Close a session and compress active functional memories into hypomnema."""
        session = self.get_memory_session(session_id)
        if session is None:
            raise KeyError(f"Functional memory session not found: {session_id}")

        memories = self.load_functional_memories(
            "",
            session_id=session_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            limit=50,
        )
        if synthesis.strip():
            content = synthesis.strip()
        else:
            chosen = memories[:8]
            details = "; ".join(
                f"{m['memory_type']}: {m['content']}" for m in chosen
            )
            title = session.get("title") or session_id
            content = (
                f"Session continuity from {title}: {details}"
                if details
                else f"Session {title} closed without durable functional memories."
            )

        confidence = (
            sum(float(m["confidence"]) for m in memories) / len(memories)
            if memories
            else 0.55
        )
        salience = max((float(m["salience"]) for m in memories), default=0.45)
        hypomnema_id = None
        if memories or synthesis.strip():
            hypomnema_id = self.write_hypomnema_entry(
                content,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                source="synthesized",
                authored_by="agent" if synthesis.strip() else "system",
                author_id=agent_id if synthesis.strip() else "mnemos",
                density=0.72,
                domain="situational",
                tags=["session-close", "functional-memory", project_scope],
                confidence=confidence,
                salience=salience,
                related_session_id=session_id,
            )

        now = _utc_now()
        conn = self._get_conn()
        if hypomnema_id:
            conn.execute(
                """
                UPDATE functional_memories
                SET is_deleted = 1,
                    promoted_to_hypomnema_id = ?,
                    updated_at = ?
                WHERE session_id = ? AND is_deleted = 0
                """,
                (hypomnema_id, now, session_id),
            )
        conn.execute(
            """
            UPDATE memory_sessions
            SET status = 'closed', updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (now, now, session_id),
        )
        self._commit()
        return {
            "session": self.get_memory_session(session_id),
            "hypomnema_id": hypomnema_id,
            "functional_memories": len(memories),
            "content": content,
        }

    def get_functional_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict[str, int]:
        """Count active functional memory and session state."""
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        where_sql = " AND ".join(where)
        conn = self._get_conn()
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN is_deleted = 0 AND pinned = 1 THEN 1 ELSE 0 END) AS pinned,
              SUM(CASE WHEN is_deleted = 0 AND needs_confirmation = 1 THEN 1 ELSE 0 END) AS needs_confirmation
            FROM functional_memories
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        session_row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
            FROM memory_sessions
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        return {
            "functional_total": int(row["total"] or 0),
            "functional_active": int(row["active"] or 0),
            "functional_pinned": int(row["pinned"] or 0),
            "functional_needs_confirmation": int(row["needs_confirmation"] or 0),
            "functional_sessions_active": int(session_row["active"] or 0),
            "functional_sessions_closed": int(session_row["closed"] or 0),
        }

    @staticmethod
    def _hydrate_functional_row(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = _decode_json(row.pop("metadata_json", "{}"), {})
        row["needs_confirmation"] = bool(row["needs_confirmation"])
        row["pinned"] = bool(row["pinned"])
        row["is_deleted"] = bool(row["is_deleted"])
        return row

    # ── Hypomnema ──

    def write_hypomnema_entry(
        self,
        content: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        entry_kind: str = "continuity",
        authored_by: str | None = None,
        author_id: str = "",
        author_model: str = "",
        author_session: str = "",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        """Write a scoped hypomnema continuity entry.

        Hypomnema is durable, relationship-scoped continuity that can be
        revised before it graduates into shared Mnemos engrams.

        ``author_id`` is the agent scope that wrote the entry; ``author_model``
        is the model that agent was running, when known. Several models can
        share one scope, and the model is what tells a reader whether a note
        is its own or a colleague's. Empty means unsigned, never unknown-but-
        assumed. ``author_session`` is the harness session that wrote it, when
        known. ``authored_by`` says what kind of writer it was: a continuity
        note's counterpart to a memory's ``author_kind``.
        """
        if source not in VALID_HYPO_SOURCES:
            raise ValueError(f"Unsupported hypomnema source: {source}")
        if authored_by is None:
            authored_by = "coauthored" if source == "co-formed" else "unknown"
        if entry_kind not in VALID_HYPO_ENTRY_KINDS:
            raise ValueError(f"Unsupported hypomnema entry kind: {entry_kind}")
        if authored_by not in VALID_HYPO_AUTHORSHIP:
            raise ValueError(f"Unsupported hypomnema authorship: {authored_by}")
        if domain not in VALID_HYPO_DOMAINS:
            raise ValueError(f"Unsupported hypomnema domain: {domain}")
        if not content.strip():
            raise ValueError("Hypomnema content cannot be empty")

        now = _utc_now()
        entry_id = _new_id()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO hypomnema_entries(
                id, agent_id, person_id, project_scope, content,
                entry_kind, authored_by, author_id, author_model, author_session,
                source, density, domain, tags_json, confidence, salience,
                active, foundational, revision_count, revisions_json,
                related_session_id, related_engram_id, created_at, last_revised_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, '[]', ?, ?, ?, ?)
            """,
            (
                entry_id,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                entry_kind,
                authored_by,
                author_id.strip(),
                (author_model or "").strip(),
                (author_session or "").strip(),
                source,
                _clamp(density),
                domain,
                _encode_json(_split_tags(tags)),
                _clamp(confidence),
                _clamp(salience),
                int(foundational),
                related_session_id,
                related_engram_id,
                now,
                now,
            ),
        )
        self._commit()
        return entry_id

    def write_handoff(
        self,
        text: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        author_id: str = "",
        author_model: str = "",
        author_session: str = "",
        retire_crowded: bool = True,
    ) -> str:
        """Atomically replace this session's handoff, preserving exact prose.

        ``author_model`` signs the handoff with the model that wrote it. The
        next session may be a different model; the signature is how it knows.

        ``author_session`` is the harness session that wrote it. A handoff
        replaces only the one its own session left before: several sessions
        often work one scope at once, and each keeps its own note. Writers
        that can't say which session they are share the empty session and
        replace each other, as every writer did before sessions were told
        apart. Beyond ``HANDOFF_SESSIONS_KEPT`` sessions' notes in a scope,
        the oldest is retired; its prose stays in history. With
        ``retire_crowded`` False (code older than the store), no other
        session's note is retired: they stay active until current code
        retires them.
        """

        if not text.strip():
            raise ValueError("Handoff text cannot be empty")

        session = (author_session or "").strip()
        conn = self._get_conn()
        new_id = _new_id()
        now = _utc_now()
        try:
            self._begin_immediate()
            prior = conn.execute(
                """
                SELECT * FROM hypomnema_entries
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND entry_kind = 'handoff' AND active = 1
                  AND author_session = ?
                LIMIT 1
                """,
                (agent_id, person_id, project_scope, session),
            ).fetchone()
            if prior is not None:
                revisions = _decode_json(prior["revisions_json"], [])
                revisions.append({
                    "at": now,
                    "prior_content": prior["content"],
                    "reason": "superseded: newer agent-written session handoff",
                })
                conn.execute(
                    """
                    UPDATE hypomnema_entries
                    SET active = 0,
                        revision_count = revision_count + 1,
                        revisions_json = ?, last_revised_at = ?
                    WHERE id = ?
                    """,
                    (_encode_json(revisions), now, prior["id"]),
                )

            conn.execute(
                """
                INSERT INTO hypomnema_entries(
                    id, agent_id, person_id, project_scope, content,
                    entry_kind, authored_by, author_id, author_model,
                    author_session, source,
                    density, domain, tags_json, confidence, salience,
                    active, foundational, revision_count, revisions_json,
                    created_at, last_revised_at, surface_count
                ) VALUES (?, ?, ?, ?, ?, 'handoff', 'agent', ?, ?, ?, 'observed',
                          0.9, 'situational', ?, 1.0, 1.0,
                          1, 0, 0, '[]', ?, ?, 0)
                """,
                (
                    new_id,
                    agent_id,
                    person_id,
                    project_scope,
                    text,
                    (author_id or agent_id).strip(),
                    (author_model or "").strip(),
                    session,
                    _encode_json(["session-handoff", "continuity"]),
                    now,
                    now,
                ),
            )
            if prior is not None:
                conn.execute(
                    "UPDATE hypomnema_entries SET superseded_by = ? WHERE id = ?",
                    (new_id, prior["id"]),
                )
            # Bound how many sessions' notes stay active. A note pushed out
            # here is retired, not superseded: nothing replaced its content.
            crowded = [] if not retire_crowded else conn.execute(
                f"""
                SELECT id, content, revisions_json FROM hypomnema_entries
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND entry_kind = 'handoff' AND active = 1
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET {int(HANDOFF_SESSIONS_KEPT)}
                """,
                (agent_id, person_id, project_scope),
            ).fetchall()
            for row in crowded:
                revisions = _decode_json(row["revisions_json"], [])
                revisions.append({
                    "at": now,
                    "prior_content": row["content"],
                    "reason": (
                        f"retired: {HANDOFF_SESSIONS_KEPT} newer sessions have "
                        "left handoffs since"
                    ),
                })
                conn.execute(
                    """
                    UPDATE hypomnema_entries
                    SET active = 0,
                        revision_count = revision_count + 1,
                        revisions_json = ?, last_revised_at = ?
                    WHERE id = ?
                    """,
                    (_encode_json(revisions), now, row["id"]),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return new_id

    def get_latest_handoff(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = True,
    ) -> dict[str, Any] | None:
        """Return the newest handoff in this exact scope."""

        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND entry_kind = 'handoff'"
        )
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self._get_conn().execute(
            sql, (agent_id, person_id, project_scope)
        ).fetchone()
        return self._hydrate_hypomnema_row(dict(row)) if row else None

    def live_handoffs(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        reader_session: str = "",
        limit: int = PACKET_HANDOFFS,
        within_hours: float = LIVE_HANDOFF_HOURS,
    ) -> list[dict[str, Any]]:
        """The handoffs a starting session is handed, in the order it reads them.

        First the note the reader's own session left, if it left one: a
        session coming back from compaction or a resume gets its own thread
        back before anyone else's. Otherwise the newest note, however old, as
        before sessions were told apart. Then other sessions' notes, newest
        first, while they are no older than ``within_hours``, up to ``limit``
        notes in all.
        """

        rows = self._get_conn().execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND entry_kind = 'handoff' AND active = 1
            ORDER BY created_at DESC, rowid DESC
            """,
            (agent_id, person_id, project_scope),
        ).fetchall()
        notes = [self._hydrate_hypomnema_row(dict(row)) for row in rows]
        if not notes:
            return []
        reader = (reader_session or "").strip()
        first = next(
            (note for note in notes if reader and note.get("author_session") == reader),
            notes[0],
        )
        cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        others = [
            note for note in notes
            if note["id"] != first["id"] and _written_since(note.get("created_at"), cutoff)
        ]
        return [first, *others[: max(0, int(limit) - 1)]]

    def recallable_handoffs(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> list[dict[str, Any]]:
        """Every handoff recall searches in this exact scope: the ones in use,
        then the older ones, newest first.

        An older handoff is one a newer handoff from its session replaced
        (``superseded``), or one pushed out when more sessions left notes than
        are kept (``retired``). Its words stay true of their day, and a
        question about that day finds them. One the agent forgot, or archived
        by a correction, stays gone. Before these were searched, a superseded
        handoff was unreachable, and it led to a confident "no record".
        """
        rows = self._get_conn().execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND entry_kind = 'handoff'
            ORDER BY active DESC, created_at DESC, rowid DESC
            """,
            (agent_id, person_id, project_scope),
        ).fetchall()
        handoffs = []
        for row in rows:
            note = self._hydrate_hypomnema_row(dict(row))
            if not note.get("active") and handoff_retirement(note) is None:
                continue
            handoffs.append(note)
        return handoffs

    def standalone_notes(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> list[dict[str, Any]]:
        """Live notes other than handoffs with no memory of their own, newest
        first: continuity notes written some way other than a capture, and
        Mnemos's reports (an identity divergence, a maintenance report).

        A capture's note is one object with its memory (``capture_pair``), and
        recall finds the pair through the memory. A note with no memory is
        found only as itself, so recall ranks it with the memories by its
        words and its meaning, as it searched every such note before.
        """
        rows = self._get_conn().execute(
            f"""
            SELECT h.* FROM hypomnema_entries h {_NOTE_MEMORY_JOIN}
            WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?
              AND h.entry_kind != 'handoff' AND h.graduated_to_engram_id IS NULL
              AND {_NOTE_LIVE}
            ORDER BY h.created_at DESC, h.rowid DESC
            """,
            (agent_id, person_id, project_scope),
        ).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def hypomnema_signers(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> list[str]:
        """Distinct models that have signed active notes in this exact scope."""

        rows = self._get_conn().execute(
            """
            SELECT DISTINCT author_model FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND active = 1 AND author_model != ''
            ORDER BY author_model
            """,
            (agent_id, person_id, project_scope),
        ).fetchall()
        return [row[0] for row in rows]

    def mark_handoff_surfaced(
        self,
        handoff_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> bool:
        """Record a real delivery of the currently active scoped handoff."""

        cursor = self._get_conn().execute(
            """
            UPDATE hypomnema_entries
            SET last_surfaced_at = ?, surface_count = surface_count + 1
            WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
              AND entry_kind = 'handoff' AND active = 1
            """,
            (_utc_now(), handoff_id, agent_id, person_id, project_scope),
        )
        self._commit()
        return cursor.rowcount == 1

    def get_hypomnema_entry(
        self,
        entry_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = False,
    ) -> dict[str, Any] | None:
        """Load a hypomnema entry by scoped ID."""
        conn = self._get_conn()
        query = (
            "SELECT * FROM hypomnema_entries "
            "WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [entry_id, agent_id, person_id, project_scope]
        if active_only:
            query += " AND active = 1"
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._hydrate_hypomnema_row(dict(row))

    def get_hypomnema_entry_for_engram(
        self,
        engram_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any] | None:
        """The active continuity note a memory was captured as, if any: the
        note paired with it (``graduated_to_engram_id``, as ``notes_for_engram``
        reads it).

        Capture writes both an engram and a scoped hypomnema note, one object.
        Anything that has learned something *about* an engram needs this to
        reach the layer the session packet is built from; writing only to the
        engram puts it somewhere the automatic path does not read.

        Never a note that only references the memory (``related_engram_id``):
        a note written to interpret it, or a correction's note that keeps such
        a reference. Matched by the reference, a reflection landed in the
        newest note naming the memory, which could be one of those, and a
        correction's own memory was never reached through its note.

        Returns None for engrams encoded outside the simple capture path,
        which legitimately have no note.
        """
        notes = self.notes_for_engram(
            engram_id, agent_id=agent_id, person_id=person_id,
            project_scope=project_scope, active_only=True,
        )
        return notes[0] if notes else None

    def search_hypomnema(
        self,
        query: str = "",
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 8,
        include_inactive: bool = False,
        exclude_kinds: tuple[str, ...] = (),
        live_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Search scoped hypomnema entries by text, confidence, and salience.

        ``exclude_kinds`` leaves out entry kinds a caller renders elsewhere or
        must never touch. Handoffs rank near the top of every search (they are
        stored at full confidence and salience), so a caller that filters them
        out after ranking loses a slot to each one.

        By default only live notes are searched (``_NOTE_LIVE``), for what is
        shown: a note whose memory has gone quiet or faded is left out with
        it, and comes back when it does. ``live_only=False`` searches every
        active note, whatever its memory's state, for finding what a
        correction or a forget names: a pair that went quiet still holds its
        words, and waking it would bring them back. ``include_inactive``
        searches every note.
        """
        conn = self._get_conn()
        sql = (
            f"SELECT h.* FROM hypomnema_entries h {_NOTE_MEMORY_JOIN} "
            "WHERE h.agent_id = ? AND h.person_id = ? AND h.project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if not include_inactive:
            sql += f" AND {_NOTE_LIVE}" if live_only else " AND h.active = 1"
        kinds = [kind for kind in exclude_kinds if kind in VALID_HYPO_ENTRY_KINDS]
        if kinds:
            sql += f" AND h.entry_kind NOT IN ({', '.join('?' for _ in kinds)})"
            params.extend(kinds)
        # Every note in scope is scored. A cap applied *before* scoring is a
        # silent amnesia: at 200 notes the old `LIMIT 100` made half of an
        # agent's continuity unreachable no matter how relevant it was, and
        # the packet still returned its full eight entries and looked
        # healthy. Continuity is a small, curated layer by design — scoring
        # a few thousand rows in Python costs milliseconds, and a store that
        # has grown past the ceiling below has a different problem than
        # ranking.
        sql += (
            " ORDER BY h.foundational DESC, h.last_revised_at DESC"
            f" LIMIT {_MAX_HYPOMNEMA_CANDIDATES}"
        )
        rows = conn.execute(sql, params).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._hydrate_hypomnema_row(dict(row))
            score = (
                _lexical_score(query, item["content"]) * 0.55
                + float(item["confidence"]) * 0.2
                + float(item["salience"]) * 0.2
                + (0.05 if item["foundational"] else 0.0)
            )
            if not query:
                score = (
                    float(item["confidence"]) * 0.4
                    + float(item["salience"]) * 0.4
                    + (0.1 if item["foundational"] else 0.0)
                )
            item["score"] = round(score, 4)
            scored.append(item)

        scored.sort(
            key=lambda item: (item["score"], item["last_revised_at"]),
            reverse=True,
        )
        return scored[: max(1, limit)]

    def get_hypomnema_entries_by_tag(
        self,
        tag: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Scoped hypomnema entries carrying an exact tag, newest first."""
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND tags_json LIKE ?"
        )
        # Quote-delimited match keeps the tag token-exact inside the JSON
        # array (so "dream" never matches "dream-journal").
        params: list[Any] = [agent_id, person_id, project_scope, f'%"{tag}"%']
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY last_revised_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = conn.execute(sql, params).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def revise_hypomnema_entry(
        self,
        entry_id: str,
        new_content: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        confidence: float | None = None,
        salience: float | None = None,
        author_model: str | None = None,
        revised_by: str = "",
        author_session: str | None = None,
    ) -> str:
        """Revise an existing hypomnema entry while preserving the old version.

        ``author_model`` re-signs the entry, for a revision that replaces its
        words with the reviser's. Left as ``None`` the signature stays with
        the original author — right for a revision that only adds to their
        words. ``revised_by`` is recorded in the revision trail either way.
        ``author_session`` re-signs the session the same way.
        """
        if not new_content.strip():
            raise ValueError("Revised hypomnema content cannot be empty")
        if not reason.strip():
            raise ValueError("Revision reason cannot be empty")

        now = _utc_now()
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
            """,
            (entry_id, agent_id, person_id, project_scope),
        ).fetchone()
        if row is None:
            raise KeyError(f"Hypomnema entry not found for scope: {entry_id}")

        revisions = _decode_json(row["revisions_json"], [])
        revision: dict[str, Any] = {
            "at": now,
            "prior_content": row["content"],
            "reason": reason.strip(),
        }
        signer = (author_model if author_model is not None else row["author_model"]) or ""
        session = (
            author_session if author_session is not None else row["author_session"]
        ) or ""
        if revised_by.strip():
            revision["revised_by"] = revised_by.strip()
        if signer.strip() != (row["author_model"] or ""):
            revision["prior_author_model"] = row["author_model"] or ""
        revisions.append(revision)
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET content = ?,
                confidence = ?,
                salience = ?,
                author_model = ?,
                author_session = ?,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (
                new_content.strip(),
                _clamp(confidence if confidence is not None else row["confidence"]),
                _clamp(salience if salience is not None else row["salience"]),
                signer.strip(),
                session.strip(),
                _encode_json(revisions),
                now,
                entry_id,
            ),
        )
        # Its passages were cut from the words it no longer holds: gone with
        # them, in this transaction, so recall never finds it by its old
        # meaning. It waits to be indexed again, and a correction re-indexes
        # it at once when it can (MnemosRuntime._correct_other_note).
        conn.execute("DELETE FROM passage_vectors WHERE item_id = ?", (entry_id,))
        self._commit()
        return entry_id

    def supersede_hypomnema_entry(
        self,
        entry_id: str,
        new_content: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        author_model: str | None = None,
        author_session: str | None = None,
    ) -> str:
        """Replace an active hypomnema entry with a new entry and audit link.

        The new entry keeps the original authorship unless ``author_model``
        (and ``author_session``) sign it for whoever wrote the replacement.
        """
        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        new_id = self.write_hypomnema_entry(
            new_content,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            source=row["source"],
            entry_kind=row["entry_kind"],
            authored_by=row["authored_by"],
            author_id=row["author_id"],
            author_model=row.get("author_model", "") if author_model is None else author_model,
            author_session=(
                row.get("author_session", "") if author_session is None else author_session
            ),
            density=row["density"],
            domain=row["domain"],
            tags=row["tags"],
            confidence=row["confidence"],
            salience=row["salience"],
            foundational=row["foundational"],
            related_session_id=row["related_session_id"],
            related_engram_id=row["related_engram_id"],
        )

        now = _utc_now()
        revisions = list(row["revisions"])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": f"superseded: {reason.strip()}",
            }
        )
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET active = 0,
                superseded_by = ?,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (new_id, _encode_json(revisions), now, entry_id),
        )
        self._commit()
        return new_id

    def archive_hypomnema_entry(
        self,
        entry_id: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        revised_by: str = "",
        author_session: str = "",
    ) -> str:
        """Deactivate a scoped hypomnema entry while preserving its revision trail.

        ``revised_by`` and ``author_session`` record who took it out of use (the
        model and harness session of whoever corrected or forgot it), when known.
        """
        if not reason.strip():
            raise ValueError("Archive reason cannot be empty")

        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        now = _utc_now()
        revisions = list(row["revisions"])
        revision: dict[str, Any] = {
            "at": now,
            "prior_content": row["content"],
            "reason": f"archived: {reason.strip()}",
        }
        if (revised_by or "").strip():
            revision["revised_by"] = revised_by.strip()
        if (author_session or "").strip():
            revision["revised_by_session"] = author_session.strip()
        revisions.append(revision)
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET active = 0,
                revision_count = revision_count + 1,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (_encode_json(revisions), now, entry_id),
        )
        self._commit()
        return entry_id

    def mark_hypomnema_promoted(self, entry_id: str, engram_id: str) -> None:
        """Record that a hypomnema entry graduated into a Mnemos engram."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE hypomnema_entries SET graduated_to_engram_id = ? WHERE id = ?",
            (engram_id, entry_id),
        )
        self._commit()

    # ── Capture pairs ──
    #
    # A capture is one object kept in two layers: the continuity note the
    # briefing is built from, and the memory the graph holds. Both are written
    # in one transaction, and the note records the pair
    # (``graduated_to_engram_id``). That row is the pair's only record, and it
    # is read both ways: ``capture_pair`` reaches the note and the memory from
    # either one's id. A second copy of the link, kept on the memory, could
    # disagree with the first, which is the split pairs exist to end.
    #
    # Only one capture writing them together makes a note and a memory a
    # pair (promotion pairs a note with a memory made from its own words the
    # same way). ``related_engram_id`` is a reference: a capture's note names
    # the memory it was captured as there too, but a note written through the
    # advanced tools can name a memory it only interprets or summarises, and
    # that memory is never its pair. Captures from before the pair was
    # recorded are linked once, when the store is opened by this code
    # (``_link_older_capture_pairs``), and any that older code writes later
    # are linked by the next maintenance cycle (``link_capture_pairs``).

    def save_capture_pair(
        self,
        engram: Engram,
        content: str,
        *,
        standing_mark: dict[str, str] | None = None,
        **note: Any,
    ) -> str:
        """Save a capture's memory and its continuity note together, and
        return the note's id.

        One transaction: both land or neither does. The note is paired with
        the memory (``graduated_to_engram_id``), so it is never promoted into
        a second one, and names it as the memory it was captured as
        (``related_engram_id``), unless ``related_engram_id`` is given: a note
        that replaces one referencing another memory keeps that reference.
        ``note`` takes ``write_hypomnema_entry``'s keywords. The memory's
        vector is not written here: see ``Encoder.finish``.

        ``standing_mark`` (``by``, ``session``, ``at``) marks the memory
        standing in the same transaction, signed as given: the agent said so
        when it captured it, or it replaces a memory that was.
        """
        note["related_engram_id"] = note.get("related_engram_id") or engram.id
        with self.transaction() as conn:
            self.save_engram(engram)
            note_id = self.write_hypomnema_entry(content, **note)
            conn.execute(
                "UPDATE hypomnema_entries SET graduated_to_engram_id = ? WHERE id = ?",
                (engram.id, note_id),
            )
            if standing_mark is not None:
                self.set_standing(
                    engram.id,
                    True,
                    by=standing_mark.get("by", ""),
                    session=standing_mark.get("session", ""),
                    at=standing_mark.get("at") or None,
                )
        return note_id

    @staticmethod
    def pair_memory_id(note: dict[str, Any]) -> str | None:
        """The memory a continuity note is paired with, or None: never a
        memory it only references."""
        return note.get("graduated_to_engram_id") or None

    def notes_for_engram(
        self,
        engram_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """The continuity notes in one exact scope paired with a memory,
        active ones first, then the most recently revised. A note that only
        references the memory is not among them."""
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND graduated_to_engram_id = ?"
        )
        if active_only:
            sql += " AND active = 1"
        rows = self._get_conn().execute(
            sql + " ORDER BY active DESC, last_revised_at DESC",
            (agent_id, person_id, project_scope, engram_id),
        ).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def capture_pair(
        self,
        pair_id: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
    ) -> tuple[dict[str, Any] | None, Engram | None]:
        """The continuity note and the memory one capture wrote, reached from
        either one's id, in one exact scope, in whatever state they are.

        ``(note, None)`` for a note with no memory of its own (a note that
        only references one, and a handoff or a report, which are never half
        of a pair), ``(None, engram)`` for a memory no note is paired with,
        and ``(None, None)`` when the id names neither here. From a memory,
        its note is the active one paired with it, else the one most
        recently retired with it.
        """
        scope = {"agent_id": agent_id, "person_id": person_id, "project_scope": project_scope}
        note = self.get_hypomnema_entry(pair_id, **scope)
        if note is not None:
            memory_id = self.pair_memory_id(note) if note.get("entry_kind") == "continuity" else None
            engram = self.get_engram_in_scope(memory_id, **scope) if memory_id else None
            return note, engram
        engram = self.get_engram_in_scope(pair_id, **scope)
        if engram is None:
            return None, None
        notes = self.notes_for_engram(engram.id, **scope, active_only=False)
        return (notes[0] if notes else None), engram

    def successor(
        self, pair_id: str, *, agent_id: str, person_id: str, project_scope: str,
    ) -> str | None:
        """The note or memory that replaced this one through a correction,
        in the same scope, or None: a note's ``superseded_by``, a memory's
        ``lineage.superseded_by``."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT superseded_by FROM hypomnema_entries "
            "WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?",
            (pair_id, agent_id, person_id, project_scope),
        ).fetchone()
        if row is not None:
            return row[0] or None
        row = conn.execute(
            "SELECT lineage FROM engrams "
            "WHERE id = ? AND owner_agent_id = ? AND person_id = ? AND project_scope = ?",
            (pair_id, agent_id, person_id, project_scope),
        ).fetchone()
        if row is None:
            return None
        lineage = _decode_json(row[0], {})
        return (lineage.get("superseded_by") or None) if isinstance(lineage, dict) else None

    def retire_engram(self, engram: Engram, *, reason: str) -> bool:
        """Archive a memory a correction replaced or the agent forgot, and
        say whether this changed anything.

        Its words stay, in the memory and the archive: nothing is deleted.
        One already archived for a reason like this stays exactly as it is.
        One that had only faded there is archived again with ``reason``, so
        recall no longer brings it back: it has been replaced or forgotten.
        """
        if engram.state == "archived" and self.archive_reason(engram.id) not in FADED_ARCHIVE_REASONS:
            return False
        self.archive_engram(engram, reason=reason)
        return True

    def _merge_lineage(
        self,
        conn: sqlite3.Connection,
        engram_id: str,
        *,
        supersedes: list[str] | None = None,
        superseded_by: str | None = None,
    ) -> None:
        """Add to one memory's lineage in place, keeping what it holds."""
        row = conn.execute("SELECT lineage FROM engrams WHERE id = ?", (engram_id,)).fetchone()
        if row is None:
            return
        lineage = _decode_json(row[0], {})
        if not isinstance(lineage, dict):
            lineage = {}
        if supersedes:
            lineage["supersedes"] = list(dict.fromkeys([*(lineage.get("supersedes") or []), *supersedes]))
        if superseded_by:
            lineage["superseded_by"] = superseded_by
        conn.execute(
            "UPDATE engrams SET lineage = ? WHERE id = ?", (_encode_json(lineage), engram_id)
        )

    def record_correction(
        self,
        *,
        note_id: str,
        engram_id: str,
        replaced_notes: list[str],
        replaced_engrams: list[str],
        words_before: str | None,
        resolution_before: float = 1.0,
        author_model: str = "",
        author_session: str = "",
    ) -> bool:
        """Write what a correction replaced, in one transaction, and say
        whether a version entry was written.

        The replacement pair (``note_id``, ``engram_id``) and the pair it
        replaced stay linked both ways: each replaced memory gets a
        ``supersedes`` link from the replacement and a
        ``lineage.superseded_by`` naming it, the replacement's lineage lists
        what it supersedes, and each replaced note's ``superseded_by`` names
        the replacement note. The replacement's history gains a version
        keeping ``words_before``, signed by whoever corrected, but only when
        they differ from the replacement's words: a version is written only
        when the words change.
        """
        written = False
        with self.transaction() as conn:
            for old_id in replaced_engrams:
                self._save_connection_no_commit(
                    conn,
                    engram_id,
                    Connection(target_id=old_id, relation=SUPERSEDES, strength=1.0, formed_by="correction"),
                )
                self._merge_lineage(conn, old_id, superseded_by=engram_id)
            if replaced_engrams:
                self._merge_lineage(conn, engram_id, supersedes=list(replaced_engrams))
            for old_note in replaced_notes:
                conn.execute(
                    "UPDATE hypomnema_entries SET superseded_by = ? WHERE id = ?",
                    (note_id, old_note),
                )
            row = conn.execute("SELECT content FROM engrams WHERE id = ?", (engram_id,)).fetchone()
            before = (words_before or "").strip()
            if row is not None and before and before != (row[0] or "").strip():
                conn.execute(
                    "INSERT INTO versions "
                    "(engram_id, version_num, content_snapshot, resolution_at_version, "
                    "changed_at, change_reason, author_model, author_session) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        engram_id,
                        self._next_version_num(conn, engram_id),
                        words_before,
                        resolution_before,
                        _utc_now(),
                        CORRECTION_VERSION_REASON,
                        (author_model or "").strip(),
                        (author_session or "").strip(),
                    ),
                )
                written = True
        return written

    # ── Standing memories ──
    #
    # How the human wants the agent to work in every session (see
    # STANDING_COLUMNS). The agent marks one when it captures it, or later by
    # the memory's id. A mark or an unmark is signed and dated, and changes
    # nothing else: no words, no version, no link.

    def standing_mark(self, engram_id: str) -> dict[str, str] | None:
        """Who marked this memory standing, in which session, and when
        (``by``, ``session``, ``at``); None when it is not standing."""
        if not self.has_engram_column("standing"):
            return None
        row = self._get_conn().execute(
            f"SELECT {', '.join(STANDING_COLUMNS)} FROM engrams WHERE id = ?",
            (engram_id,),
        ).fetchone()
        if row is None or not row["standing"]:
            return None
        return {
            "by": row["standing_by"] or "",
            "session": row["standing_session"] or "",
            "at": row["standing_at"] or "",
        }

    def set_standing(
        self,
        engram_id: str,
        standing: bool,
        *,
        by: str = "",
        session: str = "",
        at: str | None = None,
    ) -> bool:
        """Mark a memory standing, or unmark it, signed by ``by`` (the model)
        and ``session``, at ``at`` (now, unless given), and say whether that
        changed it.

        Only the flag and its signature are written: never the words, a
        version, a link or the memory's state. Marking a memory that is
        already marked, or unmarking one that is not, writes nothing, so the
        signature keeps saying who made the mark in force and when.
        """
        value = 1 if standing else 0
        conn = self._get_conn()
        self._begin_immediate()
        try:
            cursor = conn.execute(
                "UPDATE engrams SET standing = ?, standing_by = ?, standing_session = ?, "
                "standing_at = ? WHERE id = ? AND standing != ?",
                (
                    value,
                    (by or "").strip(),
                    (session or "").strip(),
                    at or _utc_now(),
                    engram_id,
                    value,
                ),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return cursor.rowcount == 1

    def standing_engrams(
        self, *, agent_id: str, person_id: str, project_scope: str,
    ) -> list[dict[str, Any]]:
        """The memories marked standing in one exact scope that are still in
        use, the newest mark first: each one's ``id``, ``content``, ``state``,
        ``created_at`` and mark (``standing_by``, ``standing_session``,
        ``standing_at``).

        One gone quiet is among them: decay leaves a marked memory where it
        is, and the mark is the agent's word that it belongs in every session.
        One forgotten, replaced by a correction or faded into the archive is
        not. A store opened read-only from before the mark existed has none.
        """
        if not self.has_engram_column("standing"):
            return []
        rows = self._get_conn().execute(
            "SELECT id, content, state, created_at, "
            "standing_by, standing_session, standing_at FROM engrams "
            "WHERE owner_agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND standing = 1 AND state IN ('active', 'dormant') "
            "ORDER BY standing_at DESC, id DESC",
            (agent_id, person_id, project_scope),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_hypomnema_promotion_candidates(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List stable hypomnema entries ready to become Mnemos engrams.

        A note that references a memory it interprets or summarises
        (``related_engram_id``) is never one: promoting it would pair it with
        a memory no capture wrote together with it. Nor is a note Mnemos
        wrote (``authored_by`` 'system'), such as a closed session's summary:
        promoted, its words would become a memory Mnemos wrote, and words in
        memory come only from the agent. Left out here, not skipped later, so
        such a note never holds a place another note could have.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND active = 1
              AND entry_kind = 'continuity'
              AND authored_by != 'system'
              AND graduated_to_engram_id IS NULL
              AND related_engram_id IS NULL
              AND confidence >= 0.82
              AND salience >= 0.65
              AND (revision_count >= 1 OR foundational = 1)
            ORDER BY foundational DESC, confidence DESC, salience DESC, created_at ASC
            LIMIT ?
            """,
            (agent_id, person_id, project_scope, limit),
        ).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def get_hypomnema_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict[str, int]:
        """Count hypomnema entries for a scope.

        ``hypomnema_active`` and ``hypomnema_foundational`` count live notes
        (``_NOTE_LIVE``), the ones a reader is shown: a note whose memory has
        gone quiet or faded is not counted until it comes back.
        """
        conn = self._get_conn()
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        where_sql = " AND ".join(where)
        noted_sql = " AND ".join(f"h.{condition}" for condition in where)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN {_NOTE_LIVE} THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN h.foundational = 1 AND {_NOTE_LIVE} THEN 1 ELSE 0 END) AS foundational,
              SUM(CASE WHEN h.graduated_to_engram_id IS NOT NULL THEN 1 ELSE 0 END) AS promoted
            FROM hypomnema_entries h {_NOTE_MEMORY_JOIN}
            WHERE {noted_sql}
            """,
            params,
        ).fetchone()
        candidate_query = (
            "SELECT COUNT(*) FROM hypomnema_entries "
            f"WHERE {where_sql} "
            "AND active = 1 "
            "AND entry_kind = 'continuity' "
            "AND authored_by != 'system' "
            "AND graduated_to_engram_id IS NULL "
            "AND related_engram_id IS NULL "
            "AND confidence >= 0.82 "
            "AND salience >= 0.65 "
            "AND (revision_count >= 1 OR foundational = 1)"
        )
        candidate_row = conn.execute(candidate_query, params).fetchone()
        candidates = int(candidate_row[0] or 0)
        return {
            "hypomnema_total": int(row["total"] or 0),
            "hypomnema_active": int(row["active"] or 0),
            "hypomnema_foundational": int(row["foundational"] or 0),
            "hypomnema_promoted": int(row["promoted"] or 0),
            "hypomnema_promotion_candidates": candidates,
        }

    @staticmethod
    def _hydrate_hypomnema_row(row: dict[str, Any]) -> dict[str, Any]:
        row["tags"] = _decode_json(row.pop("tags_json", "[]"), [])
        row["revisions"] = _decode_json(row.pop("revisions_json", "[]"), [])
        row["active"] = bool(row["active"])
        row["foundational"] = bool(row["foundational"])
        return row

    # ── Emotional State ──

    def save_emotional_state(
        self, state: EmotionalState, agent_id: str = "default"
    ) -> None:
        """Save an emotional state snapshot to history."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO emotional_state_history "
            "(agent_id, curiosity, restlessness, warmth, clarity, "
            "creative_flow, isolation, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                state.curiosity,
                state.restlessness,
                state.warmth,
                state.clarity,
                state.creative_flow,
                state.isolation,
                state.timestamp,
            ),
        )
        self._commit()

    def get_latest_emotional_state(
        self, agent_id: str = "default"
    ) -> EmotionalState | None:
        """Get the most recent emotional state for an agent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM emotional_state_history "
            "WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return EmotionalState.from_dict(dict(row))

    # ── Identity ──

    # ── Reflection queue ──────────────────────────────────────────────
    #
    # Work the agent does on its own memory. Maintenance proposes; the agent
    # answers through mnemos_reflect. Nothing here ever writes an answer on
    # the agent's behalf.

    MAX_SURFACINGS = 3

    def enqueue_reflection(
        self,
        kind: str,
        target_id: str,
        prompt: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        expires_in_days: int = 30,
    ) -> str | None:
        """Propose a reflection. Returns None if this was already asked.

        Asking twice about the same memory is nagging, so the unique index
        makes a repeat enqueue a no-op rather than a duplicate. A
        reaffirmation only collides with one still waiting: it is asked
        again about the same belief once the last one is answered.

        No excerpt is stored. The queue holds a ``target_id`` and nothing else
        quotable, so ``pending_reflections`` resolves the text live and a
        forgotten memory has no second copy here to leak from.
        """
        if kind not in REFLECTION_KINDS:
            raise ValueError(f"Unsupported reflection kind: {kind}")

        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=expires_in_days)).isoformat()
        entry_id = _new_id()
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO reflection_queue(
                    id, agent_id, person_id, project_scope, kind, target_id,
                    prompt, excerpt, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (entry_id, agent_id, person_id, project_scope, kind, target_id,
                 prompt, now.isoformat(), expires),
            )
            self._commit()
        except sqlite3.IntegrityError:
            # Already asked. The failed INSERT leaves an open transaction, so
            # it must be rolled back — otherwise the next write on this
            # connection dies with "cannot start a transaction within a
            # transaction", far from the cause.
            self._rollback()
            return None
        return entry_id

    def pending_reflections(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Unanswered reflections worth showing, oldest and least-shown first.

        ``excerpt`` is resolved from the engram **at read time**, and the join
        drops any request whose subject is gone or archived. Both properties
        are load-bearing:

        * The queue used to carry a frozen ``content[:160]`` snapshot, so a
          memory the human had asked Mnemos to forget was read back to the
          agent until the surfacing quota ran out. Clearing by quota is not
          deletion.
        * A request pointing at an archived engram is a dead end — ``reflect()``
          answers "the memory is no longer there" — so surfacing one spends the
          agent's turn to reach nothing.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            """
            SELECT q.*, e.content AS live_content
            FROM reflection_queue q
            JOIN engrams e ON e.id = q.target_id
            WHERE q.agent_id = ? AND q.person_id = ? AND q.project_scope = ?
              AND q.answered_at IS NULL
              AND q.surfaced_count < ?
              AND (q.expires_at IS NULL OR q.expires_at > ?)
              AND e.state != 'archived'
            ORDER BY q.surfaced_count ASC, q.created_at ASC
            LIMIT ?
            """,
            (agent_id, person_id, project_scope, self.MAX_SURFACINGS, now, limit),
        ).fetchall()

        items = []
        for row in rows:
            item = dict(row)
            item["excerpt"] = " ".join((item.pop("live_content") or "").split())[:160]
            items.append(item)
        return items

    def purge_stale_reflections(self) -> int:
        """Drop unanswered requests whose subject is archived or gone.

        ``pending_reflections`` already refuses to surface these, so this is
        about the copy on disk rather than the one on screen. Stores written
        before excerpts were removed still hold a frozen ``content[:160]`` of
        memories the human may since have asked Mnemos to forget, and no
        deletion path reached it. Existing stores do not otherwise self-heal.

        Returns the number of rows removed.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            DELETE FROM reflection_queue
            WHERE answered_at IS NULL
              AND target_id NOT IN (SELECT id FROM engrams WHERE state != 'archived')
            """
        )
        self._commit()
        return cur.rowcount or 0

    def mark_reflections_surfaced(self, ids: list[str]) -> None:
        """Record that these were shown, so an ignored item eventually stops."""
        if not ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE reflection_queue SET surfaced_count = surfaced_count + 1 WHERE id = ?",
            [(i,) for i in ids],
        )
        self._commit()

    def answer_reflection(
        self,
        target_id: str,
        answer: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        reflection_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the agent's answer. Returns the item, or None if not pending.

        Answers the oldest pending ask about ``target_id``, or exactly the ask
        ``reflection_id`` names when given.
        """
        conn = self._get_conn()
        if reflection_id is not None:
            row = conn.execute(
                """
                SELECT * FROM reflection_queue
                WHERE agent_id = ? AND person_id = ? AND project_scope = ?
                  AND target_id = ? AND id = ? AND answered_at IS NULL
                """,
                (agent_id, person_id, project_scope, target_id, reflection_id),
            ).fetchone()
        else:
            row = self.pending_reflection_for(
                target_id, agent_id=agent_id, person_id=person_id,
                project_scope=project_scope,
            )
        if row is None:
            return None
        conn.execute(
            "UPDATE reflection_queue SET answered_at = ?, answer = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), answer, row["id"]),
        )
        self._commit()
        return dict(row)

    def pending_reflection_for(
        self,
        target_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any] | None:
        """The ask ``answer_reflection`` would answer about ``target_id``.

        Reads only: the ask stays pending and its showings are unchanged.
        """
        row = self._get_conn().execute(
            """
            SELECT * FROM reflection_queue
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND target_id = ? AND answered_at IS NULL
            ORDER BY created_at ASC LIMIT 1
            """,
            (agent_id, person_id, project_scope, target_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def reflection_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, int]:
        conn = self._get_conn()
        scope = (agent_id, person_id, project_scope)
        where = "agent_id = ? AND person_id = ? AND project_scope = ?"
        pending = conn.execute(
            f"SELECT COUNT(*) FROM reflection_queue WHERE {where} AND answered_at IS NULL",
            scope,
        ).fetchone()[0]
        answered = conn.execute(
            f"SELECT COUNT(*) FROM reflection_queue WHERE {where} AND answered_at IS NOT NULL",
            scope,
        ).fetchone()[0]
        return {"pending": pending, "answered": answered}

    def save_identity(self, identity: AgentIdentity) -> None:
        """Save identity while preserving its append-only kernel and history."""
        conn = self._get_conn()
        agent_id = identity.memory_profile.agent_id
        existing = self.get_identity(agent_id)
        if existing is not None:
            if identity.kernel_id != existing.kernel_id:
                raise ValueError("Identity kernel_id is immutable")
            self._validate_identity_invariants(existing.invariants, identity.invariants)
            old_history = [epoch.to_dict() for epoch in existing.epoch_history]
            new_history = [epoch.to_dict() for epoch in identity.epoch_history]
            if new_history[:len(old_history)] != old_history:
                raise ValueError("Identity epoch history is append-only")
            if identity.epoch_state.epoch_number < existing.epoch_state.epoch_number:
                raise ValueError("Identity epoch number cannot move backward")
        data = identity.to_dict()
        data["agent_id"] = agent_id
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "agent_id")

        conn.execute(
            f"INSERT INTO agent_identity ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
            list(data.values()),
        )
        self._commit()

    @classmethod
    def _validate_identity_invariants(cls, old: Any, new: Any, path: str = "invariants") -> None:
        """Reject removal or rewriting of any existing invariant value."""
        if isinstance(old, dict):
            if not isinstance(new, dict):
                raise ValueError(f"Identity {path} cannot change type")
            for key, value in old.items():
                if key not in new:
                    raise ValueError(f"Identity {path}.{key} cannot be removed")
                cls._validate_identity_invariants(value, new[key], f"{path}.{key}")
            return
        if isinstance(old, list):
            if not isinstance(new, list) or new[:len(old)] != old:
                raise ValueError(f"Identity {path} is append-only")
            return
        if new != old:
            raise ValueError(f"Identity {path} is immutable")

    def get_identity(self, agent_id: str = "default") -> AgentIdentity | None:
        """Load agent identity."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM agent_identity WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        return AgentIdentity.from_dict(dict(row))

    # ── Meta ──

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a meta value. Returns default when the key is absent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta value."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._commit()

    def min_code_version(self) -> int | None:
        """The lowest maintenance code version allowed to maintain this store.

        None when no version has recorded itself yet, or the value is not a
        number (the next raise overwrites it).
        """
        value = self.get_meta(MIN_CODE_VERSION_KEY)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def raise_min_code_version(self, version: int) -> int:
        """Record that code at ``version`` has opened this store.

        Only ever raises the value: code older than the stored minimum leaves
        it alone. Both statements run in one immediate transaction, so two
        servers starting together cannot interleave a read and a write and
        lower what the newer one set. Returns the minimum now stored.
        """
        conn = self._get_conn()
        self._begin_immediate()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                (MIN_CODE_VERSION_KEY, str(int(version))),
            )
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = ? AND CAST(value AS INTEGER) < ?",
                (str(int(version)), MIN_CODE_VERSION_KEY, int(version)),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        stored = self.min_code_version()
        return stored if stored is not None else int(version)

    # ── Trace: what each tool call saw and wrote ──

    def record_trace(
        self,
        *,
        tool: str,
        agent_id: str,
        person_id: str,
        project_scope: str,
        session: str = "",
        author_model: str = "",
        read_ids: list[str] | tuple[str, ...] = (),
        written_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Record one tool call: the ids it showed or returned, and the ids it
        wrote. Ids only, never text. Rows older than ``TRACE_KEEP_DAYS`` go in
        the same transaction, so the table stays small."""
        now = datetime.now(timezone.utc)
        conn = self._get_conn()
        try:
            self._begin_immediate()
            conn.execute(
                "INSERT INTO memory_trace (at, tool, agent_id, person_id, project_scope, "
                "session, author_model, read_ids, written_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(), tool, agent_id, person_id, project_scope,
                    session or "", author_model or "",
                    json.dumps(list(dict.fromkeys(read_ids))),
                    json.dumps(list(dict.fromkeys(written_ids))),
                ),
            )
            conn.execute(
                "DELETE FROM memory_trace WHERE at < ?",
                ((now - timedelta(days=TRACE_KEEP_DAYS)).isoformat(),),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def set_min_code_version(self, version: int) -> None:
        """Set the minimum outright, lower or higher. Only a human's reset
        (`mnemos repair min-code-version`) does this; code opening the store
        only ever raises it."""
        if int(version) < 1:
            raise ValueError("The minimum code version is 1 or more.")
        self.set_meta(MIN_CODE_VERSION_KEY, str(int(version)))

    # ── Consolidation Log ──

    def log_consolidation(
        self,
        log_id: str,
        pass_name: str,
        started_at: str,
        completed_at: str | None = None,
        stats: dict | None = None,
        agent_id: str | None = None,
        person_id: str | None = None,
        project_scope: str | None = None,
    ) -> None:
        """Log a consolidation pass."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_log "
            "(id, agent_id, person_id, project_scope, pass_name, started_at, completed_at, stats) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (log_id, agent_id, person_id, project_scope, pass_name, started_at,
             completed_at, json.dumps(stats or {})),
        )
        self._commit()

    def get_consolidation_runs(
        self, pass_name: str, limit: int = 5, *, agent_id: str | None = None,
        person_id: str | None = None, project_scope: str | None = None,
    ) -> list[dict]:
        """Most recent consolidation_log rows for a pass, newest first.

        The stats column is JSON-decoded. The table has no agent_id
        column; passes that need agent scoping carry it inside stats.
        """
        conn = self._get_conn()
        query = "SELECT * FROM consolidation_log WHERE pass_name = ?"
        params: list[Any] = [pass_name]
        if agent_id is not None and person_id is not None and project_scope is not None:
            query += " AND agent_id = ? AND person_id = ? AND project_scope = ?"
            params.extend([agent_id, person_id, project_scope])
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["stats"] = json.loads(item.get("stats") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["stats"] = {}
            out.append(item)
        return out

    # ── Stats ──

    def get_stats(
        self, agent_id: str = "default", *, person_id: str | None = None,
        project_scope: str | None = None,
    ) -> dict:
        """Get summary statistics for an agent's memory."""
        conn = self._get_conn()
        stats = {}

        # Engram counts by state
        for state in ("active", "consolidating", "dormant", "archived"):
            if person_id is not None and project_scope is not None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? "
                    "AND person_id = ? AND project_scope = ? AND state = ?",
                    (agent_id, person_id, project_scope, state),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?",
                    (agent_id, state),
                ).fetchone()
            stats[f"engrams_{state}"] = row[0] if row else 0

        # Connection count
        if person_id is not None and project_scope is not None:
            row = conn.execute(
                """SELECT COUNT(*) FROM connections c JOIN engrams e ON e.id = c.source_id
                   WHERE e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?""",
                (agent_id, person_id, project_scope),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        stats["connections"] = row[0] if row else 0

        # Belief count
        row = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE agent_id = ? AND superseded_by IS NULL",
            (agent_id,),
        ).fetchone()
        stats["beliefs_active"] = row[0] if row else 0

        # Version count (reconsolidation events)
        if person_id is not None and project_scope is not None:
            row = conn.execute(
                """SELECT COUNT(*) FROM versions v JOIN engrams e ON e.id = v.engram_id
                   WHERE e.owner_agent_id = ? AND e.person_id = ? AND e.project_scope = ?""",
                (agent_id, person_id, project_scope),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        stats["reconsolidation_events"] = row[0] if row else 0

        # Archive count
        stats["archived"] = stats["engrams_archived"]

        # Hypomnema counts use the default person/project scope for status.
        stats.update(self.get_hypomnema_stats(
            agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        ))

        # Functional memory counts cover active working context and review load.
        stats.update(self.get_functional_stats(
            agent_id=agent_id, person_id=person_id, project_scope=project_scope,
        ))

        # Accessibility distribution
        scope_sql = ""
        params: list[Any] = [agent_id]
        if person_id is not None and project_scope is not None:
            scope_sql = " AND person_id = ? AND project_scope = ?"
            params.extend([person_id, project_scope])
        rows = conn.execute(
            "SELECT AVG(accessibility) as avg_acc, MIN(accessibility) as min_acc, "
            "MAX(accessibility) as max_acc FROM engrams "
            "WHERE owner_agent_id = ? AND state = 'active'" + scope_sql,
            params,
        ).fetchone()
        if rows and rows["avg_acc"] is not None:
            stats["accessibility_avg"] = round(rows["avg_acc"], 3)
            stats["accessibility_min"] = round(rows["min_acc"], 3)
            stats["accessibility_max"] = round(rows["max_acc"], 3)

        return stats


class ReadOnlyEngramStore(EngramStore):
    """An existing store opened so that nothing can change it.

    Opening an ``EngramStore`` is itself a write: it migrates the schema and
    stamps ``schema_version`` and the minimum code version on every open,
    which can rewrite the file even when nothing else happens, and would tell
    older code to stand down. A diagnostic has to leave the store exactly as it
    found it, so this skips all of that and asks SQLite for a read-only
    connection. Every read works as usual; any write raises
    ``sqlite3.OperationalError`` instead of landing. The store must exist:
    looking at memory never brings a store into being.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"No Mnemos store at {self.db_path}")
        self._conn: sqlite3.Connection | None = None
        self._transaction_depth = 0

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"{self.db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn
