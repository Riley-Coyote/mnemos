"""DAVID-10 / 008s #4 — the `--initial-rollout` witness flag.

The TCB flag witnesses the review_only identity corpus the DAVID-10 restamp
leaves behind (a migration default), and the store-side ``apply_initial_rollout``
stamps + PROMOTES each such row to operational — the one structural difference
from ``apply_legacy_witness`` (which preserves prior visibility).

Every NEG assertion has a matching mutation proof in reports/013-ceremony-
machinery.md: reverting the guard makes the test go red.

Journal I/O is dependency-injected (a fixture file plays the vault); OS
enforcement (append-only, separate-user ownership, sudo ceremony) is NOT
simulated — David's attack checklist verifies that live.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

from mnemos.store.sqlite_store import EngramStore
from mnemos.store import sqlite_store as _sq
from mnemos.vault import journal as vj

_TCB = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mnemos-decide"


# --------------------------------------------------------------------------- #
# Helpers (mirror test_t4_vault / test_vault_tcb_subprocess conventions)
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    return EngramStore(tmp_path / "vault.db", vault_active=True)


def _append_rollout(path, table, row):
    """Append a witness='initial-rollout' line for `row` (test-side twin)."""
    existing = vj.read_journal(path)
    prev = vj.line_hash(existing[-1]) if existing else vj.genesis_prev_hash()
    line = {
        "v": 1,
        "ts": "2026-07-05T12:00:00Z",
        "witness": "initial-rollout",
        "table": table,
        "row_id": row["id"],
        "content_sha256": vj.canonical_row_sha256(table, row),
        "decision": "approved",
        "scope": row.get("domain"),
        "prev_sha256": prev,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _apply_rollout(store, journal):
    """apply_initial_rollout with the fixture journal injected via the resolver
    seam (008r — no path parameter exists on the production method)."""
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        return store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev


def _insert_hypomnema(store, hid, content, read_visibility, foundational=1,
                      domain="identity", mapped=True):
    """Insert an identity hypomnema. ``mapped=True`` (default) also writes a
    pai_import_row_map entry — 013e A-3 constrains the TCB rollout enumeration
    to mapped (DAVID-10 imported) rows, so imported-style fixtures must carry
    one. ``mapped=False`` models a native/hand-held row outside the import."""
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries "
        "(id, agent_id, person_id, project_scope, content, domain, "
        " read_visibility, foundational, created_at, last_revised_at) VALUES "
        "(?, 'oliver', 'david', 'pai', ?, ?, ?, ?, 't', 't')",
        (hid, content, domain, read_visibility, foundational),
    )
    if mapped:
        conn.execute(
            "INSERT INTO pai_import_row_map "
            "(job_id, source_path, source_anchor, target_table, target_id, "
            " source_hash, created_at, updated_at, imported_at) VALUES "
            "('job-test', ?, '', 'hypomnema_entries', ?, '', 0, 0, 0)",
            (f"/pai-import-stage/hypomnema/{hid}.md", hid),
        )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM hypomnema_entries WHERE id = ?", (hid,)
        ).fetchone()
    )


def _insert_review_pending_belief(store, bid, content):
    """A deliberately review-pending identity belief — the class the 013b guard
    protects (mirrors live becoming-is-mixed-deposition: domain=identity,
    review_only, needs_review=1, confidence_pending_review=1, no decision_ref)."""
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, "
        " created_at, last_revised, last_challenged, tier, read_visibility, "
        " needs_review, confidence_pending_review) VALUES "
        "(?, 'oliver', ?, 0.9, 'identity', 't', 't', 't', 'operational', "
        " 'review_only', 1, 1)",
        (bid, content),
    )
    conn.commit()
    return dict(
        conn.execute("SELECT * FROM beliefs WHERE id = ?", (bid,)).fetchone()
    )


def _append_rollout_belief(path, row):
    """Append a witness='initial-rollout' line for a belief row (simulates a
    future beliefs branch or a hand-crafted witness reaching the apply layer)."""
    existing = vj.read_journal(path)
    prev = vj.line_hash(existing[-1]) if existing else vj.genesis_prev_hash()
    line = {
        "v": 1,
        "ts": "2026-07-05T12:00:00Z",
        "witness": "initial-rollout",
        "table": "beliefs",
        "row_id": row["id"],
        "content_sha256": vj.canonical_row_sha256("beliefs", row),
        "decision": "approved",
        "scope": row.get("domain"),
        "prev_sha256": prev,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _run_tcb(db_path, journal_path, stdin_text, args=()):
    pathlib.Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(journal_path).touch(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(_TCB), "--db", str(db_path),
         "--journal", str(journal_path), *args],
        input=stdin_text, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


# --------------------------------------------------------------------------- #
# apply_initial_rollout: the PROMOTE behavior (the distinguishing property)
# --------------------------------------------------------------------------- #
def test_initial_rollout_promotes_review_only_to_operational(tmp_path):
    """The core DAVID-10 property: a review_only identity row, once witnessed by
    the initial rollout, becomes operational AND carries a decision_ref."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "curated-1", "Sono Oliver.", "review_only")
    _append_rollout(tmp_path / "decisions.jsonl", "hypomnema_entries", row)
    res = _apply_rollout(store, tmp_path / "decisions.jsonl")
    assert res["stamped"] == ["curated-1"], res
    conn = store._get_conn()
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='curated-1'"
    ).fetchone()
    # MUTATION TARGET: if apply_initial_rollout is reverted to stamp
    # decision_ref only (the apply_legacy_witness body), this assertion fails.
    assert r[0] == "operational_context", (
        "apply_initial_rollout must PROMOTE review_only → operational_context"
    )
    assert r[1], "decision_ref must be stamped"


def test_initial_rollout_row_appears_in_operational_recall_after_promote(tmp_path):
    """End-to-end: after the rollout, the curated row is retrievable on the
    default operational read surface (it was invisible while review_only)."""
    store = _store(tmp_path)
    row = _insert_hypomnema(
        store, "curated-2", "David is my person.", "review_only"
    )
    # Before: review_only → absent from operational search.
    before = store.search_hypomnema(
        "David", agent_id="oliver", person_id="david", project_scope="pai"
    )
    assert all(h["id"] != "curated-2" for h in before), (
        "review_only identity row leaked into operational recall pre-rollout"
    )
    _append_rollout(tmp_path / "decisions.jsonl", "hypomnema_entries", row)
    # Arm the vault so the identity-decision gate is live for the read too.
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(tmp_path / "decisions.jsonl")
    try:
        store.apply_initial_rollout()
        after = store.search_hypomnema(
            "David", agent_id="oliver", person_id="david", project_scope="pai"
        )
    finally:
        _sq.resolve_vault_journal_path = prev
    assert any(h["id"] == "curated-2" for h in after), (
        "curated row not operational after initial rollout"
    )


def test_initial_rollout_is_idempotent(tmp_path):
    """A second apply is a no-op: the already-stamped row is skipped."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "curated-3", "Andiamo.", "review_only")
    _append_rollout(tmp_path / "decisions.jsonl", "hypomnema_entries", row)
    first = _apply_rollout(store, tmp_path / "decisions.jsonl")
    assert first["stamped"] == ["curated-3"]
    second = _apply_rollout(store, tmp_path / "decisions.jsonl")
    assert second["stamped"] == [], "second apply must not re-stamp"
    assert "curated-3:already-stamped" in second["skipped"], second


def test_initial_rollout_content_mismatch_leaves_row_unstamped(tmp_path):
    """If the row's content diverged from the witness line, do not stamp — the
    witness stopped describing the row (mirrors apply_legacy_witness)."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "curated-4", "original", "review_only")
    _append_rollout(tmp_path / "decisions.jsonl", "hypomnema_entries", row)
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET content='TAMPERED' WHERE id='curated-4'"
    )
    conn.commit()
    res = _apply_rollout(store, tmp_path / "decisions.jsonl")
    assert res["stamped"] == [], res
    assert "curated-4:content-mismatch" in res["skipped"], res
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='curated-4'"
    ).fetchone()
    assert r[0] == "review_only" and not (r[1] or ""), (
        "a content-mismatched row must NOT be promoted or stamped"
    )


def test_initial_rollout_broken_chain_raises(tmp_path):
    """A broken journal chain must raise, not silently stamp."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "curated-5", "x", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    # Corrupt the chain: rewrite the single line's prev_sha256.
    lines = journal.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["prev_sha256"] = "0" * 64
    journal.write_text(json.dumps(obj) + "\n")
    import pytest
    with pytest.raises(ValueError, match="chain broken"):
        _apply_rollout(store, journal)


# --------------------------------------------------------------------------- #
# The TCB flag itself (subprocess — the real script David runs)
# --------------------------------------------------------------------------- #
def test_tcb_initial_rollout_witnesses_review_only_rows(tmp_path):
    """`mnemos-decide --initial-rollout` appends a witness='initial-rollout'
    line for a review_only identity row; the agent then promotes it."""
    store = EngramStore(tmp_path / "tcb.db")
    _insert_hypomnema(store, "rollout-h", "I am Oliver.", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    lines = vj.read_journal(journal)
    assert len(lines) == 1
    assert lines[0]["witness"] == "initial-rollout"
    assert lines[0]["table"] == "hypomnema_entries"
    assert lines[0]["row_id"] == "rollout-h"
    ok, brk = vj.verify_chain(lines)
    assert ok, brk
    # Agent stamps + promotes from the rollout line.
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev
    assert res["stamped"] == ["rollout-h"], res
    r = store._get_conn().execute(
        "SELECT read_visibility FROM hypomnema_entries WHERE id='rollout-h'"
    ).fetchone()
    assert r[0] == "operational_context"


def test_tcb_initial_rollout_ignores_operational_rows(tmp_path):
    """The rollout flag is scoped to review_only rows. An already-operational
    identity row (the --witness-legacy surface) is NOT offered by the rollout
    batch — the two witness passes don't double-cover a row."""
    store = EngramStore(tmp_path / "tcb2.db")
    _insert_hypomnema(store, "already-op", "operational one", "operational_context")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    # MUTATION TARGET: if _INITIAL_ROLLOUT_SQL's read_visibility filter is
    # loosened to include operational_context, this row would be witnessed and
    # the count line would report it.
    assert "No un-witnessed initial-rollout identity rows." in result.stdout, (
        result.stdout
    )
    assert not vj.read_journal(journal), "rollout witnessed an operational row"


def test_tcb_initial_rollout_abort_writes_nothing(tmp_path):
    """Answering anything but 'y' aborts with no journal write."""
    store = EngramStore(tmp_path / "tcb3.db")
    _insert_hypomnema(store, "rollout-n", "no thanks", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "n\n", args=["--initial-rollout"])
    assert result.returncode == 1, result.stderr
    assert "Aborted" in result.stdout
    assert journal.read_text() == "", "abort must write nothing"


