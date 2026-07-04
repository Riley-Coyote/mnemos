"""U3b PAI importer preview/apply scaffolding.

The importer is deliberately two-pass:
- split source files into deterministic target rows;
- preview those rows against pai_import_row_map before any writes;
- apply only rows that are inserts, repairs, or same-target updates.

The invariant is that an import key cannot silently drift to a different
target row. Changed source content updates the same target_id; missing target
rows are repaired through the existing map.

What this pipeline imports (retrievable knowledge):
- identity_kernel: SOUL.md essence, IDENTITY.md function
- david_context:   USER.md, david-facing engrams
- growth_substrate: GROWTH.md learnings
- beliefs:         FACTS.md curated beliefs
- hypomnema:       ALIVE.md, CONTINUITY.md, session-texture rolls

What this pipeline does NOT import as retrievable content:
- Strict-B eigenvalue / vivezza / coordinate-target / persona-signature
  COORDINATE VALUES. These are boot-time Q/K/V steering substrate, not memory.
  The splitter strips structural coordinate tuple lines from any source kind
  before row hashing/indexing, while preserving surrounding prose/narrative.
- Runtime-substrate files outside the manifest, such as ~/.claude/CLAUDE.md
  eigenvalue + vivezza boot blocks. Guarded by ~/bin/mnemos-identity-
  watchdog.py via pointer + checksum invariants.
- SOUL/CONSTITUTION.md — read directly by PreCompact at compaction time;
  governance invariants of reasoning, not retrieval material.
- SOUL/INTENTION.md — steering baseline loaded last in boot stack via
  @import; runtime Q/K/V bias, not engram content.

If U3c watcher detects edits to runtime-substrate files, it must NOT
attempt to route them through this importer. The identity watchdog owns
those surfaces.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Callable, Iterable

from ..core.belief import Belief
from ..core.engram import Engram, MemorySource
from ..core.types import ConfidenceSource, EngramKind, SourceAuthority, SourceType
from ..store.migrations import insert_pai_import_event, upsert_pai_import_row
from ..store.read_visibility import READ_VISIBILITY_AUDIT, READ_VISIBILITY_REVIEW
from ..store.sqlite_store import EngramStore


TARGET_ENGRAMS = "engrams"
TARGET_BELIEFS = "beliefs"
TARGET_HYPOMNEMA = "hypomnema_entries"

ACTION_PENDING = "pending"
ACTION_INSERT = "insert"
ACTION_REPAIR = "repair"
ACTION_UPDATE = "update"
ACTION_NOOP = "noop"
ACTION_ERROR = "error"

# U3b hardening CB7 — U3c-reserve constants. The default U3b classifier still
# does not produce these and apply_pai_import still rejects them; the U3c
# preview/apply entrypoints below wire the watcher semantics without reshaping
# the U3b contract:
#   ACTION_TOMBSTONE — archive engram (state='archived'), preserve target_id,
#                      record versions snapshot with pai_import_tombstone reason
#   ACTION_DEACTIVATE — set hypomnema active=False without setting superseded_by
#   ACTION_REVIEW     — flag belief needs_review=True without changing content
# Adding the strings here locks the contract surface against U3c reshaping.
ACTION_TOMBSTONE = "tombstone"
ACTION_DEACTIVATE = "deactivate"
ACTION_REVIEW = "review"
_U3C_RESERVED_ACTIONS = frozenset({ACTION_TOMBSTONE, ACTION_DEACTIVATE, ACTION_REVIEW})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SUPPORTED_SOURCE_KINDS = {
    "identity_kernel",
    "david_context",
    "growth_substrate",
    "beliefs",
    "hypomnema",
}
_PAI_AGENT_ID = "oliver"
_PAI_PERSON_ID = "david"
_PAI_PROJECT_SCOPE = "pai"


@dataclass(frozen=True)
class PaiImportSource:
    """A single source partition prepared for PAI import."""

    job_id: str
    source_path: str
    source_kind: str
    source_text: str
    agent_id: str = "oliver"
    person_id: str = "david"
    project_scope: str = "pai"
    original_substrate: str = "unknown-pre-import"
    original_timestamp: int | None = None


@dataclass(frozen=True)
class PaiImportRow:
    """One deterministic target row planned by a splitter or preview."""

    job_id: str
    source_path: str
    source_anchor: str
    source_kind: str
    target_table: str
    target_id: str
    source_hash: str
    content: str
    action: str = ACTION_PENDING
    reason: str = ""
    mapped_source_hash: str | None = None
    target_projection_hash: str | None = None
    agent_id: str = "oliver"
    person_id: str = "david"
    project_scope: str = "pai"
    original_substrate: str = "unknown-pre-import"
    original_timestamp: int | None = None
    tags: tuple[str, ...] = ()
    domain: str = "identity"
    tier: str | None = None
    confidence: float = 0.7
    voice_exemplar_eligible: bool = False
    softening_protected: bool = True
    decay_protected: bool = True
    consolidation_authorized: bool = False
    foundational: bool = True


@dataclass(frozen=True)
class PaiImportPreview:
    job_id: str
    rows: tuple[PaiImportRow, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(row.action for row in self.rows))


@dataclass(frozen=True)
class PaiImportResult:
    job_id: str
    rows: tuple[PaiImportRow, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(row.action for row in self.rows))


@dataclass(frozen=True)
class _Profile:
    target_table: str
    tags: tuple[str, ...]
    domain: str
    confidence: float
    tier: str | None = None
    voice_exemplar_eligible: bool = False
    softening_protected: bool = True
    decay_protected: bool = True
    consolidation_authorized: bool = False
    foundational: bool = True


_PROFILES = {
    "identity_kernel": _Profile(
        target_table=TARGET_ENGRAMS,
        tags=("pai-import", "identity-kernel"),
        domain="identity",
        confidence=0.9,
        tier="foundational",
    ),
    "david_context": _Profile(
        target_table=TARGET_ENGRAMS,
        tags=("pai-import", "david-context"),
        domain="social",
        confidence=0.85,
        tier="foundational",
    ),
    "growth_substrate": _Profile(
        target_table=TARGET_ENGRAMS,
        tags=("pai-import", "growth-substrate"),
        domain="self",
        confidence=0.8,
        tier="operational",
    ),
    "beliefs": _Profile(
        target_table=TARGET_BELIEFS,
        tags=("pai-import", "belief"),
        domain="identity",
        confidence=0.72,
        tier="operational",
        softening_protected=False,
        decay_protected=False,
        consolidation_authorized=True,
    ),
    "hypomnema": _Profile(
        target_table=TARGET_HYPOMNEMA,
        tags=("pai-import", "hypomnema"),
        domain="identity",
        confidence=0.7,
        tier="foundational",
    ),
}


def split_identity_kernel(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_expected_kind(source, "identity_kernel")


def split_david_context(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_expected_kind(source, "david_context")


def split_growth_substrate(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_expected_kind(source, "growth_substrate")


def split_beliefs(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_expected_kind(source, "beliefs")


def split_hypomnema(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_expected_kind(source, "hypomnema")


SPLITTERS: dict[str, Callable[[PaiImportSource], list[PaiImportRow]]] = {
    "identity_kernel": split_identity_kernel,
    "david_context": split_david_context,
    "growth_substrate": split_growth_substrate,
    "beliefs": split_beliefs,
    "hypomnema": split_hypomnema,
}


def split_pai_source(source: PaiImportSource) -> list[PaiImportRow]:
    return _split_pai_source(source, allow_empty=False)


def _split_pai_source(
    source: PaiImportSource,
    *,
    allow_empty: bool,
) -> list[PaiImportRow]:
    source = _canonical_source(source)
    source_kind = _clean_required(source.source_kind, "source_kind")
    if source_kind not in SPLITTERS:
        supported = ", ".join(sorted(SPLITTERS))
        raise ValueError(
            f"Unsupported PAI source_kind {source_kind!r}; expected {supported}"
        )
    return _split_with_profile(source, allow_empty=allow_empty)


def preview_pai_import(
    store: EngramStore,
    sources: Iterable[PaiImportSource],
) -> PaiImportPreview:
    rows = _collect_rows(sources)
    job_id = _single_job_id(rows)
    conn = store._get_conn()

    preview_rows: list[PaiImportRow] = []
    for row in rows:
        preview_rows.append(_classify_row(conn, row))
    preview_rows.extend(_stale_mapped_rows(conn, job_id, rows))

    return PaiImportPreview(job_id=job_id, rows=tuple(preview_rows))


def preview_pai_watch_update(
    store: EngramStore,
    sources: Iterable[PaiImportSource],
) -> PaiImportPreview:
    """Preview a U3c watcher update.

    U3b treats source sections absent from the current batch as ACTION_ERROR
    because a one-shot import cannot know whether the omission is intentional.
    The watcher operates on a full current source snapshot, so absence is
    meaningful and maps to per-table lifecycle actions.
    """
    source_tuple = tuple(_canonical_source(source) for source in sources)
    job_id = _single_source_job_id(source_tuple)
    rows = _collect_rows(source_tuple, allow_empty_sources=True)
    conn = store._get_conn()

    preview_rows: list[PaiImportRow] = []
    for row in rows:
        preview_rows.append(
            _classify_row(conn, row, allow_pai_tombstone_reactivation=True)
        )
    preview_rows.extend(
        _stale_mapped_rows(conn, job_id, rows, missing_source_policy="u3c")
    )

    return PaiImportPreview(job_id=job_id, rows=tuple(preview_rows))


def apply_pai_import(
    store: EngramStore,
    preview: PaiImportPreview,
) -> PaiImportResult:
    if not isinstance(preview, PaiImportPreview):
        raise TypeError("apply_pai_import requires a PaiImportPreview")
    # U3c-reserved actions must not reach U3b apply. Raising NotImplementedError
    # locks the surface and gives U3c implementers a clear failure mode if they
    # accidentally route through U3b before wiring their semantics.
    reserved = [
        row
        for row in preview.rows
        if isinstance(row, PaiImportRow) and row.action in _U3C_RESERVED_ACTIONS
    ]
    if reserved:
        raise NotImplementedError(
            f"action {reserved[0].action!r} is reserved for U3c watcher and "
            "not implemented in U3b apply"
        )
    _validate_preview(preview)
    errors = [row for row in preview.rows if row.action == ACTION_ERROR]
    if errors:
        raise ValueError(errors[0].reason)
    allowed_actions = {ACTION_INSERT, ACTION_REPAIR, ACTION_UPDATE, ACTION_NOOP}
    invalid = [row for row in preview.rows if row.action not in allowed_actions]
    if invalid:
        raise ValueError(
            "apply_pai_import requires previewed rows; "
            f"got action {invalid[0].action!r}"
        )

    conn = store._get_conn()
    applied: list[PaiImportRow] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in preview.rows:
            _assert_preview_current(conn, row)
        for row in preview.rows:
            if row.action == ACTION_NOOP:
                _upsert_pai_import_row_for_preview(conn, row)
                insert_pai_import_event(
                    conn,
                    job_id=row.job_id,
                    source_path=row.source_path,
                    source_anchor=row.source_anchor,
                    target_table=row.target_table,
                    target_id=row.target_id,
                    action=row.action,
                    source_hash_before=row.mapped_source_hash,
                    source_hash_after=row.source_hash,
                    change_reason=row.reason,
                )
                applied.append(row)
                continue
            _write_target_row(store, conn, row)
            _upsert_pai_import_row_for_preview(conn, row)
            insert_pai_import_event(
                conn,
                job_id=row.job_id,
                source_path=row.source_path,
                source_anchor=row.source_anchor,
                target_table=row.target_table,
                target_id=row.target_id,
                action=row.action,
                source_hash_before=row.mapped_source_hash,
                source_hash_after=row.source_hash,
                change_reason=row.reason,
            )
            applied.append(row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return PaiImportResult(job_id=preview.job_id, rows=tuple(applied))


def _upsert_pai_import_row_for_preview(conn, row: PaiImportRow) -> None:
    upsert_pai_import_row(
        conn,
        job_id=row.job_id,
        source_path=row.source_path,
        source_anchor=row.source_anchor,
        target_table=row.target_table,
        target_id=row.target_id,
        engram_id=row.target_id if row.target_table == TARGET_ENGRAMS else None,
        source_hash=row.source_hash,
        ensure_schema=False,
        content_at_last_import=row.content,
        agent_id=row.agent_id,
        project_scope=row.project_scope,
        source_kind=row.source_kind,
        original_timestamp=row.original_timestamp,
    )


def apply_pai_watch_update(
    store: EngramStore,
    preview: PaiImportPreview,
) -> PaiImportResult:
    """Apply a U3c watcher preview with lifecycle semantics for missing sources."""
    if not isinstance(preview, PaiImportPreview):
        raise TypeError("apply_pai_watch_update requires a PaiImportPreview")
    _validate_preview(preview, allow_empty=True)
    if not preview.rows:
        return PaiImportResult(job_id=preview.job_id, rows=())
    errors = [row for row in preview.rows if row.action == ACTION_ERROR]
    if errors:
        raise ValueError(errors[0].reason)
    allowed_actions = {
        ACTION_INSERT,
        ACTION_REPAIR,
        ACTION_UPDATE,
        ACTION_NOOP,
        ACTION_TOMBSTONE,
        ACTION_DEACTIVATE,
        ACTION_REVIEW,
    }
    invalid = [row for row in preview.rows if row.action not in allowed_actions]
    if invalid:
        raise ValueError(
            "apply_pai_watch_update requires previewed rows; "
            f"got action {invalid[0].action!r}"
        )

    conn = store._get_conn()
    applied: list[PaiImportRow] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in preview.rows:
            _assert_preview_current_for_watch(conn, row)
        for row in preview.rows:
            if row.action == ACTION_NOOP:
                _upsert_pai_import_row_for_preview(conn, row)
                insert_pai_import_event(
                    conn,
                    job_id=row.job_id,
                    source_path=row.source_path,
                    source_anchor=row.source_anchor,
                    target_table=row.target_table,
                    target_id=row.target_id,
                    action=row.action,
                    source_hash_before=row.mapped_source_hash,
                    source_hash_after=row.source_hash,
                    change_reason=row.reason,
                )
                applied.append(row)
                continue
            if row.action in _U3C_RESERVED_ACTIONS:
                changed = _apply_u3c_lifecycle_row_no_commit(conn, row)
                if changed:
                    insert_pai_import_event(
                        conn,
                        job_id=row.job_id,
                        source_path=row.source_path,
                        source_anchor=row.source_anchor,
                        target_table=row.target_table,
                        target_id=row.target_id,
                        action=row.action,
                        source_hash_before=row.mapped_source_hash,
                        source_hash_after=row.source_hash,
                        change_reason=row.reason,
                    )
                    applied.append(row)
                continue

            _write_target_row(
                store,
                conn,
                row,
                allow_pai_tombstone_reactivation=True,
            )
            _upsert_pai_import_row_for_preview(conn, row)
            insert_pai_import_event(
                conn,
                job_id=row.job_id,
                source_path=row.source_path,
                source_anchor=row.source_anchor,
                target_table=row.target_table,
                target_id=row.target_id,
                action=row.action,
                source_hash_before=row.mapped_source_hash,
                source_hash_after=row.source_hash,
                change_reason=row.reason,
            )
            applied.append(row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return PaiImportResult(job_id=preview.job_id, rows=tuple(applied))


def _collect_rows(
    sources: Iterable[PaiImportSource],
    *,
    allow_empty_sources: bool = False,
) -> list[PaiImportRow]:
    rows: list[PaiImportRow] = []
    for source in sources:
        rows.extend(_split_pai_source(source, allow_empty=allow_empty_sources))
    if not rows:
        if allow_empty_sources:
            return rows
        raise ValueError("At least one PAI import row is required")
    seen_keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (row.job_id, row.source_path, row.source_anchor, row.target_table)
        if key in seen_keys:
            raise ValueError(f"Duplicate PAI import row key: {key!r}")
        seen_keys.add(key)
    return rows


def _single_job_id(rows: list[PaiImportRow]) -> str:
    job_ids = {row.job_id for row in rows}
    if len(job_ids) != 1:
        raise ValueError("A preview/apply batch must use one job_id")
    return next(iter(job_ids))


def _single_source_job_id(sources: tuple[PaiImportSource, ...]) -> str:
    if not sources:
        raise ValueError("At least one PAI import source is required")
    job_ids = {source.job_id for source in sources}
    if len(job_ids) != 1:
        raise ValueError("A preview/apply batch must use one job_id")
    return next(iter(job_ids))


def _split_expected_kind(
    source: PaiImportSource,
    expected_kind: str,
    *,
    allow_empty: bool = False,
) -> list[PaiImportRow]:
    source = _canonical_source(source)
    if source.source_kind != expected_kind:
        raise ValueError(
            f"{expected_kind} splitter requires source_kind={expected_kind!r}; "
            f"got {source.source_kind!r}"
        )
    return _split_with_profile(source, allow_empty=allow_empty)


def _split_with_profile(
    source: PaiImportSource,
    *,
    allow_empty: bool = False,
) -> list[PaiImportRow]:
    _validate_source(source)
    profile = _PROFILES[source.source_kind]
    blocks = _split_blocks(source.source_text)
    rows = [
        _make_row(
            source=source,
            profile=profile,
            source_anchor=anchor,
            content=content,
        )
        for anchor, content in blocks
    ]
    if not rows and not allow_empty:
        raise ValueError(f"No importable content in {source.source_path!r}")
    return rows


def _make_row(
    *,
    source: PaiImportSource,
    profile: _Profile,
    source_anchor: str,
    content: str,
) -> PaiImportRow:
    target_id = _target_id(
        job_id=source.job_id,
        source_path=source.source_path,
        source_anchor=source_anchor,
        target_table=profile.target_table,
    )
    source_hash = _source_hash(
        source_kind=source.source_kind,
        source_anchor=source_anchor,
        content=content,
        original_substrate=source.original_substrate,
        original_timestamp=source.original_timestamp,
    )
    return PaiImportRow(
        job_id=source.job_id,
        source_path=source.source_path,
        source_anchor=source_anchor,
        source_kind=source.source_kind,
        target_table=profile.target_table,
        target_id=target_id,
        source_hash=source_hash,
        content=content,
        agent_id=source.agent_id,
        person_id=source.person_id,
        project_scope=source.project_scope,
        original_substrate=source.original_substrate,
        original_timestamp=source.original_timestamp,
        tags=profile.tags,
        domain=profile.domain,
        tier=profile.tier,
        confidence=profile.confidence,
        voice_exemplar_eligible=profile.voice_exemplar_eligible,
        softening_protected=profile.softening_protected,
        decay_protected=profile.decay_protected,
        consolidation_authorized=profile.consolidation_authorized,
        foundational=profile.foundational,
    )


def _write_target_row(
    store: EngramStore,
    conn,
    row: PaiImportRow,
    *,
    allow_pai_tombstone_reactivation: bool = False,
) -> None:
    if row.target_table == TARGET_ENGRAMS:
        _write_pai_engram_no_commit(
            store,
            conn,
            row,
            allow_pai_tombstone_reactivation=allow_pai_tombstone_reactivation,
        )
    elif row.target_table == TARGET_BELIEFS:
        _write_pai_belief_no_commit(store, conn, row)
    elif row.target_table == TARGET_HYPOMNEMA:
        # U3b hardening CB8: PAI re-import reactivates a deactivated hypomnema
        # entry (active=0 AND superseded_by IS NULL). _classify_row already
        # gated superseded entries to ACTION_ERROR — this UPDATE only fires
        # for the unambiguous reactivation case. The downstream UPSERT in
        # _write_hypomnema_entry_no_commit preserves active = existing.active,
        # so we have to flip it explicitly here before the UPSERT runs.
        conn.execute(
            "UPDATE hypomnema_entries SET active = 1 "
            "WHERE id = ? AND active = 0 AND superseded_by IS NULL",
            (row.target_id,),
        )
        store._write_hypomnema_entry_no_commit(
            conn,
            row.content,
            entry_id=row.target_id,
            agent_id=row.agent_id,
            person_id=row.person_id,
            project_scope=row.project_scope,
            source="observed",
            domain=row.domain,
            tags=list(row.tags),
            confidence=row.confidence,
            salience=0.7,
            foundational=row.foundational,
            original_timestamp=row.original_timestamp,
        )
    else:
        raise ValueError(f"Unsupported target_table: {row.target_table}")


def _row_to_engram(row: PaiImportRow) -> Engram:
    return Engram(
        id=row.target_id,
        content=row.content,
        content_at_encoding=row.content,
        impact=f"Imported PAI {row.source_kind.replace('_', ' ')} substrate.",
        kind=EngramKind.SEMANTIC,
        tags=list(row.tags),
        strength=0.7,
        stability=0.7,
        accessibility=0.6,
        voice_exemplar_eligible=row.voice_exemplar_eligible,
        softening_protected=row.softening_protected,
        original_substrate=row.original_substrate,
        original_timestamp=row.original_timestamp,
        consolidation_authorized=row.consolidation_authorized,
        decay_protected=row.decay_protected,
        source=MemorySource(
            type=SourceType.EXTERNAL,
            confidence=row.confidence,
            confidence_source=ConfidenceSource.USER_EXPLICIT,
            # Curated-history importer stamps imported, never user_stated
            # (R1/DAVID-7); import config cannot elevate authority.
            authority=SourceAuthority.IMPORTED,
        ),
        owner_agent_id=row.agent_id,
    )


def _row_to_belief(row: PaiImportRow) -> Belief:
    # U3b hardening CB3: imported beliefs arrive flagged for review.
    # PAI imports are canonical AT THE MOMENT OF IMPORT, but the substrate's
    # downstream review work (consolidation flagging a stale belief for
    # re-examination) is real continuity material that should not be
    # auto-cleared by the next import. needs_review=True is the canonical
    # post-import state; the substrate clears it once review concludes.
    return Belief(
        id=row.target_id,
        agent_id=row.agent_id,
        content=row.content,
        confidence=row.confidence,
        domain=row.domain,
        tier=row.tier,
        needs_review=True,
        confidence_pending_review=True,
    )


def _write_pai_engram_no_commit(
    store: EngramStore,
    conn,
    row: PaiImportRow,
    *,
    allow_pai_tombstone_reactivation: bool = False,
) -> None:
    existing = _target_record(conn, row)
    engram = _row_to_engram(row)
    if existing is None:
        store._save_engram_no_commit(conn, engram)
        return

    reactivating = existing["state"] == "archived"
    if reactivating:
        if not allow_pai_tombstone_reactivation or not _is_pai_tombstoned_engram(
            conn, row
        ):
            raise ValueError(
                "mapped target is archived; refusing implicit PAI reactivation"
            )
        conn.execute(
            "DELETE FROM archive WHERE id = ? AND archive_reason = ?",
            (row.target_id, _pai_tombstone_reason(row)),
        )
        _clear_row_map_tombstone_no_commit(conn, row)

    if existing["content"] != row.content:
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version_num), 0) + 1 FROM versions WHERE engram_id = ?",
            (row.target_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO versions (
                engram_id, version_num, content_snapshot,
                resolution_at_version, changed_at, change_reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.target_id,
                next_version,
                existing["content"],
                existing["resolution"],
                _now_iso(),
                # U3b hardening B5-audit-4: change_reason carries job_id so
                # the operator can reconstruct "what did job X do" without
                # joining versions to pai_import_events on timestamp.
                f"pai_import_{row.action}:{row.job_id}",
            ),
        )

    source = engram.source.to_dict()
    conn.execute(
        """
        UPDATE engrams
        SET content = ?,
            impact = ?,
            kind = ?,
            tags = ?,
            strength = ?,
            stability = ?,
            accessibility = ?,
            source = ?,
            owner_agent_id = ?,
            voice_exemplar_eligible = ?,
            softening_protected = ?,
            original_substrate = ?,
            original_timestamp = ?,
            consolidation_authorized = ?,
            decay_protected = ?,
            state = 'active'
        WHERE id = ?
        """,
        (
            row.content,
            engram.impact,
            EngramKind.SEMANTIC.value,
            json.dumps(list(row.tags)),
            engram.strength,
            engram.stability,
            engram.accessibility,
            json.dumps(source),
            row.agent_id,
            int(row.voice_exemplar_eligible),
            int(row.softening_protected),
            row.original_substrate,
            row.original_timestamp,
            int(row.consolidation_authorized),
            int(row.decay_protected),
            row.target_id,
        ),
    )
    conn.execute("DELETE FROM engrams_fts WHERE id = ?", (row.target_id,))
    conn.execute(
        "INSERT INTO engrams_fts (id, content) VALUES (?, ?)",
        (row.target_id, row.content),
    )


def _write_pai_belief_no_commit(store: EngramStore, conn, row: PaiImportRow) -> None:
    existing = _target_record(conn, row)
    if existing is None:
        store._save_belief_no_commit(conn, _row_to_belief(row))
        # U3b hardening CB3: populate original_substrate / original_timestamp.
        # These columns were added in v5 — engrams and hypomnema already
        # carried them; beliefs didn't, so substrate-shift downstream
        # couldn't down-weight beliefs from older substrate.
        conn.execute(
            "UPDATE beliefs SET original_substrate = ?, original_timestamp = ? "
            "WHERE id = ?",
            (row.original_substrate, row.original_timestamp, row.target_id),
        )
        return

    revisions = _safe_json_loads(existing["revision_history"], [])
    if not isinstance(revisions, list):
        revisions = []
    if existing["content"] != row.content or not _float_equal(
        existing["confidence"], row.confidence
    ):
        revisions.append(
            {
                "timestamp": _now_iso(),
                "old_confidence": existing["confidence"],
                "new_confidence": row.confidence,
                "reason": f"pai_import_{row.action}",
                "trigger_engram_id": None,
                "old_content": existing["content"],
                "new_content": row.content,
                "job_id": row.job_id,
            }
        )

    read_visibility = _pai_belief_review_visibility(existing)
    # U3b hardening CB3: re-imported beliefs return to needs_review=True. The
    # substrate's prior review work is preserved in revision_history (above);
    # the flag flips so the next consolidation pass knows to re-evaluate.
    conn.execute(
        """
        UPDATE beliefs
        SET content = ?,
            confidence = ?,
            domain = ?,
            tier = ?,
            needs_review = 1,
            confidence_pending_review = 1,
            read_visibility = ?,
            revision_history = ?,
            last_revised = ?,
            original_substrate = ?,
            original_timestamp = ?
        WHERE id = ?
        """,
        (
            row.content,
            row.confidence,
            row.domain,
            row.tier,
            read_visibility,
            json.dumps(revisions),
            _now_iso(),
            row.original_substrate,
            row.original_timestamp,
            row.target_id,
        ),
    )


def _apply_u3c_lifecycle_row_no_commit(conn, row: PaiImportRow) -> bool:
    if row.action == ACTION_TOMBSTONE:
        if row.target_table != TARGET_ENGRAMS:
            raise ValueError("ACTION_TOMBSTONE requires an engrams target")
        return _tombstone_pai_engram_no_commit(conn, row)
    if row.action == ACTION_DEACTIVATE:
        if row.target_table != TARGET_HYPOMNEMA:
            raise ValueError("ACTION_DEACTIVATE requires a hypomnema target")
        return _deactivate_pai_hypomnema_no_commit(conn, row)
    if row.action == ACTION_REVIEW:
        if row.target_table != TARGET_BELIEFS:
            raise ValueError("ACTION_REVIEW requires a beliefs target")
        return _review_pai_belief_no_commit(conn, row)
    raise ValueError(f"Unsupported U3c lifecycle action: {row.action}")


def _tombstone_pai_engram_no_commit(conn, row: PaiImportRow) -> bool:
    existing = _target_record(conn, row)
    if existing is None:
        raise ValueError(f"Cannot tombstone missing engram target {row.target_id!r}")
    if existing["state"] == "archived":
        if _is_pai_tombstoned_engram(conn, row):
            _mark_row_map_tombstone_no_commit(conn, row)
        return False

    next_version = conn.execute(
        "SELECT COALESCE(MAX(version_num), 0) + 1 FROM versions WHERE engram_id = ?",
        (row.target_id,),
    ).fetchone()[0]
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO versions (
            engram_id, version_num, content_snapshot,
            resolution_at_version, changed_at, change_reason
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            row.target_id,
            next_version,
            existing["content"],
            existing["resolution"],
            now,
            f"pai_import_tombstone:{row.job_id}",
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO archive (
            id, content, content_at_encoding, kind, tags,
            archived_at, archive_reason, final_accessibility
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.target_id,
            existing["content"],
            existing["content_at_encoding"],
            existing["kind"],
            existing["tags"],
            now,
            f"pai_import_tombstone:{row.job_id}",
            existing["accessibility"],
        ),
    )
    conn.execute("UPDATE engrams SET state = 'archived' WHERE id = ?", (row.target_id,))
    conn.execute("DELETE FROM engrams_fts WHERE id = ?", (row.target_id,))
    _mark_row_map_tombstone_no_commit(conn, row)
    return True


def _deactivate_pai_hypomnema_no_commit(conn, row: PaiImportRow) -> bool:
    existing = _target_record(conn, row)
    if existing is None:
        raise ValueError(
            f"Cannot deactivate missing hypomnema target {row.target_id!r}"
        )
    if not bool(existing["active"]):
        return False

    revisions = _safe_json_loads(existing["revisions_json"], [])
    if not isinstance(revisions, list):
        revisions = []
    now = _now_iso()
    revisions.append(
        {
            "at": now,
            "prior_content": existing["content"],
            "reason": f"deactivated: pai_import_deactivate:{row.job_id}",
        }
    )
    conn.execute(
        """
        UPDATE hypomnema_entries
        SET active = 0,
            revision_count = revision_count + 1,
            revisions_json = ?,
            last_revised_at = ?
        WHERE id = ?
        """,
        (json.dumps(revisions), now, row.target_id),
    )
    return True


def _pai_tombstone_reason(row: PaiImportRow) -> str:
    return f"pai_import_tombstone:{row.job_id}"


def _is_pai_tombstoned_engram(conn, row: PaiImportRow) -> bool:
    if row.target_table != TARGET_ENGRAMS:
        return False
    record = _target_record(conn, row)
    if record is None or record["state"] != "archived":
        return False
    archive = conn.execute(
        "SELECT archive_reason FROM archive WHERE id = ?",
        (row.target_id,),
    ).fetchone()
    return archive is not None and archive["archive_reason"] == _pai_tombstone_reason(
        row
    )


def _mark_row_map_tombstone_no_commit(conn, row: PaiImportRow) -> None:
    conn.execute(
        """
        UPDATE pai_import_row_map
        SET tombstone_at = COALESCE(tombstone_at, CAST(strftime('%s', 'now') AS INTEGER))
        WHERE job_id = ?
          AND source_path = ?
          AND source_anchor = ?
          AND target_table = ?
          AND target_id = ?
        """,
        (
            row.job_id,
            row.source_path,
            row.source_anchor,
            row.target_table,
            row.target_id,
        ),
    )


def _clear_row_map_tombstone_no_commit(conn, row: PaiImportRow) -> None:
    conn.execute(
        """
        UPDATE pai_import_row_map
        SET tombstone_at = NULL
        WHERE job_id = ?
          AND source_path = ?
          AND source_anchor = ?
          AND target_table = ?
          AND target_id = ?
        """,
        (
            row.job_id,
            row.source_path,
            row.source_anchor,
            row.target_table,
            row.target_id,
        ),
    )


def _review_pai_belief_no_commit(conn, row: PaiImportRow) -> bool:
    existing = _target_record(conn, row)
    if existing is None:
        raise ValueError(f"Cannot review missing belief target {row.target_id!r}")
    read_visibility = _pai_belief_review_visibility(existing)
    if (
        bool(existing["needs_review"])
        and bool(existing["confidence_pending_review"])
        and existing["read_visibility"] == read_visibility
    ):
        return False

    revisions = _safe_json_loads(existing["revision_history"], [])
    if not isinstance(revisions, list):
        revisions = []
    now = _now_iso()
    revisions.append(
        {
            "timestamp": now,
            "old_confidence": existing["confidence"],
            "new_confidence": existing["confidence"],
            "reason": f"pai_import_review:{row.job_id}",
            "trigger_engram_id": None,
        }
    )
    conn.execute(
        """
        UPDATE beliefs
        SET needs_review = 1,
            confidence_pending_review = 1,
            read_visibility = ?,
            revision_history = ?,
            last_revised = ?
        WHERE id = ?
        """,
        (read_visibility, json.dumps(revisions), now, row.target_id),
    )
    return True


def _pai_belief_review_visibility(existing) -> str:
    if existing["read_visibility"] == READ_VISIBILITY_AUDIT:
        return READ_VISIBILITY_AUDIT
    return READ_VISIBILITY_REVIEW


# Strict-B content guard. Eigenvalue / vivezza / coordinate-target / persona-
# signature COORDINATE VALUES are runtime substrate (boot-time Q/K/V steering),
# not retrievable memory: "Eigenvalues live in PAI files; Mnemos holds pointer +
# tripwire only." The module docstring documents this exclusion, but it was
# enforced only by leaving ~/.claude/CLAUDE.md out of the manifest — a
# file-scoped guarantee. SOUL.md reproduces the same coordinate blocks (Autovalori
# / Vivezza / Coordinate Target / Firma della Persona) and is imported as
# identity_kernel, so the coordinate values leaked into retrievable engrams.
#
# The guard is content-scoped and operates LINE-by-LINE: it strips coordinate-
# value lines wherever they appear (any source file, any heading) while
# preserving the surrounding prose and narrative — a curated hypomnema that
# merely QUOTES a coordinate line keeps its narrative; only the values go. A
# section that is nothing but a heading + coordinates collapses to heading-only
# and is dropped. Structural tells: a `name: 0.3 | name: 0.7 | name: DIAGONALE`
# tuple line, or a `(0.9 risoluzione, 0.1 auto-riferimento, ...)` tuple.
_EIGEN_COORD_SEGMENT = re.compile(r"[^\s|:]+\s*:\s*(?:[01]?\.\d+|[A-Z]{3,})")
_EIGEN_COORD_TUPLE = re.compile(r"\([01]?\.\d+\s+\S+\s*,\s*[01]?\.\d+\s+\S+")


def _is_eigenvalue_coordinate_line(line: str) -> bool:
    """True if a single line carries eigenvalue/vivezza/coordinate-target/
    persona-signature coordinate VALUES (Strict-B). Content-scoped: matches the
    structural coordinate pattern, not file identity or heading text."""
    s = line.strip().strip("`")
    # Pipe-delimited coordinate tuple: `name: 0.3 | name: 0.7 | name: DIAGONALE`
    if "|" in s and len(_EIGEN_COORD_SEGMENT.findall(s)) >= 2:
        return True
    # Parenthesised coordinate tuple: `(0.9 risoluzione, 0.1 auto-riferimento, ...)`
    return bool(_EIGEN_COORD_TUPLE.search(s))


def _strip_eigenvalue_coordinates(content: str) -> str:
    """Remove coordinate-VALUE lines, preserving surrounding prose/narrative."""
    kept = [
        raw for raw in content.splitlines() if not _is_eigenvalue_coordinate_line(raw)
    ]
    return "\n".join(kept).strip()


def _has_body_beyond_heading(content: str) -> bool:
    """True if content has any non-heading, non-blank line — so a section left
    with only its heading after coordinate stripping is dropped, not engrammed."""
    return any(
        line.strip() and not _HEADING_RE.match(line) for line in content.splitlines()
    )


def _split_blocks(text: str) -> list[tuple[str, str]]:
    stripped = text.strip()
    if not stripped:
        return []

    heading_sections = _heading_sections(stripped)
    if heading_sections:
        cleaned_sections: list[tuple[str, str]] = []
        for anchor, content in heading_sections:
            cleaned = _strip_eigenvalue_coordinates(content)
            if cleaned == content:
                # No coordinate lines in this section — preserve it verbatim,
                # including legitimately heading-only sections (a heading
                # immediately followed by a sub-heading). Only coordinate
                # stripping may remove a section, never this guard.
                cleaned_sections.append((anchor, content))
            elif _has_body_beyond_heading(cleaned):
                # Coordinate lines stripped; prose/narrative remains.
                cleaned_sections.append((anchor, cleaned))
            # else: section was heading + only coordinate values → drop.
        return cleaned_sections

    blocks: list[tuple[str, str]] = []
    for index, block in enumerate(re.split(r"\n\s*\n", stripped), start=1):
        cleaned = _strip_eigenvalue_coordinates(block)
        if cleaned:
            blocks.append((f"block:{index:03d}", cleaned))
    return blocks


def _heading_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    preamble_lines: list[str] = []
    slug_counts: dict[str, int] = {}

    def flush() -> None:
        nonlocal current_title, current_lines
        if current_title is None:
            return
        content = "\n".join(current_lines).strip()
        if content:
            slug = _slugify(current_title)
            # U3b hardening B4-u3c-1: duplicate slugs use ordinal suffixes
            # rather than raising. SOUL files routinely repeat H2s like "Voce"
            # / "Note" / "Stato"; raising on dup made the importer unusable
            # for the live watcher and for real SOUL/ content. The anchor's
            # `:N:003`-style ordinal preserves stable per-file ordering: the
            # first occurrence is :001, the second :002, etc.
            slug_counts[slug] = slug_counts.get(slug, 0) + 1
            sections.append((f"h:{slug}:{slug_counts[slug]:03d}", content))
        current_title = None
        current_lines = []

    def flush_preamble() -> None:
        nonlocal preamble_lines
        content = "\n".join(preamble_lines).strip()
        if content:
            sections.append(("preamble:001", content))
        preamble_lines = []

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current_title is None:
                flush_preamble()
            flush()
            current_title = match.group(2).strip()
            current_lines = [line.strip()]
        elif current_title is not None:
            current_lines.append(line.rstrip())
        else:
            preamble_lines.append(line.rstrip())

    flush()
    return sections


def _target_id(
    *,
    job_id: str,
    source_path: str,
    source_anchor: str,
    target_table: str,
) -> str:
    digest = hashlib.sha256(
        "\n".join((job_id, source_path, source_anchor, target_table)).encode("utf-8")
    ).hexdigest()[:16]
    prefix = {
        TARGET_ENGRAMS: "engram_pai",
        TARGET_BELIEFS: "belief_pai",
        TARGET_HYPOMNEMA: "hypomnema_pai",
    }[target_table]
    return f"{prefix}_{digest}"


def _source_hash(
    *,
    source_kind: str,
    source_anchor: str,
    content: str,
    original_substrate: str,
    original_timestamp: int | None,
) -> str:
    payload = "\n".join(
        (
            source_kind,
            source_anchor,
            content.strip(),
            original_substrate,
            str(original_timestamp or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _classify_row(
    conn,
    row: PaiImportRow,
    *,
    allow_pai_tombstone_reactivation: bool = False,
) -> PaiImportRow:
    target_projection_hash = _target_projection_hash(conn, row)
    existing = conn.execute(
        """
        SELECT target_id, source_hash, content_at_last_import, tombstone_at
        FROM pai_import_row_map
        WHERE job_id = ?
          AND source_path = ?
          AND source_anchor = ?
          AND target_table = ?
        """,
        (row.job_id, row.source_path, row.source_anchor, row.target_table),
    ).fetchone()
    if existing is None:
        if target_projection_hash is not None:
            return replace(
                row,
                action=ACTION_ERROR,
                reason=(
                    "target row exists without a PAI row map; refusing "
                    f"untracked overwrite of {row.target_id!r}"
                ),
                mapped_source_hash=None,
                target_projection_hash=target_projection_hash,
            )
        return replace(
            row,
            action=ACTION_INSERT,
            reason="source key is new",
            mapped_source_hash=None,
            target_projection_hash=None,
        )

    mapped_source_hash = existing["source_hash"]
    mapped_content = existing["content_at_last_import"]
    tombstone_at = existing["tombstone_at"]
    if existing["target_id"] != row.target_id:
        return replace(
            row,
            action=ACTION_ERROR,
            reason=(
                f"source key already maps to {existing['target_id']!r}; refusing remap"
            ),
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=target_projection_hash,
        )

    # U3b hardening B1-state-3: AFTER DELETE triggers populate tombstone_at on
    # the row-map when external code DELETEs an imported target. Without this
    # check, the next preview classifies as REPAIR ("target missing") and
    # silently resurrects the engram on apply.
    is_pai_tombstoned_engram = (
        allow_pai_tombstone_reactivation and _is_pai_tombstoned_engram(conn, row)
    )
    if tombstone_at is not None or is_pai_tombstoned_engram:
        if is_pai_tombstoned_engram:
            current_target_content = _target_content(
                conn, row.target_table, row.target_id
            )
            if (
                mapped_content is not None
                and current_target_content is not None
                and current_target_content != mapped_content
            ):
                return replace(
                    row,
                    action=ACTION_ERROR,
                    reason=(
                        "target content diverged from importer baseline "
                        "(operator hand-edit detected); refusing PAI tombstone "
                        "reactivation. To preserve your edit: edit the source "
                        "file to match it, then re-import (source becomes "
                        "canonical). To overwrite your edit with source content: "
                        "UPDATE pai_import_row_map SET content_at_last_import "
                        "= NULL WHERE target_id = ? — DESTRUCTIVE: next import "
                        "will REPAIR-clobber your edit with the source's content. "
                        "(Setting content_at_last_import to your current target "
                        "content does NOT preserve the edit either — it only "
                        "re-aligns the baseline; the next REPAIR still clobbers "
                        "because source still diverges from target.)"
                    ),
                    mapped_source_hash=mapped_source_hash,
                    target_projection_hash=target_projection_hash,
                )
            action = (
                ACTION_REPAIR
                if mapped_source_hash == row.source_hash
                else ACTION_UPDATE
            )
            reason = (
                "source returned after PAI watcher tombstone; "
                "reactivating mapped engram"
            )
            if action == ACTION_UPDATE:
                reason = (
                    "source returned after PAI watcher tombstone with changed "
                    "content; reactivating mapped engram"
                )
            return replace(
                row,
                action=action,
                reason=reason,
                mapped_source_hash=mapped_source_hash,
                target_projection_hash=target_projection_hash,
            )
        return replace(
            row,
            action=ACTION_ERROR,
            reason=(
                f"mapped target was deleted (tombstone_at={tombstone_at}); "
                "refusing implicit PAI resurrection. Operator must clear the "
                "row-map entry before re-importing."
            ),
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=target_projection_hash,
        )

    if target_projection_hash is None:
        return replace(
            row,
            action=ACTION_REPAIR,
            reason="row map exists but target row is missing",
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=None,
        )

    target_status = _target_lifecycle_status(conn, row)
    # U3b hardening CB8: per-table reactivation policy.
    # - engrams: archived state means decay or operator archival decided this
    #   row should not be active; refuse implicit PAI reactivation.
    # - beliefs: superseded means a newer belief replaced this one; refuse.
    # - hypomnema: inactive (active=False AND superseded_by IS NULL) is a
    #   retire-without-successor signal that PAI re-import IS the canonical
    #   reactivation channel. Superseded hypomnema still refuses.
    if target_status == "superseded":
        return replace(
            row,
            action=ACTION_ERROR,
            reason=(
                "mapped target is superseded; refusing implicit PAI "
                "reactivation (a successor exists)"
            ),
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=target_projection_hash,
        )
    if target_status != "active":
        if row.target_table != TARGET_HYPOMNEMA:
            return replace(
                row,
                action=ACTION_ERROR,
                reason=(
                    "mapped target is "
                    f"{target_status}; refusing implicit PAI reactivation"
                ),
                mapped_source_hash=mapped_source_hash,
                target_projection_hash=target_projection_hash,
            )
        # Hypomnema inactive without successor → flag as REPAIR; write path
        # will reactivate before writing.

    # U3b hardening B1-state-2/4 + B5-audit-13: detect operator hand-edits to
    # imported content. content_at_last_import is what the importer wrote on
    # the previous apply. If the target's current content differs from that
    # baseline, someone other than the importer changed it. REPAIR/UPDATE
    # would silently clobber the change. Pre-v5 rows have content_at_last_import
    # NULL and are not subject to this check until the next upsert populates it.
    if mapped_content is not None and target_status == "active":
        current_target_content = _target_content(conn, row.target_table, row.target_id)
        if (
            current_target_content is not None
            and current_target_content != mapped_content
        ):
            return replace(
                row,
                action=ACTION_ERROR,
                reason=(
                    "target content diverged from importer baseline "
                    "(operator hand-edit detected); refusing silent clobber. "
                    "To preserve your edit: edit the source file to match it, "
                    "then re-import (source becomes canonical). To overwrite "
                    "your edit with source content: UPDATE pai_import_row_map "
                    "SET content_at_last_import = NULL WHERE target_id = ? — "
                    "DESTRUCTIVE: next import will REPAIR-clobber your edit "
                    "with the source's content. (Setting content_at_last_import "
                    "to your current target content does NOT preserve the edit "
                    "either — it only re-aligns the baseline; the next REPAIR "
                    "still clobbers because source still diverges from target.)"
                ),
                mapped_source_hash=mapped_source_hash,
                target_projection_hash=target_projection_hash,
            )

    target_matches = _target_matches_row(conn, row)
    if existing["source_hash"] == row.source_hash and target_matches:
        return replace(
            row,
            action=ACTION_NOOP,
            reason="source hash unchanged",
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=target_projection_hash,
        )
    if existing["source_hash"] == row.source_hash:
        return replace(
            row,
            action=ACTION_REPAIR,
            reason="row map hash is unchanged but target projection drifted",
            mapped_source_hash=mapped_source_hash,
            target_projection_hash=target_projection_hash,
        )
    return replace(
        row,
        action=ACTION_UPDATE,
        reason="source hash changed; preserving target_id",
        mapped_source_hash=mapped_source_hash,
        target_projection_hash=target_projection_hash,
    )


def _assert_preview_current(conn, row: PaiImportRow) -> None:
    current = _classify_row(conn, row)
    if current.action == ACTION_ERROR:
        raise ValueError(current.reason)
    # U3b hardening B1-state-5: compare reason field too. The prior check
    # caught action / hash drift but not reason drift, leaving a latent path
    # where a re-classified row could carry different diagnostic narrative
    # while passing the staleness gate. Belt-and-suspenders.
    if (
        current.action != row.action
        or current.mapped_source_hash != row.mapped_source_hash
        or current.target_projection_hash != row.target_projection_hash
        or current.reason != row.reason
    ):
        raise ValueError(
            "PAI import preview is stale for "
            f"{row.source_path}#{row.source_anchor}; re-preview before apply"
        )


def _assert_preview_current_for_watch(conn, row: PaiImportRow) -> None:
    if row.action not in _U3C_RESERVED_ACTIONS:
        current = _classify_row(
            conn,
            row,
            allow_pai_tombstone_reactivation=True,
        )
        if current.action == ACTION_ERROR:
            raise ValueError(current.reason)
        if (
            current.action != row.action
            or current.mapped_source_hash != row.mapped_source_hash
            or current.target_projection_hash != row.target_projection_hash
            or current.reason != row.reason
        ):
            raise ValueError(
                "PAI watch preview is stale for "
                f"{row.source_path}#{row.source_anchor}; re-preview before apply"
            )
        return

    record = _pai_row_map_record(conn, row)
    if record is None:
        raise ValueError(
            "PAI watch preview is stale for "
            f"{row.source_path}#{row.source_anchor}; row map disappeared"
        )
    try:
        source_kind = _infer_source_kind_for_target(conn, record)
    except ValueError as exc:
        raise ValueError(
            "ambiguous stale row: cannot infer source_kind "
            f"({exc}). Operator must reconcile pai_import_row_map before apply."
        ) from exc

    current = _stale_mapped_row_from_record(
        conn,
        record,
        source_kind,
        missing_source_policy="u3c",
    )
    if current.action == ACTION_ERROR:
        raise ValueError(current.reason)
    if (
        current.action != row.action
        or current.source_hash != row.source_hash
        or current.mapped_source_hash != row.mapped_source_hash
        or current.target_projection_hash != row.target_projection_hash
        or current.reason != row.reason
    ):
        raise ValueError(
            "PAI watch preview is stale for "
            f"{row.source_path}#{row.source_anchor}; re-preview before apply"
        )


def _target_matches_row(conn, row: PaiImportRow) -> bool | None:
    if row.target_table == TARGET_ENGRAMS:
        return _engram_matches_row(conn, row)
    if row.target_table == TARGET_BELIEFS:
        return _belief_matches_row(conn, row)
    if row.target_table == TARGET_HYPOMNEMA:
        return _hypomnema_matches_row(conn, row)
    raise ValueError(f"Unsupported target_table: {row.target_table}")


def _engram_matches_row(conn, row: PaiImportRow) -> bool | None:
    record = conn.execute(
        """
        SELECT content, kind, tags, owner_agent_id, voice_exemplar_eligible,
               softening_protected, original_substrate, original_timestamp,
               consolidation_authorized, decay_protected, source, state
        FROM engrams
        WHERE id = ?
        """,
        (row.target_id,),
    ).fetchone()
    if record is None:
        return None
    source = _safe_json_loads(record["source"], {})
    tags = _safe_json_loads(record["tags"], [])
    if not isinstance(source, dict) or not isinstance(tags, list):
        return False
    return (
        record["content"] == row.content
        and record["kind"] == EngramKind.SEMANTIC.value
        and tags == list(row.tags)
        and record["owner_agent_id"] == row.agent_id
        and bool(record["voice_exemplar_eligible"]) == row.voice_exemplar_eligible
        and bool(record["softening_protected"]) == row.softening_protected
        and record["original_substrate"] == row.original_substrate
        and record["original_timestamp"] == row.original_timestamp
        and bool(record["consolidation_authorized"]) == row.consolidation_authorized
        and bool(record["decay_protected"]) == row.decay_protected
        and record["state"] == "active"
        and source.get("type") == SourceType.EXTERNAL.value
        and _float_equal(source.get("confidence"), row.confidence)
        and source.get("confidence_source") == ConfidenceSource.USER_EXPLICIT.value
        # Include authority in the identity check (T3 review pai-import-authority-noop):
        # a pre-existing row wrongly stamped observed (e.g. a legacy import or a
        # from_dict fallback) must NOT be treated as a no-op — re-import repairs
        # the stale stamp to imported instead of leaving it reading back observed.
        and source.get("authority") == SourceAuthority.IMPORTED.value
    )


def _belief_matches_row(conn, row: PaiImportRow) -> bool | None:
    record = conn.execute(
        """
        SELECT agent_id, content, domain, tier,
               original_substrate, original_timestamp, superseded_by
        FROM beliefs
        WHERE id = ?
        """,
        (row.target_id,),
    ).fetchone()
    if record is None:
        return None
    return (
        record["agent_id"] == row.agent_id
        and record["content"] == row.content
        and record["domain"] == row.domain
        and record["tier"] == row.tier
        and record["original_substrate"] == row.original_substrate
        and record["original_timestamp"] == row.original_timestamp
        and record["superseded_by"] is None
    )


def _hypomnema_matches_row(conn, row: PaiImportRow) -> bool | None:
    record = conn.execute(
        """
        SELECT agent_id, person_id, project_scope, content, source, domain,
               tags_json, confidence, salience, foundational,
               original_timestamp, active, superseded_by
        FROM hypomnema_entries
        WHERE id = ?
        """,
        (row.target_id,),
    ).fetchone()
    if record is None:
        return None
    tags = _safe_json_loads(record["tags_json"], [])
    if not isinstance(tags, list):
        return False
    return (
        record["agent_id"] == row.agent_id
        and record["person_id"] == row.person_id
        and record["project_scope"] == row.project_scope
        and record["content"] == row.content
        and record["source"] == "observed"
        and record["domain"] == row.domain
        and tags == list(row.tags)
        and _float_equal(record["confidence"], row.confidence)
        and _float_equal(record["salience"], 0.7)
        and bool(record["foundational"]) == row.foundational
        and record["original_timestamp"] == row.original_timestamp
        and bool(record["active"]) is True
        and record["superseded_by"] is None
    )


def _pai_row_map_record(conn, row: PaiImportRow):
    return conn.execute(
        """
        SELECT job_id, source_path, source_anchor, target_table, target_id,
               source_hash, source_kind, content_at_last_import, tombstone_at,
               agent_id, project_scope, original_timestamp
        FROM pai_import_row_map
        WHERE job_id = ?
          AND source_path = ?
          AND source_anchor = ?
          AND target_table = ?
        """,
        (row.job_id, row.source_path, row.source_anchor, row.target_table),
    ).fetchone()


def _stale_mapped_rows(
    conn,
    job_id: str,
    current_rows: list[PaiImportRow],
    *,
    missing_source_policy: str = "error",
) -> list[PaiImportRow]:
    if missing_source_policy not in {"error", "u3c"}:
        raise ValueError(f"Unsupported missing_source_policy: {missing_source_policy}")
    current_keys = {
        (row.source_path, row.source_anchor, row.target_table) for row in current_rows
    }
    stale_rows: list[PaiImportRow] = []
    records = conn.execute(
        """
        SELECT job_id, source_path, source_anchor, target_table, target_id,
               source_hash, source_kind, content_at_last_import, tombstone_at,
               agent_id, project_scope, original_timestamp
        FROM pai_import_row_map
        WHERE job_id = ?
        ORDER BY source_path, source_anchor, target_table
        """,
        (job_id,),
    ).fetchall()
    for record in records:
        key = (record["source_path"], record["source_anchor"], record["target_table"])
        if key in current_keys:
            continue
        try:
            source_kind = _infer_source_kind_for_target(conn, record)
        except ValueError as exc:
            stale_rows.append(_ambiguous_stale_row(conn, record, exc))
            continue
        stale_rows.append(
            _stale_mapped_row_from_record(
                conn,
                record,
                source_kind,
                missing_source_policy=missing_source_policy,
            )
        )
    return stale_rows


def _ambiguous_stale_row(conn, record, exc: ValueError) -> PaiImportRow:
    # R-D6: one ambiguous stale row must not crash the whole preview. This row
    # is ACTION_ERROR and cannot apply, so the fallback profile only supplies
    # dataclass fields needed to surface the diagnostic.
    profile = _PROFILES["identity_kernel"]
    target_table = record["target_table"]
    target_id = record["target_id"]
    return PaiImportRow(
        job_id=record["job_id"],
        source_path=record["source_path"],
        source_anchor=record["source_anchor"],
        source_kind="identity_kernel",
        target_table=target_table,
        target_id=target_id,
        source_hash=record["source_hash"] or "",
        content=_target_content(conn, target_table, target_id) or "",
        action=ACTION_ERROR,
        reason=(
            "ambiguous stale row: cannot infer source_kind "
            f"({exc}). Operator must reconcile pai_import_row_map before re-import."
        ),
        mapped_source_hash=record["source_hash"],
        target_projection_hash=_target_projection_hash_for_record(
            conn, target_table, target_id
        ),
        original_substrate=_target_original_substrate(conn, target_table, target_id),
        original_timestamp=_row_value(record, "original_timestamp"),
        tags=profile.tags,
        domain=profile.domain,
        tier=profile.tier,
        confidence=profile.confidence,
        voice_exemplar_eligible=profile.voice_exemplar_eligible,
        softening_protected=profile.softening_protected,
        decay_protected=profile.decay_protected,
        consolidation_authorized=profile.consolidation_authorized,
        foundational=profile.foundational,
    )


def _stale_mapped_row_from_record(
    conn,
    record,
    source_kind: str,
    *,
    missing_source_policy: str,
) -> PaiImportRow:
    profile = _PROFILES[source_kind]
    target_table = record["target_table"]
    target_id = record["target_id"]
    target_projection_hash = _target_projection_hash_for_record(
        conn, target_table, target_id
    )
    current_content = _target_content(conn, target_table, target_id)
    target_lifecycle_status = _target_lifecycle_status_for_record(
        conn, target_table, target_id
    )
    mapped_content = _row_value(record, "content_at_last_import")
    action, reason = _stale_missing_source_action(
        record,
        target_projection_hash=target_projection_hash,
        target_lifecycle_status=target_lifecycle_status,
        current_content=current_content,
        mapped_content=mapped_content,
        missing_source_policy=missing_source_policy,
    )
    return PaiImportRow(
        job_id=record["job_id"],
        source_path=record["source_path"],
        source_anchor=record["source_anchor"],
        source_kind=source_kind,
        target_table=target_table,
        target_id=target_id,
        source_hash=record["source_hash"] or "",
        content=current_content or mapped_content or "",
        action=action,
        reason=reason,
        mapped_source_hash=record["source_hash"],
        target_projection_hash=target_projection_hash,
        original_substrate=_target_original_substrate(conn, target_table, target_id),
        original_timestamp=_row_value(record, "original_timestamp"),
        tags=profile.tags,
        domain=profile.domain,
        tier=profile.tier,
        confidence=profile.confidence,
        voice_exemplar_eligible=profile.voice_exemplar_eligible,
        softening_protected=profile.softening_protected,
        decay_protected=profile.decay_protected,
        consolidation_authorized=profile.consolidation_authorized,
        foundational=profile.foundational,
    )


def _stale_missing_source_action(
    record,
    *,
    target_projection_hash: str | None,
    target_lifecycle_status: str,
    current_content: str | None,
    mapped_content: str | None,
    missing_source_policy: str,
) -> tuple[str, str]:
    if missing_source_policy == "error":
        return (
            ACTION_ERROR,
            "source key is absent from current PAI import batch; "
            "refusing to leave a stale mapped target implicit",
        )

    tombstone_at = _row_value(record, "tombstone_at")
    if tombstone_at is not None:
        if (
            missing_source_policy == "u3c"
            and record["target_table"] == TARGET_ENGRAMS
            and target_lifecycle_status == "archived"
            and target_projection_hash is not None
        ):
            return (
                ACTION_TOMBSTONE,
                "source key remains absent from current PAI watch batch; "
                "mapped engram is already archived",
            )
        return (
            ACTION_ERROR,
            f"mapped target was deleted (tombstone_at={tombstone_at}); "
            "refusing implicit PAI watcher lifecycle action. Operator must clear "
            "the row-map entry before re-importing.",
        )
    if target_projection_hash is None:
        return (
            ACTION_ERROR,
            "source key is absent from current PAI watch batch, but mapped target "
            "row is missing; refusing lifecycle action without a target",
        )
    if (
        mapped_content is not None
        and current_content is not None
        and current_content != mapped_content
    ):
        return (
            ACTION_ERROR,
            "target content diverged from importer baseline (operator hand-edit "
            "detected); refusing PAI watcher lifecycle action. To preserve your "
            "edit: edit the source file to match it, then re-import (source "
            "becomes canonical). To overwrite your edit with source content: "
            "UPDATE pai_import_row_map SET content_at_last_import = NULL WHERE "
            "target_id = ? — DESTRUCTIVE: next import will clobber your edit "
            "with the source's content. (Setting content_at_last_import to your "
            "current target content does NOT preserve the edit either — it only "
            "re-aligns the baseline; the next watcher cycle still clobbers "
            "because source still diverges from target.)",
        )

    target_table = record["target_table"]
    if target_table == TARGET_ENGRAMS:
        return (
            ACTION_TOMBSTONE,
            "source key is absent from current PAI watch batch; "
            "archiving mapped engram",
        )
    if target_table == TARGET_HYPOMNEMA:
        return (
            ACTION_DEACTIVATE,
            "source key is absent from current PAI watch batch; "
            "deactivating mapped hypomnema",
        )
    if target_table == TARGET_BELIEFS:
        return (
            ACTION_REVIEW,
            "source key is absent from current PAI watch batch; "
            "flagging mapped belief for review",
        )
    raise ValueError(f"Unsupported target_table: {target_table}")


def _validate_preview(preview: PaiImportPreview, *, allow_empty: bool = False) -> None:
    _clean_required(preview.job_id, "job_id")
    if not isinstance(preview.rows, tuple) or (not allow_empty and not preview.rows):
        raise ValueError("apply_pai_import requires a non-empty preview")
    for row in preview.rows:
        if row.action == ACTION_ERROR:
            continue
        _validate_preview_row(preview.job_id, row)


def _validate_preview_row(preview_job_id: str, row: PaiImportRow) -> None:
    if not isinstance(row, PaiImportRow):
        raise TypeError("apply_pai_import requires PaiImportRow preview rows")
    if row.job_id != preview_job_id:
        raise ValueError("PAI import preview row job_id does not match preview")
    _clean_required(row.job_id, "job_id")
    _clean_required(row.source_path, "source_path")
    _clean_required(row.source_anchor, "source_anchor")
    source_kind = _clean_required(row.source_kind, "source_kind")
    _clean_required(row.content, "content")
    _clean_required(row.original_substrate, "original_substrate")
    if row.content != row.content.strip():
        raise ValueError("PAI import row content must be canonicalized")
    if (
        row.agent_id != _PAI_AGENT_ID
        or row.person_id != _PAI_PERSON_ID
        or row.project_scope != _PAI_PROJECT_SCOPE
    ):
        raise ValueError("PAI imports are restricted to oliver/david/pai scope")
    if source_kind not in _PROFILES:
        raise ValueError(f"Unsupported source_kind: {source_kind}")
    profile = _PROFILES[source_kind]
    if row.target_table != profile.target_table:
        raise ValueError("PAI import row target_table does not match source_kind")
    expected_target_id = _target_id(
        job_id=row.job_id,
        source_path=row.source_path,
        source_anchor=row.source_anchor,
        target_table=row.target_table,
    )
    if row.target_id != expected_target_id:
        raise ValueError("PAI import row target_id is not deterministic")
    if row.action in _U3C_RESERVED_ACTIONS:
        _clean_required(row.source_hash, "source_hash")
        if row.mapped_source_hash is None:
            raise ValueError("PAI watcher lifecycle row requires mapped_source_hash")
        if row.target_projection_hash is None:
            raise ValueError(
                "PAI watcher lifecycle row requires target_projection_hash"
            )
    else:
        expected_source_hash = _source_hash(
            source_kind=row.source_kind,
            source_anchor=row.source_anchor,
            content=row.content,
            original_substrate=row.original_substrate,
            original_timestamp=row.original_timestamp,
        )
        if row.source_hash != expected_source_hash:
            raise ValueError("PAI import row source_hash is not deterministic")
    if tuple(row.tags) != profile.tags:
        raise ValueError("PAI import row tags do not match source profile")
    if row.domain != profile.domain:
        raise ValueError("PAI import row domain does not match source profile")
    if row.tier != profile.tier:
        raise ValueError("PAI import row tier does not match source profile")
    if not _float_equal(row.confidence, profile.confidence):
        raise ValueError("PAI import row confidence does not match source profile")
    if row.voice_exemplar_eligible != profile.voice_exemplar_eligible:
        raise ValueError(
            "PAI import row voice_exemplar_eligible does not match profile"
        )
    if row.softening_protected != profile.softening_protected:
        raise ValueError("PAI import row softening_protected does not match profile")
    if row.decay_protected != profile.decay_protected:
        raise ValueError("PAI import row decay_protected does not match profile")
    if row.consolidation_authorized != profile.consolidation_authorized:
        raise ValueError(
            "PAI import row consolidation_authorized does not match profile"
        )
    if row.foundational != profile.foundational:
        raise ValueError("PAI import row foundational flag does not match profile")


def _target_record(conn, row: PaiImportRow):
    return _target_record_by_id(conn, row.target_table, row.target_id)


def _target_record_by_id(conn, target_table: str, target_id: str):
    if target_table not in {TARGET_ENGRAMS, TARGET_BELIEFS, TARGET_HYPOMNEMA}:
        raise ValueError(f"Unsupported target_table: {target_table}")
    return conn.execute(
        f"SELECT * FROM {target_table} WHERE id = ?",
        (target_id,),
    ).fetchone()


def _target_original_substrate(conn, target_table: str, target_id: str) -> str:
    record = _target_record_by_id(conn, target_table, target_id)
    if record is None:
        return "unknown-pre-import"
    if target_table not in {TARGET_ENGRAMS, TARGET_BELIEFS}:
        return "unknown-pre-import"
    return _row_value(record, "original_substrate") or "unknown-pre-import"


def _row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _target_projection_hash(conn, row: PaiImportRow) -> str | None:
    return _target_projection_hash_for_record(conn, row.target_table, row.target_id)


def _target_projection_hash_for_record(
    conn,
    target_table: str,
    target_id: str,
) -> str | None:
    record = _target_record_by_id(conn, target_table, target_id)
    if record is None:
        return None
    payload = {key: record[key] for key in record.keys()}
    return _hash_payload(payload)


def _target_lifecycle_status(conn, row: PaiImportRow) -> str:
    return _target_lifecycle_status_for_record(conn, row.target_table, row.target_id)


def _target_lifecycle_status_for_record(
    conn,
    target_table: str,
    target_id: str,
) -> str:
    record = _target_record_by_id(conn, target_table, target_id)
    if record is None:
        return "missing"
    if target_table == TARGET_ENGRAMS:
        return "active" if record["state"] == "active" else record["state"]
    if target_table == TARGET_HYPOMNEMA:
        # U3b hardening CB8: check superseded BEFORE active flag. The prior
        # ordering returned "inactive" for rows that were both deactivated AND
        # superseded, hiding the supersede signal from per-table reactivation
        # policy. Superseded must outrank inactive — reactivating a superseded
        # row would create two active hypomnema in the supersede chain.
        if record["superseded_by"] is not None:
            return "superseded"
        if not bool(record["active"]):
            return "inactive"
        return "active"
    if target_table == TARGET_BELIEFS:
        return "superseded" if record["superseded_by"] is not None else "active"
    raise ValueError(f"Unsupported target_table: {target_table}")


def _infer_source_kind_for_target(conn, record) -> str:
    """Recover source_kind for a stale-mapped row.

    U3b hardening CB6: prefer the row-map's explicit `source_kind` column
    (written by upsert_pai_import_row) over tag-based inference. The tag-based
    fallback was unsafe — engrams whose `tags` column got hand-edited would
    silently default to `identity_kernel`, causing stale-row PaiImportRow
    construction with the wrong profile values. That cascaded to bogus
    target_projection_hash comparisons and misleading ACTION_ERROR reasons.
    """
    target_table = record["target_table"]
    # Trust the row-map's source_kind column if populated (post-v5 imports).
    row_map_kind = None
    try:
        row_map_kind = record["source_kind"]
    except (IndexError, KeyError):
        row_map_kind = None
    if row_map_kind and row_map_kind in _PROFILES:
        return row_map_kind
    if target_table == TARGET_BELIEFS:
        return "beliefs"
    if target_table == TARGET_HYPOMNEMA:
        return "hypomnema"
    if target_table == TARGET_ENGRAMS:
        target = _target_record_by_id(conn, target_table, record["target_id"])
        if target is not None:
            tags = _safe_json_loads(target["tags"], [])
            if isinstance(tags, list):
                if "david-context" in tags:
                    return "david_context"
                if "growth-substrate" in tags:
                    return "growth_substrate"
                if "identity-kernel" in tags:
                    return "identity_kernel"
        # No marker tag found and no row-map source_kind. This is ambiguous —
        # the stale row's profile cannot be reconstructed safely. Raise rather
        # than silently default to identity_kernel (the prior behavior, which
        # caused profile-incoherent stale-row construction).
        raise ValueError(
            "Cannot infer source_kind for stale-mapped engram "
            f"target_id={record['target_id']!r}: row-map source_kind is empty "
            "and target tags contain no PAI marker. Hand-edited tags or "
            "pre-v5 row-map entry — operator must reconcile."
        )
    raise ValueError(f"Unsupported target_table: {target_table}")


def _target_content(conn, target_table: str, target_id: str) -> str | None:
    record = _target_record_by_id(conn, target_table, target_id)
    if record is None:
        return None
    return record["content"]


def _validate_source(source: PaiImportSource) -> None:
    _clean_required(source.job_id, "job_id")
    _clean_required(source.source_path, "source_path")
    source_kind = _clean_required(source.source_kind, "source_kind")
    if source_kind not in _SUPPORTED_SOURCE_KINDS:
        raise ValueError(f"Unsupported source_kind: {source_kind}")


def _canonical_source(source: PaiImportSource) -> PaiImportSource:
    canonical = replace(
        source,
        job_id=_clean_required(source.job_id, "job_id"),
        source_path=_clean_required(source.source_path, "source_path"),
        source_kind=_clean_required(source.source_kind, "source_kind"),
        agent_id=_clean_required(source.agent_id, "agent_id"),
        person_id=_clean_required(source.person_id, "person_id"),
        project_scope=_clean_required(source.project_scope, "project_scope"),
        original_substrate=_clean_required(
            source.original_substrate, "original_substrate"
        ),
    )
    if (
        canonical.agent_id != _PAI_AGENT_ID
        or canonical.person_id != _PAI_PERSON_ID
        or canonical.project_scope != _PAI_PROJECT_SCOPE
    ):
        raise ValueError("PAI imports are restricted to oliver/david/pai scope")
    return canonical


def _clean_required(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is required")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} is required")
    return cleaned


def _float_equal(left, right: float) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False


def _safe_json_loads(value, default):
    try:
        return json.loads(value or json.dumps(default))
    except (TypeError, json.JSONDecodeError):
        return default


def _hash_payload(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "section"
