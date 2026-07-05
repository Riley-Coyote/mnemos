"""Journal↔table reconciliation for identity-tier rows.

The read-path validator (in the store) is a cheap structural check: an
identity-tier row with no ``decision_ref`` is kept out of operational reads. It
deliberately does NOT open the journal on the hot path. This module is the other
half — the periodic full verification the watchdog and session-start run — and
it is what actually closes the hole a raw-SQL attacker opens.

That attacker does not forge a ``decision_ref``. He clears ``foundational`` or
rewrites ``domain='topical'`` and the row stops being identity-tier *by its own
columns* — so a tier-filtered query never looks at it again. The only thing that
still knows the row was once witnessed is the journal. So we reconcile in **both
directions**:

- **table → journal:** every currently identity-tier row must carry a
  ``decision_ref`` that resolves to a chain-valid approved line. Missing → orphan;
  present-but-unresolvable → forged/broken. Either way, re-quarantine.
- **journal → table:** every approved journal line must still have a live row
  carrying its ``decision_ref``, still tier-signalled, still matching the
  approved content. A de-tiered / re-domained / content-mutated row is invisible
  to direction A but caught here by its persistent ``decision_ref``.

Re-quarantine = force ``read_visibility='review_only'`` (recoverable by
re-approval; never silent trust). This module never deletes and never writes the
journal; the journal stays ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import journal as vault_journal

_IDENTITY_TABLES = ("beliefs", "hypomnema_entries")

# Row is identity-tier by its own columns (mirrors the store's read-path signal).
_TIER_PREDICATE = {
    "beliefs": "(tier = 'foundational' OR domain IN ('identity', 'foundational'))",
    "hypomnema_entries": "(foundational = 1 OR domain IN ('identity', 'foundational'))",
}


@dataclass
class ReconcileReport:
    """Result of a reconciliation pass. ``ok`` iff no findings and no re-quarantines."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    requarantined: list[dict[str, Any]] = field(default_factory=list)
    chain_ok: bool = True
    chain_break_index: int = -1

    @property
    def ok(self) -> bool:
        return not self.findings and not self.requarantined and self.chain_ok

    def _add(
        self,
        findings_list: list[dict[str, Any]],
        *,
        severity: str,
        kind: str,
        table: str | None,
        row_id: str | None,
        proposal_id: str | None,
        detail: str,
    ) -> None:
        findings_list.append(
            {
                "severity": severity,
                "kind": kind,
                "table": table,
                "row_id": row_id,
                "proposal_id": proposal_id,
                "detail": detail,
            }
        )


def _row_content(table: str, row: dict[str, Any]) -> str:
    return str(row.get("content", ""))


def _applied_fields_match_payload(
    table: str, row: dict[str, Any], proposal: dict[str, Any]
) -> bool:
    """Verify the row is the journaled target AND every WITNESSED field matches.

    **008k canonical rule (aligned with 008g E7/E8):** one definition of
    "witnessed" governs every surface — the fields ``canonical_content_sha256``
    binds that live on the target row: ``content``, ``domain``,
    ``tier``/``foundational`` (tier signal), ``scope`` (agent_id +
    person_id/project_scope for hypomnema), and the row-binding
    (``target_id``/``target_surface``). Nothing else. Confidence, density,
    salience, source, tags, revision counts are mutable-after-witnessing —
    per 008g explicitly, to prevent review-fatigue from routine consolidation.

    Lifecycle fields (``superseded_by`` / ``active``) STAY checked here — a
    ref-carrying row whose lifecycle changed without vault mediation is
    exactly the raw-SQL-bypass class E2B leaves to reconcile as the
    redundant-detection layer (008g pattern: write-side degrade + reconcile
    catches bypass).
    """
    payload = proposal.get("payload") or {}
    # target identity + surface (008e-r3 #1)
    if str(row.get("id", "")) != str(proposal.get("target_id", "")):
        return False
    if table != proposal.get("target_surface"):
        return False
    # witnessed content
    row_content = str(row.get("content", "")).strip()
    payload_content = str(payload.get("content", "")).strip()
    if row_content != payload_content:
        return False
    # witnessed domain
    if str(row.get("domain", "")) != str(proposal.get("domain", "")):
        return False
    # witnessed scope (008e-r3 #1)
    if table == "hypomnema_entries":
        for scope_field in ("agent_id", "person_id", "project_scope"):
            if str(row.get(scope_field, "")) != str(proposal.get(scope_field, "")):
                return False
    elif table == "beliefs":
        if str(row.get("agent_id", "")) != str(proposal.get("agent_id", "")):
            return False
    # witnessed tier signal + lifecycle-visibility (r6 #3 stays)
    if table == "beliefs":
        # tier: apply forces 'foundational' for identity blast (008e #7)
        if str(row.get("tier", "")) != "foundational":
            return False
        # lifecycle: supersession hides content from operational reads —
        # ref-carrying tamper class, not a witnessed-field mutation
        if row.get("superseded_by"):
            return False
        return True
    # hypomnema_entries
    if not bool(row.get("foundational")):
        return False
    # lifecycle: deactivate/supersede hide from operational reads
    if not bool(row.get("active", 1)):
        return False
    if row.get("superseded_by"):
        return False
    return True