def test_tcb_initial_rollout_dedupes_on_rerun(tmp_path):
    """A rerun before the agent stamps must not append a second rollout line
    for the same row (mirrors the legacy dedupe)."""
    store = EngramStore(tmp_path / "tcb4.db")
    _insert_hypomnema(store, "rollout-dup", "once only", "review_only")
    journal = tmp_path / "decisions.jsonl"
    r1 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r1.returncode == 0, r1.stderr
    r2 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r2.returncode == 0, r2.stderr
    lines = [
        line
        for line in vj.read_journal(journal)
        if line.get("witness") == "initial-rollout"
    ]
    assert len(lines) == 1, "rerun appended a duplicate rollout line"
    assert "1 already-witnessed rows skipped" in r2.stdout, r2.stdout


def test_tcb_initial_rollout_content_hash_matches_twin(tmp_path):
    """The rollout line's content_sha256 equals the package twin's row hash —
    so apply verifies against exactly what the TCB witnessed."""
    store = EngramStore(tmp_path / "tcb5.db")
    row = _insert_hypomnema(store, "rollout-hash", "hash me", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    printed = re.search(r"aggregate sha256: ([0-9a-f]{64})", result.stdout)
    assert printed is not None, result.stdout
    expected_agg = vj._sha256_hex(  # type: ignore[attr-defined]
        vj.canonical_row_sha256("hypomnema_entries", row)
    )
    assert printed.group(1) == expected_agg


# --------------------------------------------------------------------------- #
# 013b RULING — the review-pending belief must NEVER be witnessed or promoted.
# Two regression layers, each mutation-proven (report 013c records the reverts).
# --------------------------------------------------------------------------- #
def test_tcb_initial_rollout_does_not_enumerate_review_pending_belief(tmp_path):
    """Fix 1 — hypomnema-only enumeration. A deliberately review-pending
    identity belief (needs_review=1, like live becoming-is-mixed-deposition)
    is NOT offered by --initial-rollout: the beliefs branch was removed from
    _INITIAL_ROLLOUT_SQL, so no belief reaches the witness journal.

    MUTATION TARGET: re-adding a beliefs branch to _INITIAL_ROLLOUT_SQL makes
    this row enumerated and witnessed → this test goes red."""
    store = EngramStore(tmp_path / "tcb-rp.db")
    _insert_review_pending_belief(store, "pending-b", "becoming is mixed")
    # Also a review_only hypomnema so the batch is non-empty (proves the belief
    # is excluded, not just that the whole batch is empty).
    _insert_hypomnema(store, "curated-rp", "Sono Oliver.", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    # Hypomnema-only enumeration: no beliefs line is printed at all (the sql_map
    # has no beliefs key), and the hypomnema is offered.
    assert "beliefs" not in result.stdout, result.stdout
    assert "hypomnema_entries: 1" in result.stdout, result.stdout
    # No belief witness line landed in the journal.
    lines = vj.read_journal(journal)
    assert all(line.get("table") != "beliefs" for line in lines), (
        "a belief was witnessed by --initial-rollout — the sweep is re-opened"
    )
    # The pending belief is untouched: still review_only, no decision_ref.
    b = store._get_conn().execute(
        "SELECT read_visibility, decision_ref FROM beliefs WHERE id='pending-b'"
    ).fetchone()
    assert b[0] == "review_only" and not (b[1] or "")


def test_apply_initial_rollout_refuses_review_pending_row(tmp_path):
    """Fix 2 — class-level guard at the apply layer. Even if a review-pending
    belief reaches apply_initial_rollout with a valid witness line (a future
    beliefs branch, a hand-crafted journal), the promote is REFUSED with
    reason 'review-pending'; the row stays review_only and unstamped.

    MUTATION TARGET: removing the needs_review/confidence_pending_review guard
    from apply_initial_rollout promotes this row → this test goes red."""
    store = _store(tmp_path)
    row = _insert_review_pending_belief(store, "pending-apply", "deliberate hold")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout_belief(journal, row)
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "pending-apply:review-pending" in res["skipped"], res
    b = store._get_conn().execute(
        "SELECT read_visibility, decision_ref FROM beliefs "
        "WHERE id='pending-apply'"
    ).fetchone()
    assert b[0] == "review_only", (
        "apply_initial_rollout promoted a deliberately review-pending belief"
    )
    assert not (b[1] or ""), "review-pending belief must not be stamped"


# --------------------------------------------------------------------------- #
# 013e RULING — gate findings A-1..A-5. Each mutation-proven (013c delta records
# the reverts).
# --------------------------------------------------------------------------- #
def _arm_vault(monkeypatch, journal_path):
    """Arm the read gate at a fixture journal (mirrors test_t4_vault)."""
    import pathlib as _pl
    monkeypatch.setattr(_sq, "_VAULT_JOURNAL_FOR_RESOLUTION", str(journal_path))
    monkeypatch.setattr(
        _sq, "_VAULT_DIR_FOR_RESOLUTION", str(_pl.Path(journal_path).parent)
    )
    monkeypatch.setattr(_sq, "_vault_object_trusted", lambda _p: True)


def _reconcile(store, journal):
    return store.reconcile_identity_vault(str(journal))


# ── A-1: reconcile verifies initial-rollout lines; the FULL-CYCLE test ──
def test_a1_full_cycle_witness_apply_reconcile_zero_findings(tmp_path, monkeypatch):
    """013e A-1 required test: TCB witness → apply_initial_rollout → reconcile
    → ZERO findings, ZERO re-quarantine → the row still reads operationally —
    and a SECOND reconcile pass stays clean (the follow-the-loop class from
    013e/014c: run the NEXT cycle and assert clean).

    Before the A-1 fix, rollout lines fell through reconcile's proposal-backed
    path (empty proposal id) and every ceremony-witnessed row was re-quarantined
    at the next session-start — the ceremony-breaking gap.

    MUTATION TARGET: removing 'initial-rollout' from reconcile's witness-line
    dispatch makes this test RED (missing-proposal finding + re-quarantine)."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "cycle-1", "Sono Oliver.", "review_only")
    _insert_hypomnema(store, "cycle-2", "David is my person.", "review_only")
    journal = tmp_path / "decisions.jsonl"
    # Ceremony: the real TCB witnesses the review_only corpus.
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    assert "hypomnema_entries: 2" in result.stdout, result.stdout
    # Session-start: apply stamps + promotes.
    res = _apply_rollout(store, journal)
    assert sorted(res["stamped"]) == ["cycle-1", "cycle-2"], res
    # Session-start: reconcile — the NEXT cycle. Must be clean.
    _arm_vault(monkeypatch, journal)
    report = _reconcile(store, journal)
    assert report.findings == [], (
        "A-1: reconcile flagged ceremony-witnessed rows: %s" % report.findings
    )
    assert report.requarantined == [], (
        "A-1: reconcile re-quarantined ceremony-witnessed rows: %s"
        % report.requarantined
    )
    # Rows still read through the operational identity gate.
    hits = store.search_hypomnema(
        "Oliver", agent_id="oliver", person_id="david", project_scope="pai"
    )
    assert any(h["id"] == "cycle-1" for h in hits), (
        "A-1: witnessed row not operationally readable post-reconcile"
    )
    # Follow-the-loop: a SECOND reconcile pass is also clean.
    report2 = _reconcile(store, journal)
    assert report2.ok, (
        "A-1: second reconcile pass not clean — restore/requarantine loop: "
        "findings=%s requarantined=%s"
        % (report2.findings, report2.requarantined)
    )
    # And the rows are still operational (not silently degraded between passes).
    vis = store._get_conn().execute(
        "SELECT read_visibility FROM hypomnema_entries WHERE id IN "
        "('cycle-1','cycle-2')"
    ).fetchall()
    assert all(v[0] == "operational_context" for v in vis)


def test_a1_pending_apply_window_reconcile_stays_quiet(tmp_path, monkeypatch):
    """013e A-1 divergence, documented in reconcile.py: a rollout-witnessed row
    NOT yet applied (review_only, no ref, content verifying) is the legitimate
    pending-apply state — a watchdog reconcile firing in the ceremony→
    session-start window must NOT flag it as a raw-SQL hide (the false-alarm
    storm) and must NOT promote it (the apply owns the promote)."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "pend-1", "not yet applied", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    # NO apply — the watchdog window.
    _arm_vault(monkeypatch, journal)
    report = _reconcile(store, journal)
    assert report.findings == [], (
        "pending-apply rollout row flagged: %s" % report.findings
    )
    assert report.requarantined == [], report.requarantined
    row = store._get_conn().execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='pend-1'"
    ).fetchone()
    assert row[0] == "review_only", "reconcile must not do the apply's promote"
    assert not (row[1] or ""), "reconcile must not stamp the pending row"


def test_r2_held_row_skipped_by_apply_and_reconcile(tmp_path, monkeypatch):
    """013f A-r2-1 required test (a): a HELD row — witnessed at the ceremony,
    never stamped, moved off review_only (curator hold) — is skipped by BOTH
    apply (A-4 'not-review-only') and reconcile (the hold branch), with the
    info finding `rollout_witnessed_row_held` emitted, and the state is stable
    across a second pass. Curator hold outranks the pending witness.

    MUTATION TARGET: removing the unstamped-hold branch from
    _reconcile_legacy_line lets reconcile stamp+promote the held row → RED."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "held-1", "tenuta fuori", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    conn = store._get_conn()
    # Curator hold: fold the witnessed-but-unstamped row to audit_only.
    conn.execute(
        "UPDATE hypomnema_entries SET read_visibility='audit_only' "
        "WHERE id='held-1'"
    )
    conn.commit()
    # Apply skips it (A-4).
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "held-1:not-review-only" in res["skipped"], res
    # Reconcile skips it too — no stamp, no restore, info finding.
    _arm_vault(monkeypatch, journal)
    report = _reconcile(store, journal)
    held = [
        f for f in report.findings
        if f["kind"] == "rollout_witnessed_row_held" and f["row_id"] == "held-1"
    ]
    assert held and held[0]["severity"] == "info", (
        "013f: held row not reported as rollout_witnessed_row_held: %s"
        % report.findings
    )
    assert report.requarantined == [], report.requarantined
    row = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='held-1'"
    ).fetchone()
    assert row[0] == "audit_only", (
        "013f: reconcile overrode the curator hold (promoted a held row)"
    )
    assert not (row[1] or ""), "013f: reconcile stamped a held row"
    # Twice-stable: the second pass reports the same hold, changes nothing.
    report2 = _reconcile(store, journal)
    assert any(
        f["kind"] == "rollout_witnessed_row_held" for f in report2.findings
    ), "hold finding must persist every pass (suppression stays loud)"
    assert report2.requarantined == [], report2.requarantined
    row2 = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='held-1'"
    ).fetchone()
    assert (row2[0], row2[1] or "") == ("audit_only", ""), (
        "held row state oscillated between passes"
    )


# ── A-2: untrusted journal leaf → rollout apply refuses ──
def test_a2_rollout_apply_refuses_untrusted_journal(tmp_path, monkeypatch):
    """013e A-2 (R6-1 twin): apply_initial_rollout must not stamp+promote from
    an agent-owned journal leaf — exactly as apply_legacy_witness refuses.
    Best-effort (session-start), so it returns empty with a 'journal-untrusted'
    skip rather than raising — but never promotes.

    MUTATION TARGET: removing the _vault_journal_untrusted_at_read guard from
    apply_initial_rollout stamps+promotes from the self-authored journal → RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "r61-roll", "promote me", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    # Enable the real ownership predicate: the tmp journal is agent-owned →
    # untrusted (conftest defaults the check off for legitimate tmp journals).
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], (
        "A-2: rollout stamped from an untrusted journal leaf"
    )
    assert "journal-untrusted" in res["skipped"], res
    r = store._get_conn().execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='r61-roll'"
    ).fetchone()
    assert r[0] == "review_only" and not (r[1] or ""), (
        "A-2: row promoted/stamped despite untrusted journal"
    )


