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
from typing import Any

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
) -> ReconcileReport:
    """Reconcile identity-tier rows against the vault journal, both directions.

    ``store`` is an ``EngramStore``. ``apply_quarantine=False`` runs a dry audit
    (findings only, no writes) — used by tests that assert detection without
    mutation. Returns a :class:`ReconcileReport`.
    """
    report = ReconcileReport()
    conn = store._get_conn()

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
            conn.commit()
        return report
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
    for idx, line in enumerate(trusted):
        proposal_id = str(line.get("proposal_id", ""))
        if proposal_id:
            latest_by_proposal[proposal_id] = (idx, line)
        elif str(line.get("decision", "")) == "approved":
            # Legacy line: no proposal, never superseded — keep as-is.
            ref_to_line[vault_journal.line_hash(line)] = {**line, "_line_index": idx}
    for idx, line in latest_by_proposal.values():
        if str(line.get("decision", "")) != "approved":
            continue
        ref_to_line[vault_journal.line_hash(line)] = {**line, "_line_index": idx}

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
                        report.requarantined, severity="high", kind="requarantined",
                        table=table, row_id=row["id"], proposal_id=None,
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
                        report.requarantined, severity="high", kind="requarantined",
                        table=table, row_id=row["id"], proposal_id=None,
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
                    report.requarantined, severity="high", kind="requarantined",
                    table=table, row_id=row["id"], proposal_id=None,
                    detail="forced review_only (untrusted ref, any tier)",
                )

    # ── Direction B: journal → table ──
    # Every trusted approved line must still have a live row carrying its ref,
    # still tier-signalled, still matching the approved content. Finds rows by
    # ref, so a de-tiered row (invisible to direction A) is still caught.
    for ref, line in ref_to_line.items():
        # Legacy batch-witness lines (DAVID-9 c) have no proposal — they witness
        # an existing row. Verify against the row itself (canonical_row_sha256),
        # not a proposal. Steady-state read path is unaffected: the stamped row
        # carries an ordinary decision_ref.
        if str(line.get("witness", "")) == "legacy":
            _reconcile_legacy_line(
                report, conn, ref, line, apply_quarantine
            )
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
                        report, conn, table, row["id"], proposal_id,
                        "de-tiered: row no longer carries its identity tier signal",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif proposal is None:
                    _flag_and_quarantine(
                        report, conn, table, row["id"], proposal_id,
                        "witnessed row's proposal row is gone",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif not proposal_ok:
                    _flag_and_quarantine(
                        report, conn, table, row["id"], proposal_id,
                        "proposal content no longer hashes to the approved content",
                        apply_quarantine,
                    )
                    changed = changed or apply_quarantine
                elif not fields_ok:
                    _flag_and_quarantine(
                        report, conn, table, row["id"], proposal_id,
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
                                report.findings, severity="high",
                                kind="witnessed_row_hidden",
                                table=table, row_id=row["id"],
                                proposal_id=proposal_id,
                                detail="witnessed identity row hidden via "
                                       "audit_only — restored to operational",
                            )
                        if _restore_operational(conn, table, row["id"]):
                            changed = True
                            report._add(
                                report.requarantined, severity="info",
                                kind="restored", table=table, row_id=row["id"],
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
                # 008k E2B composition: if the row is already at review_only
                # (legitimately degraded via the write path per E7/E8/E2B),
                # the ref was cleared through the correct channel — don't
                # re-flag as tamper. The fallback exists to catch the raw-SQL
                # bypass case where the row is still operational with no ref.
                fallback_row_dict = dict(fallback_row)
                if fallback_row_dict.get("read_visibility") == "review_only":
                    pass  # legitimately degraded; no finding
                else:
                    _flag_and_quarantine(
                        report, conn, fallback_table, fallback_id, proposal_id,
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

    if changed:
        conn.commit()
    return report


def _reconcile_legacy_line(
    report: ReconcileReport,
    conn: Any,
    ref: str,
    line: dict[str, Any],
    apply_quarantine: bool,
) -> None:
    """Verify a legacy batch-witness line against its stamped row (both directions)."""
    table = str(line.get("table", ""))
    row_id = str(line.get("row_id", ""))
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
            # 008k E2B composition: legitimately-degraded (review_only + ref
            # cleared via the write-path E7/E8/E2B) → no finding.
            if dict(fallback).get("read_visibility") == "review_only":
                pass
            else:
                _flag_and_quarantine(
                    report, conn, table, row_id, None,
                    "legacy-witnessed row's decision_ref was cleared "
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
                detail="legacy witness line has no live row carrying its decision_ref",
            )
        return
    for row in rows:
        row = dict(row)
        if not _is_tier_signalled(table, row):
            _flag_and_quarantine(
                report, conn, table, row["id"], None,
                "de-tiered: legacy-witnessed row no longer carries its tier signal",
                apply_quarantine,
            )
        elif vault_journal.canonical_row_sha256(table, row) != str(
            line.get("content_sha256", "")
        ):
            _flag_and_quarantine(
                report, conn, table, row["id"], None,
                "legacy-witnessed row content mutated away from the witness",
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
                report, conn, table, row["id"], None,
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
                        report.findings, severity="high",
                        kind="witnessed_row_hidden", table=table,
                        row_id=row["id"], proposal_id=None,
                        detail="legacy-witnessed identity row hidden via "
                               "audit_only — restored to operational",
                    )
                if _restore_operational(conn, table, row["id"]):
                    report._add(
                        report.requarantined, severity="info",
                        kind="restored", table=table, row_id=row["id"],
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
        report.findings, severity="critical", kind="witnessed_row_tampered",
        table=table, row_id=row_id, proposal_id=proposal_id, detail=detail,
    )
    if apply_quarantine and _requarantine(conn, table, row_id):
        report._add(
            report.requarantined, severity="critical", kind="requarantined",
            table=table, row_id=row_id, proposal_id=proposal_id,
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
                    report.requarantined, severity="critical",
                    kind="requarantined_journal_corrupt",
                    table=table, row_id=row["id"], proposal_id=None,
                    detail="corrupt journal — witness cannot be verified",
                )


def _restore_operational(conn: Any, table: str, row_id: str) -> bool:
    """Promote a row from ANY non-operational visibility back to operational.

    008r-review (reconcile-misses-audit-only-hide): the WHERE clause was
    ``= 'review_only'``, so a witnessed row hidden via ``audit_only`` was never
    restored even when its witness verified — the caller's guard was widened to
    all non-operational states, so this UPDATE must be too, or the hide
    survives. Returns True if a row changed.
    """
    cur = conn.execute(
        f"UPDATE {table} SET read_visibility = 'operational_context' "
        f"WHERE id = ? AND read_visibility != 'operational_context'",
        (row_id,),
    )
    return cur.rowcount > 0
