#!/usr/bin/env python3
"""restamp_david10.py — DAVID-10 hypomnema restamp, PREPARED FOR DAVID'S HAND.

Implements the disposition in reports/011-oliver-decides-david10.md (the
"decidi tu" ruling) with the 2026-07-05 refinement recorded in the executor
task: the REMOVE buckets are NOT deleted — they restamp to
``domain=long-arc, foundational=0, read_visibility=audit_only`` (folded out of
operational recall, kept for audit/history). The KEEP buckets restamp per 011.

The mapping is keyed deterministically on ``pai_import_row_map.source_path`` —
the ground-truth import provenance, not on row content or a heuristic. Every
imported hypomnema row belongs to exactly one bucket; the script aborts if any
mapped row falls outside the known buckets or if any per-bucket count does not
match the expected disposition.

SAFETY (this is a live-DB write when run with --execute, so it is built to be
run by David, never by an agent against live ~/.mnemos):
  * The DB path is a REQUIRED positional argument. There is no default; it never
    silently targets ~/.mnemos.
  * The canonical live paths are hard-refused even if passed explicitly, unless
    --i-am-david-restamping-live is ALSO given (the deliberate ceremony flag).
  * Dry-run is the DEFAULT. It prints per-bucket expected-vs-actual counts and
    ABORTS on any mismatch, writing nothing.
  * --execute requires --snapshot <path> pointing at a fresh backup that already
    exists on disk (proving David took a snapshot first). The snapshot must be a
    distinct SQLite backup whose full user-table content digest matches the
    target's current pre-write state; the target side of that parity check is
    recomputed under BEGIN IMMEDIATE before any UPDATE.
  * The full dry-run validation re-runs inside the same transaction before any
    UPDATE.
  * Every UPDATE is idempotent: re-running against an already-restamped DB is a
    no-op (0 rows changed) and still validates clean.

Scope: hypomnema_entries only (the restamp is hypomnema-scoped per report 009).
Beliefs and engrams in pai_import_row_map are governed by their own flows and
are NOT touched here.

Stdlib only: argparse, os, pathlib, sqlite3, sys.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sqlite3
import sys

# ---------------------------------------------------------------------------
# The live paths this script must never write unless David explicitly opts in.
# Mirrors tests/conftest.py::_LIVE_MNEMOS_PATHS (the 008m incident guard).
# ---------------------------------------------------------------------------
_LIVE_MNEMOS_PATHS = {
    str(pathlib.Path(os.path.expanduser("~/.mnemos/memory.db")).resolve()),
    "/Users/davidef/.mnemos/memory.db",
}


# ---------------------------------------------------------------------------
# Bucket definitions. Each bucket is (name, source_path SQL predicate,
# expected_rows, domain, foundational, read_visibility).
#
# source_path predicates are keyed on the path segment under
# .../pai-import-stage/. They are mutually exclusive and exhaustive over the
# imported hypomnema corpus (verified against the live snapshot 2026-07-05).
#
# Expected counts are the ground truth from the snapshot ec0cdb4f… (report 009
# arithmetic, corrected: CURIOUS/BUILT/NOTICED is 38, not the "35" approximation
# in 011 — 009 row 7 "…/ALIVE = 39" is the exact figure).
# ---------------------------------------------------------------------------
_LIKE = "m.source_path LIKE ?"


class Bucket:
    def __init__(
        self,
        name: str,
        like_patterns: tuple[str, ...],
        expected: int,
        domain: str,
        foundational: int,
        read_visibility: str,
        disposition: str,
    ) -> None:
        self.name = name
        self.like_patterns = like_patterns
        self.expected = expected
        self.domain = domain
        self.foundational = foundational
        self.read_visibility = read_visibility
        self.disposition = disposition

    def where(self) -> tuple[str, list[str]]:
        """The join predicate matching this bucket's rows (OR over patterns)."""
        clause = " OR ".join([_LIKE] * len(self.like_patterns))
        return "(" + clause + ")", list(self.like_patterns)