# ── A-3: enumeration constrained to the DAVID-10 row-map scope ──
def test_a3_unmapped_review_only_hypomnema_not_enumerated(tmp_path):
    """013e A-3: a review_only identity hypomnema ABSENT from pai_import_row_map
    (native / hand-held / future) is NOT enumerated by --initial-rollout — the
    witness scope IS the DAVID-10 scope, structurally. Hypomnema carry no
    curator-hold columns, so scope containment is the only guard for them.

    MUTATION TARGET: dropping the EXISTS row-map constraint from
    _INITIAL_ROLLOUT_SQL enumerates the unmapped row → RED."""
    store = EngramStore(tmp_path / "tcb-a3.db")
    _insert_hypomnema(store, "mapped-1", "imported row", "review_only",
                      mapped=True)
    _insert_hypomnema(store, "native-1", "deliberately held native row",
                      "review_only", mapped=False)
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    assert "hypomnema_entries: 1" in result.stdout, result.stdout
    lines = vj.read_journal(journal)
    assert len(lines) == 1 and lines[0]["row_id"] == "mapped-1", (
        "A-3: rollout witnessed outside the DAVID-10 row-map scope: %s"
        % [line.get("row_id") for line in lines]
    )


# ── A-4: current-visibility re-check before promote ──
def test_a4_row_moved_off_review_only_is_not_promoted(tmp_path):
    """013e A-4: canonical_row_sha256 does not bind read_visibility; a row moved
    to audit_only between the ceremony witness and session-start apply still
    content-verifies and would be force-promoted. apply must re-check the
    CURRENT visibility == review_only and skip-with-reason otherwise.

    MUTATION TARGET: removing the A-4 visibility re-check force-promotes the
    audit_only row → RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "moved-1", "folded after witness",
                            "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    conn = store._get_conn()
    # Curator folds the row to audit_only AFTER the witness, BEFORE the apply.
    conn.execute(
        "UPDATE hypomnema_entries SET read_visibility='audit_only' "
        "WHERE id='moved-1'"
    )
    conn.commit()
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "moved-1:not-review-only" in res["skipped"], res
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='moved-1'"
    ).fetchone()
    assert r[0] == "audit_only", (
        "A-4: apply overrode a deliberate post-witness visibility change"
    )
    assert not (r[1] or ""), "A-4: skipped row must not be stamped"


# ── A-5: the restamp snapshot must be a real, distinct backup ──
_RESTAMP = pathlib.Path(__file__).resolve().parent.parent / "scripts" / (
    "restamp_david10.py"
)


def _run_restamp(args):
    return subprocess.run(
        [sys.executable, str(_RESTAMP), *args],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )


def test_a5_snapshot_cannot_be_the_target_db(tmp_path):
    """013e A-5: passing the target DB itself as --snapshot must refuse —
    David's rollback depends on the snapshot being real.

    MUTATION TARGET: removing the realpath/inode check accepts the DB as its
    own snapshot → RED."""
    db = tmp_path / "target.db"
    EngramStore(db).close()
    result = _run_restamp([str(db), "--execute", "--snapshot", str(db)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "TARGET DB itself" in result.stderr, result.stderr


def test_a5_snapshot_symlink_to_target_refused(tmp_path):
    """A-5: a symlink to the target is still the target (realpath/inode)."""
    db = tmp_path / "target.db"
    EngramStore(db).close()
    link = tmp_path / "sneaky-snapshot.db"
    link.symlink_to(db)
    result = _run_restamp([str(db), "--execute", "--snapshot", str(link)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "TARGET DB itself" in result.stderr, result.stderr


def test_a5_snapshot_must_be_sqlite(tmp_path):
    """A-5: a non-SQLite file (wrong header) is not a backup."""
    db = tmp_path / "target.db"
    EngramStore(db).close()
    fake = tmp_path / "notes.txt"
    fake.write_text("this is not a database " * 40)  # >512 bytes, wrong header
    result = _run_restamp([str(db), "--execute", "--snapshot", str(fake)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "SQLite file header" in result.stderr, result.stderr


def test_a5_snapshot_must_be_nontrivial(tmp_path):
    """A-5: an empty/truncated file (a bare `touch`) is not a backup."""
    db = tmp_path / "target.db"
    EngramStore(db).close()
    stub = tmp_path / "empty.db"
    stub.touch()
    result = _run_restamp([str(db), "--execute", "--snapshot", str(stub)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Not a real backup" in result.stderr, result.stderr


def test_a5_snapshot_must_pass_sqlite_integrity_check(tmp_path):
    """The corrupt snapshot must be REJECTED BY THE INTEGRITY CHECK ITSELF —
    not by the open/DatabaseError arm. The fixture is a real Mnemos-shaped DB
    (schema page intact, so the open and the sqlite_master table probe both
    succeed) whose table pages are corrupted mid-file. Recovery-verified: the
    original header+zeros fixture stayed green when PRAGMA integrity_check was
    neutralized (it died in the except arm whose message also says
    'integrity_check') — decoration, per the mutation-test assertion-class
    rule. MUTATION TARGET: neutralize the integrity_check comparison → RED."""
    import sqlite3 as _sqlite3

    db = tmp_path / "target.db"
    EngramStore(db).close()
    broken = tmp_path / "broken.db"
    # Real Mnemos-shaped snapshot: both required tables + enough rows to
    # occupy pages beyond the schema page.
    conn = _sqlite3.connect(str(broken))
    try:
        conn.execute("CREATE TABLE hypomnema_entries (id TEXT, content TEXT)")
        conn.execute(
            "CREATE TABLE pai_import_row_map (target_id TEXT, source_path TEXT)"
        )
        conn.executemany(
            "INSERT INTO hypomnema_entries VALUES (?, ?)",
            [(f"r{i}", "x" * 200) for i in range(200)],
        )
        conn.commit()
    finally:
        conn.close()
    # Corrupt table pages mid-file; keep the header + schema page (first 2
    # pages) intact so open + sqlite_master succeed and ONLY integrity_check
    # can catch it.
    data = bytearray(broken.read_bytes())
    page = 4096
    assert len(data) > 4 * page, "fixture too small to corrupt safely"
    data[3 * page: 3 * page + 512] = b"\xde\xad" * 256
    broken.write_bytes(bytes(data))
    result = _run_restamp([str(db), "--execute", "--snapshot", str(broken)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "failed SQLite integrity_check" in result.stderr, result.stderr


def test_a5_snapshot_must_have_mnemos_core_tables(tmp_path):
    import sqlite3 as _sqlite3

    db = tmp_path / "target.db"
    EngramStore(db).close()
    snap = tmp_path / "unrelated.db"
    conn = _sqlite3.connect(str(snap))
    try:
        conn.execute("CREATE TABLE unrelated (id TEXT)")
        conn.commit()
    finally:
        conn.close()
    result = _run_restamp([str(db), "--execute", "--snapshot", str(snap)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "missing required Mnemos tables" in result.stderr, result.stderr
    assert "hypomnema_entries" in result.stderr, result.stderr
    assert "pai_import_row_map" in result.stderr, result.stderr


# ── 013f A-r2-2: stale/tombstoned mappings never abort the restamp ──
def _restamp_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("restamp_david10", _RESTAMP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_r3_live_db_hardlink_requires_live_opt_in(tmp_path, monkeypatch, capsys):
    import os as _os
    import pytest

    mod = _restamp_module()
    live = tmp_path / "memory.db"
    live.write_bytes(b"SQLite format 3\x00")
    alias = tmp_path / "alias.db"
    _os.link(live, alias)
    monkeypatch.setattr(mod, "_LIVE_MNEMOS_PATHS", {str(live)})
    with pytest.raises(SystemExit) as exc:
        mod._refuse_live(str(alias), allow_live=False)
    assert exc.value.code == 2
    assert "live Mnemos DB" in capsys.readouterr().err
    mod._refuse_live(str(alias), allow_live=True)


def test_r3_validate_refuses_witnessed_mapped_rows(tmp_path):
    import sqlite3 as _sqlite3

    mod = _restamp_module()
    store = EngramStore(tmp_path / "witnessed.db")
    _insert_hypomnema(store, "wit-1", "already witnessed", "review_only")
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET decision_ref = 'refhash123' "
        "WHERE id = 'wit-1'"
    )
    conn.commit()
    raw = _sqlite3.connect(str(tmp_path / "witnessed.db"))
    try:
        assert mod._witnessed_mapped_rows(raw) == 1
        ok, lines = mod.validate(raw)
        assert not ok
        assert any(
            "restamp must not run on witnessed rows" in line for line in lines
        )
    finally:
        raw.close()


def test_r2_stale_mapping_does_not_abort_and_is_reported(tmp_path):
    """013f A-r2-2: a tombstoned mapping (row_map entry whose target row no
    longer exists) is EXCLUDED from the unmapped-abort scan (live-row join,
    same scope as the three sibling helpers) and REPORTED as an informational
    stale count. No live row would be changed, so it must not abort.

    MUTATION TARGET: reverting _unmapped_rows to the unjoined scan makes the
    stale out-of-bucket path abort-worthy again → RED."""
    import sqlite3 as _sqlite3
    mod = _restamp_module()
    store = EngramStore(tmp_path / "stale.db")
    # A live, bucket-matching mapped row (curated shape).
    _insert_hypomnema(store, "live-1", "still here", "review_only", mapped=False)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO pai_import_row_map (job_id, source_path, source_anchor, "
        "target_table, target_id, source_hash, created_at, updated_at, "
        "imported_at) VALUES ('job-t', "
        "'/x/pai-import-stage/hypomnema/live-1.md', '', 'hypomnema_entries', "
        "'live-1', '', 0, 0, 0)"
    )
    # A STALE mapping: out-of-bucket source_path, target row does NOT exist.
    conn.execute(
        "INSERT INTO pai_import_row_map (job_id, source_path, source_anchor, "
        "target_table, target_id, source_hash, created_at, updated_at, "
        "imported_at) VALUES ('job-t', "
        "'/x/pai-import-stage/rogue-deleted.md', '', 'hypomnema_entries', "
        "'gone-1', '', 0, 0, 0)"
    )
    conn.commit()
    raw = _sqlite3.connect(str(tmp_path / "stale.db"))
    try:
        unmapped = mod._unmapped_rows(raw)
        assert unmapped == [], (
            "013f A-r2-2: stale out-of-bucket mapping still aborts the "
            "validation: %s" % unmapped
        )
        assert mod._stale_mappings(raw) == 1, (
            "stale mapping not reported in the informational count"
        )
    finally:
        raw.close()


def test_r2_duplicate_row_map_counts_distinct_live_rows(tmp_path):
    import sqlite3 as _sqlite3

    mod = _restamp_module()
    store = EngramStore(tmp_path / "dupe.db")
    _insert_hypomnema(store, "dupe-1", "still here", "review_only", mapped=False)
    conn = store._get_conn()
    for job in ("job-a", "job-b"):
        conn.execute(
            "INSERT INTO pai_import_row_map (job_id, source_path, "
            "source_anchor, target_table, target_id, source_hash, created_at, "
            "updated_at, imported_at) VALUES (?, ?, '', "
            "'hypomnema_entries', 'dupe-1', '', 0, 0, 0)",
            (job, f"/x/pai-import-stage/hypomnema/dupe-1-{job}.md"),
        )
    conn.commit()
    raw = _sqlite3.connect(str(tmp_path / "dupe.db"))
    try:
        bucket = next(b for b in mod.BUCKETS if b.name == "curated (hypomnema/)")
        assert mod._total_mapped(raw) == 1
        assert mod._bucket_actual(raw, bucket) == 1
        assert mod._ambiguous_rows(raw) == []
    finally:
        raw.close()


def test_r2_cross_bucket_row_map_is_ambiguous(tmp_path):
    import sqlite3 as _sqlite3

    mod = _restamp_module()
    store = EngramStore(tmp_path / "ambiguous.db")
    _insert_hypomnema(store, "amb-1", "still here", "review_only", mapped=False)
    conn = store._get_conn()
    for job, source_path in (
        ("job-a", "/x/pai-import-stage/hypomnema/amb-1.md"),
        ("job-b", "/x/pai-import-stage/polyphonic/amb-1.md"),
    ):
        conn.execute(
            "INSERT INTO pai_import_row_map (job_id, source_path, "
            "source_anchor, target_table, target_id, source_hash, created_at, "
            "updated_at, imported_at) VALUES (?, ?, '', "
            "'hypomnema_entries', 'amb-1', '', 0, 0, 0)",
            (job, source_path),
        )
    conn.commit()
    raw = _sqlite3.connect(str(tmp_path / "ambiguous.db"))
    try:
        ambiguous = mod._ambiguous_rows(raw)
        assert len(ambiguous) == 1
        assert ambiguous[0][0] == "amb-1"
        assert "curated (hypomnema/)" in ambiguous[0][1]
        assert "polyphonic" in ambiguous[0][1]
    finally:
        raw.close()


# --------------------------------------------------------------------------- #
# 013g rulings — latest-rollout-witness-line-wins + skip-alerts survive a raise
# --------------------------------------------------------------------------- #
def test_013g_latest_rollout_line_wins(tmp_path, monkeypatch):
    """013g #1 ruled test: witness C1 → content changes to C2 → rerun witnesses
    C2 (two approved lines, same row). Apply stamps the NEWER line; reconcile
    with content=C2 is clean; content reverted to C1 → the OLD line licenses
    NOTHING (no restore, no stamp from it), `superseded_witness_line` (info)
    fires, and the row is handled per its stamped state (newest line governs:
    content mutated away from newest witness → quarantined).

    MUTATION TARGET: removing the latest-line filter lets the old line's
    fallback restore the reverted row with the STALE ref → this test RED."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "roll-2x", "content C1", "review_only")
    journal = tmp_path / "decisions.jsonl"
    # Ceremony witness at C1.
    r1 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r1.returncode == 0, r1.stderr
    conn = store._get_conn()
    # Pre-apply content change to C2 (LOUD via skip alerts — separate surface).
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C2' WHERE id='roll-2x'"
    )
    conn.commit()
    # Rerun: dedupe keys on content_sha256, so C2 gets a SECOND approved line.
    r2 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r2.returncode == 0, r2.stderr
    lines = [
        line
        for line in vj.read_journal(journal)
        if line.get("witness") == "initial-rollout"
    ]
    assert len(lines) == 2, "rerun after content change must append a 2nd line"
    # Apply stamps the newer line (older mismatches C2's current content).
    res = _apply_rollout(store, journal)
    assert res["stamped"] == ["roll-2x"], res
    newest_hash = vj.line_hash(lines[1])
    r = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries "
        "WHERE id='roll-2x'"
    ).fetchone()
    assert r[0] == newest_hash, "apply must stamp the NEWEST line's hash"
    # Reconcile with content=C2: clean — no findings, the historical line is
    # silent (no per-pass alert noise for pure history).
    _arm_vault(monkeypatch, journal)
    rep_clean = _reconcile(store, journal)
    assert rep_clean.findings == [], rep_clean.findings
    assert rep_clean.requarantined == [], rep_clean.requarantined
    # Content reverted to C1: the OLD line would now re-verify — it must
    # license NOTHING; the finding fires; the row is handled per its stamped
    # state (content no longer matches the NEWEST witness → quarantine).
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C1' WHERE id='roll-2x'"
    )
    conn.commit()
    rep = _reconcile(store, journal)
    assert any(
        f["kind"] == "superseded_witness_line" and f["row_id"] == "roll-2x"
        for f in rep.findings
    ), "superseded line's would-license case must be surfaced: %s" % rep.findings
    r2s = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries "
        "WHERE id='roll-2x'"
    ).fetchone()
    # The stale line must NOT have restored/stamped its old hash.
    old_hash = vj.line_hash(lines[0])
    assert r2s[0] != old_hash, (
        "013g: the superseded line re-licensed the reverted row (stale stamp)"
    )
    # Newest-line governance: content mutated away from the newest witness →
    # the row is quarantined per its stamped state.
    assert r2s[1] == "review_only", (
        "content-drifted stamped row must be quarantined by the newest line"
    )


