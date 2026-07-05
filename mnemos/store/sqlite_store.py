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
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.engram import Connection, Engram, VersionRef
from ..core.belief import Belief
from ..core.emotional_state import EmotionalState
from ..core.identity import AgentIdentity
from .migrations import (
    apply_u3a_schema_migration,
    apply_u3b_hardening_schema_migration,
    apply_u6_6_inner_life_schema_migration,
    get_current_version,
    run_migrations,
)
from .read_visibility import (
    HYPO_PROMOTION_MIN_CONFIDENCE,
    HYPO_PROMOTION_MIN_SALIENCE,
    HYPO_REVIEW_CANDIDATE_SQL,
    READ_VISIBILITY_AUDIT as READ_VISIBILITY_AUDIT,
    READ_VISIBILITY_OPERATIONAL,
    READ_VISIBILITY_REVIEW,
    VALID_READ_VISIBILITIES,
    classify_hypomnema_read_visibility,
)


# Schema version — increment when tables change
SCHEMA_VERSION = 9

VALID_PROPOSAL_AUTHORITIES = {
    "user_stated",
    "imported",
    "observed",
    "generated",
}
VALID_PROPOSAL_KINDS = {
    "episodic",
    "semantic",
    "procedural",
    "prospective",
}
PROPOSAL_AUTHORITY_ALIASES = {
    "agent_generated": "generated",
    "agent_observed": "observed",
    "system_policy": "generated",
    "operator_review": "observed",
}
PROPOSAL_KIND_ALIASES = {
    "belief": "semantic",
    "engram": "episodic",
    "hypomnema": "semantic",
    "functional_memory": "prospective",
    "identity": "semantic",
    "modulation": "prospective",
    "correction": "semantic",
    "promotion": "semantic",
}
VALID_PROPOSAL_TARGET_SURFACES = {
    "engrams",
    "beliefs",
    "hypomnema_entries",
    "functional_memories",
    "dynamic_modulations",
    "identity_profile",
    "runtime_context",
}
VALID_PROPOSAL_BLAST_RADII = {"low", "medium", "high", "identity", "foundational"}
VALID_PROPOSAL_STATUSES = {
    "pending_review",
    "deferred",
    "approved",
    "rejected",
    "applied",
    "superseded",
}
RAW_PROPOSAL_STATUSES = {"pending_review", "deferred", "rejected"}
# Terminal for raw write_proposal upserts. Future reviewed decision APIs may
# decide deferred proposals through a separate append-only audit path.
PROPOSAL_TERMINAL_STATUSES = VALID_PROPOSAL_STATUSES - {"pending_review"}

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

VALID_HYPO_SOURCES = {"observed", "synthesized", "co-formed"}
VALID_HYPO_DOMAINS = {
    "foundational",
    "identity",
    "recurring",
    "long-arc",
    "topical",
    "situational",
}
# RFC domain axis for proposal rows (T3/D7): the six hypomnema domains plus
# "general" (the legitimate low-blast catch-all the raw producer API defaults
# to). Enum-checked in write_proposal so a producer cannot pass an unknown
# domain (merge-review §5.1(b)).
VALID_PROPOSAL_DOMAINS = VALID_HYPO_DOMAINS | {"general"}

VALID_INNER_LIFE_EVENT_TYPES = {
    "session_finalized",
    "turn_finalized",
    "turn_message",
    "tool_event",
    "file_event",
    "test_outcome",
    "skip",
    "error",
}

# Allowed column names for engrams table — prevents SQL injection via to_dict() keys
_ENGRAM_COLUMNS = frozenset(
    {
        "id",
        "content",
        "content_at_encoding",
        "impact",
        "resolution",
        "kind",
        "tags",
        "schema_refs",
        "strength",
        "stability",
        "accessibility",
        "encoding_context",
        "source",
        "lineage",
        "owner_agent_id",
        "visibility",
        "state",
        "created_at",
        "last_accessed",
        "access_count",
        "reconsolidation_count",
        "voice_exemplar_eligible",
        "softening_protected",
        "original_substrate",
        "original_timestamp",
        "consolidation_authorized",
        "decay_protected",
        "read_visibility",
    }
)

# Allowed column names for beliefs table
_BELIEF_COLUMNS = frozenset(
    {
        "id",
        "agent_id",
        "content",
        "confidence",
        "domain",
        "created_at",
        "last_revised",
        "last_challenged",
        "revision_history",
        "superseded_by",
        "supporting_engram_ids",
        "tier",
        "needs_review",
        "confidence_pending_review",
        "read_visibility",
        "decision_ref",
    }
)