# Ordered so the printout reads REMOVE → KEEP-witnessed → KEEP-operational.
BUCKETS: tuple[Bucket, ...] = (
    # --- REMOVE buckets: folded to audit_only (present for history, out of
    #     operational recall). 2026-07-05 refinement: NOT deleted. ---
    Bucket(
        "continuity_archive", ("%/continuity_archive/%",), 697,
        "long-arc", 0, "audit_only", "REMOVE→fold",
    ),
    Bucket(
        "state (active-context/CONTINUITY)",
        ("%/active-context.md", "%/CONTINUITY.md"), 17,
        "long-arc", 0, "audit_only", "REMOVE→fold",
    ),
    Bucket(
        "ALIVE snapshot", ("%/ALIVE.md",), 1,
        "long-arc", 0, "audit_only", "REMOVE→fold",
    ),
    # --- KEEP witnessed, foundational=1 (the deliberate self-core) ---
    Bucket(
        "curated (hypomnema/)", ("%/hypomnema/%",), 325,
        "identity", 1, "review_only", "KEEP witnessed f=1",
    ),
    Bucket(
        "hypomnema_from_growth", ("%/hypomnema_from_growth.md",), 18,
        "identity", 1, "review_only", "KEEP witnessed f=1",
    ),
    # --- KEEP witnessed, foundational=0 (identity-context + living edge) ---
    Bucket(
        "polyphonic", ("%/polyphonic/%",), 79,
        "identity", 0, "review_only", "KEEP witnessed f=0",
    ),
    Bucket(
        "CURIOUS/BUILT/NOTICED",
        ("%/CURIOUS.md", "%/BUILT.md", "%/NOTICED.md"), 38,
        "identity", 0, "review_only", "KEEP witnessed f=0",
    ),
    # --- KEEP operational, not witnessed (expression/context, mine, unlocked) ---
    Bucket(
        "artifacts", ("%/artifacts/%",), 64,
        "long-arc", 0, "operational_context", "KEEP operational",
    ),
    Bucket(
        "studio/README", ("%/studio/README.md",), 6,
        "topical", 0, "operational_context", "KEEP operational",
    ),
)

_TOTAL_EXPECTED = sum(b.expected for b in BUCKETS)  # 1245


def _resolve(db_path: str) -> str:
    try:
        return str(pathlib.Path(db_path).expanduser().resolve())
    except (OSError, ValueError, TypeError):
        return str(db_path)


def _refuse_live(db_path: str, allow_live: bool) -> None:
    if allow_live:
        return
    resolved = _resolve(db_path)
    target_identity = None
    try:
        target_stat = os.stat(db_path)
        target_identity = (target_stat.st_dev, target_stat.st_ino)
    except FileNotFoundError:
        pass
    live_inode_match = False
    if target_identity is not None:
        for live_path in _LIVE_MNEMOS_PATHS:
            try:
                live_stat = os.stat(live_path)
            except FileNotFoundError:
                continue
            if target_identity == (live_stat.st_dev, live_stat.st_ino):
                live_inode_match = True
                break
    if (
        resolved in _LIVE_MNEMOS_PATHS
        or str(db_path) in _LIVE_MNEMOS_PATHS
        or live_inode_match
    ):
        sys.stderr.write(
            "REFUSING: %s is the live Mnemos DB. This script is prepared for "
            "David's hand — pass --i-am-david-restamping-live ONLY as part of "
            "the deliberate ceremony sequence, after a snapshot.\n" % db_path
        )
        sys.exit(2)


def _bucket_actual(conn: sqlite3.Connection, bucket: Bucket) -> int:
    where, params = bucket.where()
    sql = (
        "SELECT COUNT(DISTINCT h.id) FROM hypomnema_entries h "
        "JOIN pai_import_row_map m "
        "  ON m.target_id = h.id AND m.target_table = 'hypomnema_entries' "
        "WHERE " + where
    )
    return int(conn.execute(sql, params).fetchone()[0])