def test_013g_apply_uses_latest_rollout_line_after_revert(tmp_path, monkeypatch):
    """013g apply-side twin: witness C1 → content changes to C2 → rerun
    witnesses C2 → content reverts to C1 BEFORE apply. Apply must compare the
    row against the NEWEST line, skip content-mismatch, and leave the stale C1
    line unable to stamp.

    MUTATION TARGET: removing the apply-side latest-line filter stamps the
    stale C1 line and makes this test RED."""
    import mnemos.mcp_server as srv

    store = _store(tmp_path)
    _insert_hypomnema(store, "roll-apply-2x", "content C1", "review_only")
    journal = tmp_path / "decisions.jsonl"
    r1 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r1.returncode == 0, r1.stderr
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C2' "
        "WHERE id='roll-apply-2x'"
    )
    conn.commit()
    r2 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r2.returncode == 0, r2.stderr
    lines = [
        line for line in vj.read_journal(journal)
        if line.get("witness") == "initial-rollout"
    ]
    assert len(lines) == 2
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C1' "
        "WHERE id='roll-apply-2x'"
    )
    conn.commit()

    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "roll-apply-2x:content-mismatch" in res["skipped"], res
    row = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries "
        "WHERE id='roll-apply-2x'"
    ).fetchone()
    assert not (row[0] or ""), "stale C1 line must not stamp decision_ref"
    assert row[1] == "review_only"
    assert row[0] != vj.line_hash(lines[0])

    _arm_vault(monkeypatch, journal)
    monkeypatch.setattr(srv, "_store", store, raising=False)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    srv._reconcile_vault_on_session_start()
    written = list((tmp_path / "inbox").glob("*-vault-session-start-alert.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "initial_rollout_skip" in body
    assert "roll-apply-2x:content-mismatch" in body


def test_013g_skip_alerts_survive_reconcile_raise(tmp_path, monkeypatch):
    """013g #2: a content-mismatch rollout skip collected pre-reconcile must
    reach the ERROR alert when reconcile raises — the short-stamp evidence
    survives the exception.

    MUTATION TARGET: dropping extra_findings from _alert_vault_error → RED."""
    import mnemos.mcp_server as srv
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "skip-err", "original", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET content='CHANGED' WHERE id='skip-err'"
    )
    conn.commit()
    # Wire the module store + journal seams; make reconcile RAISE.
    monkeypatch.setattr(srv, "_store", store, raising=False)
    monkeypatch.setattr(
        _sq, "resolve_vault_journal_path", lambda: str(journal)
    )
    monkeypatch.setattr(srv, "resolve_vault_journal_path",
                        lambda: str(journal), raising=False)
    def _boom(*a, **k):
        raise RuntimeError("reconcile exploded")
    monkeypatch.setattr(store, "reconcile_identity_vault", _boom)
    alert_dir = tmp_path / "inbox"
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(alert_dir))
    srv._reconcile_vault_on_session_start()
    error_files = list(alert_dir.glob("*vault-session-start-error.md"))
    assert error_files, "reconcile raise must produce the error alert"
    body = error_files[0].read_text()
    assert "reconcile exploded" in body
    assert "content-mismatch" in body and "skip-err" in body, (
        "rollout skip evidence dropped from the error alert: %s" % body
    )