SQL_CREATE_TABLES = """
-- Core engram storage
CREATE TABLE IF NOT EXISTS engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
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
    visibility TEXT NOT NULL DEFAULT 'private',
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0,
    voice_exemplar_eligible INTEGER NOT NULL DEFAULT 1
        CHECK (voice_exemplar_eligible IN (0, 1)),
    softening_protected INTEGER NOT NULL DEFAULT 0
        CHECK (softening_protected IN (0, 1)),
    original_substrate TEXT,
    original_timestamp INTEGER,
    consolidation_authorized INTEGER NOT NULL DEFAULT 1
        CHECK (consolidation_authorized IN (0, 1)),
    decay_protected INTEGER NOT NULL DEFAULT 0
        CHECK (decay_protected IN (0, 1))
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
    PRIMARY KEY (engram_id, version_num)
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
    tier TEXT CHECK (tier IS NULL OR tier IN ('foundational', 'operational', 'tactical')),
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    confidence_pending_review INTEGER NOT NULL DEFAULT 0
        CHECK (confidence_pending_review IN (0, 1)),
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    -- T4 vault: journal-line hash licensing an identity-tier row to be operational.
    -- NULL/'' on an identity/foundational row → forced review_only by the read-path validator.
    decision_ref TEXT
);

-- Hypomnema: scoped durable continuity that can revise before promotion
CREATE TABLE IF NOT EXISTS hypomnema_entries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'observed'
        CHECK (source IN ('observed', 'synthesized', 'co-formed')),
    density REAL NOT NULL DEFAULT 0.5,
    domain TEXT NOT NULL DEFAULT 'topical'
        CHECK (domain IN ('foundational', 'identity', 'recurring', 'long-arc', 'topical', 'situational')),
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    foundational INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    revisions_json TEXT NOT NULL DEFAULT '[]',
    original_timestamp INTEGER,
    related_session_id TEXT,
    related_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    graduated_to_engram_id TEXT REFERENCES engrams(id) ON DELETE SET NULL,
    superseded_by TEXT REFERENCES hypomnema_entries(id),
    created_at TEXT NOT NULL,
    last_revised_at TEXT NOT NULL,
    last_challenged_at TEXT,
    -- T4 vault: journal-line hash licensing an identity-tier row to be operational.
    -- NULL/'' on an identity/foundational row → forced review_only by the read-path validator.
    decision_ref TEXT
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
    read_visibility TEXT NOT NULL DEFAULT 'operational_context'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
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
    pass_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}'
);

-- Private U6.6 inner-life provenance ledger. These rows are operational
-- evidence below memory; they are not engrams, hypomnema, beliefs, identity
-- patches, candidates, or shared-pool publications.
CREATE TABLE IF NOT EXISTS inner_life_events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'session_finalized', 'turn_finalized', 'turn_message',
            'tool_event', 'file_event', 'test_outcome', 'skip', 'error'
        )),
    process_name TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    session_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    role TEXT,
    source_message_id TEXT,
    source_path TEXT,
    source_timestamp TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    content_excerpt TEXT NOT NULL DEFAULT '',
    event_tags_json TEXT NOT NULL DEFAULT '[]',
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    rollout_tag TEXT NOT NULL DEFAULT '',
    gate_decision TEXT NOT NULL DEFAULT 'ledger_only',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- PAI import source-to-row map for idempotent importer re-runs and repair
CREATE TABLE IF NOT EXISTS pai_import_row_map (
    job_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_anchor TEXT NOT NULL DEFAULT '',
    target_table TEXT NOT NULL DEFAULT 'engrams'
        CHECK (target_table IN ('engrams', 'beliefs', 'hypomnema_entries')),
    target_id TEXT NOT NULL,
    engram_id TEXT,
    source_hash TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    imported_at INTEGER NOT NULL,
    PRIMARY KEY (job_id, source_path, source_anchor, target_table)
);

-- Afferent Membrane proposal ledger: generated candidates and durable transitions
CREATE TABLE IF NOT EXISTS proposal_ledger (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    source_authority TEXT NOT NULL
        CHECK (source_authority IN ('user_stated', 'imported', 'observed', 'generated')),
    kind TEXT NOT NULL
        CHECK (kind IN ('episodic', 'semantic', 'procedural', 'prospective')),
    domain TEXT NOT NULL DEFAULT 'general',
    target_surface TEXT NOT NULL
        CHECK (target_surface IN (
            'engrams', 'beliefs', 'hypomnema_entries',
            'functional_memories', 'dynamic_modulations',
            'identity_profile', 'runtime_context'
        )),
    transition TEXT NOT NULL,
    blast_radius TEXT NOT NULL DEFAULT 'medium'
        CHECK (blast_radius IN ('low', 'medium', 'high', 'identity', 'foundational')),
    read_visibility TEXT NOT NULL DEFAULT 'audit_only'
        CHECK (read_visibility IN ('operational_context', 'review_only', 'audit_only')),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review', 'deferred', 'approved', 'rejected', 'applied', 'superseded')),
    reason TEXT NOT NULL DEFAULT '',
    gate_version TEXT NOT NULL DEFAULT 'affmem-v1',
    target_id TEXT,
    provenance_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    decided_at INTEGER,
    applied_at INTEGER
);

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
CREATE INDEX IF NOT EXISTS idx_engrams_last_accessed ON engrams(last_accessed);
CREATE INDEX IF NOT EXISTS idx_connections_source ON connections(source_id);
CREATE INDEX IF NOT EXISTS idx_connections_target ON connections(target_id);
CREATE INDEX IF NOT EXISTS idx_beliefs_domain ON beliefs(agent_id, domain);
CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_target
    ON pai_import_row_map(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_pai_import_row_map_job
    ON pai_import_row_map(job_id);
CREATE INDEX IF NOT EXISTS idx_hypomnema_scope_revised
    ON hypomnema_entries(agent_id, person_id, project_scope, last_revised_at DESC)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_hypomnema_promotion
    ON hypomnema_entries(agent_id, project_scope, created_at)
    WHERE active = 1 AND graduated_to_engram_id IS NULL;
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
CREATE INDEX IF NOT EXISTS idx_proposal_ledger_status_scope
    ON proposal_ledger(agent_id, person_id, project_scope, status, created_at);
CREATE INDEX IF NOT EXISTS idx_proposal_ledger_visibility
    ON proposal_ledger(read_visibility, status);
CREATE INDEX IF NOT EXISTS idx_emotional_history_agent ON emotional_state_history(agent_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_inner_life_events_scope
    ON inner_life_events(agent_id, person_id, project_scope, created_at);
CREATE INDEX IF NOT EXISTS idx_inner_life_events_session
    ON inner_life_events(session_id, event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_inner_life_events_rollout
    ON inner_life_events(rollout_tag, event_type, created_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    text_terms = _tokenize(text)
    if not text_terms:
        return 0.0
    return len(query_terms & text_terms) / max(1, len(query_terms))


_READ_VISIBILITY_DEFAULT = object()

_READ_VISIBILITY_ORDER = {
    READ_VISIBILITY_OPERATIONAL: 0,
    READ_VISIBILITY_REVIEW: 1,
    READ_VISIBILITY_AUDIT: 2,
}


def _normalize_read_visibility(
    value: str | None,
    *,
    allow_all: bool = True,
    default: str = READ_VISIBILITY_OPERATIONAL,
) -> str | None:
    if value is None and allow_all:
        return None
    read_visibility = (value if value is not None else default).strip()
    if not read_visibility:
        read_visibility = default
    if read_visibility not in VALID_READ_VISIBILITIES:
        raise ValueError(f"Unsupported read_visibility: {read_visibility}")
    return read_visibility


def _normalize_read_visibility_values(
    value: str | Sequence[str] | None,
    *,
    allow_all: bool = True,
    default: str = READ_VISIBILITY_OPERATIONAL,
) -> tuple[str, ...] | None:
    if value is None:
        return None if allow_all else (default,)
    if isinstance(value, str):
        return (_normalize_read_visibility(value, allow_all=False, default=default),)
    values = tuple(
        _normalize_read_visibility(item, allow_all=False, default=default)
        for item in value
    )
    if not values:
        raise ValueError("read_visibility must include at least one visibility")
    return values


def _append_read_visibility_filter(
    sql: str,
    params: list[Any],
    column: str,
    values: tuple[str, ...] | None,
) -> str:
    if values is None:
        return sql
    placeholders = ", ".join("?" for _ in values)
    params.extend(values)
    return f"{sql} AND {column} IN ({placeholders})"


def _append_hypomnema_review_candidate_filter(
    sql: str,
    params: list[Any],
    *,
    negate: bool = False,
) -> str:
    params.extend([HYPO_PROMOTION_MIN_CONFIDENCE, HYPO_PROMOTION_MIN_SALIENCE])
    if negate:
        return f"{sql} AND NOT ({HYPO_REVIEW_CANDIDATE_SQL})"
    return f"{sql} AND ({HYPO_REVIEW_CANDIDATE_SQL})"


def _apply_read_visibility_filter(
    sql: str,
    params: list[Any],
    column: str,
    read_visibility: str | Sequence[str] | None,
) -> str:
    normalized = _normalize_read_visibility_values(read_visibility)
    return _append_read_visibility_filter(sql, params, column, normalized)


# Identity-tier signal per table — the columns that make a row identity/foundational.
# Only these two tables can express it; engrams/functional_memories cannot (no
# domain/tier column), so identity content never lands there (apply rejects them).
# 008g-r6 #4 (bug caught by test): tier is nullable on beliefs, so a plain
# `tier = 'foundational' OR ...` evaluates to NULL for regular beliefs (tier
# is NULL) — then `NOT NULL` is NULL, which SQL treats as false in WHERE,
# silently excluding non-identity beliefs from ANY query that carries the
# gate. IFNULL forces a defined value so the OR short-circuits correctly.
_IDENTITY_TIER_SIGNAL_SQL = {
    "beliefs": (
        "(IFNULL(tier, '') = 'foundational' "
        "OR domain IN ('identity', 'foundational'))"
    ),
    "hypomnema_entries": "(foundational = 1 OR domain IN ('identity', 'foundational'))",
}


_CANONICAL_VAULT_DIR = "/usr/local/var/mnemos-vault"
_CANONICAL_VAULT_JOURNAL = "/usr/local/var/mnemos-vault/decisions.jsonl"

# 008r-review (env-resolver-redirects-agent-vault): tests-only injection seam,
# SPLIT from production. resolve_vault_journal_path() reads NO environment — an
# agent controls its own env, so honoring MNEMOS_VAULT_JOURNAL let it redirect
# every vault read/write to a fake journal, or set the var empty to DISABLE the
# gate outright (identity rows read operational, unverified, until the next
# watchdog cron). These default to the pinned canonical paths; only tests
# reassign them (monkeypatch-class — a subprocess or separate MCP server never
# inherits them, unlike env). Production cannot reach this channel.
_VAULT_DIR_FOR_RESOLUTION = _CANONICAL_VAULT_DIR
_VAULT_JOURNAL_FOR_RESOLUTION = _CANONICAL_VAULT_JOURNAL


def _resolve_vault_active(vault_active: bool | None) -> bool:
    """Infer whether the T4 read-path validator enforces (008e E1).

    Explicit bool wins. Otherwise the vault is active iff
    ``resolve_vault_journal_path()`` is non-None — i.e. the canonical vault
    directory exists (008r-review: no env channel; the install *is* the
    directory appearing at ``/usr/local/var/mnemos-vault``). Tests that need the
    gate armed/inert set ``_VAULT_DIR_FOR_RESOLUTION`` (the conftest points it at
    a non-existent path by default so no test arms against a system vault by
    accident). Statted once at store construction, never on the read path.
    """
    if vault_active is not None:
        return vault_active
    return resolve_vault_journal_path() is not None


def _vault_object_trusted(path: str) -> bool:
    """True iff ``path`` is a root/vault-owned object the agent cannot forge.

    008r-review (vault-resolver-trusts-unverified-path): the arm-check must not
    trust a path the current (agent) user owns or can write — otherwise a
    pre-created fake vault dir + journal on a user-writable /usr/local would be
    treated as installed. Rejected iff the current process owns the object or
    can write it (``os.access`` honors macOS ACLs). Root (uid 0) is trusted (the
    vault user / installer context). Tests monkeypatch this to accept the tmp
    fixtures they own.
    """
    import os as _os
    try:
        st = _os.stat(path)
    except OSError:
        return False  # unstattable → not trustworthy
    uid = _os.getuid()
    if uid == 0:
        return True
    if st.st_uid == uid:
        return False  # the agent owns it → could have forged it
    if _os.access(path, _os.W_OK):
        return False  # agent-writable (POSIX bits or ACL) → forgeable
    return True


def resolve_vault_journal_path() -> str | None:
    """Return the canonical vault journal path if the vault is installed, else None.

    008r-review (env-resolver-redirects-agent-vault): NO environment channel.
    Single source of truth for the arm-check and downstream consumers (apply,
    apply_legacy_witness, session-start reconcile, read-path gate activation).
    An agent controls its own environment; honoring ``MNEMOS_VAULT_JOURNAL``
    let it redirect all of these to a fake journal, or set it empty to disable
    the gate. Removed — production pins the canonical path.

    The vault is ARMED iff its DIRECTORY exists — David's install *is* the
    directory appearing at ``/usr/local/var/mnemos-vault``.

    **008g-r6 fail-CLOSED preserved.** The dir and journal are root/vault-owned
    (0750 / 0640 uappnd); the agent cannot delete them to disarm. If the journal
    FILE is missing while the dir exists (a privileged deletion, a typo during a
    manual repair), the path still arms and ``read_journal([])`` returns empty →
    the reconciler re-quarantines every stamped identity row rather than
    silently fail-open. Only the *absence of the vault dir* (never installed, or
    a deliberate root-level uninstall) is inert.

    Tests inject a fixture via ``_VAULT_DIR_FOR_RESOLUTION`` /
    ``_VAULT_JOURNAL_FOR_RESOLUTION`` and monkeypatch ``_vault_object_trusted``
    to accept the tmp fixtures they own (production never sets them).
    """
    vault_dir = _VAULT_DIR_FOR_RESOLUTION
    if not Path(vault_dir).exists():
        return None  # pre-install → inert
    # 008r-review (vault-resolver-trusts-unverified-path): existence is not
    # enough. On a user-writable /usr/local (Homebrew — the installer's own
    # ancestor check warns about it), an agent could pre-create the vault dir +
    # a fake decisions.jsonl and make every vault read/apply/session-start trust
    # an attacker-controlled journal. So the DIRECTORY must be root/vault-owned
    # and not agent-writable (os.access honors ACLs). An agent-owned dir means
    # no real vault was ever installed here → inert (no stamped rows to protect).
    if not _vault_object_trusted(vault_dir):
        return None
    # 008r-review (vault-untrusted-journal-disarms-gate): the dir is a TRUSTED
    # install, so ARM unconditionally — return the canonical journal path even
    # if the journal is missing or (despite the 0750 dir that should prevent it)
    # untrusted. That is an installed-but-BROKEN state, and the gate must FAIL
    # CLOSED: armed → session-start reconciles, sees the missing/empty/broken
    # journal, and re-quarantines every stamped identity row. Returning None
    # here on an untrusted journal (an earlier revision did) is fail-OPEN —
    # reads would skip the identity gate and reconciliation would not run.
    return _VAULT_JOURNAL_FOR_RESOLUTION


def identity_decision_gate_sql(table: str, *, active: bool) -> str:
    """Return the T4 vault gate SQL fragment (008e E3: single source of truth).

    Empty string when inert or the table has no identity signal — safe to
    unconditionally append. The predicate is **row-by-row against the row's own
    ``read_visibility``** (008e #4 correction): an identity-tier row without a
    ``decision_ref`` is excluded only from *operational* reads, so a mixed
    ``(operational_context, review_only)`` request drops the unwitnessed row
    from the operational half of the union but keeps it visible for review.

    Same predicate used by ``EngramStore`` and by raw-SQL peer surfaces (e.g.
    the visualization dashboard) — no textual duplication, no drift risk.
    """
    if not active:
        return ""
    tier_signal = _IDENTITY_TIER_SIGNAL_SQL.get(table)
    if tier_signal is None:
        return ""
    return (
        f" AND NOT ({table}.read_visibility = '{READ_VISIBILITY_OPERATIONAL}' "
        f"AND {tier_signal} "
        "AND (decision_ref IS NULL OR decision_ref = ''))"
    )


def _append_identity_decision_gate(
    sql: str,
    table: str,
    visibility_values: tuple[str, ...] | None,
    active: bool,
) -> str:
    """Store-side compose helper: wraps :func:`identity_decision_gate_sql`.

    No-op when the caller opts fully into admin (``visibility_values is None``);
    the row-by-row predicate is safe even when operational_context is present
    alongside review_only in a mixed set (008e #4).
    """
    if visibility_values is None:
        return sql
    return sql + identity_decision_gate_sql(table, active=active)


def _stricter_read_visibility(
    existing_visibility: str,
    incoming_visibility: str,
) -> str:
    return max(
        (existing_visibility, incoming_visibility),
        key=lambda value: _READ_VISIBILITY_ORDER.get(value, 0),
    )


def _strictest_read_visibility_sql(table_name: str) -> str:
    return (
        "CASE "
        f"WHEN {table_name}.read_visibility = '{READ_VISIBILITY_AUDIT}' "
        f"OR excluded.read_visibility = '{READ_VISIBILITY_AUDIT}' "
        f"THEN '{READ_VISIBILITY_AUDIT}' "
        f"WHEN {table_name}.read_visibility = '{READ_VISIBILITY_REVIEW}' "
        f"OR excluded.read_visibility = '{READ_VISIBILITY_REVIEW}' "
        f"THEN '{READ_VISIBILITY_REVIEW}' "
        f"ELSE '{READ_VISIBILITY_OPERATIONAL}' "
        "END"
    )


def _preserved_quarantined_flag_sql(table_name: str, column_name: str) -> str:
    strictest_visibility = _strictest_read_visibility_sql(table_name)
    return (
        "CASE "
        f"WHEN {strictest_visibility} != '{READ_VISIBILITY_OPERATIONAL}' "
        f"THEN MAX({table_name}.{column_name}, excluded.{column_name}) "
        f"ELSE excluded.{column_name} "
        "END"
    )


def _clean_choice(value: str, allowed: set[str], label: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in allowed:
        raise ValueError(f"Unsupported {label}: {cleaned}")
    return cleaned


def _clean_choice_with_aliases(
    value: str,
    allowed: set[str],
    aliases: dict[str, str],
    label: str,
) -> str:
    cleaned = (value or "").strip()
    canonical = aliases.get(cleaned, cleaned)
    if canonical not in allowed:
        raise ValueError(f"Unsupported {label}: {cleaned}")
    return canonical


def _stricter_hypomnema_visibility(
    existing_visibility: str,
    classified_visibility: str,
) -> str:
    return _stricter_read_visibility(existing_visibility, classified_visibility)


def _classify_hypomnema_domain_from_text(
    text: str, *, fallback: str = "situational"
) -> str:
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("identity", "who i am", "who you are", "selfhood", "soul.md")
    ):
        return "identity"
    if any(
        marker in lowered
        for marker in (
            "always",
            "preference",
            "prefers",
            "principle",
            "boundary",
            "foundational",
        )
    ):
        return "foundational"
    if any(marker in lowered for marker in ("again", "recurring", "usually", "often")):
        return "recurring"
    if any(
        marker in lowered
        for marker in ("roadmap", "long term", "long-term", "future", "arc")
    ):
        return "long-arc"
    if any(
        marker in lowered for marker in ("current", "today", "temporary", "session")
    ):
        return "situational"
    return fallback


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

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        vault_active: bool | None = None,
    ):
        """Open a Mnemos store.

        Args:
            db_path: SQLite file path. Created if missing in read-write mode.
            read_only: When True, open via `file:...?mode=ro` URI and skip
                schema bootstrap entirely. Required for preview/inspection
                paths that must not mutate the database — without this, the
                default constructor runs `executescript`, `ALTER TABLE`,
                migrations, and a `meta` write on every instantiation, which
                silently upgrades older-schema DBs and writes 2+ bytes even on
                already-current DBs. Caller must guarantee the DB exists.
            vault_active: Whether the T4 read-path validator enforces the
                witnessed-decision requirement on identity-tier reads. When
                ``None`` (default) it is inferred: active iff a vault journal is
                configured (``MNEMOS_VAULT_JOURNAL``) AND the file exists — i.e.
                David has run the install ceremony. Before the vault exists the
                gate is inert, so the pre-vault identity corpus reads exactly as
                it does today (design §6: "stays where it is now until
                installed"). Apply and reconcile are always on regardless.
        """
        self.db_path = Path(db_path).expanduser()
        self._read_only = read_only
        self._vault_active = _resolve_vault_active(vault_active)
        self._conn: sqlite3.Connection | None = None
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"read_only EngramStore requires existing db: {self.db_path}"
                )
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with schema. Never called in read_only mode."""
        if self._read_only:
            raise RuntimeError("_init_db must not be called on a read-only store")
        conn = self._get_conn()
        current_version = get_current_version(conn)
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported "
                f"{SCHEMA_VERSION}"
            )
        # NOTE (review 003b): executescript runs before migrations. Migration
        # self-repair guards must never key on objects SQL_CREATE_TABLES creates —
        # this boot path pre-creates them, so such a guard would never fire. See
        # migrate_v7_afferent_u2_5_proposal_contract for the instance this bit.
        conn.executescript(SQL_CREATE_TABLES)
        # Migrate: add impact column if missing (v0.1 → v0.2)
        try:
            conn.execute(
                "ALTER TABLE engrams ADD COLUMN impact TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()
        run_migrations(conn, target_version=SCHEMA_VERSION)
        apply_u3a_schema_migration(conn)
        apply_u3b_hardening_schema_migration(conn)
        apply_u6_6_inner_life_schema_migration(conn)
        # Set schema version
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create SQLite connection.

        Read-only mode opens via SQLite URI form (`file:...?mode=ro`), which
        makes the connection reject INSERT/UPDATE/DELETE/DDL at the SQLite
        layer. WAL pragma is incompatible with read-only mode (would attempt
        to create WAL file); we skip the write-side pragmas there.
        """
        if self._conn is None:
            if self._read_only:
                uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
                self._conn = sqlite3.connect(
                    uri,
                    uri=True,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                # foreign_keys is harmless on read-only; PRAGMA query is allowed
                self._conn.execute("PRAGMA foreign_keys=ON")
            else:
                self._conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Engram CRUD ──

    def save_engram(self, engram: Engram) -> None:
        """Insert or update an engram.

        All operations (engram table, FTS index, connections, versions) are
        wrapped in a single transaction for atomicity.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._save_engram_no_commit(conn, engram)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _save_engram_no_commit(self, conn: sqlite3.Connection, engram: Engram) -> None:
        data = engram.to_dict()

        # Validate column names to prevent SQL injection
        safe_data = {k: v for k, v in data.items() if k in _ENGRAM_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(
            (
                f"{k}={_strictest_read_visibility_sql('engrams')}"
                if k == "read_visibility"
                else f"{k}=excluded.{k}"
            )
            for k in safe_data
            if k != "id"
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

        # Save versions
        for version in engram.versions:
            self._save_version_no_commit(conn, engram.id, version)

    def get_engram(
        self,
        engram_id: str,
        *,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> Engram | None:
        """Load an engram by ID, including connections and versions.

        ``read_visibility`` is an explicit access filter. The default is
        ``READ_VISIBILITY_OPERATIONAL`` (fail-closed): quarantined
        (``review_only``/``audit_only``) rows are excluded unless a caller
        opts into unfiltered admin access by passing ``read_visibility=None``
        explicitly (R5, T3/D8-A — the ``None`` opt-in is grep-auditable).
        """
        conn = self._get_conn()
        query = "SELECT * FROM engrams WHERE id = ?"
        params: list[Any] = [engram_id]
        normalized = _normalize_read_visibility(read_visibility)
        if normalized is not None:
            query += " AND read_visibility = ?"
            params.append(normalized)
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None

        engram = Engram.from_dict(dict(row))

        # Load connections
        engram.connections = self.get_connections(
            engram_id,
            read_visibility=read_visibility,
        )

        # Load versions (inherit the caller's visibility filter)
        engram.versions = self._get_versions(engram_id, read_visibility=read_visibility)

        return engram

    def get_active_engrams(
        self,
        agent_id: str | None = "default",
        limit: int = 1000,
        load_connections: bool = True,
        include_decay_protected: bool = True,
        require_consolidation_authorized: bool = False,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[Engram]:
        """Get all active engrams for an agent, sorted by accessibility.

        Args:
            agent_id: Which agent's engrams to return. If None, returns all
                agents' active engrams (useful for shared DB consolidation).
            load_connections: If True, load connections for each engram.
                Set to False for bulk operations where connections aren't needed
                (e.g., decay pass only needs accessibility/strength fields).
            include_decay_protected: If False, exclude engrams that the
                decay pass must not mutate.
            require_consolidation_authorized: If True, exclude read-only
                imported engrams from consolidation mutation candidates.
            read_visibility: Defaults to operational-context rows. Pass None
                only for explicit review/audit callers that need all rows.
        """
        conn = self._get_conn()
        predicates = ["state = 'active'"]
        params: list[Any] = []
        if agent_id is not None:
            predicates.append("owner_agent_id = ?")
            params.append(agent_id)
        if not include_decay_protected:
            predicates.append("decay_protected = 0")
        if require_consolidation_authorized:
            predicates.append("consolidation_authorized = 1")
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            predicates.append(f"read_visibility IN ({placeholders})")
            params.extend(visibility_values)
        where = " AND ".join(predicates)
        params.append(limit)

        if agent_id is None:
            rows = conn.execute(
                f"SELECT * FROM engrams WHERE {where} "
                "ORDER BY accessibility DESC LIMIT ?",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM engrams WHERE {where} "
                "ORDER BY accessibility DESC LIMIT ?",
                params,
            ).fetchall()
        engrams = [Engram.from_dict(dict(r)) for r in rows]
        if load_connections:
            for engram in engrams:
                engram.connections = self.get_connections(
                    engram.id,
                    read_visibility=read_visibility,
                )
                # R5 (T3): this engram already cleared the caller's visibility
                # filter (it is in `engrams`), so gate its versions by the
                # engram's own tier — a single value, so it is safe even when
                # the caller passed a sequence of visibilities to the list read.
                # Without this, an admin/review list read returned quarantined
                # engrams with empty version histories.
                engram.versions = self._get_versions(
                    engram.id, read_visibility=engram.read_visibility
                )
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
        conn.commit()

    def count_engrams(
        self,
        agent_id: str | None = "default",
        state: str = "active",
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> int:
        """Count engrams for an agent in a given state.

        Args:
            agent_id: Agent to count for. If None, counts all agents.
            state: Engram state to filter by.
            read_visibility: Defaults to operational-context rows. Pass None
                only for explicit audit/admin counts across all visibility tiers.
        """
        conn = self._get_conn()
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if agent_id is None:
            params: list[Any] = [state]
            query = "SELECT COUNT(*) FROM engrams WHERE state = ?"
        else:
            params = [agent_id, state]
            query = (
                "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?"
            )
        query = _append_read_visibility_filter(
            query,
            params,
            "read_visibility",
            visibility_values,
        )
        row = conn.execute(query, params).fetchone()
        return row[0] if row else 0

    # ── Full-Text Search ──

    def search_fts(
        self,
        query: str,
        limit: int = 50,
        *,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[Engram]:
        """Search engrams using FTS5 full-text search."""
        conn = self._get_conn()
        visibility_values = _normalize_read_visibility_values(read_visibility)
        predicates = ["engrams_fts MATCH ?", "e.state = 'active'"]
        params: list[Any] = [query]
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            predicates.append(f"e.read_visibility IN ({placeholders})")
            params.extend(visibility_values)
        params.append(limit)
        rows = conn.execute(
            "SELECT e.* FROM engrams e "
            "JOIN engrams_fts f ON e.id = f.id "
            f"WHERE {' AND '.join(predicates)} "
            "ORDER BY rank LIMIT ?",
            params,
        ).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]

    # ── Connections ──

    def save_connection(self, source_id: str, conn_obj: Connection) -> None:
        """Save a typed connection (with auto-commit)."""
        conn = self._get_conn()
        self._save_connection_no_commit(conn, source_id, conn_obj)
        conn.commit()

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

    def get_connections(
        self,
        engram_id: str,
        *,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[Connection]:
        """Get all connections FROM an engram.

        Default is ``READ_VISIBILITY_OPERATIONAL`` (fail-closed): connections
        touching a quarantined endpoint are excluded unless a caller opts into
        unfiltered admin access with explicit ``read_visibility=None``
        (R5, T3/D8-A).
        """
        conn = self._get_conn()
        visibility_values = _normalize_read_visibility_values(read_visibility)
        params: list[Any] = [engram_id]
        if visibility_values is None:
            rows = conn.execute(
                "SELECT * FROM connections WHERE source_id = ?", params
            ).fetchall()
        else:
            placeholders = ", ".join("?" for _ in visibility_values)
            params.extend(visibility_values)
            params.extend(visibility_values)
            rows = conn.execute(
                "SELECT c.* FROM connections c "
                "JOIN engrams source ON source.id = c.source_id "
                "JOIN engrams target ON target.id = c.target_id "
                "WHERE c.source_id = ? "
                f"AND source.read_visibility IN ({placeholders}) "
                f"AND target.read_visibility IN ({placeholders})",
                params,
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
        conn = self._get_conn()
        conn.execute(
            """UPDATE connections
               SET relation = ?, strength = ?, formed_by = ?
               WHERE source_id = ? AND target_id = ?""",
            (
                connection.relation.value
                if hasattr(connection.relation, "value")
                else str(connection.relation),
                connection.strength,
                connection.formed_by,
                source_id,
                connection.target_id,
            ),
        )
        conn.commit()

    def remove_connection(self, source_id: str, target_id: str) -> None:
        """Remove a connection between two engrams."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM connections WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        )
        conn.commit()

    def get_recent_engrams(
        self,
        agent_id: str | None = None,
        since: "datetime | None" = None,
        limit: int = 50,
        require_consolidation_authorized: bool = False,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list:
        """Get recently created engrams, optionally filtered by agent and time.

        Args:
            agent_id: Filter by agent ID (optional).
            since: Only return engrams created after this datetime (optional).
            limit: Maximum number to return.
            require_consolidation_authorized: If True, exclude read-only
                imported engrams from consolidation review inputs.
            read_visibility: Defaults to operational-context rows. Pass None
                for explicit review/audit scans.

        Returns:
            List of Engram objects, most recent first.
        """
        query = "SELECT * FROM engrams WHERE state = 'active'"
        params: list = []
        normalized_visibility = _normalize_read_visibility(read_visibility)

        if agent_id:
            query += " AND owner_agent_id = ?"
            params.append(agent_id)

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        if require_consolidation_authorized:
            query += " AND consolidation_authorized = 1"

        if normalized_visibility is not None:
            query += " AND read_visibility = ?"
            params.append(normalized_visibility)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        rows = conn.execute(query, params).fetchall()
        return [Engram.from_dict(dict(r)) for r in rows]

    def get_connected_engram_ids(
        self,
        engram_id: str,
        max_depth: int = 2,
        *,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> set[str]:
        """Get IDs of engrams connected within max_depth hops.

        R5 (T3): graph traversal (a named connection-discovery producer input)
        excludes quarantined engrams. Default ``READ_VISIBILITY_OPERATIONAL``
        (fail-closed): a connected engram whose ``read_visibility`` is not
        operational is not traversed and not returned, so quarantined content
        cannot re-enter a producer via the connection graph. Explicit
        ``read_visibility=None`` opts into unfiltered admin traversal.
        """
        normalized = _normalize_read_visibility(read_visibility)
        # R5 (T3 review r5-connected-root-visibility-not-checked): the ROOT must
        # satisfy the filter too. A quarantined root passed under the operational
        # default must not expand its edges and leak operational neighbours.
        if normalized is not None:
            root_visible = (
                self._get_conn()
                .execute(
                    "SELECT 1 FROM engrams WHERE id = ? AND read_visibility = ?",
                    (engram_id, normalized),
                )
                .fetchone()
            )
            if root_visible is None:
                return set()
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
                if normalized is None:
                    rows = conn.execute(
                        "SELECT target_id FROM connections WHERE source_id = ? "
                        "UNION SELECT source_id FROM connections WHERE target_id = ?",
                        (eid, eid),
                    ).fetchall()
                else:
                    # Exclude neighbours whose engram row is quarantined. The
                    # JOIN drops any neighbour id without an operational engram
                    # row (fail-closed) so review/audit content is never
                    # surfaced through the graph.
                    rows = conn.execute(
                        "SELECT c.nid FROM ("
                        "SELECT target_id AS nid FROM connections WHERE source_id = ? "
                        "UNION SELECT source_id AS nid FROM connections WHERE target_id = ?"
                        ") c JOIN engrams e ON e.id = c.nid "
                        "WHERE e.read_visibility = ?",
                        (eid, eid, normalized),
                    ).fetchall()
                next_frontier.update(r[0] for r in rows)
            frontier = next_frontier - visited

        visited.discard(engram_id)
        return visited

    # ── Versions ──

    def _save_version(self, engram_id: str, version: VersionRef) -> None:
        """Save a version snapshot (with auto-commit)."""
        conn = self._get_conn()
        self._save_version_no_commit(conn, engram_id, version)
        conn.commit()

    def _save_version_no_commit(
        self, conn: sqlite3.Connection, engram_id: str, version: VersionRef
    ) -> None:
        """Save a version snapshot without committing (for use in transactions)."""
        conn.execute(
            "INSERT OR REPLACE INTO versions "
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

    def _get_versions(
        self,
        engram_id: str,
        *,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[VersionRef]:
        """Get version history for an engram.

        R5 (T3): the ``versions`` table has no visibility column, so version
        history inherits the parent engram's ``read_visibility`` — when a
        filter is active, versions are returned only if the parent engram
        satisfies it (fail-closed via JOIN). Called from ``get_engram`` with
        the caller's filter threaded through; direct callers default operational.
        """
        conn = self._get_conn()
        normalized = _normalize_read_visibility(read_visibility)
        if normalized is None:
            rows = conn.execute(
                "SELECT * FROM versions WHERE engram_id = ? ORDER BY version_num",
                (engram_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT v.* FROM versions v "
                "JOIN engrams e ON e.id = v.engram_id "
                "WHERE v.engram_id = ? AND e.read_visibility = ? "
                "ORDER BY v.version_num",
                (engram_id, normalized),
            ).fetchall()
        return [VersionRef.from_dict(dict(r)) for r in rows]

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
        conn.commit()

    def search_archive(
        self,
        query: str,
        limit: int = 20,
        *,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[dict]:
        """Search archived engrams by content (for resharpen).

        R5 (T3): quarantined archived content is not retrievable by ``LIKE``.
        Default is ``READ_VISIBILITY_OPERATIONAL`` (fail-closed); explicit
        ``read_visibility=None`` opts into unfiltered admin access.

        The ``archive`` table carries no ``read_visibility`` column, but
        ``archive_engram`` leaves the source engram row in place with
        ``state='archived'`` and its ``read_visibility`` intact, so the filter
        recovers visibility by JOINing back to ``engrams`` on ``id``. An orphan
        archive row (no backing engram) is excluded — fail-closed, since its
        visibility cannot be established.
        """
        conn = self._get_conn()
        params: list[Any] = [f"%{query}%", f"%{query}%"]
        normalized = _normalize_read_visibility(read_visibility)
        if normalized is None:
            sql = (
                "SELECT * FROM archive "
                "WHERE (content LIKE ? OR content_at_encoding LIKE ?) LIMIT ?"
            )
            params.append(limit)
        else:
            sql = (
                "SELECT a.* FROM archive a "
                "JOIN engrams e ON e.id = a.id "
                "WHERE (a.content LIKE ? OR a.content_at_encoding LIKE ?) "
                "AND e.read_visibility = ? LIMIT ?"
            )
            params.append(normalized)
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Beliefs ──

    def save_belief(self, belief: Belief) -> None:
        """Insert or update a belief."""
        conn = self._get_conn()
        self._save_belief_no_commit(conn, belief)
        conn.commit()

    def save_reviewed_belief(self, belief: Belief) -> None:
        """Insert or update a belief after an explicit review decision."""
        conn = self._get_conn()
        self._save_belief_no_commit(conn, belief, allow_visibility_promotion=True)
        conn.commit()

    def _maybe_degrade_witnessed_belief(
        self, conn: sqlite3.Connection, belief: Belief
    ) -> dict[str, str] | None:
        """008g E7: witnessed-field split for belief upserts.

        Returns None when no witness is at stake (no existing row, no existing
        decision_ref, or witnessed fields byte-unchanged). Returns a degrade
        record when a witnessed field would change — the caller then clears
        the ref, forces review_only, and emits a trace proposal, all in the
        same transaction.

        Witnessed fields per ``canonical_row_sha256``: content, domain, tier
        (foundational-equivalent), agent_id. Everything else — confidence,
        timestamps, revision_history — is NOT witnessed and does not degrade.
        """
        from ..vault import journal as vault_journal

        existing = conn.execute(
            "SELECT id, agent_id, content, domain, tier, decision_ref "
            "FROM beliefs WHERE id = ?",
            (belief.id,),
        ).fetchone()
        if existing is None:
            return None
        existing = dict(existing)
        if not (existing.get("decision_ref") or "").strip():
            return None
        # Beliefs have no 'foundational' column; canonical_row_sha256 normalizes
        # from tier=='foundational' so we don't need to inject the field.
        old_hash = vault_journal.canonical_row_sha256("beliefs", existing)
        new_shape = {
            "id": belief.id,
            "agent_id": belief.agent_id,
            "content": belief.content,
            "domain": belief.domain,
            "tier": belief.tier or "",
        }
        new_hash = vault_journal.canonical_row_sha256("beliefs", new_shape)
        if old_hash == new_hash:
            return None
        return {
            "old_hash": old_hash,
            "new_hash": new_hash,
            "prior_decision_ref": str(existing["decision_ref"]),
        }

    def _emit_witness_degrade_trace(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        row_id: str,
        agent_id: str,
        person_id: str,
        project_scope: str,
        domain: str,
        degrade: dict[str, str],
    ) -> None:
        """008g E7/E8: emit the review-queue trace for a witnessed-field degrade.

        Deterministic ``id`` from (table, row_id, new_hash) so repeated identical
        mutations are idempotent, but a NEW mutation to the same row emits a
        distinct trace. Payload carries old/new witness hashes so David can
        diff exactly what changed.
        """
        trace_id = f"degrade-{table}-{row_id}-{degrade['new_hash'][:16]}"
        payload = {
            "old_row_hash": degrade["old_hash"],
            "new_row_hash": degrade["new_hash"],
            "prior_decision_ref": degrade["prior_decision_ref"],
            "row_id": row_id,
            "table": table,
        }
        now = int(datetime.now(timezone.utc).timestamp())
        conn.execute(
            """
            INSERT INTO proposal_ledger (
                id, agent_id, person_id, project_scope, source_authority, kind,
                domain, target_surface, transition, blast_radius, read_visibility,
                status, reason, gate_version, target_id, provenance_ids_json,
                payload_json, created_at, updated_at, decided_at, applied_at
            ) VALUES (?, ?, ?, ?, 'observed', 'semantic', ?, ?,
                      'witnessed row mutation degrade', 'identity', 'audit_only',
                      'pending_review', ?, 'affmem-v1', ?, '[]', ?, ?, ?, NULL, NULL)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                trace_id,
                agent_id,
                person_id,
                project_scope,
                domain,
                table,
                f"witnessed {table} row {row_id} mutated; degraded pending re-witness",
                row_id,
                _encode_json(payload),
                now,
                now,
            ),
        )

    def _degrade_and_trace_lifecycle(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        row_id: str,
        prior_decision_ref: str,
        reason: str,
        agent_id: str,
        person_id: str,
        project_scope: str,
        domain: str,
        trace_detail: str | None = None,
    ) -> None:
        """008k E2B: clear decision_ref + force review_only on a witnessed row
        whose lifecycle/content changed outside the vault path, and emit a
        D4-shaped trace proposal — all inside the caller's transaction.

        Used by supersede/archive (lifecycle) and PAI re-import (008k-r13 #3/#4:
        content/domain/tier or deactivate). ``trace_detail`` overrides the
        review-queue text so David sees WHY the row degraded. The trace ``id``
        is deterministic per (table, row_id, prior_decision_ref) so an
        idempotent repeat doesn't multiply traces.
        """
        conn.execute(
            f"UPDATE {table} SET decision_ref = NULL, "
            "read_visibility = 'review_only' WHERE id = ?",
            (row_id,),
        )
        trace_id = f"lifecycle-{table}-{row_id}-{prior_decision_ref[:16]}"
        payload = {
            "prior_decision_ref": prior_decision_ref,
            "reason": reason,
            "row_id": row_id,
            "table": table,
        }
        detail = trace_detail or (
            f"witnessed {table} row {row_id} superseded/archived; "
            "degraded pending re-witness"
        )
        now = int(datetime.now(timezone.utc).timestamp())
        conn.execute(
            """
            INSERT INTO proposal_ledger (
                id, agent_id, person_id, project_scope, source_authority, kind,
                domain, target_surface, transition, blast_radius, read_visibility,
                status, reason, gate_version, target_id, provenance_ids_json,
                payload_json, created_at, updated_at, decided_at, applied_at
            ) VALUES (?, ?, ?, ?, 'observed', 'semantic', ?, ?,
                      'witnessed row lifecycle degrade', 'identity', 'audit_only',
                      'pending_review', ?, 'affmem-v1', ?, '[]', ?, ?, ?, NULL, NULL)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                trace_id,
                agent_id,
                person_id,
                project_scope,
                domain or "general",
                table,
                detail,
                row_id,
                _encode_json(payload),
                now,
                now,
            ),
        )

    def _save_belief_no_commit(
        self,
        conn: sqlite3.Connection,
        belief: Belief,
        *,
        allow_visibility_promotion: bool = False,
    ) -> None:
        # 008g E7: witnessed-field split — check BEFORE the upsert whether this
        # write changes any witnessed field on a ref-carrying row.
        degrade = self._maybe_degrade_witnessed_belief(conn, belief)
        data = belief.to_dict()

        # Validate column names
        safe_data = {k: v for k, v in data.items() if k in _BELIEF_COLUMNS}
        columns = ", ".join(safe_data.keys())
        placeholders = ", ".join("?" for _ in safe_data)
        updates = ", ".join(
            (
                f"{k}={_strictest_read_visibility_sql('beliefs')}"
                if k == "read_visibility" and not allow_visibility_promotion
                else f"{k}={_preserved_quarantined_flag_sql('beliefs', k)}"
                if (
                    k in {"needs_review", "confidence_pending_review"}
                    and not allow_visibility_promotion
                )
                else f"{k}=excluded.{k}"
            )
            for k in safe_data
            if k != "id"
        )

        conn.execute(
            f"INSERT INTO beliefs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(safe_data.values()),
        )
        if degrade is not None:
            # 008g E7: atomic degrade — same transaction as the upsert, so no
            # reader sees the mutated row operational with the stale ref.
            conn.execute(
                "UPDATE beliefs SET decision_ref = NULL, "
                "read_visibility = 'review_only' WHERE id = ?",
                (belief.id,),
            )
            self._emit_witness_degrade_trace(
                conn,
                table="beliefs",
                row_id=belief.id,
                agent_id=belief.agent_id,
                person_id="user",
                project_scope="global",
                domain=belief.domain,
                degrade=degrade,
            )

    def get_beliefs(
        self,
        agent_id: str = "default",
        domain: str | None = None,
        active_only: bool = True,
        include_pending_review: bool = False,
        read_visibility: str | Sequence[str] | None | object = _READ_VISIBILITY_DEFAULT,
    ) -> list[Belief]:
        """Get beliefs, excluding pending-confidence rows unless opted in.

        Imported or changed PAI beliefs use ``confidence_pending_review`` to
        keep stale confidence out of normal consumers. Belief review passes
        ``include_pending_review=True`` so it can resolve those rows.
        """
        conn = self._get_conn()
        query = "SELECT * FROM beliefs WHERE agent_id = ?"
        params: list[Any] = [agent_id]
        if read_visibility is _READ_VISIBILITY_DEFAULT:
            visibility_values = (
                (READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW)
                if include_pending_review
                else (READ_VISIBILITY_OPERATIONAL,)
            )
        else:
            visibility_values = _normalize_read_visibility_values(read_visibility)  # type: ignore[arg-type]

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if active_only:
            query += " AND superseded_by IS NULL"

        if not include_pending_review:
            query += " AND confidence_pending_review = 0"

        query = _append_read_visibility_filter(
            query,
            params,
            "read_visibility",
            visibility_values,
        )
        query = _append_identity_decision_gate(query, "beliefs", visibility_values, self._vault_active)

        query += " ORDER BY confidence DESC"
        rows = conn.execute(query, params).fetchall()
        return [Belief.from_dict(dict(r)) for r in rows]

    # ── Proposal Ledger ──

    def write_proposal(
        self,
        *,
        source_authority: str,
        kind: str,
        target_surface: str,
        transition: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        domain: str = "general",
        blast_radius: str = "medium",
        read_visibility: str = READ_VISIBILITY_AUDIT,
        status: str = "pending_review",
        reason: str = "",
        gate_version: str = "affmem-v1",
        target_id: str | None = None,
        provenance_ids: list[str] | tuple[str, ...] | None = None,
        payload: dict[str, Any] | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a durable-affecting candidate in the proposal ledger.

        Proposal rows are review artifacts: they preserve authority, target
        surface, transition, blast radius, gate version, provenance, payload,
        visibility, and lifecycle status without making candidate prose part of
        operational context. Domains are enum-checked against the six hypomnema
        domains plus ``general``; unknown non-empty domains fail closed.

        This is the raw producer API: same-ID writes may update only
        ``pending_review`` rows. Once a row is ``deferred``, ``rejected``,
        ``applied``, ``approved``, or ``superseded``, raw writes fail closed;
        reviewed decisions must use a separate append-only decision path.
        """
        source_authority = _clean_choice_with_aliases(
            source_authority,
            VALID_PROPOSAL_AUTHORITIES,
            PROPOSAL_AUTHORITY_ALIASES,
            "source authority",
        )
        kind = _clean_choice_with_aliases(
            kind,
            VALID_PROPOSAL_KINDS,
            PROPOSAL_KIND_ALIASES,
            "proposal kind",
        )
        target_surface = _clean_choice(
            target_surface,
            VALID_PROPOSAL_TARGET_SURFACES,
            "target surface",
        )
        blast_radius = _clean_choice(
            blast_radius,
            VALID_PROPOSAL_BLAST_RADII,
            "blast radius",
        )
        # T3/D7: enum-check the domain. Empty/None keeps the "general" catch-all;
        # an unknown non-empty domain raises (producers are trusted callers, so
        # loud is correct — merge-review §5.1(b)).
        domain = _clean_choice(
            domain.strip() or "general",
            VALID_PROPOSAL_DOMAINS,
            "proposal domain",
        )
        if status not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Unsupported proposal status: {status}")
        if status not in RAW_PROPOSAL_STATUSES:
            raise ValueError(
                f"Proposal status {status!r} cannot be created directly before review gates"
            )
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=READ_VISIBILITY_AUDIT,
        )
        if normalized_visibility == READ_VISIBILITY_OPERATIONAL:
            raise ValueError(
                "Proposal rows cannot be operational until applied by a reviewed gate"
            )
        if not transition.strip():
            raise ValueError("Proposal transition cannot be empty")

        pid = (proposal_id or "").strip() or _new_id()
        # 008e-r2 #1: identity/foundational blast MUST carry a stable target_id
        # BEFORE the journal line is written (target_id is in the hashed content
        # field-set). Auto-generate here for producers that leave it None, so
        # the reconciler's fallback locator has a target to look up even after
        # a triple-clear attack. Non-identity proposals keep the None option.
        if blast_radius in ("identity", "foundational"):
            resolved_target_id = (target_id or "").strip() or _new_id()
        else:
            resolved_target_id = (target_id or "").strip() or None
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT status FROM proposal_ledger WHERE id = ?",
            (pid,),
        ).fetchone()
        if existing is not None and existing["status"] in PROPOSAL_TERMINAL_STATUSES:
            raise ValueError(
                "Terminal proposal rows are immutable; use a new proposal_id"
            )

        now = int(datetime.now(timezone.utc).timestamp())
        provenance = [
            str(item).strip() for item in (provenance_ids or []) if str(item).strip()
        ]
        payload_json = _encode_json(payload or {})
        decided_at = now if status in {"deferred", "rejected"} else None
        applied_at = None
        cursor = conn.execute(
            f"""
            INSERT INTO proposal_ledger (
                id, agent_id, person_id, project_scope, source_authority, kind,
                domain, target_surface, transition, blast_radius, read_visibility,
                status, reason, gate_version, target_id, provenance_ids_json,
                payload_json, created_at, updated_at, decided_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                source_authority = excluded.source_authority,
                kind = excluded.kind,
                domain = excluded.domain,
                target_surface = excluded.target_surface,
                transition = excluded.transition,
                blast_radius = excluded.blast_radius,
                read_visibility = {_strictest_read_visibility_sql("proposal_ledger")},
                status = CASE
                    WHEN proposal_ledger.status IN (
                        'deferred', 'approved', 'rejected', 'applied', 'superseded'
                    )
                    THEN proposal_ledger.status
                    ELSE excluded.status
                END,
                reason = excluded.reason,
                gate_version = excluded.gate_version,
                target_id = excluded.target_id,
                provenance_ids_json = excluded.provenance_ids_json,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                decided_at = CASE
                    WHEN proposal_ledger.status IN (
                        'deferred', 'approved', 'rejected', 'applied', 'superseded'
                    )
                    THEN proposal_ledger.decided_at
                    WHEN excluded.status IN ('deferred', 'rejected')
                    THEN excluded.updated_at
                    ELSE proposal_ledger.decided_at
                END,
                applied_at = proposal_ledger.applied_at
            WHERE proposal_ledger.status NOT IN (
                'deferred', 'approved', 'rejected', 'applied', 'superseded'
            )
            """,
            (
                pid,
                agent_id,
                person_id,
                project_scope,
                source_authority,
                kind,
                domain,
                target_surface,
                transition.strip(),
                blast_radius,
                normalized_visibility,
                status,
                reason.strip(),
                gate_version.strip() or "affmem-v1",
                resolved_target_id,
                _encode_json(provenance),
                payload_json,
                now,
                now,
                decided_at,
                applied_at,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(
                "Terminal proposal rows are immutable; use a new proposal_id"
            )
        conn.commit()
        proposal = self.get_proposal(pid)
        if proposal is None:
            raise RuntimeError(f"Failed to write proposal: {pid}")
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Load one proposal ledger row by ID."""
        row = (
            self._get_conn()
            .execute(
                "SELECT * FROM proposal_ledger WHERE id = ?",
                (proposal_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return self._hydrate_proposal_row(dict(row))

    def list_proposals(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        status: str | None = "pending_review",
        read_visibility: str | None = READ_VISIBILITY_REVIEW,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List proposal ledger rows for an explicit review surface.

        ``read_visibility`` defaults to the ordinary review surface. Pass
        ``None`` only from explicit audit/admin code paths that intentionally
        inspect all proposal rows.
        """
        if status is not None and status not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Unsupported proposal status: {status}")
        normalized_visibility = _normalize_read_visibility(read_visibility)
        sql = (
            "SELECT * FROM proposal_ledger "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if normalized_visibility is not None:
            sql += " AND read_visibility = ?"
            params.append(normalized_visibility)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = self._get_conn().execute(sql, params).fetchall()
        return [self._hydrate_proposal_row(dict(row)) for row in rows]

    def count_proposals(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        status: str | None = "pending_review",
        read_visibility: str | None = READ_VISIBILITY_REVIEW,
    ) -> int:
        """Count proposal ledger rows for a specific visibility surface."""
        if status is not None and status not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Unsupported proposal status: {status}")
        normalized_visibility = _normalize_read_visibility(read_visibility)
        sql = (
            "SELECT COUNT(*) FROM proposal_ledger "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if normalized_visibility is not None:
            sql += " AND read_visibility = ?"
            params.append(normalized_visibility)
        row = self._get_conn().execute(sql, params).fetchone()
        return int(row[0] or 0)

    def list_audit_proposals(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List audit-only proposal rows for a deliberate audit/admin surface."""
        return self.list_proposals(
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            status=status,
            read_visibility=READ_VISIBILITY_AUDIT,
            limit=limit,
        )

    @staticmethod
    def _hydrate_proposal_row(row: dict[str, Any]) -> dict[str, Any]:
        row["provenance_ids"] = _decode_json(row.pop("provenance_ids_json", "[]"), [])
        row["payload"] = _decode_json(row.pop("payload_json", "{}"), {})
        return row

    # ── Vault: identity-tier decision application ──

    # The only surfaces that can structurally carry an identity tier signal, so
    # the only surfaces a witnessed identity decision may write. engrams /
    # functional_memories have no domain/tier column — applying identity content
    # there would launder it past the read-path validator into permanent
    # invisibility, so those surfaces are a loud error, not a silent write.
    IDENTITY_APPLY_SURFACES = ("beliefs", "hypomnema_entries")

    def apply_identity_decision(
        self,
        proposal_id: str,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Apply (or terminally reject) an identity-tier proposal per the vault journal.

        008r (overturns 008m): apply has NO journal-path parameter. The
        canonical vault journal path is resolved here
        (``resolve_vault_journal_path``) and nowhere else, so apply can only
        ever read the real vault — the decision *which journal is authoritative*
        is not in-process code's to make. Tests inject a fixture journal by
        monkeypatching the resolver (a mechanism that cannot ship), never
        through a call parameter: a parameter is an affordance that will
        eventually be passed by someone with a plausible reason, while a
        monkeypatch is a git-visible edit to the vault module. This aligns the
        applier with 008m's watchdog Addition 1 — no production component of the
        vault has a redirectable journal path.

        The journal — a vault-owned, append-only, hash-chained file the agent
        cannot write — is the sole authority. This method reads it, and does
        nothing durable unless it can cite a chain-verified line whose
        ``content_sha256`` matches the proposal's content recomputed *now*
        (closing TOCTOU) and whose decision is ``approved`` or ``rejected``.

        All verification completes BEFORE the write transaction opens: no BEGIN
        until the chain is intact and the hash matches. An approved decision
        writes the target row ``operational_context`` with
        ``decision_ref`` = the journal line's hash (the one thing that licenses
        an identity row to be operational) and flips the proposal to ``applied``.
        A rejected decision flips the proposal to ``rejected`` with the same
        ``decision_ref`` discipline and writes no target row.

        Raises ``ValueError`` (never a silent no-op) on: unknown proposal,
        non-identity blast radius, unsupported target surface, missing journal
        line, broken chain, hash mismatch, or a non-terminal-eligible decision.
        """
        from ..vault import journal as vault_journal

        # 008r (overturns 008m): resolve the canonical vault journal path here
        # and NOWHERE else — no parameter can redirect it. Tests inject via the
        # resolver seam (monkeypatch resolve_vault_journal_path).
        resolved = resolve_vault_journal_path()
        if resolved is None:
            raise ValueError(
                "Vault not armed (no journal path resolved); cannot apply "
                "an identity decision without a canonical vault journal"
            )
        journal_path: str | Path = resolved

        # Journal read + chain verify happen OUTSIDE the transaction — the
        # journal is a separate file owned by the vault user, not the DB.
        # Everything that touches the proposal ledger row happens under the
        # write lock (BEGIN IMMEDIATE) so a concurrent write_proposal cannot
        # mutate hashed fields or flip the status between validation and
        # apply — 008g-r8 #1 (TOCTOU close).
        lines = vault_journal.read_journal(journal_path)
        chain_ok, break_index = vault_journal.verify_chain(lines)
        if not chain_ok:
            raise ValueError(
                f"Vault journal chain broken at line {break_index}; refusing apply"
            )
        decision = vault_journal.find_decision(lines, proposal_id)
        if decision is None:
            raise ValueError(
                f"No vault journal decision for proposal {proposal_id}; "
                "identity-tier apply requires a witnessed journal line"
            )
        decision_kind = str(decision.get("decision", ""))
        if decision_kind not in ("approved", "rejected"):
            raise ValueError(
                f"Journal decision for {proposal_id} is {decision_kind!r}; "
                "only 'approved' or 'rejected' are applicable"
            )
        # decision_ref is the hash of the raw stored line (not the augmented
        # dict find_decision returns — that carries a synthetic _line_index).
        decision_ref = vault_journal.line_hash(lines[decision["_line_index"]])

        now_ts = now or _utc_now()
        conn = self._get_conn()
        # 008g-r8 #1: BEGIN IMMEDIATE takes the write lock now — any other
        # writer blocks until we commit/rollback. Then we RELOAD the proposal
        # inside the lock and re-run every validation; nothing can change the
        # ledger row between validation and apply.
        conn.execute("BEGIN IMMEDIATE")
        try:
            proposal = self.get_proposal(proposal_id)
            if proposal is None:
                raise ValueError(f"Unknown proposal: {proposal_id}")
            if proposal["blast_radius"] not in ("identity", "foundational"):
                raise ValueError(
                    "apply_identity_decision is only for identity/foundational "
                    f"proposals; proposal {proposal_id} has "
                    f"blast_radius={proposal['blast_radius']!r}"
                )
            if proposal["status"] not in ("pending_review", "deferred"):
                raise ValueError(
                    f"Proposal {proposal_id} is not applicable in status "
                    f"{proposal['status']!r}"
                )
            surface = proposal["target_surface"]
            if surface not in self.IDENTITY_APPLY_SURFACES:
                raise ValueError(
                    "identity-tier apply supports beliefs/hypomnema_entries; "
                    f"extending to {surface} requires a schema unit — escalate"
                )
            # 008g-r8 #2: fail closed on empty target_id. r2 #1 auto-gens for
            # NEW proposals via write_proposal, but a pre-v9 pending proposal
            # in the DB may have target_id=NULL and its journal line was
            # hashed with target_id=''. Applying would invent an id and the
            # row's fields would never match the hash — an immediate reconcile
            # tamper. David re-files with target_id specified.
            if not str(proposal.get("target_id") or "").strip():
                raise ValueError(
                    f"Proposal {proposal_id} has no target_id — refusing apply "
                    "(pre-v9 unbound proposals must be re-filed with a stable "
                    "target_id before witnessing)"
                )
            live_hash = vault_journal.canonical_content_sha256(proposal)
            if str(decision.get("content_sha256", "")) != live_hash:
                raise ValueError(
                    f"Content hash mismatch for {proposal_id}: proposal changed "
                    "since the decision was recorded; refusing apply"
                )

            target_row_id: str | None = None
            if decision_kind == "approved":
                target_row_id = self._apply_identity_write_no_commit(
                    conn, proposal, decision_ref=decision_ref, now_ts=now_ts
                )
                new_status = "applied"
            else:
                new_status = "rejected"
            # 008e #2 residual (declared): we do NOT write the resolved
            # target_id back to proposal_ledger — target_id is in the content
            # hash field-set, so mutating it after apply would break the
            # reconciler's TOCTOU hash check. The fallback locator works when
            # the producer specified target_id up-front (typical); auto-
            # generated target_ids fall through to direction-B's
            # "missing_witnessed_row" finding (visible, David-actionable) but
            # cannot re-quarantine automatically. Silently keeping the row
            # operational as non-identity is a determined-adversary path that
            # the design's §1 already declares out-of-in-account-scope.
            cursor = conn.execute(
                """
                UPDATE proposal_ledger
                SET status = ?, decided_at = ?, applied_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending_review', 'deferred')
                """,
                (
                    new_status,
                    now_ts if new_status == "rejected" else proposal.get("decided_at"),
                    now_ts if new_status == "applied" else None,
                    now_ts,
                    proposal_id,
                ),
            )
            # 008g-r8 #1: if the UPDATE affected zero rows, the ledger row
            # changed status between load-under-lock and here (should be
            # impossible since we hold BEGIN IMMEDIATE, but defense-in-depth).
            # Roll back — a target row must not survive an unmatched ledger update.
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Proposal {proposal_id} ledger status update matched "
                    f"{cursor.rowcount} rows; refusing to leave a witnessed row "
                    "with an unwritten ledger transition"
                )
            _ = target_row_id  # kept for future ledger-side reference tracking
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        result = self.get_proposal(proposal_id)
        assert result is not None
        result["decision_ref"] = decision_ref
        return result

    def reconcile_identity_vault(
        self,
        journal_path: str | Path,
        *,
        apply_quarantine: bool = True,
    ) -> Any:
        """Reconcile identity-tier rows against the vault journal (both directions).

        Thin delegate to ``mnemos.vault.reconcile``. Run at session-start and by
        the watchdog; ``apply_quarantine=False`` is a dry audit.
        """
        from ..vault.reconcile import reconcile_identity_tier

        return reconcile_identity_tier(
            self, journal_path, apply_quarantine=apply_quarantine
        )

    def apply_legacy_witness(self) -> dict[str, list[str]]:
        """Stamp legacy identity rows from batch-witness journal lines (DAVID-9 c).

        008r (overturns 008m): no journal-path parameter. The canonical vault
        path is resolved here and nowhere else; tests inject via the resolver
        seam (monkeypatch resolve_vault_journal_path), never a call parameter.

        The imported SOUL corpus predates the proposal ledger, so those rows have
        no proposal to witness. ``mnemos-decide --witness-legacy`` appends one
        ``witness='legacy'`` line per row (hashing the row itself via
        ``canonical_row_sha256``); this reads those lines and stamps each
        matching row's ``decision_ref`` = the line hash, keeping it operational.

        One-time and idempotent: an already-stamped row is skipped; a row whose
        content no longer matches its witness line is left unstamped and
        reported (the witness stopped describing the row). Runs at session-start;
        touches only rows a witness line names.
        """
        from ..vault import journal as vault_journal

        # 008r: no redirectable journal path (see apply_identity_decision).
        resolved = resolve_vault_journal_path()
        if resolved is None:
            # No vault armed → nothing to stamp; return empty result rather
            # than raise (session-start calls this best-effort).
            return {"stamped": [], "skipped": []}
        journal_path: str | Path = resolved

        lines = vault_journal.read_journal(journal_path)
        chain_ok, break_index = vault_journal.verify_chain(lines)
        if not chain_ok:
            raise ValueError(
                f"Vault journal chain broken at line {break_index}; "
                "refusing legacy witness"
            )
        conn = self._get_conn()
        stamped: list[str] = []
        skipped: list[str] = []
        changed = False
        for line in lines:
            if str(line.get("witness", "")) != "legacy":
                continue
            if str(line.get("decision", "")) != "approved":
                continue
            table = str(line.get("table", ""))
            row_id = str(line.get("row_id", ""))
            if table not in self.IDENTITY_APPLY_SURFACES:
                skipped.append(f"{row_id}:bad-table")
                continue
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()
            if row is None:
                skipped.append(f"{row_id}:row-missing")
                continue
            row = dict(row)
            if (row.get("decision_ref") or "").strip():
                skipped.append(f"{row_id}:already-stamped")
                continue
            if vault_journal.canonical_row_sha256(table, row) != str(
                line.get("content_sha256", "")
            ):
                skipped.append(f"{row_id}:content-mismatch")
                continue
            # 008e E2: stamp decision_ref ONLY. Preserve prior read_visibility —
            # a pre-vault row that was intentionally review_only/audit_only was
            # NOT part of the U4/06-28 approval this batch materializes; it was
            # flagged for review, and the curator's intent stands. Rows the TCB
            # batch offered were already filtered to operational-only.
            conn.execute(
                f"UPDATE {table} SET decision_ref = ? WHERE id = ?",
                (vault_journal.line_hash(line), row_id),
            )
            changed = True
            stamped.append(row_id)
        if changed:
            conn.commit()
        return {"stamped": stamped, "skipped": skipped}

    def _apply_identity_write_no_commit(
        self,
        conn: sqlite3.Connection,
        proposal: dict[str, Any],
        *,
        decision_ref: str,
        now_ts: str,
    ) -> str:
        """Write the witnessed identity row operational with its decision_ref.

        Returns the resolved ``target_id`` so ``apply_identity_decision`` can
        persist it back to the proposal ledger — the reconciler's fallback
        locator (008e #2) depends on it.

        Bypasses the review-only floor that ``classify_hypomnema_read_visibility``
        imposes on identity content precisely because ``decision_ref`` is what
        licenses operational visibility — a row written here without going
        through the journal-verified path above cannot exist.
        """
        payload = proposal.get("payload") or {}
        content = str(payload.get("content", "")).strip()
        if not content:
            raise ValueError("Identity decision payload has no content to apply")
        surface = proposal["target_surface"]
        target_id = (proposal.get("target_id") or "").strip() or _new_id()
        domain = proposal["domain"]
        # 008-r14 #2: reject a scope mismatch BEFORE the upsert. The upsert
        # overwrites content/domain/decision_ref on an existing row id but does
        # NOT rewrite agent_id/person_id/project_scope — so a proposal whose
        # target_id collides with an existing row in a DIFFERENT scope would
        # write witnessed identity content into that other scope. Refuse: a
        # witnessed decision applies only to a row in its own scope (or a new
        # row). This mirrors the scope binding reconcile already enforces.
        prop_agent = str(proposal.get("agent_id", "default"))
        prop_person = str(proposal.get("person_id", "user"))
        prop_scope = str(proposal.get("project_scope", "global"))
        existing_scope = conn.execute(
            "SELECT agent_id"
            + (", person_id, project_scope" if surface == "hypomnema_entries" else "")
            + f" FROM {surface} WHERE id = ?",
            (target_id,),
        ).fetchone()
        if existing_scope is not None:
            existing_scope = dict(existing_scope)
            if str(existing_scope.get("agent_id", "")) != prop_agent or (
                surface == "hypomnema_entries"
                and (
                    str(existing_scope.get("person_id", "")) != prop_person
                    or str(existing_scope.get("project_scope", "")) != prop_scope
                )
            ):
                raise ValueError(
                    f"identity apply target {target_id!r} exists in a different "
                    "scope than the proposal; refusing cross-scope witnessed write"
                )
        # 008e #7: the written row MUST carry an identity-tier signal, else the
        # read-path validator can't police it — an identity-blast proposal with
        # payload tier='operational' or domain='general' would otherwise apply
        # as a non-identity-tier row that is operational forever regardless of
        # decision_ref. Enforce here, at the apply chokepoint.
        if domain not in ("identity", "foundational"):
            raise ValueError(
                "identity-blast proposal must carry identity/foundational domain; "
                f"got domain={domain!r} — reject or re-file with a legal domain"
            )

        if surface == "beliefs":
            confidence = float(payload.get("confidence", 0.3) or 0.3)
            # Force foundational tier for identity blast — payload cannot
            # downgrade the tier signal to operational/tactical.
            tier = "foundational"
            conn.execute(
                """
                INSERT INTO beliefs (
                    id, agent_id, content, confidence, domain, created_at,
                    last_revised, last_challenged, tier, needs_review,
                    confidence_pending_review, read_visibility, decision_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'operational_context', ?)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    confidence = excluded.confidence,
                    domain = excluded.domain,
                    tier = excluded.tier,
                    last_revised = excluded.last_revised,
                    needs_review = 0,
                    confidence_pending_review = 0,
                    read_visibility = 'operational_context',
                    decision_ref = excluded.decision_ref,
                    -- 008g-r7 #3: reset lifecycle fields on re-approval, or a
                    -- previously-superseded belief stays invisible AND
                    -- reconcile (r6 #3) would flag the newly-witnessed row as
                    -- tamper because superseded_by wasn't cleared.
                    superseded_by = NULL
                """,
                (
                    target_id,
                    proposal.get("agent_id", "default"),
                    content,
                    confidence,
                    domain,
                    now_ts,
                    now_ts,
                    now_ts,
                    tier,
                    decision_ref,
                ),
            )
        else:  # hypomnema_entries
            density = float(payload.get("density", 0.5) or 0.5)
            confidence = float(payload.get("confidence", 0.6) or 0.6)
            salience = float(payload.get("salience", 0.5) or 0.5)
            source = str(payload.get("source", "co-formed")) or "co-formed"
            if source not in VALID_HYPO_SOURCES:
                raise ValueError(f"Unsupported hypomnema source: {source}")
            tags_json = _encode_json(
                list(payload["tags"])
                if isinstance(payload.get("tags"), (list, tuple))
                else []
            )
            conn.execute(
                """
                INSERT INTO hypomnema_entries (
                    id, agent_id, person_id, project_scope, content, source,
                    density, domain, read_visibility, tags_json, confidence,
                    salience, active, foundational, revision_count, revisions_json,
                    created_at, last_revised_at, decision_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'operational_context', ?, ?, ?,
                          1, 1, 0, '[]', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    source = excluded.source,
                    density = excluded.density,
                    domain = excluded.domain,
                    read_visibility = 'operational_context',
                    tags_json = excluded.tags_json,
                    confidence = excluded.confidence,
                    salience = excluded.salience,
                    foundational = 1,
                    last_revised_at = excluded.last_revised_at,
                    decision_ref = excluded.decision_ref,
                    -- 008g-r7 #3: reset lifecycle fields on re-approval.
                    active = 1,
                    superseded_by = NULL
                """,
                (
                    target_id,
                    proposal.get("agent_id", "default"),
                    proposal.get("person_id", "user"),
                    proposal.get("project_scope", "global"),
                    content,
                    source,
                    density,
                    domain,
                    tags_json,
                    confidence,
                    salience,
                    now_ts,
                    now_ts,
                    decision_ref,
                ),
            )
        return target_id

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
        conn.commit()
        session = self.get_memory_session(sid)
        if session is None:
            raise RuntimeError(f"Failed to start memory session: {sid}")
        return session

    def get_memory_session(self, session_id: str) -> dict[str, Any] | None:
        """Load a functional memory session by ID."""
        row = (
            self._get_conn()
            .execute(
                "SELECT * FROM memory_sessions WHERE id = ?",
                (session_id,),
            )
            .fetchone()
        )
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
        conn.commit()
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
        read_visibility: str | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Write or update a functional memory entry.

        Functional memory is the live, revisable working layer. It is useful
        for current task state, open questions, corrections, commitments, and
        preferences that have not yet earned hypomnema or engram status.
        Entries with ``needs_confirmation=True`` default to ``review_only`` so
        they do not enter operational packets until confirmed.
        """
        if memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")
        if not content.strip():
            raise ValueError("Functional memory content cannot be empty")

        now = _utc_now()
        fid = (memory_id or "").strip() or _new_id()
        session = (session_id or "").strip() or None
        # Write-path existing-row check: must see rows at any visibility so an
        # upsert preserves/strengthens a quarantined row's visibility rather
        # than treating it as absent. Admin opt-in (R5, T3/D8-A).
        existing = self.get_functional_memory(
            fid, include_deleted=True, read_visibility=None
        )
        default_visibility = (
            existing["read_visibility"]
            if existing is not None and read_visibility is None
            else (
                READ_VISIBILITY_REVIEW
                if needs_confirmation
                else READ_VISIBILITY_OPERATIONAL
            )
        )
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=default_visibility,
        )
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

        functional_visibility = _strictest_read_visibility_sql("functional_memories")
        conn.execute(
            f"""
            INSERT INTO functional_memories(
                id, session_id, agent_id, person_id, project_scope, content,
                memory_type, confidence, salience, needs_confirmation, pinned,
                source, read_visibility, metadata_json, created_at, updated_at, expires_at,
                is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                content = excluded.content,
                memory_type = excluded.memory_type,
                confidence = excluded.confidence,
                salience = excluded.salience,
                needs_confirmation = CASE
                    WHEN functional_memories.needs_confirmation = 1
                    AND ({functional_visibility}) != '{READ_VISIBILITY_OPERATIONAL}'
                    THEN 1
                    ELSE excluded.needs_confirmation
                END,
                pinned = excluded.pinned,
                source = excluded.source,
                read_visibility = {functional_visibility},
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
                normalized_visibility,
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
        conn.commit()
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
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> dict[str, Any] | None:
        """Load a functional memory by ID.

        R5 (T3): default ``READ_VISIBILITY_OPERATIONAL`` (fail-closed) — a
        quarantined row is excluded unless a caller opts into unfiltered admin
        access with explicit ``read_visibility=None``.
        """
        sql = "SELECT * FROM functional_memories WHERE id = ?"
        params: list[Any] = [memory_id]
        if not include_deleted:
            sql += " AND is_deleted = 0"
        normalized = _normalize_read_visibility(read_visibility)
        if normalized is not None:
            sql += " AND read_visibility = ?"
            params.append(normalized)
        row = self._get_conn().execute(sql, params).fetchone()
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
        exclude_needs_confirmation: bool = False,
        include_deleted: bool = False,
        limit: int = 12,
        read_visibility: str | Sequence[str] | None | object = _READ_VISIBILITY_DEFAULT,
    ) -> list[dict[str, Any]]:
        """Search functional memories for the current scope/session.

        Normal loads default to operational-context rows. Confirmation queues
        default to operational plus review-only rows, excluding audit-only
        unless ``read_visibility`` is explicitly supplied.
        """
        if memory_type and memory_type not in VALID_FUNCTIONAL_TYPES:
            raise ValueError(f"Unsupported functional memory type: {memory_type}")
        if needs_confirmation_only and exclude_needs_confirmation:
            raise ValueError(
                "needs_confirmation_only and exclude_needs_confirmation conflict"
            )

        sql = (
            "SELECT * FROM functional_memories "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        if read_visibility is _READ_VISIBILITY_DEFAULT:
            visibility_values = (
                (READ_VISIBILITY_OPERATIONAL, READ_VISIBILITY_REVIEW)
                if needs_confirmation_only
                else (READ_VISIBILITY_OPERATIONAL,)
            )
        else:
            visibility_values = _normalize_read_visibility_values(read_visibility)  # type: ignore[arg-type]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if needs_confirmation_only:
            sql += " AND needs_confirmation = 1"
        if exclude_needs_confirmation:
            sql += " AND needs_confirmation = 0"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
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
            details = "; ".join(f"{m['memory_type']}: {m['content']}" for m in chosen)
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
        domain = _classify_hypomnema_domain_from_text(content, fallback="situational")
        foundational = domain in {"identity", "foundational"}
        hypomnema_id = None
        hypomnema_visibility = None
        if memories or synthesis.strip():
            hypomnema_id = self.write_hypomnema_entry(
                content,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                source="synthesized",
                density=0.72,
                domain=domain,
                tags=["session-close", "functional-memory", project_scope],
                confidence=confidence,
                salience=salience,
                foundational=foundational,
                related_session_id=session_id,
            )
            hypomnema = self.get_hypomnema_entry(
                hypomnema_id,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                read_visibility=None,
            )
            hypomnema_visibility = (
                hypomnema.get("read_visibility") if hypomnema is not None else None
            )

        now = _utc_now()
        conn = self._get_conn()
        promoted_memory_ids = [m["id"] for m in memories]
        if (
            hypomnema_id
            and promoted_memory_ids
            and hypomnema_visibility == READ_VISIBILITY_OPERATIONAL
        ):
            placeholders = ", ".join("?" for _ in promoted_memory_ids)
            conn.execute(
                f"""
                UPDATE functional_memories
                SET is_deleted = 1,
                    promoted_to_hypomnema_id = ?,
                    updated_at = ?
                WHERE session_id = ? AND is_deleted = 0
                  AND id IN ({placeholders})
                """,
                (hypomnema_id, now, session_id, *promoted_memory_ids),
            )
        conn.execute(
            """
            UPDATE memory_sessions
            SET status = 'closed', updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (now, now, session_id),
        )
        conn.commit()
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
        read_visibility: str | Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Count active functional memory and session state."""
        functional_where = ["agent_id = ?"]
        session_where = ["agent_id = ?"]
        functional_params: list[Any] = [agent_id]
        session_params: list[Any] = [agent_id]
        if person_id is not None:
            functional_where.append("person_id = ?")
            session_where.append("person_id = ?")
            functional_params.append(person_id)
            session_params.append(person_id)
        if project_scope is not None:
            functional_where.append("project_scope = ?")
            session_where.append("project_scope = ?")
            functional_params.append(project_scope)
            session_params.append(project_scope)
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            functional_where.append(f"read_visibility IN ({placeholders})")
            functional_params.extend(visibility_values)
        functional_where_sql = " AND ".join(functional_where)
        session_where_sql = " AND ".join(session_where)
        conn = self._get_conn()
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN is_deleted = 0 AND pinned = 1 THEN 1 ELSE 0 END) AS pinned,
              SUM(CASE WHEN is_deleted = 0 AND needs_confirmation = 1 THEN 1 ELSE 0 END) AS needs_confirmation
            FROM functional_memories
            WHERE {functional_where_sql}
            """,
            functional_params,
        ).fetchone()
        session_row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
            FROM memory_sessions
            WHERE {session_where_sql}
            """,
            session_params,
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
        entry_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        read_visibility: str | None = None,
        original_timestamp: int | None = None,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        """Write a scoped hypomnema continuity entry.

        Hypomnema is durable, relationship-scoped continuity that can be
        revised before it graduates into shared Mnemos engrams. New stable or
        foundational promotion candidates default to review visibility; callers
        may set explicit visibility when a gate has already classified them.
        """
        conn = self._get_conn()
        entry_id = self._write_hypomnema_entry_no_commit(
            conn,
            content,
            entry_id=entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            source=source,
            density=density,
            domain=domain,
            tags=tags,
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            read_visibility=read_visibility,
            original_timestamp=original_timestamp,
            related_session_id=related_session_id,
            related_engram_id=related_engram_id,
        )
        conn.commit()
        return entry_id

    def _write_hypomnema_entry_no_commit(
        self,
        conn: sqlite3.Connection,
        content: str,
        *,
        entry_id: str | None = None,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        source: str = "observed",
        density: float = 0.5,
        domain: str = "topical",
        tags: str | list[str] | tuple[str, ...] | None = None,
        confidence: float = 0.6,
        salience: float = 0.5,
        foundational: bool = False,
        read_visibility: str | None = None,
        original_timestamp: int | None = None,
        related_session_id: str | None = None,
        related_engram_id: str | None = None,
    ) -> str:
        if source not in VALID_HYPO_SOURCES:
            raise ValueError(f"Unsupported hypomnema source: {source}")
        if domain not in VALID_HYPO_DOMAINS:
            raise ValueError(f"Unsupported hypomnema domain: {domain}")
        if not content.strip():
            raise ValueError("Hypomnema content cannot be empty")

        now = _utc_now()
        entry_id = (entry_id or "").strip() or _new_id()
        existing = conn.execute(
            """
            SELECT agent_id, person_id, project_scope, content, revision_count,
                   revisions_json, read_visibility, domain, foundational,
                   decision_ref
            FROM hypomnema_entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        # 008g E8: witnessed-field split — check BEFORE the upsert whether this
        # write changes any witnessed field on a ref-carrying row. Trace emit
        # happens after the upsert; the whole thing lives in the caller's
        # transaction so no reader sees an intermediate state.
        hypo_degrade: dict[str, str] | None = None
        if existing is not None and (
            str(dict(existing).get("decision_ref") or "").strip()
        ):
            from ..vault import journal as vault_journal
            existing_dict = dict(existing)
            existing_dict["id"] = entry_id
            old_hash = vault_journal.canonical_row_sha256(
                "hypomnema_entries", existing_dict
            )
            new_shape = {
                "id": entry_id,
                "agent_id": agent_id,
                "person_id": person_id,
                "project_scope": project_scope,
                "content": content.strip(),
                "domain": domain,
                "foundational": 1 if foundational else 0,
            }
            new_hash = vault_journal.canonical_row_sha256(
                "hypomnema_entries", new_shape
            )
            if old_hash != new_hash:
                hypo_degrade = {
                    "old_hash": old_hash,
                    "new_hash": new_hash,
                    "prior_decision_ref": str(existing_dict["decision_ref"]),
                }
        if existing is not None and (
            existing["agent_id"] != agent_id
            or existing["person_id"] != person_id
            or existing["project_scope"] != project_scope
        ):
            raise ValueError(
                "Hypomnema entry ID already exists outside the requested scope"
            )
        revision_count = 0
        revisions_json = "[]"
        if existing is not None:
            revision_count = int(existing["revision_count"] or 0)
            revisions = _decode_json(existing["revisions_json"], [])
            if existing["content"] != content.strip():
                revisions.append(
                    {
                        "content": existing["content"],
                        "revised_at": now,
                        "reason": "pai_import_update",
                    }
                )
                revision_count += 1
            revisions_json = _encode_json(revisions)
        classified_visibility = classify_hypomnema_read_visibility(
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            revision_count=revision_count,
            domain=domain,
        )
        required_visibility = classified_visibility
        if existing is not None:
            required_visibility = _stricter_hypomnema_visibility(
                existing["read_visibility"],
                classified_visibility,
            )
        default_visibility = required_visibility
        normalized_visibility = _normalize_read_visibility(
            read_visibility,
            allow_all=False,
            default=default_visibility,
        )
        if normalized_visibility != _stricter_hypomnema_visibility(
            normalized_visibility,
            required_visibility,
        ):
            raise ValueError(
                "Hypomnema requires review visibility before operational use"
            )
        conn.execute(
            """
            INSERT INTO hypomnema_entries(
                id, agent_id, person_id, project_scope, content, source,
                density, domain, read_visibility, tags_json, confidence, salience,
                active, foundational, revision_count, revisions_json,
                original_timestamp, related_session_id, related_engram_id,
                created_at, last_revised_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                source = excluded.source,
                density = excluded.density,
                domain = excluded.domain,
                read_visibility = excluded.read_visibility,
                tags_json = excluded.tags_json,
                confidence = excluded.confidence,
                salience = excluded.salience,
                active = hypomnema_entries.active,
                foundational = excluded.foundational,
                revision_count = excluded.revision_count,
                revisions_json = excluded.revisions_json,
                original_timestamp = excluded.original_timestamp,
                related_session_id = excluded.related_session_id,
                related_engram_id = excluded.related_engram_id,
                last_revised_at = excluded.last_revised_at
            """,
            (
                entry_id,
                agent_id,
                person_id,
                project_scope,
                content.strip(),
                source,
                _clamp(density),
                domain,
                normalized_visibility,
                _encode_json(_split_tags(tags)),
                _clamp(confidence),
                _clamp(salience),
                int(foundational),
                revision_count,
                revisions_json,
                original_timestamp,
                related_session_id,
                related_engram_id,
                now,
                now,
            ),
        )
        if hypo_degrade is not None:
            # 008g E8: atomic degrade — same transaction as the upsert.
            conn.execute(
                "UPDATE hypomnema_entries SET decision_ref = NULL, "
                "read_visibility = 'review_only' WHERE id = ?",
                (entry_id,),
            )
            self._emit_witness_degrade_trace(
                conn,
                table="hypomnema_entries",
                row_id=entry_id,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                domain=domain,
                degrade=hypo_degrade,
            )
        return entry_id

    def get_hypomnema_entry(
        self,
        entry_id: str,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        active_only: bool = False,
        read_visibility: str | None = READ_VISIBILITY_OPERATIONAL,
    ) -> dict[str, Any] | None:
        """Load a hypomnema entry by scoped ID.

        Default is ``READ_VISIBILITY_OPERATIONAL`` (fail-closed): quarantined
        entries are excluded unless a caller opts into unfiltered admin access
        with explicit ``read_visibility=None`` (R5, T3/D8-A).
        """
        conn = self._get_conn()
        query = (
            "SELECT * FROM hypomnema_entries "
            "WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [entry_id, agent_id, person_id, project_scope]
        if active_only:
            query += " AND active = 1"
        visibility_values = _normalize_read_visibility_values(read_visibility)
        query = _append_read_visibility_filter(
            query,
            params,
            "read_visibility",
            visibility_values,
        )
        query = _append_identity_decision_gate(
            query, "hypomnema_entries", visibility_values, self._vault_active
        )
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._hydrate_hypomnema_row(dict(row))

    def search_hypomnema(
        self,
        query: str = "",
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 8,
        include_inactive: bool = False,
        exclude_promotion_candidates: bool = False,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[dict[str, Any]]:
        """Search scoped hypomnema entries by text, confidence, and salience.

        The default read surface is operational context. Operational packet and
        runtime callers can also exclude promotion candidates so stable-but-
        unreviewed continuity does not steer the agent as prose.
        """
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [agent_id, person_id, project_scope]
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if not include_inactive:
            sql += " AND active = 1"
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
        sql = _append_identity_decision_gate(sql, "hypomnema_entries", visibility_values, self._vault_active)
        if exclude_promotion_candidates:
            sql = _append_hypomnema_review_candidate_filter(
                sql,
                params,
                negate=True,
            )
        sql += " ORDER BY foundational DESC, last_revised_at DESC LIMIT 100"
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
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
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
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if active_only:
            sql += " AND active = 1"
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
        sql = _append_identity_decision_gate(sql, "hypomnema_entries", visibility_values, self._vault_active)
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
        read_visibility: str | Sequence[str] | None = None,
    ) -> str:
        """Revise an existing hypomnema entry while preserving the old version."""
        if not new_content.strip():
            raise ValueError("Revised hypomnema content cannot be empty")
        if not reason.strip():
            raise ValueError("Revision reason cannot be empty")

        now = _utc_now()
        conn = self._get_conn()
        sql = (
            "SELECT * FROM hypomnema_entries "
            "WHERE id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?"
        )
        params: list[Any] = [entry_id, agent_id, person_id, project_scope]
        sql = _apply_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            read_visibility,
        )
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise KeyError(f"Hypomnema entry not found for scope: {entry_id}")

        revisions = _decode_json(row["revisions_json"], [])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": reason.strip(),
            }
        )
        new_confidence = _clamp(
            confidence if confidence is not None else row["confidence"]
        )
        new_salience = _clamp(salience if salience is not None else row["salience"])
        new_revision_count = int(row["revision_count"] or 0) + 1
        new_domain = _classify_hypomnema_domain_from_text(
            new_content,
            fallback=row["domain"],
        )
        new_foundational = bool(row["foundational"]) or new_domain in {
            "identity",
            "foundational",
        }
        classified_visibility = classify_hypomnema_read_visibility(
            confidence=new_confidence,
            salience=new_salience,
            foundational=new_foundational,
            revision_count=new_revision_count,
            domain=new_domain,
        )
        new_read_visibility = _stricter_hypomnema_visibility(
            row["read_visibility"],
            classified_visibility,
        )
        # 008g E8 (round 5): revise_hypomnema_entry is a second write path that
        # was missing the witnessed-field check. Compute the same degrade
        # signal here — content/domain/foundational/scope changes on a ref-
        # carrying row must clear the ref + review_only + trace, same
        # transaction as the UPDATE, or the vault silently accepts mutations
        # via this API.
        row_dict = dict(row)
        rev_degrade: dict[str, str] | None = None
        if str(row_dict.get("decision_ref") or "").strip():
            from ..vault import journal as vault_journal
            row_dict["id"] = entry_id
            old_hash = vault_journal.canonical_row_sha256(
                "hypomnema_entries", row_dict
            )
            new_shape = {
                "id": entry_id,
                "agent_id": agent_id,
                "person_id": person_id,
                "project_scope": project_scope,
                "content": new_content.strip(),
                "domain": new_domain,
                "foundational": 1 if new_foundational else 0,
            }
            new_hash = vault_journal.canonical_row_sha256(
                "hypomnema_entries", new_shape
            )
            if old_hash != new_hash:
                rev_degrade = {
                    "old_hash": old_hash,
                    "new_hash": new_hash,
                    "prior_decision_ref": str(row_dict["decision_ref"]),
                }
        conn.execute(
            """
            UPDATE hypomnema_entries
            SET content = ?,
                confidence = ?,
                salience = ?,
                domain = ?,
                foundational = ?,
                revision_count = revision_count + 1,
                read_visibility = ?,
                revisions_json = ?,
                last_revised_at = ?
            WHERE id = ?
            """,
            (
                new_content.strip(),
                new_confidence,
                new_salience,
                new_domain,
                int(new_foundational),
                new_read_visibility,
                _encode_json(revisions),
                now,
                entry_id,
            ),
        )
        if rev_degrade is not None:
            # 008g E8: atomic degrade in the same transaction.
            conn.execute(
                "UPDATE hypomnema_entries SET decision_ref = NULL, "
                "read_visibility = 'review_only' WHERE id = ?",
                (entry_id,),
            )
            self._emit_witness_degrade_trace(
                conn,
                table="hypomnema_entries",
                row_id=entry_id,
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                domain=new_domain,
                degrade=rev_degrade,
            )
        conn.commit()
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
        read_visibility: str | Sequence[str] | None = None,
    ) -> str:
        """Replace an active hypomnema entry with a new entry and audit link."""
        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
            read_visibility=read_visibility,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        new_domain = _classify_hypomnema_domain_from_text(
            new_content,
            fallback=row["domain"],
        )
        new_foundational = bool(row["foundational"]) or new_domain in {
            "identity",
            "foundational",
        }
        replacement_visibility = (
            row["read_visibility"]
            if row["read_visibility"] != READ_VISIBILITY_OPERATIONAL
            else None
        )
        new_id = self.write_hypomnema_entry(
            new_content,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            source="synthesized",
            density=row["density"],
            domain=new_domain,
            tags=row["tags"],
            confidence=row["confidence"],
            salience=row["salience"],
            foundational=new_foundational,
            read_visibility=replacement_visibility,
            original_timestamp=row["original_timestamp"],
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
        # 008k E2B: if the row being superseded carries a witness (decision_ref),
        # clear that ref + force review_only + emit a D4-shaped trace proposal
        # in the SAME transaction as the supersede write. Reconcile's r6 #3
        # lifecycle check (superseded_by NOT NULL) then never false-fires on
        # legitimate flows — write-side degrade is primary; reconcile catches
        # only raw-SQL bypass.
        witness_ref = str(row.get("decision_ref") or "").strip()
        conn.execute("BEGIN IMMEDIATE")
        try:
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
            if witness_ref:
                self._degrade_and_trace_lifecycle(
                    conn,
                    table="hypomnema_entries",
                    row_id=entry_id,
                    prior_decision_ref=witness_ref,
                    reason=f"superseded: {reason.strip()}",
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    domain=str(row.get("domain") or ""),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return new_id

    def archive_hypomnema_entry(
        self,
        entry_id: str,
        *,
        reason: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        read_visibility: str | Sequence[str] | None = None,
    ) -> str:
        """Deactivate a scoped hypomnema entry while preserving its revision trail."""
        if not reason.strip():
            raise ValueError("Archive reason cannot be empty")

        row = self.get_hypomnema_entry(
            entry_id,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            active_only=True,
            read_visibility=read_visibility,
        )
        if row is None:
            raise KeyError(f"Active hypomnema entry not found for scope: {entry_id}")

        now = _utc_now()
        revisions = list(row["revisions"])
        revisions.append(
            {
                "at": now,
                "prior_content": row["content"],
                "reason": f"archived: {reason.strip()}",
            }
        )
        conn = self._get_conn()
        # 008k E2B: same clear-and-quarantine + trace as supersede path.
        witness_ref = str(row.get("decision_ref") or "").strip()
        conn.execute("BEGIN IMMEDIATE")
        try:
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
            if witness_ref:
                self._degrade_and_trace_lifecycle(
                    conn,
                    table="hypomnema_entries",
                    row_id=entry_id,
                    prior_decision_ref=witness_ref,
                    reason=f"archived: {reason.strip()}",
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    domain=str(row.get("domain") or ""),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return entry_id

    def mark_hypomnema_promoted(self, entry_id: str, engram_id: str) -> None:
        """Record that a hypomnema entry graduated into a Mnemos engram."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE hypomnema_entries SET graduated_to_engram_id = ? WHERE id = ?",
            (engram_id, entry_id),
        )
        conn.commit()

    def get_hypomnema_promotion_candidates(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        limit: int = 10,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> list[dict[str, Any]]:
        """List stable hypomnema entries ready to become Mnemos engrams.

        Defaults to operational-context candidates; explicit review surfaces
        may include review-only candidates. Audit-only candidates require an
        explicit audit visibility read.
        """
        conn = self._get_conn()
        visibility_values = _normalize_read_visibility_values(read_visibility)
        sql = """
            SELECT * FROM hypomnema_entries
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
        """
        params: list[Any] = [
            agent_id,
            person_id,
            project_scope,
        ]
        sql = _append_hypomnema_review_candidate_filter(sql, params)
        sql = _append_read_visibility_filter(
            sql,
            params,
            "read_visibility",
            visibility_values,
        )
        sql = _append_identity_decision_gate(sql, "hypomnema_entries", visibility_values, self._vault_active)
        sql += (
            " ORDER BY foundational DESC, confidence DESC, salience DESC, created_at ASC"
            " LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [self._hydrate_hypomnema_row(dict(row)) for row in rows]

    def get_hypomnema_stats(
        self,
        *,
        agent_id: str = "default",
        person_id: str | None = None,
        project_scope: str | None = None,
        read_visibility: str | Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Count hypomnema entries for a scope."""
        conn = self._get_conn()
        where = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        visibility_values = _normalize_read_visibility_values(read_visibility)
        if person_id is not None:
            where.append("person_id = ?")
            params.append(person_id)
        if project_scope is not None:
            where.append("project_scope = ?")
            params.append(project_scope)
        if visibility_values is not None:
            placeholders = ", ".join("?" for _ in visibility_values)
            where.append(f"read_visibility IN ({placeholders})")
            params.extend(visibility_values)
        where_sql = " AND ".join(where)
        # 008g-r6 #4: extend T4 gate to hypomnema stats. Unwitnessed identity
        # rows must not steer counts even if operational_context slips through.
        # 008k-r12 #2: but an explicit admin read (read_visibility=None →
        # visibility_values is None) must see everything, matching the
        # store/audit contract and get_beliefs' read_visibility=None semantics.
        gate = (
            ""
            if visibility_values is None
            else identity_decision_gate_sql(
                "hypomnema_entries", active=self._vault_active
            )
        )
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN foundational = 1 AND active = 1 THEN 1 ELSE 0 END) AS foundational,
              SUM(CASE WHEN graduated_to_engram_id IS NOT NULL THEN 1 ELSE 0 END) AS promoted
            FROM hypomnema_entries
            WHERE {where_sql}{gate}
            """,
            params,
        ).fetchone()
        candidate_params = list(params)
        candidate_query = _append_hypomnema_review_candidate_filter(
            f"SELECT COUNT(*) FROM hypomnema_entries WHERE {where_sql}",
            candidate_params,
        )
        # 008g-r7 #4: the promotion-candidate count also flows into status/
        # context surfaces (belief_review, IdentityProfile weighting). Apply
        # the same T4 gate so an unwitnessed identity hypomnema can't drive
        # promotion signals.
        candidate_query += gate
        candidate_row = conn.execute(candidate_query, candidate_params).fetchone()
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
        conn.commit()

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

    def save_identity(self, identity: AgentIdentity) -> None:
        """Save agent identity."""
        conn = self._get_conn()
        data = identity.to_dict()
        data["agent_id"] = identity.memory_profile.agent_id
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "agent_id")

        conn.execute(
            f"INSERT INTO agent_identity ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
            list(data.values()),
        )
        conn.commit()

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
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta value."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()

    # ── Consolidation Log ──

    def log_consolidation(
        self,
        log_id: str,
        pass_name: str,
        started_at: str,
        completed_at: str | None = None,
        stats: dict | None = None,
    ) -> None:
        """Log a consolidation pass."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_log "
            "(id, pass_name, started_at, completed_at, stats) "
            "VALUES (?, ?, ?, ?, ?)",
            (log_id, pass_name, started_at, completed_at, json.dumps(stats or {})),
        )
        conn.commit()

    def get_consolidation_runs(self, pass_name: str, limit: int = 5) -> list[dict]:
        """Most recent consolidation_log rows for a pass, newest first.

        The stats column is JSON-decoded. The table has no agent_id
        column; passes that need agent scoping carry it inside stats.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM consolidation_log WHERE pass_name = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (pass_name, limit),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["stats"] = json.loads(item.get("stats") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["stats"] = {}
            out.append(item)
        return out

    # ── Inner-Life Event Ledger ──

    def upsert_inner_life_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        process_name: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        session_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        role: str | None = None,
        source_message_id: str | None = None,
        source_path: str | None = None,
        source_timestamp: str | None = None,
        content_hash: str = "",
        content_excerpt: str = "",
        event_tags: list[str] | tuple[str, ...] | str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
        metadata: dict[str, Any] | None = None,
        rollout_tag: str = "",
        gate_decision: str = "ledger_only",
        commit: bool = True,
    ) -> dict[str, Any]:
        """Insert or update a private U6.6 event-ledger row.

        This helper intentionally writes only to `inner_life_events`. It never
        encodes memory or touches beliefs, hypomnema, identity, candidates, or
        sharing surfaces.

        Pass ``commit=False`` to let a caller compose this write into a larger
        transaction it owns (see ``save_engram_with_inner_life_event``); the row
        is then finalized by the caller's commit.
        """
        if event_type not in VALID_INNER_LIFE_EVENT_TYPES:
            raise ValueError(f"Unsupported inner-life event_type: {event_type}")
        idempotency_key = idempotency_key.strip()
        process_name = process_name.strip()
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if not process_name:
            raise ValueError("process_name is required")

        conn = self._get_conn()
        now = _utc_now()
        existing = conn.execute(
            """
            SELECT id, created_at FROM inner_life_events
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        event_id = existing["id"] if existing is not None else _new_id()
        created_at = existing["created_at"] if existing is not None else now
        conn.execute(
            """
            INSERT INTO inner_life_events (
                id, idempotency_key, event_type, process_name, agent_id,
                person_id, project_scope, session_id, thread_id, turn_id,
                role, source_message_id, source_path, source_timestamp,
                content_hash, content_excerpt, event_tags_json,
                source_ids_json, metadata_json, rollout_tag, gate_decision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                event_type = excluded.event_type,
                process_name = excluded.process_name,
                agent_id = excluded.agent_id,
                person_id = excluded.person_id,
                project_scope = excluded.project_scope,
                session_id = excluded.session_id,
                thread_id = excluded.thread_id,
                turn_id = excluded.turn_id,
                role = excluded.role,
                source_message_id = excluded.source_message_id,
                source_path = excluded.source_path,
                source_timestamp = excluded.source_timestamp,
                content_hash = excluded.content_hash,
                content_excerpt = excluded.content_excerpt,
                event_tags_json = excluded.event_tags_json,
                source_ids_json = excluded.source_ids_json,
                metadata_json = excluded.metadata_json,
                rollout_tag = excluded.rollout_tag,
                gate_decision = excluded.gate_decision,
                updated_at = excluded.updated_at
            """,
            (
                event_id,
                idempotency_key,
                event_type,
                process_name,
                agent_id,
                person_id,
                project_scope,
                session_id,
                thread_id,
                turn_id,
                role,
                source_message_id,
                source_path,
                source_timestamp,
                content_hash,
                content_excerpt,
                _encode_json(_split_tags(event_tags)),
                _encode_json([str(item) for item in (source_ids or [])]),
                _encode_json(metadata or {}),
                rollout_tag,
                gate_decision,
                created_at,
                now,
            ),
        )
        if commit:
            conn.commit()
        return {
            "id": event_id,
            "inserted": existing is None,
            "updated": existing is not None,
            "created_at": created_at,
            "updated_at": now,
        }

    def save_engram_with_inner_life_event(
        self, engram: Engram, **ledger_kwargs: Any
    ) -> dict[str, Any]:
        """Persist an engram and its inner-life ledger row in ONE transaction.

        The ledger row is the low-stakes idempotency guard. Writing it in the
        same transaction as the engram means a crash between the two can never
        leave a committed engram without its guard — which a retry would
        otherwise re-mint as a duplicate. Either both land or neither does.

        Race guard: if a concurrent writer inserted the same idempotency key
        between the caller's pre-check and this transaction, the ledger upsert
        resolves to an UPDATE (``inserted`` is False). The staged engram would
        then be a duplicate, so the whole transaction is rolled back and the
        result carries ``duplicate=True``.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._save_engram_no_commit(conn, engram)
            result = self.upsert_inner_life_event(**ledger_kwargs, commit=False)
            if not result["inserted"]:
                conn.rollback()
                return {**result, "duplicate": True}
            conn.commit()
            return {**result, "duplicate": False}
        except Exception:
            conn.rollback()
            raise

    def get_inner_life_events(
        self,
        *,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
        session_id: str | None = None,
        event_type: str | None = None,
        event_types: Sequence[str] | None = None,
        process_name: str | None = None,
        exclude_process_name: str | None = None,
        gate_decision: str | None = None,
        exclude_gate_decision: str | None = None,
        rollout_tag: str | None = None,
        limit: int = 100,
        recent: bool = False,
    ) -> list[dict[str, Any]]:
        """Return private U6.6 event-ledger rows.

        By default returns the oldest matching rows (``created_at ASC``) — the
        historical contract, preserved exactly. Recency consumers (cooldown /
        cadence / recent-window gates) must pass ``recent=True``: the newest
        ``limit`` rows are selected, then re-sorted ascending before returning,
        so existing ASC-assuming callers (``reversed(rows)``, ``max()`` folds,
        ``since <= t <= now`` filters) operate on the recent window instead of
        the ancient one once the ledger grows past ``limit``.

        Recency consumers that then filter in Python **must** push their
        eligibility predicates into SQL here (``event_types`` IN-set,
        ``process_name`` / ``exclude_process_name``, ``gate_decision``) so that
        ``limit`` bounds *eligible* rows. Otherwise a burst of ineligible rows
        (e.g. activity-gate telemetry) can fill the newest ``limit`` and push the
        one row the caller needs out of the slice.
        """
        conn = self._get_conn()
        predicates = ["agent_id = ?", "person_id = ?", "project_scope = ?"]
        params: list[Any] = [agent_id, person_id, project_scope]
        if session_id is not None:
            predicates.append("session_id = ?")
            params.append(session_id)
        if event_type is not None:
            predicates.append("event_type = ?")
            params.append(event_type)
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            predicates.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        if process_name is not None:
            predicates.append("process_name = ?")
            params.append(process_name)
        if exclude_process_name is not None:
            predicates.append("process_name != ?")
            params.append(exclude_process_name)
        if gate_decision is not None:
            predicates.append("gate_decision = ?")
            params.append(gate_decision)
        if exclude_gate_decision is not None:
            predicates.append("gate_decision != ?")
            params.append(exclude_gate_decision)
        if rollout_tag is not None:
            predicates.append("rollout_tag = ?")
            params.append(rollout_tag)
        params.append(max(1, limit))
        where = " AND ".join(predicates)
        if recent:
            query = f"""
            SELECT * FROM (
                SELECT * FROM inner_life_events
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) ORDER BY created_at ASC, id ASC
            """
        else:
            query = f"""
            SELECT * FROM inner_life_events
            WHERE {where}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """
        rows = conn.execute(query, params).fetchall()
        return [self._hydrate_inner_life_event_row(dict(row)) for row in rows]

    @staticmethod
    def _hydrate_inner_life_event_row(row: dict[str, Any]) -> dict[str, Any]:
        row["event_tags"] = _decode_json(row.pop("event_tags_json", "[]"), [])
        row["source_ids"] = _decode_json(row.pop("source_ids_json", "[]"), [])
        row["metadata"] = _decode_json(row.pop("metadata_json", "{}"), {})
        return row

    def get_last_activity_gate_run(
        self,
        *,
        target_process: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> dict[str, Any] | None:
        """Return the most recent activity-gate ``run`` row for ``target_process``,
        or None.

        The full eligibility predicate — including ``target_process`` pulled from
        the JSON metadata — is applied in SQL with ``LIMIT 1``, so no burst of
        newer ``run`` rows for *other* processes can evict the one we need. This
        is the cooldown gate's fix for the filter-after-limit hazard that
        ``get_inner_life_events(recent=True)`` alone cannot solve (the target is
        one predicate deeper than a plain column filter).
        """
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM inner_life_events
            WHERE agent_id = ? AND person_id = ? AND project_scope = ?
              AND event_type = 'tool_event'
              AND process_name = 'activity-gate'
              AND gate_decision = 'run'
              AND json_extract(metadata_json, '$.target_process') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (agent_id, person_id, project_scope, target_process),
        ).fetchone()
        return (
            self._hydrate_inner_life_event_row(dict(row)) if row is not None else None
        )

    # ── Stats ──

    def get_stats(
        self,
        agent_id: str = "default",
        include_pending_review: bool = False,
        *,
        person_id: str | None = None,
        project_scope: str | None = None,
        read_visibility: str | Sequence[str] | None = READ_VISIBILITY_OPERATIONAL,
    ) -> dict:
        """Get summary statistics for an agent's memory."""
        conn = self._get_conn()
        stats = {}
        visibility_values = _normalize_read_visibility_values(read_visibility)

        # Engram counts by state
        for state in ("active", "consolidating", "dormant", "archived"):
            query = (
                "SELECT COUNT(*) FROM engrams WHERE owner_agent_id = ? AND state = ?"
            )
            params: list[Any] = [agent_id, state]
            query = _append_read_visibility_filter(
                query,
                params,
                "read_visibility",
                visibility_values,
            )
            row = conn.execute(
                query,
                params,
            ).fetchone()
            stats[f"engrams_{state}"] = row[0] if row else 0

        # Connection count
        if visibility_values is None:
            row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        else:
            placeholders = ", ".join("?" for _ in visibility_values)
            row = conn.execute(
                "SELECT COUNT(*) FROM connections c "
                "JOIN engrams source ON source.id = c.source_id "
                "JOIN engrams target ON target.id = c.target_id "
                "WHERE source.owner_agent_id = ? "
                f"AND source.read_visibility IN ({placeholders}) "
                f"AND target.read_visibility IN ({placeholders})",
                [agent_id, *visibility_values, *visibility_values],
            ).fetchone()
        stats["connections"] = row[0] if row else 0

        # Belief count
        belief_query = (
            "SELECT COUNT(*) FROM beliefs WHERE agent_id = ? AND superseded_by IS NULL"
        )
        belief_params: list[Any] = [agent_id]
        if not include_pending_review:
            belief_query += " AND confidence_pending_review = 0"
        belief_query = _append_read_visibility_filter(
            belief_query,
            belief_params,
            "read_visibility",
            visibility_values,
        )
        # 008g-r6 #4: extend the T4 gate to belief count. Unwitnessed identity
        # rows must not steer dashboards/modulators via beliefs_active even if
        # they somehow reach operational_context. Same predicate the store
        # applies at get_beliefs.
        # 008k-r12 #2: admin read (visibility_values is None) bypasses the gate.
        if visibility_values is not None:
            belief_query += identity_decision_gate_sql(
                "beliefs", active=self._vault_active
            )
        row = conn.execute(belief_query, belief_params).fetchone()
        stats["beliefs_active"] = row[0] if row else 0

        # Version count (reconsolidation events)
        if visibility_values is None:
            row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        else:
            placeholders = ", ".join("?" for _ in visibility_values)
            row = conn.execute(
                "SELECT COUNT(*) FROM versions v "
                "JOIN engrams e ON e.id = v.engram_id "
                "WHERE e.owner_agent_id = ? "
                f"AND e.read_visibility IN ({placeholders})",
                [agent_id, *visibility_values],
            ).fetchone()
        stats["reconsolidation_events"] = row[0] if row else 0

        # Archive count
        if visibility_values is None:
            row = conn.execute("SELECT COUNT(*) FROM archive").fetchone()
        else:
            placeholders = ", ".join("?" for _ in visibility_values)
            row = conn.execute(
                "SELECT COUNT(*) FROM engrams "
                "WHERE owner_agent_id = ? AND state = 'archived' "
                f"AND read_visibility IN ({placeholders})",
                [agent_id, *visibility_values],
            ).fetchone()
        stats["archived"] = row[0] if row else 0

        # Hypomnema counts use the default person/project scope for status.
        stats.update(
            self.get_hypomnema_stats(
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                read_visibility=read_visibility,
            )
        )

        # Functional memory counts cover active working context and review load.
        stats.update(
            self.get_functional_stats(
                agent_id=agent_id,
                person_id=person_id,
                project_scope=project_scope,
                read_visibility=read_visibility,
            )
        )

        # Accessibility distribution
        accessibility_query = (
            "SELECT "
            "AVG(accessibility) as avg_acc, "
            "MIN(accessibility) as min_acc, "
            "MAX(accessibility) as max_acc "
            "FROM engrams WHERE owner_agent_id = ? AND state = 'active'"
        )
        accessibility_params: list[Any] = [agent_id]
        accessibility_query = _append_read_visibility_filter(
            accessibility_query,
            accessibility_params,
            "read_visibility",
            visibility_values,
        )
        rows = conn.execute(accessibility_query, accessibility_params).fetchone()
        if rows and rows["avg_acc"] is not None:
            stats["accessibility_avg"] = round(rows["avg_acc"], 3)
            stats["accessibility_min"] = round(rows["min_acc"], 3)
            stats["accessibility_max"] = round(rows["max_acc"], 3)

        return stats