def _witness_reverifies_proposal(
    store: Any,
    table: str,
    row: dict[str, Any],
    line: Mapping[str, Any],
    proposal_id: str,
) -> bool:
    """008y J: does a proposal-witnessed row STILL fully verify against its line?

    The discriminator that separates a raw-SQL *hide* (ref cleared, content
    intact — no legitimate producer) from a genuine *degrade* (a witnessed field
    actually changed → the write-path E7/E8/E2B cleared the ref legitimately).
    A full re-verify means: the line's content still hashes to the approved
    content AND every witnessed field on the row still matches the payload. If
    it fully verifies but the ref was cleared, the only way that state exists is
    a raw-SQL clear — a hidden witness, not a legitimate degrade.

    Trace-presence is NOT the discriminator (the raw-SQL adversary forges the
    trace the same way it clears the ref); witness re-verification is unforgeable
    because it anchors in the TCB-owned journal's content hash.
    """
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        return False
    content_ok = vault_journal.canonical_content_sha256(proposal) == str(
        line.get("content_sha256", "")
    )
    if not content_ok:
        return False
    return _applied_fields_match_payload(table, row, proposal)


def _witness_reverifies_legacy(
    table: str,
    row: dict[str, Any],
    line: Mapping[str, Any],
) -> bool:
    """008y J (legacy twin): does a legacy-witnessed row STILL fully verify?

    Legacy lines witness the row itself (``canonical_row_sha256``), so a full
    re-verify is: the row still hashes to the witness AND still carries its tier
    signal AND is not lifecycle-hidden (superseded/deactivated — the K checks).
    Same discriminator as the proposal twin: full-verify + cleared ref = a
    raw-SQL hide, not a legitimate degrade.
    """
    if not _is_tier_signalled(table, row):
        return False
    if vault_journal.canonical_row_sha256(table, row) != str(
        line.get("content_sha256", "")
    ):
        return False
    if row.get("superseded_by"):
        return False
    if table == "hypomnema_entries" and not bool(row.get("active", 1)):
        return False
    return True


def _requarantine(conn: Any, table: str, row_id: str) -> bool:
    """Force a row to review_only from ANY non-review_only state. Returns True if changed.

    008r-review (audit-only-tamper-not-review-quarantined): the WHERE clause was
    ``= 'operational_context'``, so a witnessed row already HIDDEN as
    ``audit_only`` (then further tampered) got a tamper finding via
    ``_flag_and_quarantine`` but was never actually forced onto the review
    surface — it stayed off operational reads AND off the review queue. Force
    review_only from anything that is not already review_only. (Direction A/A'
    only ever pass operational rows, so their behavior is unchanged; this only
    additionally captures the audit_only-hidden case.)
    """
    cur = conn.execute(
        f"UPDATE {table} SET read_visibility = 'review_only' "
        f"WHERE id = ? AND read_visibility != 'review_only'",
        (row_id,),
    )
    return cur.rowcount > 0


def reconcile_identity_tier(
    store: Any,
    journal_path: str | Path,
    *,
    apply_quarantine: bool = True,
    journal_untrusted: bool = False,
) -> ReconcileReport:
    """Reconcile identity-tier rows against the vault journal, both directions.

    ``store`` is an ``EngramStore``. ``apply_quarantine=False`` runs a dry audit
    (findings only, no writes) — used by tests that assert detection without
    mutation. Returns a :class:`ReconcileReport`.

    008y R6-1: ``journal_untrusted`` is the store's ownership verdict on the
    journal FILE (computed there — this module has zero ownership knowledge). An
    existing agent-owned journal is unusable-at-read and is routed into the
    IDENTICAL quarantine-all fail-closed handling the ``corrupt`` classification
    uses, with a distinct forensic label (``journal_untrusted``). Never inert,
    never trusted, never fail-open.
    """
    report = ReconcileReport()
    conn = store._get_conn()

    # 008y GAP-1 + 014b B-1 + 014c B-3: the write span runs LOCKED, UNIFORMLY
    # across EVERY write branch. Three things compose here:
    #   GAP-1  — reconcile's main read-decide-write span must hold BEGIN IMMEDIATE
    #            (apply_identity_decision already does) so an in-process adversary
    #            cannot mutate a row between reconcile's SELECT and its UPDATE.
    #   B-1    — the acquisition must FAIL CLOSED: no try/except around BEGIN
    #            IMMEDIATE, so a raised lock propagates and no write branch ever
    #            runs unlocked (degraded protection must be LOUD, not silent).
    #   B-3    — the TWO early fail-closed quarantine-all branches
    #            (journal_untrusted, corrupt) also write (they iterate
    #            _requarantine UPDATEs — NOT statement-atomic) and previously ran
    #            BEFORE the lock. So acquire the span lock ONCE at function entry
    #            on the write path — it covers the two early branches AND the main
    #            body — and commit ONCE at the end.
    # No production caller invokes reconcile inside an open transaction
    # (session-start runs apply_legacy_witness then reconcile, each committing;
    # the watchdog calls reconcile as its first op; every store write method
    # commits before returning), so a raised BEGIN IMMEDIATE is a real acquisition
    # failure (an unexpected open transaction, a busy timeout) and MUST propagate.
    # Only taken on the write path (apply_quarantine=True); a dry audit is
    # read-only and takes no lock. Reconcile is bounded by identity-row count.
    span_lock = False
    if apply_quarantine:
        conn.execute("BEGIN IMMEDIATE")
        span_lock = True

    try:
        _reconcile_under_lock(
            store, conn, journal_path, report, apply_quarantine, journal_untrusted
        )
    except Exception:
        if span_lock:
            conn.rollback()
        raise
    # Commit once at the end of the span (releases the lock, persists EVERY write
    # branch atomically). A dry audit takes no lock and writes nothing.
    if span_lock:
        conn.commit()
    return report