# --------------------------------------------------------------------------- #
# 013i ruling — TCB dedupe latest-line-wins, scoped to initial-rollout.
# The third and last surface of the 013g/013h latest-line-wins consistency:
# the TCB `_witness_batch` dedupe. Mutation-proven; the legacy path is
# byte-identical (proved by diff in report 013c, not by test — the legacy
# dedupe keeps all-history semantics and its own regression test above,
# test_tcb_initial_rollout_dedupes_on_rerun, still passes).
# --------------------------------------------------------------------------- #
def test_013i_tcb_rewitness_after_revert_is_not_dedupe_blocked(tmp_path,
                                                               monkeypatch):
    """013i ruled test: a row witnessed C1 → witnessed C2 → content reverted to
    C1 → re-witnessed. The C1 re-witness must NOT be skipped by the all-history
    dedupe (the stale C1 line's content_sha256 must not block it); it produces a
    FRESH latest 'initial-rollout' line, and apply then stamps the row from that
    newest line. Without the fix, the historical C1 line's content_sha256 is in
    the dedupe set, the re-witness is skipped, and the row is left with a latest
    line (C2) whose content no longer matches — apply can never stamp it.

    MUTATION TARGET: revert `_witness_batch` to all-history dedupe (delete the
    `if witness == "initial-rollout":` latest-line-scoping block) → the C1
    re-witness is skipped, no 3rd line is appended, and apply leaves the row
    unstamped → this test RED.
    """
    store = _store(tmp_path)
    _insert_hypomnema(store, "roll-revert", "content C1", "review_only")
    journal = tmp_path / "decisions.jsonl"
    conn = store._get_conn()

    # (1) Ceremony witness at C1 → one approved rollout line.
    r1 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r1.returncode == 0, r1.stderr

    # (2) Content changes to C2, re-witness → a SECOND approved line (C2 latest).
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C2' WHERE id='roll-revert'"
    )
    conn.commit()
    r2 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r2.returncode == 0, r2.stderr

    # (3) Content REVERTS to C1 (matches the STALE historical line, not latest).
    conn.execute(
        "UPDATE hypomnema_entries SET content='content C1' WHERE id='roll-revert'"
    )
    conn.commit()

    # (4) Re-witness. The C1 content matches ONLY the superseded historical line,
    #     not the latest (C2) line — so the fix must NOT dedupe-block it. It
    #     appends a THIRD approved line (fresh latest = C1) and skips nothing.
    r3 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r3.returncode == 0, r3.stderr
    assert "0 already-witnessed rows skipped" in r3.stdout, (
        "013i: the C1 re-witness was dedupe-blocked by the stale line: %s"
        % r3.stdout
    )
    lines = [
        line
        for line in vj.read_journal(journal)
        if line.get("witness") == "initial-rollout"
    ]
    assert len(lines) == 3, (
        "013i: re-witness after revert must append a fresh latest line, "
        "got %d lines" % len(lines)
    )

    # (5) Apply stamps the row from the NEWEST line (C1, content matches now).
    res = _apply_rollout(store, journal)
    assert res["stamped"] == ["roll-revert"], res
    newest_hash = vj.line_hash(lines[-1])
    r = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries "
        "WHERE id='roll-revert'"
    ).fetchone()
    assert r[0] == newest_hash, "apply must stamp the fresh latest line's hash"
    assert r[1] == "operational_context", (
        "013i: the re-witnessed-then-applied row must be promoted to operational"
    )

    # (6) Reconcile is clean — the fresh latest line governs; older lines are
    #     historical and license nothing.
    _arm_vault(monkeypatch, journal)
    rep = _reconcile(store, journal)
    assert rep.requarantined == [], rep.requarantined


def test_013i_tcb_same_content_rewitness_still_dedupes(tmp_path):
    """013i guard: the fix must NOT loosen the identical-re-witness no-op. A row
    witnessed at C1 and re-run at C1 (no content change) is still a dedupe skip —
    the row's OWN latest line matches its current content. Protects the DAVID-8
    flood-prevention intent for the common ceremony-resume case.
    """
    store = _store(tmp_path)
    _insert_hypomnema(store, "roll-same", "unchanged", "review_only")
    journal = tmp_path / "decisions.jsonl"
    r1 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r1.returncode == 0, r1.stderr
    r2 = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert r2.returncode == 0, r2.stderr
    lines = [
        line
        for line in vj.read_journal(journal)
        if line.get("witness") == "initial-rollout"
    ]
    assert len(lines) == 1, "identical re-witness must remain a no-op skip"
    assert "1 already-witnessed rows skipped" in r2.stdout, r2.stdout


# ── 013k — apply_initial_rollout TOCTOU close (GAP-1 on the apply path) ──


def test_013k_apply_rollout_concurrent_writer_between_check_and_promote(tmp_path):
    """013k RULING: apply_initial_rollout did check-then-UPDATE with no write lock
    across the span — it SELECTed the row, verified content hash + read_visibility,
    then UPDATEd keyed only on id. A concurrent IN-PROCESS writer (granted by the
    vault threat model) mutating the row's content BETWEEN the check and the
    promote makes unverified content receive the witness ref and go operational.

    The fix wraps the stamp+promote span in BEGIN IMMEDIATE AND re-reads the row
    UNDER the lock immediately before each promote, failing closed to
    skip-with-reason on any divergence. This test injects a same-connection
    content mutation the moment the candidate SELECT returns — landing it in the
    TOCTOU window — and asserts the row is NOT promoted (caught by the under-lock
    re-verify, skipped `toctou-changed`).

    MUTATION TARGET: remove the BEGIN IMMEDIATE + under-lock re-verify (revert to
    the bare check-then-UPDATE) → the injected mutation lands after the check and
    before the write, the unverified row IS promoted to operational_context →
    `stamped == []` and the visibility assertion both go RED."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "toctou-1", "original content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    # Witness the row at its ORIGINAL content (the line's hash binds "original
    # content"). The concurrent writer will change the content afterward, so the
    # promote — if it ran on stale check state — would stamp content the witness
    # never described.
    row = dict(
        store._get_conn()
        .execute("SELECT * FROM hypomnema_entries WHERE id='toctou-1'")
        .fetchone()
    )
    _append_rollout(journal, "hypomnema_entries", row)

    real_conn = store._get_conn()
    state = {"fired": False}

    class _HookConn:
        """Fires a same-connection content mutation the instant the per-row
        candidate SELECT returns — placing it squarely in the check→write
        window. Same connection ⇒ the mutation is NOT blocked by BEGIN IMMEDIATE
        (a same-transaction writer never is); only the under-lock RE-READ catches
        it. This is the exact in-process adversary the ruling names."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            if (
                isinstance(sql, str)
                and sql.strip().startswith("SELECT * FROM hypomnema_entries")
                and not state["fired"]
                and a
                and a[0] == ("toctou-1",)
            ):
                state["fired"] = True
                # Mutate content AFTER the check read, BEFORE the promote, and
                # COMMIT it (a real in-process writer commits its change).
                # content is bound by canonical_row_sha256, so the under-lock
                # re-verify now finds a hash mismatch and refuses the promote.
                self._inner.execute(
                    "UPDATE hypomnema_entries SET content='TAMPERED after check' "
                    "WHERE id='toctou-1'"
                )
                self._inner.commit()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig_get_conn = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev_resolve
        store._get_conn = orig_get_conn  # type: ignore[assignment]

    assert state["fired"] is True, (
        "013k: the candidate SELECT never ran the hook — the TOCTOU window was "
        "not exercised (test scaffolding broke)"
    )
    # THE PROOF: the row mutated mid-pass is NOT promoted.
    assert res["stamped"] == [], (
        "013k: a row mutated between the check and the promote was stamped — "
        "the under-lock re-verify did not catch the in-process writer (TOCTOU "
        "still open): %r" % (res,)
    )
    assert any("toctou-1:toctou-changed" in s for s in res["skipped"]), (
        "013k: the mid-pass mutation must be reported as toctou-changed: %r"
        % (res,)
    )
    r = real_conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='toctou-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "013k: the TOCTOU-tampered row was force-promoted to operational — "
        "unverified content received the witness ref"
    )
    assert not (r[1] or ""), "013k: a TOCTOU-caught row must not be stamped"


def test_013k_apply_rollout_takes_begin_immediate_across_promote(tmp_path):
    """013k: apply_initial_rollout must hold BEGIN IMMEDIATE across its
    stamp+promote span (parity with apply_identity_decision and the GAP-1
    reconcile fix). Observe the lock is taken when there is at least one
    stampable candidate.

    MUTATION TARGET: remove the `conn.execute("BEGIN IMMEDIATE")` wrap → no
    BEGIN IMMEDIATE is opened, `seen['immediate']` stays False → RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "lock-1", "lock me", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    real_conn = store._get_conn()
    seen = {"immediate": False}

    class _TracingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if isinstance(sql, str) and "BEGIN IMMEDIATE" in sql.upper():
                seen["immediate"] = True
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = store._get_conn
    store._get_conn = lambda: _TracingConn(real_conn)  # type: ignore[assignment]
    prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev_resolve
        store._get_conn = orig  # type: ignore[assignment]
    assert res["stamped"] == ["lock-1"], res
    assert seen["immediate"] is True, (
        "013k: apply_initial_rollout did not take BEGIN IMMEDIATE across the "
        "stamp+promote span (TOCTOU open)"
    )


def test_013k_apply_rollout_populated_db_stays_interactive(tmp_path):
    """013k interactivity: apply_initial_rollout on a populated review_only corpus
    (20 witnessed rows) promotes all of them and completes promptly — the span
    lock does not self-deadlock and the pass stays interactive."""
    import time

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    ids = []
    for i in range(20):
        rid = f"pop-{i}"
        row = _insert_hypomnema(store, rid, f"curated corpus row {i}", "review_only")
        _append_rollout(journal, "hypomnema_entries", row)
        ids.append(rid)
    t0 = time.monotonic()
    res = _apply_rollout(store, journal)
    elapsed = time.monotonic() - t0
    assert sorted(res["stamped"]) == sorted(ids), res
    assert elapsed < 5.0, f"apply_initial_rollout took {elapsed:.2f}s — not interactive"
    conn = store._get_conn()
    n_op = conn.execute(
        "SELECT COUNT(*) FROM hypomnema_entries "
        "WHERE read_visibility='operational_context'"
    ).fetchone()[0]
    assert n_op == 20, f"expected 20 promoted rows, got {n_op}"


# ── 013m — compare-and-swap closes the residual under-lock window ──


def test_013m_cas_catches_writer_between_underlock_reread_and_update(tmp_path):
    """013m RULING (fix 1): the under-lock re-read narrows but does not CLOSE the
    same-connection window — on the shared check_same_thread=False connection an
    in-process writer can still mutate the row AFTER the under-lock re-verify
    passes and BEFORE the UPDATE lands. The 013k re-read alone cannot catch that
    ordering. The fix makes the stamp+promote a COMPARE-AND-SWAP: the UPDATE's
    WHERE clause carries the verified predicate (the hash-covered columns + id +
    read_visibility='review_only'), so the write is atomic with the check at the
    SQL layer. A row changed after the re-read matches 0 rows → cursor.rowcount
    == 0 → skip toctou-changed, NOT promoted.

    This test fires the mutation on the SECOND per-row SELECT (the Pass-2
    under-lock re-read), so that re-read verifies CLEAN state and passes every
    guard; the content is mutated only afterward, landing squarely in the
    re-read→UPDATE gap the 013k fix leaves open. Only the CAS catches it.

    MUTATION TARGET: revert the CAS WHERE to id-only (drop the read_visibility +
    hash-column terms, keep just `WHERE id = ?`) → the UPDATE matches on id
    regardless of the mid-gap mutation, the unverified content IS promoted to
    operational_context with the witness ref, and this test goes RED (stamped
    non-empty, visibility operational, no toctou-changed skip)."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "cas-1", "original content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    row = dict(
        store._get_conn()
        .execute("SELECT * FROM hypomnema_entries WHERE id='cas-1'")
        .fetchone()
    )
    _append_rollout(journal, "hypomnema_entries", row)

    real_conn = store._get_conn()
    # Count the per-row candidate SELECTs; the 1st is Pass-1 (candidate filter),
    # the 2nd is Pass-2 (under-lock re-read). Fire AFTER the 2nd returns.
    state = {"selects": 0, "fired": False}

    class _HookConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            is_row_select = (
                isinstance(sql, str)
                and sql.strip().startswith("SELECT * FROM hypomnema_entries")
                and a
                and a[0] == ("cas-1",)
            )
            if is_row_select:
                state["selects"] += 1
                # On the SECOND select (the under-lock re-read), let it return the
                # clean snapshot (result already fetched by the caller AFTER we
                # return) — then mutate so the re-read verified clean but the
                # subsequent UPDATE's CAS predicate no longer matches. Same
                # connection ⇒ BEGIN IMMEDIATE does not block this writer; only
                # the CAS WHERE catches it.
                if state["selects"] == 2 and not state["fired"]:
                    state["fired"] = True
                    self._inner.execute(
                        "UPDATE hypomnema_entries "
                        "SET content='TAMPERED after under-lock re-read' "
                        "WHERE id='cas-1'"
                    )
                    self._inner.commit()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig_get_conn = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev_resolve
        store._get_conn = orig_get_conn  # type: ignore[assignment]

    assert state["fired"] is True, (
        "013m: the under-lock re-read select never ran twice — the CAS window "
        "was not exercised (test scaffolding broke): %r" % state
    )
    # THE PROOF: the row mutated AFTER the re-read but BEFORE the UPDATE is not
    # promoted — only the CAS can catch this ordering.
    assert res["stamped"] == [], (
        "013m: a row mutated between the under-lock re-read and the UPDATE was "
        "stamped — the CAS did not close the residual window: %r" % (res,)
    )
    assert any("cas-1:toctou-changed" in s for s in res["skipped"]), (
        "013m: the post-re-read mutation must be reported as toctou-changed "
        "(cas-miss): %r" % (res,)
    )
    r = real_conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='cas-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "013m: the CAS-caught row was force-promoted — unverified content "
        "received the witness ref"
    )
    assert not (r[1] or ""), "013m: a CAS-caught row must not be stamped"