def _total_mapped(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT h.id) FROM hypomnema_entries h "
            "JOIN pai_import_row_map m "
            "  ON m.target_id = h.id AND m.target_table = 'hypomnema_entries'"
        ).fetchone()[0]
    )


def _snapshot_refusal(snapshot: str) -> str | None:
    uri = pathlib.Path(snapshot).resolve().as_uri() + "?mode=ro"
    try:
        snap = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError as exc:
        return (
            "REFUSING: snapshot %s cannot be opened read-only as SQLite: %s.\n"
            % (snapshot, exc)
        )
    try:
        integrity = snap.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            detail = integrity[0] if integrity is not None else "no result"
            return (
                "REFUSING: snapshot %s failed SQLite integrity_check: %s.\n"
                % (snapshot, detail)
            )
        # 013t F2 (missing-meta-shape-guard): `meta` is REQUIRED here even
        # though the restamp writes only hypomnema_entries — `_parity_signals`
        # reads `meta` (schema_version) on every snapshot, so a file with the
        # two data tables but no `meta` would raise an uncaught OperationalError
        # downstream instead of a controlled refusal. Requiring it here turns
        # that into a clean "missing required Mnemos tables: meta" refusal.
        required = {"hypomnema_entries", "pai_import_row_map", "meta"}
        present = {
            row[0]
            for row in snap.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN "
                "('hypomnema_entries', 'pai_import_row_map', 'meta')"
            )
        }
        missing = sorted(required - present)
        if missing:
            return (
                "REFUSING: snapshot %s is missing required Mnemos tables: %s.\n"
                % (snapshot, ", ".join(missing))
            )
        return None
    except sqlite3.DatabaseError as exc:
        return (
            "REFUSING: snapshot %s failed SQLite integrity_check: %s.\n"
            % (snapshot, exc)
        )
    finally:
        snap.close()