def _reconcile_under_lock(
    store: Any,
    conn: Any,
    journal_path: str | Path,
    report: ReconcileReport,
    apply_quarantine: bool,
    journal_untrusted: bool,
) -> None:
    """Journal classification + decision + all writes, run under the B-3 entry
    lock when ``apply_quarantine`` is True. Performs NO commits — the caller
    (``reconcile_identity_tier``) commits once around the whole span so every
    write branch (the two early quarantine-all branches and the main body) is
    both atomic and locked."""
    # 008y R6-1: an existing agent-owned journal leaf is untrustworthy — its
    # self-authored (chain-valid) lines must not drive apply/reconcile. Handle
    # BEFORE reading the lines: identical fail-closed quarantine-all as corrupt,
    # distinct label. journal.py stays format-only; the store decided trust.
    if journal_untrusted:
        report._add(
            report.findings,
            severity="critical",
            kind="journal_untrusted",
            table=None,
            row_id=None,
            proposal_id=None,
            detail="vault journal file is agent-owned/agent-writable (untrusted "
            "leaf) — quarantining every operational witnessed identity row; a "
            "self-authored journal cannot witness identity content "
            "(restore-on-verify runs once a trusted journal is in place)",
        )
        if apply_quarantine:
            _fail_closed_quarantine_all_witnessed(conn, report)
        return

    # 008i — classify the journal read. Mid-file corruption fails CLOSED:
    # quarantine every operational identity row with a ref (they can't be
    # verified until the journal is repaired). Torn-tail alarms but reads
    # the good prefix (power-loss recovery — no attacker in-scope).
    journal_read = vault_journal.read_journal_classified(journal_path)
    if journal_read.error == "corrupt":
        report._add(
            report.findings,
            severity="critical",
            kind="journal_corrupt",
            table=None,
            row_id=None,
            proposal_id=None,
            detail=journal_read.detail
            + " — quarantining every operational witnessed identity row until "
            "David repairs the journal (restore-on-verify runs on next healthy pass)",
        )
        if apply_quarantine:
            _fail_closed_quarantine_all_witnessed(conn, report)
        return
    if journal_read.error == "torn_tail":
        report._add(
            report.findings,
            severity="warning",
            kind="journal_torn_tail",
            table=None,
            row_id=None,
            proposal_id=None,
            detail=journal_read.detail
            + " — proceeding with the good prefix; no row references an "
            "un-hashed line, so this is alert-only (likely power loss mid-append)",
        )
    lines = journal_read.lines
    chain_ok, break_index = vault_journal.verify_chain(lines)
    report.chain_ok = chain_ok
    report.chain_break_index = break_index
    if not chain_ok:
        report._add(
            report.findings,
            severity="critical",
            kind="chain_break",
            table=None,
            row_id=None,
            proposal_id=None,
            detail=f"journal chain broke at line {break_index}; "
            "only lines before the break are trusted",
        )

    # Trust only approved lines before any chain break.
    trusted = lines[:break_index] if not chain_ok else lines
    # 008e #5: dedupe by proposal_id — only the LATEST decision per proposal
    # counts (mirrors apply_identity_decision.find_decision). A proposal
    # approved then rejected before apply produced a superseded approved line;
    # trusting it would raise a false "missing_witnessed_row" divergence.
    # Legacy witness lines have no proposal_id — they key on line hash and
    # keep their own single line, never superseded.
    latest_by_proposal: dict[str, tuple[int, dict[str, Any]]] = {}
    ref_to_line: dict[str, dict[str, Any]] = {}
    # 013g #1 — latest-rollout-witness-line-wins, scoped to
    # witness='initial-rollout' lines ONLY. A rollout rerun after a pre-apply
    # content change appends a second approved line for the same row (the
    # 008g-r9 dedupe keys on content_sha256, so a changed row re-witnesses);
    # the STALE approved line must not linger as a license — if content later
    # reverts, it would restore/stamp against the newest line's intent.
    # Reconcile licensing considers only the NEWEST rollout line per
    # (table, row_id); earlier ones are historical: excluded from the map,
    # surfaced as `superseded_witness_line` (info), and license nothing.
    # Legacy and proposal line semantics are untouched (008g/008i/008k;
    # proposals already supersede via latest_by_proposal below).
    latest_rollout_by_row: dict[tuple[str, str], int] = {}
    for idx, line in enumerate(trusted):
        if str(line.get("witness", "")) != "initial-rollout":
            continue
        if str(line.get("decision", "")) != "approved":
            continue
        key = (str(line.get("table", "")), str(line.get("row_id", "")))
        latest_rollout_by_row[key] = idx  # later index wins
    for idx, line in enumerate(trusted):
        proposal_id = str(line.get("proposal_id", ""))
        if proposal_id:
            latest_by_proposal[proposal_id] = (idx, line)
        elif str(line.get("decision", "")) == "approved":
            if str(line.get("witness", "")) == "initial-rollout":
                key = (str(line.get("table", "")), str(line.get("row_id", "")))
                if latest_rollout_by_row.get(key) != idx:
                    # Historical line: excluded from licensing. The info
                    # finding fires ONLY when it would otherwise have licensed
                    # an action — i.e. the row's CURRENT content hashes to
                    # this superseded line (the content-reverted case, where
                    # the old line's fallback would have restored/stamped),
                    # or a row still carries this line's hash as its ref.
                    # A clean post-rerun state (row stamped by the newest
                    # line, content current) stays finding-free — no
                    # per-pass alert noise for pure history (013f class).
                    table_name = str(line.get("table", ""))
                    row_id_val = str(line.get("row_id", ""))
                    would_license = False
                    if table_name in _IDENTITY_TABLES and row_id_val:
                        old_row = conn.execute(
                            f"SELECT * FROM {table_name} WHERE id = ?",
                            (row_id_val,),
                        ).fetchone()
                        if old_row is not None:
                            old_row = dict(old_row)
                            line_hash_val = vault_journal.line_hash(line)
                            row_ref_val = str(
                                old_row.get("decision_ref") or ""
                            ).strip()
                            if row_ref_val == line_hash_val:
                                would_license = True
                            elif vault_journal.canonical_row_sha256(
                                table_name, old_row
                            ) == str(line.get("content_sha256", "")):
                                would_license = True
                    if would_license:
                        report._add(
                            report.findings, severity="info",
                            kind="superseded_witness_line",
                            table=table_name or None,
                            row_id=row_id_val or None,
                            proposal_id=None,
                            detail="superseded initial-rollout witness line "
                            "matches the row's current state but licenses "
                            "nothing — the newest line per row governs "
                            "(013g latest-line-wins)",
                        )
                    continue
            # Legacy line: no proposal, never superseded — keep as-is.
            ref_to_line[vault_journal.line_hash(line)] = {**line, "_line_index": idx}
    for idx, line in latest_by_proposal.values():
        if str(line.get("decision", "")) != "approved":
            continue
        ref_to_line[vault_journal.line_hash(line)] = {**line, "_line_index": idx}

    _reconcile_body(store, conn, report, ref_to_line, apply_quarantine)