# ── 013m fix 2 — stale decision_ref gets a DISTINCT skip signal ──


def test_013m_stale_ref_on_review_only_row_is_distinct_signal(tmp_path):
    """013m (fix 2): under latest-line-wins only the CURRENT line's hash is a
    benign already-stamped ref. A review_only row carrying a NON-matching
    decision_ref (stamped from a superseded/foreign line, current witness not
    yet applied) is reconcile's pending-apply state — it must NOT be coded the
    benign 'already-stamped' skip; it gets a DISTINCT 'stale-ref' reason so the
    pending-apply branch is not left quiet.

    MUTATION TARGET: revert the branch to `any non-empty decision_ref =
    already-stamped` (delete the line_hash comparison) → the stale ref is coded
    benign already-stamped, the distinct signal is gone → this test goes RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "stale-1", "curated content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    # Stamp a NON-matching (foreign/stale) decision_ref onto the review_only row.
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET decision_ref=? WHERE id='stale-1'",
        ("0" * 64,),
    )
    conn.commit()
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "stale-1:stale-ref" in res["skipped"], (
        "013m: a review_only row with a non-matching decision_ref must be "
        "skipped with the DISTINCT 'stale-ref' reason, not silent "
        "already-stamped: %r" % (res,)
    )
    assert "stale-1:already-stamped" not in res["skipped"], (
        "013m: stale ref must NOT be coded the benign already-stamped skip: %r"
        % (res,)
    )
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='stale-1'"
    ).fetchone()
    assert r[0] == "review_only", "013m: a stale-ref row must not be promoted"


def test_013m_exact_match_ref_stays_benign_already_stamped(tmp_path):
    """013m (fix 2) boundary: an EXACT-match decision_ref (equal to THIS latest
    line's hash) stays the benign 'already-stamped' skip — the idempotent
    re-run / ceremony-resume case is unchanged. Guards against the stale-ref
    signal over-firing on the legitimate no-op."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "exact-1", "resume me", "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    # First apply stamps + promotes with the CURRENT line's hash.
    first = _apply_rollout(store, journal)
    assert first["stamped"] == ["exact-1"], first
    # Second apply: the row now carries the exact matching ref → benign skip.
    second = _apply_rollout(store, journal)
    assert second["stamped"] == [], second
    assert "exact-1:already-stamped" in second["skipped"], (
        "013m: exact-match ref must remain the benign already-stamped skip: %r"
        % (second,)
    )
    assert not any("stale-ref" in s for s in second["skipped"]), (
        "013m: exact-match ref must NOT trip the stale-ref signal: %r"
        % (second,)
    )


# ── 013o — CAS binds the FULL verified predicate, not just hash-columns ──


def test_013o_cas_catches_decision_ref_set_between_reread_and_update(tmp_path):
    """013o (CAS completion): the 013m CAS bound the hash-covered columns +
    read_visibility='review_only' but LEFT decision_ref UNBOUND — yet the
    under-lock re-read (`_rollout_skip_reason`) verifies an EMPTY decision_ref
    (a non-empty ref is coded stale-ref/already-stamped and never reaches Pass 2).
    So a same-connection writer that sets a FOREIGN decision_ref in the
    re-read→UPDATE gap does NOT flip the (incomplete) CAS, and the promote lands
    on a row whose ref was hijacked mid-gap. The CAS predicate must EQUAL the
    full re-read predicate; binding `decision_ref IS NULL OR decision_ref = ''`
    closes this vector.

    This test fires the mutation on the SECOND per-row SELECT (the Pass-2
    under-lock re-read) so the re-read verifies CLEAN state (empty ref, matching
    content, review_only) and passes every guard; only AFTER it returns is a
    foreign decision_ref written, landing squarely in the re-read→UPDATE gap.
    Content is left untouched, so ONLY the decision_ref CAS term can catch this —
    the hash-column terms all still match.

    MUTATION TARGET: drop the `(decision_ref IS NULL OR decision_ref = '')` term
    from the CAS WHERE → the UPDATE matches despite the foreign ref set in the
    gap, the row IS promoted to operational_context (its decision_ref overwritten
    with the witness hash), and this test goes RED (stamped non-empty, visibility
    operational, no toctou-changed skip)."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "casref-1", "original content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    row = dict(
        store._get_conn()
        .execute("SELECT * FROM hypomnema_entries WHERE id='casref-1'")
        .fetchone()
    )
    _append_rollout(journal, "hypomnema_entries", row)

    real_conn = store._get_conn()
    state = {"selects": 0, "fired": False}

    class _HookConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            is_row_select = (
                isinstance(sql, str)
                and sql.strip().startswith("SELECT * FROM hypomnema_entries")
                and a
                and a[0] == ("casref-1",)
            )
            if is_row_select:
                state["selects"] += 1
                # On the SECOND select (the under-lock re-read), the re-read sees
                # a CLEAN row (empty ref, content intact). Then set a FOREIGN
                # decision_ref so the re-read verified clean but the subsequent
                # UPDATE's CAS predicate no longer matches (a non-empty ref).
                # Same connection ⇒ BEGIN IMMEDIATE does not block this writer;
                # only the decision_ref CAS term catches it. Content is left
                # intact so the hash-column terms still match — isolating the
                # decision_ref vector.
                if state["selects"] == 2 and not state["fired"]:
                    state["fired"] = True
                    self._inner.execute(
                        "UPDATE hypomnema_entries SET decision_ref=? "
                        "WHERE id='casref-1'",
                        ("f" * 64,),
                    )
                    self._inner.commit()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig_get_conn = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev_resolve
        store._get_conn = orig_get_conn  # type: ignore[assignment]

    assert state["fired"] is True, (
        "013o: the under-lock re-read select never ran twice — the CAS window "
        "was not exercised (test scaffolding broke): %r" % state
    )
    # THE PROOF: the row whose ref was hijacked AFTER the re-read but BEFORE the
    # UPDATE is not promoted — only the decision_ref CAS term catches this.
    assert res["stamped"] == [], (
        "013o: a row whose decision_ref was set between the under-lock re-read "
        "and the UPDATE was stamped — the CAS did not bind decision_ref "
        "(incomplete predicate): %r" % (res,)
    )
    assert any("casref-1:toctou-changed" in s for s in res["skipped"]), (
        "013o: the mid-gap decision_ref set must be reported as toctou-changed "
        "(cas-miss): %r" % (res,)
    )
    r = real_conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='casref-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "013o: the CAS-caught row was force-promoted — a foreign ref set in the "
        "gap was overwritten with the witness hash and the row promoted"
    )
    assert r[1] == "f" * 64, (
        "013o: the row's mid-gap foreign ref must be left standing (the promote "
        "must not have overwritten it)"
    )


# ── 013o — reconcile stale-ref symmetry with apply ──


def test_013o_reconcile_stale_ref_on_review_only_row_is_not_silent(
    tmp_path, monkeypatch
):
    """013o (reconcile stale-ref symmetry): a review_only fallback row that fully
    content-verifies but carries a NON-EMPTY foreign decision_ref must emit a
    distinct stale-ref finding — NOT return silently through the pending-apply
    quiet branch. This is the apply/reconcile symmetry the 013m apply-side fix
    exposed: apply's `_rollout_skip_reason` now codes this shape 'stale-ref', but
    reconcile's initial-rollout pending-apply quiet branch did not require the
    row be ref-less, so a foreign-ref-carrying row stayed quiet.

    Setup: witness a review_only row via the TCB (the line binds its content
    hash), then stamp a FOREIGN decision_ref onto the row (content + review_only
    unchanged). At reconcile, `SELECT ... WHERE decision_ref = <line hash>`
    misses the row (its ref differs), so it falls to the fallback-by-id path with
    a non-empty row_ref and fully-verifying content — exactly the stale-ref
    shape. reconcile must report it distinctly and NOT promote/restore.

    MUTATION TARGET: revert the quiet-branch condition to the pre-013o form
    (drop `and row_ref` from the new stale-ref branch AND `and not row_ref` from
    the pending-apply quiet branch, i.e. the branch fires on
    review_only+hidden regardless of ref) → the foreign-ref row falls through the
    quiet branch and returns SILENTLY, no stale-ref finding is emitted, and this
    test goes RED."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "recstale-1", "curated identity content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    # Ceremony: witness the review_only row (line binds its content hash).
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    # Stamp a FOREIGN (non-matching) decision_ref onto the still-review_only row.
    # Content is unchanged, so it still verifies against its witness line — but
    # its ref differs from the line's hash, so Direction B's ref-keyed SELECT
    # misses it and the fallback-by-id path sees a non-empty row_ref.
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET decision_ref=? WHERE id='recstale-1'",
        ("a" * 64,),
    )
    conn.commit()

    _arm_vault(monkeypatch, journal)
    report = _reconcile(store, journal)

    stale = [
        f for f in report.findings
        if f["kind"] == "witnessed_row_stale_ref" and f["row_id"] == "recstale-1"
    ]
    assert stale and stale[0]["severity"] == "high", (
        "013o: a review_only row with a foreign non-empty decision_ref that "
        "verifies must emit a distinct high-severity stale-ref finding, not stay "
        "quiet: %s" % report.findings
    )
    # No promote, no restore — the row is left review_only with its foreign ref.
    assert report.requarantined == [], report.requarantined
    row = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='recstale-1'"
    ).fetchone()
    assert row[0] == "review_only", (
        "013o: reconcile must not promote a stale-ref pending row"
    )
    assert row[1] == "a" * 64, (
        "013o: reconcile must not overwrite the row's foreign ref (no restore)"
    )