def _parity_signals(conn: sqlite3.Connection) -> dict[str, int]:
    """The identity fingerprint of a DB's CURRENT state, used to prove a
    snapshot backs THE TARGET (013r F2). Three signals, each read-only:

      - ``schema_version`` (``meta`` k/v): a snapshot at a different schema
        version cannot be a fresh backup of this target.
      - ``pai_import_row_map`` count: the mapping table the restamp keys on;
        an unrelated DB with a different import history diverges here.
      - ``hypomnema_entries`` count: the table the restamp WRITES; a stale
        snapshot from an earlier/later DB state diverges here.

    A ``sqlite3.backup``/`.backup` copy taken immediately before ``--execute``
    matches on all three; a stale-or-unrelated Mnemos-shaped DB (which passes
    the shape/integrity guards) fails at least one. Missing ``meta`` row →
    schema_version 0, matching ``migrations.get_schema_version``.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    try:
        schema_version = int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        schema_version = -1  # malformed → cannot match a valid target
    row_map = conn.execute(
        "SELECT COUNT(*) FROM pai_import_row_map"
    ).fetchone()[0]
    hypomnema = conn.execute(
        "SELECT COUNT(*) FROM hypomnema_entries"
    ).fetchone()[0]
    return {
        "schema_version": schema_version,
        "pai_import_row_map": int(row_map),
        "hypomnema_entries": int(hypomnema),
    }


def _field_token(value: object) -> bytes:
    """Type-tagged, length-prefixed encoding of one column value so that NULL,
    the empty string, and the string "0" (etc.) can never collide in the
    digest. NULL → b"N"; everything else → b"S" + len + ":" + utf-8 bytes of
    str(value). SQLite returns int/float/str/None for these columns; str() is
    stable for each, and the length prefix makes the concatenation injective."""
    if value is None:
        return b"N|"
    raw = str(value).encode("utf-8")
    return b"S" + str(len(raw)).encode("ascii") + b":" + raw + b"|"


def _user_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every user table as ``(name, schema_sql)``, ordered by name.

    Excludes SQLite's internal tables (``sqlite_%``). ``schema_sql`` is the
    ``sql`` from ``sqlite_master`` so a schema change (renamed/dropped/added
    column) diverges the digest even before any row differs.
    """
    return [
        (row[0], row[1] if row[1] is not None else "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of ``table`` in schema order (``PRAGMA table_info``)."""
    return [
        row[1]
        for row in conn.execute(
            'PRAGMA table_info("%s")' % table.replace('"', '""')
        )
    ]


def _content_digest(conn: sqlite3.Connection) -> str:
    """Deterministic sha256 over the FULL contents of every user table (013v F1).

    The snapshot is a FULL-DB rollback — ``sqlite3 .backup`` restores the entire
    file, not just the restamp-touched columns — so a valid rollback backup must
    match the target on ALL user data, not merely the two tables the restamp
    reads/writes. A digest over only the restamp columns (013t) accepted a
    snapshot that diverged in a non-restamp table (a beliefs row, an engrams
    row, a non-restamp hypomnema column), leaving David's rollback illusory for
    everything outside the restamp's own footprint.

    This hashes, for EVERY user table (``sqlite_master`` ORDER BY name):
      - the table's schema (``sql``), so schema drift diverges the digest;
      - every row, every column (``PRAGMA table_info`` order), ordered by
        ``rowid`` — a stable per-row key on every rowid table — with each value
        encoded via `_field_token` so NULL / empty / ``"0"`` never collide.

    Complete by construction: every user table, every column, every row is in
    the hash, so no "missed table/column" gap remains. Same full DB → same
    digest on snapshot and target; ANY difference in ANY user table → different
    digest → refusal.
    """
    hasher = hashlib.sha256()

    for table, schema_sql in _user_tables(conn):
        hasher.update(b"T|")
        hasher.update(_field_token(table))
        hasher.update(_field_token(schema_sql))
        hasher.update(b"\n")

        columns = _table_columns(conn, table)
        col_list = ", ".join('"%s"' % c.replace('"', '""') for c in columns)
        quoted = '"%s"' % table.replace('"', '""')
        try:
            cursor = conn.execute(
                "SELECT %s FROM %s ORDER BY rowid" % (col_list, quoted)
            )
        except sqlite3.OperationalError:
            # WITHOUT ROWID tables have no rowid; fall back to ordering by the
            # full column tuple, which is an equally stable deterministic key.
            order_list = col_list if col_list else "1"
            cursor = conn.execute(
                "SELECT %s FROM %s ORDER BY %s" % (col_list, quoted, order_list)
            )
        for row in cursor:
            for value in row:
                hasher.update(_field_token(value))
            hasher.update(b"\n")

    return hasher.hexdigest()


class SnapshotParityError(Exception):
    """Raised by execute() when the under-lock parity check refuses the snapshot
    (013v F2). Carries the refusal message so main() can surface it on stderr and
    exit 2 — the same hard-refusal contract as the pre-transaction A-5 guards,
    but computed under the target's write lock to close the TOCTOU."""


def _snapshot_parity_fingerprint(snapshot: str) -> tuple[dict[str, int], str]:
    """Read the snapshot's parity signals + full-DB content digest, read-only.

    The snapshot is a separate, immutable file for the duration of the op, so
    its fingerprint is safe to compute BEFORE the target's write lock (013v F2 —
    only the TARGET side must be read under the lock to close the TOCTOU)."""
    snap_uri = pathlib.Path(snapshot).resolve().as_uri() + "?mode=ro"
    snap = sqlite3.connect(snap_uri, uri=True)
    try:
        return _parity_signals(snap), _content_digest(snap)
    finally:
        snap.close()


def _target_parity_refusal(
    conn: sqlite3.Connection,
    snapshot: str,
    snap_sig: dict[str, int],
    snap_digest: str,
) -> str | None:
    """Return a refusal string if the snapshot is not a backup OF the target,
    else None — comparing the snapshot's PRE-COMPUTED fingerprint against the
    target's CURRENT fingerprint read from ``conn`` (013r F2 / 013t F2 / 013v).

    013v F2 (snapshot-parity-toctou): ``conn`` MUST already hold the target's
    ``BEGIN IMMEDIATE`` write lock when this runs, so the target digest is read
    under the same lock that guards the UPDATEs — no concurrent writer can slip
    a change in between the parity read and the restamp. The snapshot digest was
    taken before (its file is immutable during the op); only the target side is
    (re)read here, under the lock.

    The A-5 guards prove the snapshot is a distinct, valid, Mnemos-shaped SQLite
    file — NOT that it backs THIS target. A stale or unrelated Mnemos DB passes
    all of them, and rolling back to it would be illusory. The binding check is
    a FULL-DB CONTENT DIGEST over every user table (013v F1 — the snapshot is a
    full-DB rollback, so it must match on ALL user data): the snapshot is
    accepted only if its digest equals the target's current pre-write digest.
    schema_version + the counts stay as human-legible signals on mismatch."""
    tgt_sig = _parity_signals(conn)
    tgt_digest = _content_digest(conn)
    diffs = [
        "%s (snapshot=%s, target=%s)" % (k, snap_sig[k], tgt_sig[k])
        for k in ("schema_version", "pai_import_row_map", "hypomnema_entries")
        if snap_sig[k] != tgt_sig[k]
    ]
    # The content digest is the binding check: equal counts can still hide
    # different content, so a matching digest is REQUIRED even when every count
    # signal agrees. schema_version + the counts stay in the message as
    # human-legible signals, but the digest is what decides acceptance.
    if snap_digest != tgt_digest or diffs:
        diffs.append(
            "content_digest (snapshot=%s, target=%s)"
            % (snap_digest[:12], tgt_digest[:12])
        )
        return (
            "REFUSING: snapshot %s is not a backup of the target DB — its "
            "content diverges from the target's current pre-write state: %s. A "
            "rollback to this snapshot would be illusory. Take a fresh backup "
            "of THIS DB immediately before --execute.\n"
            % (snapshot, "; ".join(diffs))
        )
    return None


def _unmapped_rows(conn: sqlite3.Connection) -> list[str]:
    """Any LIVE mapped hypomnema whose source_path falls in NO bucket → abort.

    013f A-r2-2: joined to live ``hypomnema_entries`` like the three sibling
    helpers (``_total_mapped``, ``_bucket_actual``, ``_apply_bucket``). A
    stale/tombstoned mapping (row_map entry whose target row no longer exists)
    cannot be restamped and must not ABORT the validation — it is reported as
    an informational count by ``_stale_mappings`` instead.
    """
    all_patterns: list[str] = []
    for b in BUCKETS:
        all_patterns.extend(b.like_patterns)
    not_clauses = " AND ".join(["m.source_path NOT LIKE ?"] * len(all_patterns))
    sql = (
        "SELECT DISTINCT m.source_path FROM pai_import_row_map m "
        "JOIN hypomnema_entries h ON h.id = m.target_id "
        "WHERE m.target_table = 'hypomnema_entries' AND " + not_clauses
    )
    return [r[0] for r in conn.execute(sql, all_patterns).fetchall()]


def _stale_mappings(conn: sqlite3.Connection) -> int:
    """013f A-r2-2: count row_map hypomnema entries with NO live target row.

    Informational only — a tombstoned mapping is import-history bookkeeping;
    no live row would be touched by the restamp, so it never aborts.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM pai_import_row_map m "
            "WHERE m.target_table = 'hypomnema_entries' "
            "AND NOT EXISTS (SELECT 1 FROM hypomnema_entries h "
            "WHERE h.id = m.target_id)"
        ).fetchone()[0]
    )


def _ambiguous_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    selects: list[str] = []
    params: list[str] = []
    for bucket in BUCKETS:
        where, bucket_params = bucket.where()
        selects.append(
            "SELECT h.id AS row_id, ? AS bucket_name "
            "FROM hypomnema_entries h "
            "JOIN pai_import_row_map m "
            "  ON m.target_id = h.id AND m.target_table = 'hypomnema_entries' "
            "WHERE " + where
        )
        params.append(bucket.name)
        params.extend(bucket_params)
    sql = (
        "SELECT row_id, GROUP_CONCAT(DISTINCT bucket_name) "
        "FROM ("
        + " UNION ALL ".join(selects)
        + ") GROUP BY row_id HAVING COUNT(DISTINCT bucket_name) > 1"
    )
    return [(str(row[0]), str(row[1])) for row in conn.execute(sql, params)]


def _witnessed_mapped_rows(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT h.id) FROM hypomnema_entries h "
            "JOIN pai_import_row_map m "
            "  ON m.target_id = h.id AND m.target_table = 'hypomnema_entries' "
            "WHERE h.decision_ref IS NOT NULL AND TRIM(h.decision_ref) != ''"
        ).fetchone()[0]
    )


def validate(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """Return (ok, report_lines). ok is False on ANY mismatch."""
    lines: list[str] = []
    ok = True

    total_mapped = _total_mapped(conn)
    lines.append(
        "Total mapped hypomnema rows: %d (expected %d) %s"
        % (
            total_mapped,
            _TOTAL_EXPECTED,
            "OK" if total_mapped == _TOTAL_EXPECTED else "MISMATCH",
        )
    )
    if total_mapped != _TOTAL_EXPECTED:
        ok = False

    unmapped = _unmapped_rows(conn)
    if unmapped:
        ok = False
        lines.append("UNMAPPED source_paths (must be zero):")
        for p in unmapped:
            lines.append("  - %s" % p)
    else:
        lines.append("Unmapped source_paths: 0 OK")

    ambiguous = _ambiguous_rows(conn)
    if ambiguous:
        ok = False
        lines.append("AMBIGUOUS mapped rows (must be zero):")
        for row_id, buckets in ambiguous:
            lines.append("  - %s: %s" % (row_id, buckets))
    else:
        lines.append("Ambiguous mapped rows: 0 OK")

    # 013f A-r2-2: stale/tombstoned mappings are informational, never an abort.
    stale = _stale_mappings(conn)
    lines.append(
        "Stale/tombstoned mappings (no live row; informational): %d" % stale
    )

    witnessed = _witnessed_mapped_rows(conn)
    if witnessed:
        ok = False
        lines.append(
            "Witnessed mapped rows with decision_ref: %d REFUSING - restamp "
            "must not run on witnessed rows; post-ceremony re-run would "
            "silently fold promoted rows back to review_only." % witnessed
        )
    else:
        lines.append("Witnessed mapped rows with decision_ref: 0 OK")

    lines.append("")
    header = "%-34s %6s %6s   %-9s %4s %-20s  %s" % (
        "bucket", "expect", "actual", "domain", "f", "read_visibility",
        "disposition",
    )
    lines.append(header)
    lines.append("-" * len(header))
    sum_actual = 0
    for b in BUCKETS:
        actual = _bucket_actual(conn, b)
        sum_actual += actual
        flag = "OK" if actual == b.expected else "MISMATCH"
        if actual != b.expected:
            ok = False
        lines.append(
            "%-34s %6d %6d   %-9s %4d %-20s  %s [%s]"
            % (
                b.name, b.expected, actual, b.domain, b.foundational,
                b.read_visibility, b.disposition, flag,
            )
        )
    lines.append("-" * len(header))
    lines.append(
        "%-34s %6d %6d   %s"
        % ("TOTAL", _TOTAL_EXPECTED, sum_actual,
           "OK" if sum_actual == _TOTAL_EXPECTED else "MISMATCH")
    )
    if sum_actual != _TOTAL_EXPECTED:
        ok = False

    # Provenance note on the count correction — surfaced, not hidden.
    lines.append("")
    lines.append(
        "NOTE: CURIOUS/BUILT/NOTICED expected=38 (CURIOUS 14 + BUILT 12 + "
        "NOTICED 12). Reports 011/task say '35'; 009 row-7 with ALIVE = 39 is "
        "the exact figure. Ground truth is 38 by source_path."
    )
    return ok, lines


def _apply_bucket(conn: sqlite3.Connection, bucket: Bucket) -> int:
    """Idempotent UPDATE for one bucket. Returns rows actually changed.

    Only rows whose current (domain, foundational, read_visibility) differ from
    the target are updated, so a re-run reports 0 changes and stays a no-op.
    """
    where, params = bucket.where()
    sql = (
        "UPDATE hypomnema_entries "
        "SET domain = ?, foundational = ?, read_visibility = ? "
        "WHERE id IN ("
        "  SELECT h.id FROM hypomnema_entries h "
        "  JOIN pai_import_row_map m "
        "    ON m.target_id = h.id AND m.target_table = 'hypomnema_entries' "
        "  WHERE " + where + " "
        "    AND NOT ("
        "      h.domain = ? AND h.foundational = ? AND h.read_visibility = ?"
        "    )"
        ")"
    )
    args: list[object] = [
        bucket.domain, bucket.foundational, bucket.read_visibility,
        *params,
        bucket.domain, bucket.foundational, bucket.read_visibility,
    ]
    cur = conn.execute(sql, args)
    return cur.rowcount


def execute(
    conn: sqlite3.Connection,
    snapshot: str | None = None,
    snap_sig: dict[str, int] | None = None,
    snap_digest: str | None = None,
) -> tuple[bool, list[str]]:
    """Run the restamp inside one transaction. Acquires the target write lock
    FIRST, then (013v F2) checks snapshot parity UNDER the lock before validating
    or writing — so no concurrent writer can change the target between the parity
    read and the UPDATEs. Aborts on any mismatch WITHOUT writing. Returns
    (ok, report_lines).

    ``snapshot``/``snap_sig``/``snap_digest`` carry the snapshot's pre-computed
    fingerprint (its file is immutable during the op). When they are None the
    parity gate is skipped — this path is used only where parity was already
    enforced or is not applicable; ``main`` always supplies them under
    --execute."""
    lines: list[str] = []
    conn.execute("BEGIN IMMEDIATE")

    # 013v F2: parity is checked HERE, under the write lock, against the target's
    # live state — closing the TOCTOU where the pre-lock check could be defeated
    # by a writer landing before BEGIN IMMEDIATE.
    if snapshot is not None and snap_sig is not None and snap_digest is not None:
        parity = _target_parity_refusal(conn, snapshot, snap_sig, snap_digest)
        if parity is not None:
            conn.execute("ROLLBACK")
            # Hard refusal, same contract as the A-5 guards (stderr + exit 2);
            # main() catches this and surfaces it. No rows written.
            raise SnapshotParityError(parity)

    ok, vlines = validate(conn)
    lines.extend(vlines)
    if not ok:
        conn.execute("ROLLBACK")
        lines.append("")
        lines.append("ABORTED: validation failed; no rows written, transaction "
                     "rolled back.")
        return False, lines

    lines.append("")
    lines.append("Applying restamp (idempotent per bucket):")
    total_changed = 0
    for b in BUCKETS:
        changed = _apply_bucket(conn, b)
        total_changed += changed
        lines.append("  %-34s changed %d rows" % (b.name, changed))

    # Post-UPDATE re-validate inside the same transaction (defense-in-depth).
    ok_post, _ = validate(conn)
    if not ok_post:
        conn.execute("ROLLBACK")
        lines.append("POST-CHECK FAILED: rolled back.")
        return False, lines
    conn.execute("COMMIT")
    lines.append("")
    lines.append("COMMITTED: %d rows changed total." % total_changed)
    return True, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DAVID-10 hypomnema restamp (prepared for David's hand)."
    )
    parser.add_argument("db_path", help="REQUIRED path to the target DB "
                        "(never defaults to live ~/.mnemos).")
    parser.add_argument("--execute", action="store_true",
                        help="Apply the restamp (default is dry-run).")
    parser.add_argument("--snapshot", default=None,
                        help="Path to a fresh backup that must already exist; "
                        "required with --execute (proves a snapshot was taken).")
    parser.add_argument("--i-am-david-restamping-live", action="store_true",
                        help="Deliberate opt-in to target the live ~/.mnemos DB "
                        "(ceremony only; David's hand).")
    args = parser.parse_args(argv)

    _refuse_live(args.db_path, args.i_am_david_restamping_live)

    if not os.path.exists(args.db_path):
        sys.stderr.write("DB not found: %s\n" % args.db_path)
        return 2

    if args.execute:
        if not args.snapshot:
            sys.stderr.write(
                "REFUSING: --execute requires --snapshot <path> pointing at a "
                "fresh backup that already exists (snapshot-first discipline).\n"
            )
            return 2
        if not os.path.exists(args.snapshot):
            sys.stderr.write(
                "REFUSING: snapshot %s does not exist. Take a backup first.\n"
                % args.snapshot
            )
            return 2
        # 013e A-5: the snapshot must be a REAL backup, distinct from the
        # target — David's rollback depends on it. Three checks:
        #   1. Not the target itself: realpath AND (dev, inode) must differ —
        #      catches symlinks, hardlinks, and literally passing the DB path.
        #   2. SQLite header: first 16 bytes == b'SQLite format 3\x00' — an
        #      empty `touch`ed file or a stray text file is not a backup.
        #   3. Non-trivial size: >= 512 bytes (SQLite's minimum page size); a
        #      truncated copy cannot restore anything.
        snap_real = os.path.realpath(args.snapshot)
        db_real = os.path.realpath(args.db_path)
        snap_stat = os.stat(args.snapshot)
        db_stat = os.stat(args.db_path)
        if snap_real == db_real or (
            snap_stat.st_dev == db_stat.st_dev
            and snap_stat.st_ino == db_stat.st_ino
        ):
            sys.stderr.write(
                "REFUSING: --snapshot resolves to the TARGET DB itself "
                "(%s). A snapshot that is the target cannot roll anything "
                "back. Take a real copy first.\n" % snap_real
            )
            return 2
        if snap_stat.st_size < 512:
            sys.stderr.write(
                "REFUSING: snapshot %s is %d bytes — smaller than a single "
                "SQLite page. Not a real backup.\n"
                % (args.snapshot, snap_stat.st_size)
            )
            return 2
        with open(args.snapshot, "rb") as handle:
            header = handle.read(16)
        if header != b"SQLite format 3\x00":
            sys.stderr.write(
                "REFUSING: snapshot %s does not carry the SQLite file header "
                "— not a SQLite database backup.\n" % args.snapshot
            )
            return 2
        refusal = _snapshot_refusal(args.snapshot)
        if refusal is not None:
            sys.stderr.write(refusal)
            return 2
        # 013r F2 / 013t F2 / 013v: the checks above prove the snapshot is a
        # distinct, valid, Mnemos-shaped SQLite file — NOT that it is a backup OF
        # THIS TARGET. A stale/unrelated Mnemos DB passes them and would make
        # David's rollback illusory. Read the snapshot's fingerprint (parity
        # signals + full-DB content digest) HERE — the snapshot file is immutable
        # for the op — but defer the TARGET comparison to execute(), UNDER the
        # write lock (013v F2), so no concurrent writer can change the target
        # between the parity read and the restamp UPDATEs.
        snap_sig, snap_digest = _snapshot_parity_fingerprint(args.snapshot)

    conn = sqlite3.connect(args.db_path)
    try:
        if args.execute:
            print("=== DAVID-10 restamp: EXECUTE ===")
            try:
                ok, lines = execute(
                    conn, args.snapshot, snap_sig, snap_digest
                )
            except SnapshotParityError as exc:
                # 013v F2: the under-lock parity check refused. Same hard-refusal
                # contract as the pre-transaction A-5 guards — stderr + exit 2,
                # nothing written (execute() already rolled back).
                sys.stderr.write(str(exc))
                return 2
        else:
            print("=== DAVID-10 restamp: DRY-RUN (no writes) ===")
            ok, lines = validate(conn)
        for line in lines:
            print(line)
        if not args.execute and not ok:
            print("")
            print("DRY-RUN ABORT: bucket mismatch — the disposition does not "
                  "match this DB. Nothing would be written.")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