def _reconcile_body(
    store: Any,
    conn: Any,
    report: ReconcileReport,
    ref_to_line: dict[str, dict[str, Any]],
    apply_quarantine: bool,
) -> None:
    """The read-decide-write span, run under the GAP-1 BEGIN IMMEDIATE lock when
    ``apply_quarantine`` is True (see reconcile_identity_tier)."""
    changed = False

    # ── Direction A: table → journal ──
    # Every currently identity-tier OPERATIONAL row must carry a ref that
    # resolves to a trusted approved line. 008e-r4 #6: review_only /
    # audit_only identity rows are already off the operational path — the
    # review queue owns them. Flagging them here as orphans generated
    # high-severity findings on every watchdog run for rows David deliberately
    # deferred; scope A to operational_context so it only alarms on live drift.
    for table in _IDENTITY_TABLES:
        rows = conn.execute(
            f"SELECT id, decision_ref, read_visibility FROM {table} "
            f"WHERE {_TIER_PREDICATE[table]} "
            f"AND read_visibility = 'operational_context'"
        ).fetchall()
        for row in rows:
            row = dict(row)
            ref = (row.get("decision_ref") or "").strip()
            if not ref:
                report._add(
                    report.findings,
                    severity="high",
                    kind="orphan_identity_row",
                    table=table,
                    row_id=row["id"],
                    proposal_id=None,
                    detail="identity-tier row has no decision_ref (unwitnessed)",
                )
                if apply_quarantine and _requarantine(conn, table, row["id"]):
                    changed = True
                    report._add(
                        report.requarantined,
                        severity="high",
                        kind="requarantined",
                        table=table,
                        row_id=row["id"],
                        proposal_id=None,
                        detail="forced review_only (orphan)",
                    )
            elif ref not in ref_to_line:
                report._add(
                    report.findings,
                    severity="high",
                    kind="forged_or_broken_ref",
                    table=table,
                    row_id=row["id"],
                    proposal_id=None,
                    detail="decision_ref resolves to no trusted approved journal line",
                )
                if apply_quarantine and _requarantine(conn, table, row["id"]):
                    changed = True
                    report._add(
                        report.requarantined,
                        severity="high",
                        kind="requarantined",
                        table=table,
                        row_id=row["id"],
                        proposal_id=None,
                        detail="forced review_only (forged/broken ref)",
                    )

    # ── Direction A': ref-carrying rows regardless of current tier signal ──
    # 008k-r12 #1: a witnessed row that was raw-SQL de-tiered still carries a
    # decision_ref + operational visibility but no longer matches
    # _TIER_PREDICATE, so the tier-scoped scan above skips it. If its ref
    # doesn't resolve to a trusted line (post-chain-break, or a missing
    # journal makes ref_to_line empty), Direction B never sees it either — it
    # escapes. Sweep ALL operational rows with a non-empty ref not in the
    # trusted map. De-tiered rows with a VALID pre-break ref are still caught
    # by Direction B (found by ref → tier-signal absent → quarantine).
    for table in _IDENTITY_TABLES:
        rows = conn.execute(
            f"SELECT id, decision_ref FROM {table} "
            "WHERE read_visibility = 'operational_context' "
            "AND decision_ref IS NOT NULL AND decision_ref != ''"
        ).fetchall()
        for row in rows:
            row = dict(row)
            if str(row.get("decision_ref") or "").strip() in ref_to_line:
                continue
            # Ref doesn't resolve to any trusted line — untrustworthy witness.
            report._add(
                report.findings,
                severity="high",
                kind="forged_or_broken_ref",
                table=table,
                row_id=row["id"],
                proposal_id=None,
                detail="operational row's decision_ref resolves to no trusted "
                "line (de-tiered escape / post-break / missing journal)",
            )
            if apply_quarantine and _requarantine(conn, table, row["id"]):
                changed = True
                report._add(
                    report.requarantined,
                    severity="high",
                    kind="requarantined",
                    table=table,
                    row_id=row["id"],
                    proposal_id=None,
                    detail="forced review_only (untrusted ref, any tier)",
                )

    # ── Direction B: journal → table ──
    # Every trusted approved line must still have a live row carrying its ref,
    # still tier-signalled, still matching the approved content. Finds rows by
    # ref, so a de-tiered row (invisible to direction A) is still caught.
    for ref, line in ref_to_line.items():
        # Legacy batch-witness lines (DAVID-9 c) and initial-rollout lines
        # (DAVID-10 / 008s #4 / 013e A-1) have no proposal — they witness an
        # existing row. Verify against the row itself (canonical_row_sha256),
        # not a proposal. Steady-state read path is unaffected: the stamped row
        # carries an ordinary decision_ref. 013e A-1: before this branch
        # accepted 'initial-rollout', rollout lines fell through to the
        # proposal-backed path (empty proposal id → missing-proposal finding)
        # and ALL ceremony-witnessed rows were re-quarantined at the next
        # session-start reconcile — the ceremony-breaking gap the gate caught.
        if str(line.get("witness", "")) in ("legacy", "initial-rollout"):
            _reconcile_legacy_line(report, conn, ref, line, apply_quarantine)
            changed = changed or apply_quarantine
            continue
        proposal_id = str(line.get("proposal_id", ""))
        located = False
        for table in _IDENTITY_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE decision_ref = ?", (ref,)
            ).fetchall()
            for row in rows:
                located = True
                row = dict(row)
                # (1) still tier-signalled?
                is_tier = _is_tier_signalled(table, row)
                # (2) proposal still present and hashing to the approved content?
                proposal = store.get_proposal(proposal_id)
                proposal_ok = (
                    proposal is not None
                    and vault_journal.canonical_content_sha256(proposal)
                    == str(line.get("content_sha256", ""))
                )
                # (3) row's applied fields still match the witnessed payload?
                # 008e-r2 #2: check the FULL applied field-set, not just content.
                fields_ok = proposal is not None and _applied_fields_match_payload(
                    table, row, proposal
                )
                if not is_tier:
                    _flag_and_quarantine(
                        report,
                        conn,
                        table,
                        row["id"],
                        proposal_id,
                        "de-tiered: row no longer carries its identity tier signal",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif proposal is None:
                    _flag_and_quarantine(
                        report,
                        conn,
                        table,
                        row["id"],
                        proposal_id,
                        "witnessed row's proposal row is gone",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif not proposal_ok:
                    _flag_and_quarantine(
                        report,
                        conn,
                        table,
                        row["id"],
                        proposal_id,
                        "proposal content no longer hashes to the approved content",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif not fields_ok:
                    _flag_and_quarantine(
                        report,
                        conn,
                        table,
                        row["id"],
                        proposal_id,
                        "row's applied fields no longer match the witnessed payload",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                else:
                    # 008i — restore-on-verify. Every check passed. If the row
                    # reads NON-operational, promote it back. Curator-flagged
                    # rows have no ref (direction B never reaches them);
                    # upsert-degraded rows had their refs cleared (008g).
                    #   - review_only → normal recovery from journal corruption
                    #     or a prior tamper since corrected (silent).
                    #   - audit_only → 008r-review (reconcile-misses-audit-only-
                    #     hide): a witnessed identity row is NEVER legitimately
                    #     audit_only. Direction A skips non-operational rows, so
                    #     flipping a witnessed row to audit_only hid it from
                    #     operational reads AND from the tamper scan. FLAG it as
                    #     tamper AND restore.
                    vis = row.get("read_visibility")
                    if apply_quarantine and vis and vis != "operational_context":
                        if vis == "audit_only":
                            report._add(
                                report.findings,
                                severity="high",
                                kind="witnessed_row_hidden",
                                table=table,
                                row_id=row["id"],
                                proposal_id=proposal_id,
                                detail="witnessed identity row hidden via "
                                "audit_only — restored to operational",
                            )
                        if _restore_operational(conn, table, row["id"]):
                            changed = True
                            report._add(
                                report.requarantined,
                                severity="info",
                                kind="restored",
                                table=table,
                                row_id=row["id"],
                                proposal_id=proposal_id,
                                detail="witness re-verified; restored to operational",
                            )
        if not located:
            # 008e #2: attacker's move — clear decision_ref AND the tier
            # signal columns in one raw-SQL update. The ref lookup above
            # misses (no row has the ref), direction A skips (row no longer
            # identity-tier). Fallback: look up the row by the proposal's
            # target_surface + target_id, the row's actual primary key, and
            # quarantine whatever is there. Content-hash mismatch (the row
            # was rewritten too) or tier absence both re-quarantine.
            proposal = store.get_proposal(proposal_id)
            fallback_table = (proposal or {}).get("target_surface")
            fallback_id = (proposal or {}).get("target_id")
            fallback_row = None
            if fallback_table in _IDENTITY_TABLES and fallback_id:
                fallback_row = conn.execute(
                    f"SELECT * FROM {fallback_table} WHERE id = ?", (fallback_id,)
                ).fetchone()
            if fallback_row is not None:
                # 008k E2B composition, NARROWED by 008y J: a review_only row
                # with a cleared ref is normally a legitimate write-path degrade
                # (E7/E8/E2B) — BUT only if a witnessed field actually changed.
                # If the row STILL fully verifies against its journal line
                # (content hash + every applied field intact) yet the ref was
                # cleared, no legitimate producer exists: a legitimate de-witness
                # flows through the TCB journal (Direction B locates it), a
                # legitimate degrade mutates a witnessed field. Full-verify +
                # cleared-ref = a raw-SQL HIDE → restore + finding. This narrows
                # the E2B exemption from "any review_only" to "review_only AND a
                # witnessed field genuinely changed" (fail-direction: toward
                # visibility). The discriminator is witness re-verification, not
                # trace-presence (the raw-SQL agent forges the trace).
                fallback_row_dict = dict(fallback_row)
                vis = fallback_row_dict.get("read_visibility")
                hidden = _witness_reverifies_proposal(
                    store, fallback_table, fallback_row_dict, line, proposal_id
                )
                if vis == "review_only" and not hidden:
                    pass  # genuine degrade (a witnessed field changed); no finding
                elif hidden:
                    # 008y J: witness fully re-verifies but the ref was cleared —
                    # a raw-SQL hide. Restore to operational and flag.
                    report._add(
                        report.findings,
                        severity="high",
                        kind="witnessed_row_hidden",
                        table=fallback_table,
                        row_id=fallback_id,
                        proposal_id=proposal_id,
                        detail="witnessed row's decision_ref cleared while the "
                        "witness still fully verifies (raw-SQL hide) — restored "
                        "to operational",
                    )
                    # 014c B-2: restore the decision_ref (this Direction-B ref =
                    # the journal line hash that just re-verified) atomically with
                    # the visibility flip, or the row stays ref-less and the next
                    # Direction A re-orphans it (restore→requarantine loop).
                    if apply_quarantine and _restore_operational(
                        conn, fallback_table, fallback_id, decision_ref=ref
                    ):
                        changed = True
                        report._add(
                            report.requarantined,
                            severity="info",
                            kind="restored",
                            table=fallback_table,
                            row_id=fallback_id,
                            proposal_id=proposal_id,
                            detail="witness re-verified; ref + visibility restored "
                            "to operational",
                        )
                else:
                    _flag_and_quarantine(
                        report,
                        conn,
                        fallback_table,
                        fallback_id,
                        proposal_id,
                        "witnessed row's decision_ref was cleared (fallback located "
                        "by target_surface/target_id); re-quarantined",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
            else:
                report._add(
                    report.findings,
                    severity="high",
                    kind="missing_witnessed_row",
                    table=None,
                    row_id=None,
                    proposal_id=proposal_id,
                    detail="approved journal line has no live row carrying its decision_ref",
                )

    return changed


def _reconcile_legacy_line(
    report: ReconcileReport,
    conn: Any,
    ref: str,
    line: dict[str, Any],
    apply_quarantine: bool,
) -> None:
    """Verify a legacy or initial-rollout batch-witness line against its stamped
    row (both directions). Both kinds bind the same ``canonical_row_sha256``
    fields; ``_witness_reverifies_legacy`` is the shared discriminator (013e A-1
    ruling: reuse, not duplicate)."""
    table = str(line.get("table", ""))
    row_id = str(line.get("row_id", ""))
    witness_kind = str(line.get("witness", ""))
    if table not in _IDENTITY_TABLES:
        return
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE decision_ref = ?", (ref,)
    ).fetchall()
    if not rows:
        # 008e #2: attacker cleared decision_ref and the tier signal columns on
        # a legacy-witnessed row. Fallback to the (table, row_id) recorded in
        # the legacy line itself and re-quarantine.
        fallback = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        if fallback is not None:
            # 008k E2B composition, NARROWED by 008y J (legacy twin): a
            # review_only row with a cleared ref is a legitimate write-path
            # degrade ONLY if a witnessed field actually changed. If the row
            # STILL fully verifies against its legacy witness line (row hash +
            # tier signal intact, not lifecycle-hidden) yet the ref was cleared,
            # it is a raw-SQL HIDE → restore + finding. Same discriminator as
            # the proposal fallback: witness re-verification, not trace-presence.
            fallback_dict = dict(fallback)
            vis = fallback_dict.get("read_visibility")
            hidden = _witness_reverifies_legacy(table, fallback_dict, line)
            row_ref = str(fallback_dict.get("decision_ref") or "").strip()
            if (
                witness_kind == "initial-rollout"
                and not row_ref
                and vis != "review_only"
            ):
                # 013f A-r2-1 — CURATOR HOLD OUTRANKS THE PENDING WITNESS.
                # An UNSTAMPED rollout row (decision_ref never landed) whose
                # current visibility is off review_only is curator action in
                # the ceremony→session-start window, not a hide: the row was
                # never operational, so there is nothing to "restore". This
                # branch mirrors apply_initial_rollout's A-4 skip exactly —
                # no stamp, no restore, and the anomaly stays VISIBLE via an
                # info finding every pass (suppression is loud, not silent;
                # the ceremony's expected stamp count also surfaces any
                # shortfall). The J hide-restore arm below survives for
                # LEGACY lines (a previously-stamped row whose ref was
                # cleared); for rollout lines the empty-ref shape is in-band
                # indistinguishable from never-stamped, and the ruled
                # precedence is the curator's hand. Nessun processo rimette
                # dentro una riga che la mano di David ha tenuto fuori.
                report._add(
                    report.findings, severity="info",
                    kind="rollout_witnessed_row_held", table=table,
                    row_id=row_id, proposal_id=None,
                    detail="initial-rollout witnessed row is unstamped and "
                    "held off review_only (curator hold) — not promoted; "
                    "the witness line remains in the journal",
                )
                return
            if (
                witness_kind == "initial-rollout"
                and vis == "review_only"
                and hidden
                and row_ref
            ):
                # 013o (reconcile stale-ref symmetry) — a review_only fallback
                # row that fully content-verifies but carries a NON-EMPTY foreign
                # decision_ref is NOT the benign pending-apply state. It reached
                # this fallback because `SELECT ... WHERE decision_ref = ref`
                # (ref = THIS line's hash) missed it, so its ref differs from the
                # current line's hash — a stale/superseded/foreign ref, exactly
                # the shape apply's `_rollout_skip_reason` now codes 'stale-ref'
                # (013m). Before 013o this fell through to the pending-apply quiet
                # branch below and returned SILENTLY, an asymmetry the 013m
                # apply-side fix exposed: apply reports stale-ref, reconcile stayed
                # quiet. Emit the same distinct stale-ref signal — no restore (the
                # ref is not this line's license; restoring would stamp a foreign
                # ref), no quiet. This completes the apply/reconcile stale-ref
                # symmetry (the same symmetry principle 013m applied on the apply
                # side, now on reconcile).
                report._add(
                    report.findings,
                    severity="high",
                    kind="witnessed_row_stale_ref",
                    table=table,
                    row_id=row_id,
                    proposal_id=None,
                    detail="initial-rollout witnessed row is review_only and "
                    "fully verifies but carries a non-empty foreign decision_ref "
                    "(not this line's hash) — stale-ref, not the benign "
                    "pending-apply state; apply reports the same",
                )
                return
            if (
                witness_kind == "initial-rollout"
                and vis == "review_only"
                and hidden
                and not row_ref
            ):
                # 013e A-1 divergence from the legacy twin, documented: a
                # rollout-witnessed row at review_only + no ref + fully
                # verifying is the LEGITIMATE pending-apply state, not a hide.
                # Rollout rows are review_only AT witness time by design (the
                # restamp's migration default; the promote happens at
                # apply_initial_rollout, session-start). A watchdog reconcile
                # firing in the ceremony→session-start window would otherwise
                # classify all witnessed rows as raw-SQL hides — a 460-high-
                # finding false-alarm storm on David's morning. The state is
                # fail-closed (review queue, off operational reads) and heals
                # at the next session-start apply, which re-stamps + promotes
                # any un-stamped row whose content still matches its line.
                # (For LEGACY lines the same state IS a hide — legacy rows were
                # operational at witness time; review_only+verifying+no-ref has
                # no legitimate producer there.)
                return
            if vis == "review_only" and not hidden:
                pass  # genuine degrade (a witnessed field changed); no finding
            elif hidden:
                report._add(
                    report.findings,
                    severity="high",
                    kind="witnessed_row_hidden",
                    table=table,
                    row_id=row_id,
                    proposal_id=None,
                    detail=f"{witness_kind}-witnessed row's decision_ref cleared while "
                    "the witness still fully verifies (raw-SQL hide) — restored "
                    "to operational",
                )
                # 014c B-2: restore the decision_ref (this legacy line's hash =
                # `ref`, the license that just re-verified) atomically with the
                # visibility flip, or the row stays ref-less and the next
                # Direction A re-orphans it (restore→requarantine loop).
                if apply_quarantine and _restore_operational(
                    conn, table, row_id, decision_ref=ref
                ):
                    report._add(
                        report.requarantined,
                        severity="info",
                        kind="restored",
                        table=table,
                        row_id=row_id,
                        proposal_id=None,
                        detail=f"{witness_kind} witness re-verified; ref + visibility "
                        "restored to operational",
                    )
            else:
                _flag_and_quarantine(
                    report,
                    conn,
                    table,
                    row_id,
                    None,
                    f"{witness_kind}-witnessed row's decision_ref was cleared "
                    "(fallback located by table/row_id); re-quarantined",
                    apply_quarantine,
                )
        else:
            report._add(
                report.findings,
                severity="high",
                kind="missing_witnessed_row",
                table=table,
                row_id=row_id,
                proposal_id=None,
                detail=f"{witness_kind} witness line has no live row carrying its decision_ref",
            )
        return
    for row in rows:
        row = dict(row)
        if not _is_tier_signalled(table, row):
            _flag_and_quarantine(
                report,
                conn,
                table,
                row["id"],
                None,
                f"de-tiered: {witness_kind}-witnessed row no longer carries its tier signal",
                apply_quarantine,
            )
        elif vault_journal.canonical_row_sha256(table, row) != str(
            line.get("content_sha256", "")
        ):
            _flag_and_quarantine(
                report,
                conn,
                table,
                row["id"],
                None,
                f"{witness_kind}-witnessed row content mutated away from the witness",
                apply_quarantine,
            )
        elif row.get("superseded_by") or not bool(row.get("active", 1)):
            # 008r-review audit (finding K — legacy lifecycle hide): the row's
            # content/tier/hash all verify, but it is HIDDEN from operational
            # reads via a LIFECYCLE field — superseded_by set, or active=0 (only
            # hypomnema_entries has `active`; beliefs default to 1 so this is
            # superseded_by-only there) — that canonical_row_sha256 does not
            # bind. The proposal path catches this via _applied_fields_match_
            # payload; the legacy path omitted it (path drift). A real supersede
            # clears the decision_ref (and so is not found by ref here), so a
            # ref-carrying legacy row that is superseded/deactivated is a
            # raw-SQL hide. Flag as tamper + force review_only so David is
            # alerted and the row is on the review surface.
            _flag_and_quarantine(
                report,
                conn,
                table,
                row["id"],
                None,
                "legacy-witnessed row hidden via lifecycle (superseded_by set "
                "or active=0) while still witness-verifying — re-quarantined",
                apply_quarantine,
            )
        else:
            # 008i-r10 #4: restore-on-verify for the legacy path too. Prior code
            # covered proposal-backed rows only.
            #   - review_only → normal recovery after journal repair (info).
            #   - audit_only → 008r-review (reconcile-misses-audit-only-hide): a
            #     legacy-witnessed identity row is never legitimately audit_only;
            #     flipping it there hid it from operational reads AND the tamper
            #     scan. FLAG as tamper AND restore.
            vis = row.get("read_visibility")
            if apply_quarantine and vis and vis != "operational_context":
                if vis == "audit_only":
                    report._add(
                        report.findings,
                        severity="high",
                        kind="witnessed_row_hidden",
                        table=table,
                        row_id=row["id"],
                        proposal_id=None,
                        detail="legacy-witnessed identity row hidden via "
                        "audit_only — restored to operational",
                    )
                if _restore_operational(conn, table, row["id"]):
                    report._add(
                        report.requarantined,
                        severity="info",
                        kind="restored",
                        table=table,
                        row_id=row["id"],
                        proposal_id=None,
                        detail="legacy witness re-verified; restored to operational",
                    )


def _is_tier_signalled(table: str, row: dict[str, Any]) -> bool:
    domain = str(row.get("domain", ""))
    if domain in ("identity", "foundational"):
        return True
    if table == "beliefs":
        return str(row.get("tier", "")) == "foundational"
    return bool(row.get("foundational"))


def _flag_and_quarantine(
    report: ReconcileReport,
    conn: Any,
    table: str,
    row_id: str,
    proposal_id: str | None,
    detail: str,
    apply_quarantine: bool,
) -> None:
    report._add(
        report.findings,
        severity="critical",
        kind="witnessed_row_tampered",
        table=table,
        row_id=row_id,
        proposal_id=proposal_id,
        detail=detail,
    )
    if apply_quarantine and _requarantine(conn, table, row_id):
        report._add(
            report.requarantined,
            severity="critical",
            kind="requarantined",
            table=table,
            row_id=row_id,
            proposal_id=proposal_id,
            detail="forced review_only (tamper)",
        )


def _fail_closed_quarantine_all_witnessed(conn: Any, report: ReconcileReport) -> None:
    """008i — mid-file journal corruption: quarantine every operational row
    that carries a decision_ref, in either identity table. NOT gated on the
    current tier predicate (008i-r10 #2): a witnessed row that was raw-SQL
    de-tiered still carries decision_ref + operational, and a corrupt
    journal is exactly when that hidden row must fail closed too. Rows
    without a decision_ref are already off the operational path via the
    read-path validator. Restore happens on the next healthy reconcile via
    restore-on-verify.
    """
    for table in _IDENTITY_TABLES:
        rows = conn.execute(
            f"SELECT id FROM {table} "
            "WHERE read_visibility = 'operational_context' "
            "AND decision_ref IS NOT NULL AND decision_ref != ''"
        ).fetchall()
        for row in rows:
            if _requarantine(conn, table, row["id"]):
                report._add(
                    report.requarantined,
                    severity="critical",
                    kind="requarantined_journal_corrupt",
                    table=table,
                    row_id=row["id"],
                    proposal_id=None,
                    detail="corrupt journal — witness cannot be verified",
                )


def _restore_operational(
    conn: Any, table: str, row_id: str, *, decision_ref: str | None = None
) -> bool:
    """Promote a row from ANY non-operational visibility back to operational.

    008r-review (reconcile-misses-audit-only-hide): the WHERE clause was
    ``= 'review_only'``, so a witnessed row hidden via ``audit_only`` was never
    restored even when its witness verified — the caller's guard was widened to
    all non-operational states, so this UPDATE must be too, or the hide
    survives. Returns True if a row changed.

    014c B-2 (restore-without-decision-ref): a J-hidden row was hidden by
    CLEARING its ``decision_ref`` (not just flipping visibility). Restoring
    visibility ALONE leaves the ref empty, so the identity read gate STILL
    excludes it (operational + identity-tier + no ref) and the next reconcile
    Direction A re-orphans and re-quarantines it — a restore→requarantine LOOP,
    not a recovery. When ``decision_ref`` is passed (the journal line hash that
    JUST re-verified — the line IS the license), restore it atomically with the
    visibility flip so the row both READS operational and STAYS operational on
    the next pass. When ``decision_ref`` is None (K's visibility-only hide, where
    the ref is intact), the ref is untouched — that path stays correct.
    """
    if decision_ref is not None:
        cur = conn.execute(
            f"UPDATE {table} SET read_visibility = 'operational_context', "
            "decision_ref = ? WHERE id = ? AND (read_visibility != "
            "'operational_context' OR decision_ref IS NULL OR decision_ref = '' "
            "OR decision_ref != ?)",
            (decision_ref, row_id, decision_ref),
        )
        return cur.rowcount > 0
    cur = conn.execute(
        f"UPDATE {table} SET read_visibility = 'operational_context' "
        f"WHERE id = ? AND read_visibility != 'operational_context'",
        (row_id,),
    )
    return cur.rowcount > 0