def test_013o_reconcile_refless_pending_row_stays_quiet(tmp_path, monkeypatch):
    """013o boundary: the LEGITIMATE pending-apply state (review_only, EMPTY ref,
    content verifying) must STILL stay quiet after the stale-ref branch is added
    — the new `and not row_ref` guard on the quiet branch must not over-fire on
    the ref-less case. Guards against the stale-ref branch or the tightened quiet
    condition breaking the 013e A-1 false-alarm-storm suppression."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "refless-1", "not yet applied", "review_only")
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb(store.db_path, journal, "y\n", args=["--initial-rollout"])
    assert result.returncode == 0, result.stderr
    # NO apply, NO foreign ref — the genuine pending-apply watchdog window.
    _arm_vault(monkeypatch, journal)
    report = _reconcile(store, journal)
    assert report.findings == [], (
        "013o: ref-less pending-apply row flagged — the stale-ref branch or the "
        "tightened quiet condition over-fired on the legitimate empty-ref "
        "pending state: %s" % report.findings
    )
    assert report.requarantined == [], report.requarantined
    row = store._get_conn().execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='refless-1'"
    ).fetchone()
    assert row[0] == "review_only" and not (row[1] or ""), (
        "013o: reconcile must leave the ref-less pending row untouched"
    )


# ── 013r F1 — the verification predicate must include LIFECYCLE ──


def test_013r_deactivated_row_not_promoted(tmp_path):
    """013r F1 (re-read, active): canonical_row_sha256 binds neither `active` nor
    `superseded_by`, so a row deactivated AFTER its witness line still
    content-verifies, still reads review_only, and — before this fix — was
    force-promoted operational, resurrecting a lifecycle-hidden identity row.
    The re-read must reject a not-live row with `lifecycle-hidden`.

    MUTATION TARGET: drop the `active != 1` term from `_rollout_skip_reason`
    (and the CAS `active = 1`) → the deactivated row IS promoted (stamped
    non-empty, visibility operational, no lifecycle-hidden skip) → RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "dead-1", "deactivated after witness",
                            "review_only")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    conn = store._get_conn()
    # Row deactivated AFTER the witness, BEFORE the apply — content unchanged, so
    # the hash still matches; only a lifecycle check can catch it.
    conn.execute("UPDATE hypomnema_entries SET active=0 WHERE id='dead-1'")
    conn.commit()
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "dead-1:lifecycle-hidden" in res["skipped"], res
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='dead-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "F1: a deactivated row was force-promoted — lifecycle-hidden identity "
        "row resurrected"
    )
    assert not (r[1] or ""), "F1: a lifecycle-hidden row must not be stamped"


def test_013r_superseded_row_not_promoted(tmp_path):
    """013r F1 (re-read, superseded_by): a row superseded AFTER its witness line
    still content-verifies and reads review_only; it must be rejected
    `lifecycle-hidden`. Covers the OTHER lifecycle column and the surface that
    beliefs shares (beliefs has no `active`, so superseded_by is its only
    liveness signal).

    MUTATION TARGET: drop the `superseded_by IS NOT NULL` term from
    `_rollout_skip_reason` (and the CAS `superseded_by IS NULL`) → the superseded
    row IS promoted → RED."""
    store = _store(tmp_path)
    row = _insert_hypomnema(store, "old-1", "superseded after witness",
                            "review_only")
    # A live successor row the supersede points at (FK REFERENCES hypomnema).
    _insert_hypomnema(store, "new-1", "the successor", "operational_context")
    journal = tmp_path / "decisions.jsonl"
    _append_rollout(journal, "hypomnema_entries", row)
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET superseded_by='new-1' WHERE id='old-1'"
    )
    conn.commit()
    res = _apply_rollout(store, journal)
    assert res["stamped"] == [], res
    assert "old-1:lifecycle-hidden" in res["skipped"], res
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='old-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "F1: a superseded row was force-promoted"
    )
    assert not (r[1] or ""), "F1: a superseded row must not be stamped"


def test_013r_cas_catches_deactivation_between_reread_and_update(tmp_path):
    """013r F1 (CAS half): the under-lock re-read verifies a LIVE row, then a
    same-connection writer deactivates it in the re-read→UPDATE gap (the shared
    check_same_thread=False connection ⇒ BEGIN IMMEDIATE does not block it). Only
    the CAS lifecycle term (`active = 1`) folded into the UPDATE WHERE catches
    the ordering: rowcount 0 → toctou-changed, NOT a promote. Mirrors the 013o
    decision_ref CAS test at the lifecycle column.

    MUTATION TARGET: drop the CAS `active = 1` term → the deactivated row is
    promoted operational with the witness ref despite going lifecycle-hidden in
    the gap (stamped non-empty, no toctou-changed) → RED."""
    store = _store(tmp_path)
    _insert_hypomnema(store, "casdead-1", "original content", "review_only")
    journal = tmp_path / "decisions.jsonl"
    row = dict(
        store._get_conn()
        .execute("SELECT * FROM hypomnema_entries WHERE id='casdead-1'")
        .fetchone()
    )
    _append_rollout(journal, "hypomnema_entries", row)

    real_conn = store._get_conn()
    state = {"selects": 0, "fired": False}

    class _HookConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            is_row_select = (
                isinstance(sql, str)
                and sql.strip().startswith("SELECT * FROM hypomnema_entries")
                and a
                and a[0] == ("casdead-1",)
            )
            if is_row_select:
                state["selects"] += 1
                # On the SECOND select (the under-lock re-read) the row is still
                # live and verifies clean; deactivate it AFTER the re-read so it
                # lands squarely in the re-read→UPDATE gap. Only the CAS lifecycle
                # term catches it.
                if state["selects"] == 2 and not state["fired"]:
                    state["fired"] = True
                    self._inner.execute(
                        "UPDATE hypomnema_entries SET active=0 "
                        "WHERE id='casdead-1'"
                    )
                    self._inner.commit()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig_get_conn = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    prev_resolve = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_initial_rollout()
    finally:
        _sq.resolve_vault_journal_path = prev_resolve
        store._get_conn = orig_get_conn  # type: ignore[assignment]

    assert state["fired"] is True, (
        "F1 CAS: the under-lock re-read select never ran twice — the CAS window "
        "was not exercised (test scaffolding broke): %r" % state
    )
    assert res["stamped"] == [], (
        "F1 CAS: a row deactivated between the under-lock re-read and the UPDATE "
        "was stamped — the CAS lifecycle term did not close the window: %r"
        % (res,)
    )
    assert any("casdead-1:toctou-changed" in s for s in res["skipped"]), (
        "F1 CAS: the mid-gap deactivation must be reported as toctou-changed: %r"
        % (res,)
    )
    r = real_conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries "
        "WHERE id='casdead-1'"
    ).fetchone()
    assert r[0] == "review_only", (
        "F1 CAS: the CAS-caught deactivated row was force-promoted"
    )
    assert not (r[1] or ""), "F1 CAS: a CAS-caught row must not be stamped"


# ── 013r F2 — the restamp snapshot must be a backup OF THE TARGET ──


def _make_target_and_backup(tmp_path):
    """Build a Mnemos target DB with some imported hypomnema state, then a TRUE
    fresh backup of it via sqlite3's online backup API (== David's `.backup`)."""
    import sqlite3 as _sqlite3

    db = tmp_path / "target.db"
    store = EngramStore(db, vault_active=True)
    # Give the target non-trivial, matched state in both parity tables.
    _insert_hypomnema(store, "t-1", "one", "review_only")
    _insert_hypomnema(store, "t-2", "two", "review_only")
    store.close()

    backup = tmp_path / "backup.db"
    src = _sqlite3.connect(str(db))
    try:
        dst = _sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return db, backup


def test_013r_stale_snapshot_refused(tmp_path):
    """013r F2: a Mnemos-shaped snapshot that passes every A-5 guard (distinct
    inode, SQLite header, non-trivial, integrity_check ok, core tables present)
    but is NOT a backup of THE TARGET — its parity fingerprint diverges — must be
    REFUSED. Rolling back to it would be illusory.

    MUTATION TARGET: remove the `_snapshot_parity_refusal` call in main() → the
    stale snapshot passes and --execute proceeds (returncode 0/1, no 'not a
    backup of the target' refusal) → RED."""
    db, backup = _make_target_and_backup(tmp_path)
    # Now the TARGET drifts past the snapshot: add another mapped hypomnema so the
    # target's pre-write counts no longer match the backup. The backup is a
    # genuine, valid Mnemos DB — only the parity binding catches the drift.
    store = EngramStore(db, vault_active=True)
    _insert_hypomnema(store, "t-3", "three (target drifted past snapshot)",
                      "review_only")
    store.close()

    result = _run_restamp([str(db), "--execute", "--snapshot", str(backup)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "not a backup of the target" in result.stderr, result.stderr
    assert "hypomnema_entries" in result.stderr, result.stderr


def test_013r_true_backup_of_target_passes_parity(tmp_path):
    """013r F2 boundary: a TRUE fresh backup of the target (matching
    schema_version + mapped-table counts) must PASS the parity gate — the check
    must not over-fire on the legitimate snapshot-first case David actually runs.
    It proceeds past the snapshot guards into execute()."""
    db, backup = _make_target_and_backup(tmp_path)
    result = _run_restamp([str(db), "--execute", "--snapshot", str(backup)])
    # Past the parity gate: no snapshot refusal in stderr, and it reached the
    # EXECUTE banner (whatever the disposition outcome, the snapshot was accepted).
    assert "not a backup of the target" not in result.stderr, result.stderr
    assert "REFUSING: snapshot" not in result.stderr, result.stderr
    assert "DAVID-10 restamp: EXECUTE" in result.stdout, (
        result.stdout, result.stderr
    )


# ── 013t F2 — count-parity collides; the binding check is a CONTENT DIGEST ──


def test_013t_same_count_different_content_snapshot_refused(tmp_path):
    """013t F2 (weak-snapshot-parity, error): a snapshot with the SAME row
    counts as the target — same schema_version, same pai_import_row_map count,
    same hypomnema_entries count — but DIFFERENT content in a column the restamp
    reads/writes must be REFUSED. Counts collide; only a content digest catches
    it, so accepting the count-equal-but-content-different snapshot would make
    David's rollback illusory.

    MUTATION TARGET: drop the `_content_digest` comparison in
    `_snapshot_parity_refusal` (keep only the count-signal diffs) → this
    stale-content snapshot passes the parity gate (no 'not a backup' refusal,
    --execute proceeds) → RED. The true-backup boundary test above stays GREEN,
    proving the digest is content-precise, not over-firing."""
    import sqlite3 as _sqlite3

    db, backup = _make_target_and_backup(tmp_path)
    # Drift the TARGET in place WITHOUT changing any count: flip one existing
    # row's read_visibility (a column the restamp reads/writes). The backup now
    # has identical schema_version + identical row counts in both parity tables,
    # but its content of hypomnema_entries diverges. Count-only parity is blind
    # to this; the content digest is not.
    conn = _sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE hypomnema_entries SET read_visibility = 'operational_context' "
            "WHERE id = 't-1'"
        )
        conn.commit()
        # Sanity: counts across both parity tables are unchanged vs the backup.
        bak = _sqlite3.connect(str(backup))
        try:
            for table in ("hypomnema_entries", "pai_import_row_map"):
                n_tgt = conn.execute(
                    "SELECT COUNT(*) FROM %s" % table
                ).fetchone()[0]
                n_bak = bak.execute(
                    "SELECT COUNT(*) FROM %s" % table
                ).fetchone()[0]
                assert n_tgt == n_bak, (
                    "fixture invalid: %s count drifted (%d vs %d); the test must "
                    "hold counts equal so ONLY the digest can catch the drift"
                    % (table, n_tgt, n_bak)
                )
        finally:
            bak.close()
    finally:
        conn.close()

    result = _run_restamp([str(db), "--execute", "--snapshot", str(backup)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "not a backup of the target" in result.stderr, result.stderr
    assert "content_digest" in result.stderr, result.stderr


def test_013t_snapshot_missing_meta_table_refused_not_traceback(tmp_path):
    """013t F2 (missing-meta-shape-guard, warning): a Mnemos-shaped snapshot
    with hypomnema_entries + pai_import_row_map but NO `meta` table must produce
    a CONTROLLED refusal, not an uncaught OperationalError. `_parity_signals`
    reads `meta` unconditionally, so without the shape guard including `meta`,
    the snapshot passes the shape check and then raises a raw traceback when the
    parity read hits the missing table.

    MUTATION TARGET: remove `meta` from the required-table set in the shape
    guard → this meta-less snapshot surfaces the raw sqlite3.OperationalError
    (no clean 'missing required Mnemos tables: meta' refusal) → RED."""
    import sqlite3 as _sqlite3

    db = tmp_path / "target.db"
    EngramStore(db, vault_active=True).close()
    snap = tmp_path / "no_meta.db"
    conn = _sqlite3.connect(str(snap))
    try:
        # Both data tables present + non-trivial rows (passes size/header/
        # integrity/core-tables A-5 guards), but deliberately NO `meta` table.
        conn.execute("CREATE TABLE hypomnema_entries (id TEXT, content TEXT)")
        conn.execute(
            "CREATE TABLE pai_import_row_map (target_id TEXT, source_path TEXT, "
            "target_table TEXT)"
        )
        conn.executemany(
            "INSERT INTO hypomnema_entries VALUES (?, ?)",
            [(f"r{i}", "x" * 64) for i in range(64)],
        )
        conn.commit()
    finally:
        conn.close()

    result = _run_restamp([str(db), "--execute", "--snapshot", str(snap)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "missing required Mnemos tables" in result.stderr, result.stderr
    assert "meta" in result.stderr, result.stderr
    # It must be a CLEAN refusal, never a raw traceback / OperationalError.
    assert "Traceback" not in result.stderr, result.stderr
    assert "OperationalError" not in result.stderr, result.stderr


# ── 013v F1 — the snapshot is a FULL-DB rollback: the digest must cover EVERY
#    user table, not just the restamp-touched columns ──


def _insert_belief_row(db_path, bid="drift-belief"):
    """Insert a beliefs row directly — a table the restamp NEVER touches. A
    full-DB rollback (`.backup`) restores it too, so a valid backup must match on
    it; the restamp-columns-only digest (013t) was blind to it."""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO beliefs (id, agent_id, content, confidence, domain, "
            " created_at, last_revised, last_challenged, tier, read_visibility, "
            " needs_review, confidence_pending_review) VALUES "
            "(?, 'oliver', 'a belief that only lives in the target', 0.9, "
            " 'identity', 't', 't', 't', 'operational', 'review_only', 0, 0)",
            (bid,),
        )
        conn.commit()
    finally:
        conn.close()


def test_013v_snapshot_diverging_in_non_restamp_table_refused(tmp_path):
    """013v F1 (incomplete-snapshot-digest, error): the snapshot is a FULL-DB
    rollback — `sqlite3 .backup` restores the WHOLE file — so a valid backup must
    match the target on ALL user data, not only the columns the restamp touches.
    The 013t digest hashed only restamp columns of two tables, so a snapshot that
    diverged in a NON-restamp table (a beliefs row here; equally an engrams row or
    a non-restamp hypomnema column) was accepted, and David's rollback would be
    illusory for everything outside the restamp's footprint.

    Here the target drifts past the backup by gaining a `beliefs` row — a table
    the restamp never reads or writes. The restamp-columns digest is identical;
    only the full-DB digest diverges. Must be REFUSED.

    MUTATION TARGET: revert `_content_digest` to the 013t restamp-columns-only
    form (hypomnema id/domain/foundational/read_visibility/decision_ref +
    row-map) → this beliefs-only divergence produces an IDENTICAL digest, the
    snapshot passes the parity gate, and --execute proceeds (no 'not a backup'
    refusal) → RED. The true-backup boundary test below stays GREEN, proving the
    full-DB digest is content-precise, not over-firing."""
    db, backup = _make_target_and_backup(tmp_path)
    # Target drifts in a table the restamp NEVER touches; counts in the two
    # restamp tables are unchanged, so the restamp-columns digest cannot see it.
    _insert_belief_row(db)

    result = _run_restamp([str(db), "--execute", "--snapshot", str(backup)])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "not a backup of the target" in result.stderr, result.stderr
    assert "content_digest" in result.stderr, result.stderr


def test_013v_full_db_true_backup_still_passes(tmp_path):
    """013v F1 boundary: a TRUE full-DB backup (identical in EVERY user table,
    including beliefs/engrams and the FTS shadow tables) must still PASS the
    expanded digest — the full-table hash must not over-fire on the legitimate
    snapshot-first case David runs. It reaches EXECUTE past the parity gate."""
    db, backup = _make_target_and_backup(tmp_path)
    # Non-trivial state in a non-restamp table, captured in BOTH via a fresh
    # backup, so the full-DB digest has real cross-table content to agree on.
    _insert_belief_row(db, "shared-belief")
    import sqlite3 as _sqlite3

    src = _sqlite3.connect(str(db))
    try:
        dst = _sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    result = _run_restamp([str(db), "--execute", "--snapshot", str(backup)])
    assert "not a backup of the target" not in result.stderr, result.stderr
    assert "REFUSING: snapshot" not in result.stderr, result.stderr
    assert "DAVID-10 restamp: EXECUTE" in result.stdout, (
        result.stdout, result.stderr
    )


# ── 013v F2 — the snapshot-vs-target parity compare must run UNDER the target
#    write lock, not before it (TOCTOU) ──


def test_013v_parity_recompute_is_under_the_write_lock(tmp_path):
    """013v F2 (snapshot-parity-toctou, error): the parity check must compare the
    snapshot against the target's state read UNDER `BEGIN IMMEDIATE`, not a
    pre-lock read. In the pre-lock form, a concurrent writer could modify the
    target between the parity read and the transaction, and the restamp would run
    against state the parity gate never saw.

    This exercises the window directly at the function level: the snapshot's
    fingerprint is captured when the target still matches it (David's snapshot-
    first moment), THEN a concurrent writer mutates the target, THEN `execute()`
    runs. Because `execute()` recomputes the target digest under the lock, it sees
    the post-snapshot mutation and REFUSES (raises SnapshotParityError, rolls
    back, writes nothing).

    MUTATION TARGET: remove the under-lock recompute — compare the snapshot
    against a target digest read BEFORE `BEGIN IMMEDIATE` (the pre-lock form) →
    the pre-lock read matches the snapshot, the mutation lands after it, parity
    passes, and the restamp COMMITS against unseen state → no SnapshotParityError
    is raised and the tampered row count changes → RED."""
    import sqlite3 as _sqlite3

    mod = _restamp_module()
    db, backup = _make_target_and_backup(tmp_path)

    # 1. Snapshot-first moment: target == backup. Capture the snapshot's
    #    fingerprint exactly as main() does (read-only, pre-lock).
    snap_sig, snap_digest = mod._snapshot_parity_fingerprint(str(backup))

    # 2. A concurrent in-process writer lands on the target AFTER the snapshot
    #    read but BEFORE the restamp transaction — the TOCTOU adversary. Adding a
    #    mapped hypomnema row changes both the counts and the full-DB digest.
    store = EngramStore(db, vault_active=True)
    _insert_hypomnema(store, "toctou-late", "arrived after the snapshot read",
                      "review_only")
    store.close()

    conn = _sqlite3.connect(str(db))
    try:
        rows_before = conn.execute(
            "SELECT COUNT(*) FROM hypomnema_entries"
        ).fetchone()[0]
        # 3. execute() recomputes the target digest UNDER the lock and must
        #    refuse — the mutation is inside the window the fix closes.
        import pytest

        with pytest.raises(mod.SnapshotParityError) as exc:
            mod.execute(conn, str(backup), snap_sig, snap_digest)
        assert "not a backup of the target" in str(exc.value), exc.value
        # 4. Nothing was written: the transaction rolled back, no restamp ran.
        rows_after = conn.execute(
            "SELECT COUNT(*) FROM hypomnema_entries"
        ).fetchone()[0]
        assert rows_after == rows_before, (
            "013v F2: rows changed despite parity refusal — the abort did not "
            "roll back cleanly"
        )
        stamped = conn.execute(
            "SELECT COUNT(*) FROM hypomnema_entries "
            "WHERE decision_ref IS NOT NULL AND TRIM(decision_ref) != ''"
        ).fetchone()[0]
        assert stamped == 0, (
            "013v F2: a restamp committed under a failed parity check (TOCTOU "
            "still open)"
        )
    finally:
        conn.close()
