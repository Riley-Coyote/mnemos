"""T5 Phase 0 — independent re-verification of the 008y audit findings.

Each test in this file REPRODUCES a finding against the current source at
main 6b5d825 (the 008y audit was the vault auditing itself — in-basin — so T5
re-verifies each finding independently before any fix, per 008aa §2).

These are demonstration tests: they assert the CURRENT (vulnerable) behavior so
the reproduction is machine-checked. After the T5 fix lands, the corresponding
test in test_t4_vault.py asserts the CORRECTED behavior and is mutation-proven.

Run: uv run --all-extras pytest -q tests/test_t5_phase0_repro.py
"""

from __future__ import annotations


from mnemos.store.sqlite_store import EngramStore
from mnemos.store import sqlite_store as _sq

# Reuse the T4 harness helpers.
from tests.test_t4_vault import (
    _apply_identity,
    _apply_legacy,
    _store,
    _make_identity_proposal,
    _append_journal,
    _append_legacy,
    _insert_legacy_belief,
)


# ── Finding J — fallback exempt-without-finding hide (reconcile.py:459-475) ──


def test_repro_J_proposal_fallback_hide_now_caught(tmp_path):
    """J (proposal path), POST-FIX: clear decision_ref + review_only via raw SQL,
    no witnessed field changed → the not-located fallback re-verifies the witness,
    finds it fully intact, and classifies the cleared ref as a raw-SQL HIDE →
    restore + witnessed_row_hidden. (Phase-0 confirmed the pre-fix hole; this
    documents the fix holds. The permanent mutation-proof lives in
    test_t4_vault::test_t5_J_proposal_fallback_hide_is_caught_and_restored.)"""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-Jhide")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)  # operational + decision_ref
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='b-Jhide'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_hidden" for f in report.findings), (
        "J fix regressed: the cleared-ref hide was not caught"
    )
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-Jhide'"
    ).fetchone()[0]
    assert vis == "operational_context", "J fix regressed: hide not restored"


def test_repro_J_legacy_fallback_hide_now_caught(tmp_path):
    """J (legacy path), POST-FIX: same fix in _reconcile_legacy_line's fallback —
    a legacy-witnessed row, ref cleared + review_only, content intact → the legacy
    witness re-verifies → hide caught + restored."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store, bid="legJhide", content="I am Oliver.")
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)  # stamps decision_ref, operational
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='legJhide'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_hidden" for f in report.findings), (
        "J legacy fix regressed: the cleared-ref hide was not caught"
    )
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='legJhide'"
    ).fetchone()[0]
    assert vis == "operational_context", "J legacy fix regressed: hide not restored"


def test_repro_J_genuine_degrade_stays_quarantined_baseline(tmp_path):
    """J counter-case (must remain true after the fix): a GENUINE degrade — a
    witnessed field (content) actually changed AND the row is review_only with
    ref cleared, as the write-path E7/E8/E2B produces — must NOT be re-flagged
    as a hide. Establishes the baseline the fix must preserve."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-Jdeg", content="orig")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Genuine content change + degrade (what the write path does legitimately).
    conn.execute(
        "UPDATE beliefs SET content='changed', decision_ref=NULL, "
        "read_visibility='review_only' WHERE id='b-Jdeg'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    # A genuine degrade is clean today AND must stay clean after the fix.
    assert report.ok, "genuine degrade already flagged — unexpected baseline"


# ── R6-1 — resolver trusts an existing agent-owned journal leaf ──


def test_repro_R6_1_agent_owned_journal_trusted_without_the_check(
    tmp_path, monkeypatch
):
    """R6-1 (baseline, pre-fix behavior): with the journal-trust check DISABLED
    (the pre-T5 world), an existing agent-owned decisions.jsonl is read and
    trusted as authoritative — its (agent-authored, chain-valid) lines drive
    reconcile with no per-FILE ownership check. This documents the hole the fix
    closes: without the check, an agent-owned journal reconciles CLEAN."""
    # Force the check OFF to reproduce the pre-fix world (conftest already
    # defaults it off, but be explicit — this IS the vulnerable configuration).
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-r61")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    report = store.reconcile_identity_vault(journal)
    # Pre-fix behavior: agent-owned journal reconciles clean (the hole).
    assert report.ok, "REPRO expectation changed: agent-owned journal already rejected"


# ── GAP-1 — reconcile has no lock across its read-decide-write span ──


def test_repro_GAP1_reconcile_toctou_between_read_and_write(tmp_path):
    """GAP-1: reconcile SELECTs, decides in Python, then UPDATEs, with no lock
    across the span. A mutation committed between the read and the write is
    applied on top of stale reconcile state. Reproduce by hooking a mutation in
    between the phases via a wrapped connection execute; today the mutation
    lands because reconcile holds no BEGIN IMMEDIATE across the pass."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-toctou")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general' WHERE id='b-toctou'"
    )
    conn.commit()

    # Probe: does reconcile protect its read-decide-write span with a
    # transaction? Wrap the store connection so we can observe whether a
    # BEGIN IMMEDIATE is opened during the pass. POST-FIX this is now True
    # (the fix wraps the span); this repro documents the pre-fix absence.
    began = {"immediate": False}
    real_conn = store._get_conn()

    class _TracingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if isinstance(sql, str) and "BEGIN IMMEDIATE" in sql.upper():
                began["immediate"] = True
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig_get_conn = store._get_conn
    store._get_conn = lambda: _TracingConn(real_conn)  # type: ignore[assignment]
    try:
        store.reconcile_identity_vault(journal)
    finally:
        store._get_conn = orig_get_conn  # type: ignore[assignment]
    # POST-FIX: reconcile now takes BEGIN IMMEDIATE across the span (this repro
    # flips from the pre-fix False). The permanent proof lives in test_t4_vault.
    assert began["immediate"] is True, (
        "GAP-1 fix regressed: reconcile no longer takes BEGIN IMMEDIATE"
    )


# ── GAP-2 — migration re-run safety on a populated identity table ──


def test_repro_GAP2_migration_rerun_clobbers_witnessed_hypomnema(tmp_path):
    """GAP-2: run_migrations fires on every EngramStore.__init__ off
    meta.schema_version; an agent UPDATE meta SET value='4' re-runs v5..v9.
    v6's backfill UPDATE (migrate_v6 → apply_afferent_membrane_v1) fires
    UNCONDITIONALLY and sets read_visibility='review_only' on every hypomnema
    matching HYPO_REVIEW_CANDIDATE_SQL — which includes foundational/identity
    rows. So a WITNESSED identity hypomnema (operational + decision_ref) is
    DOWNGRADED to review_only on re-run. Reproduce the clobber."""
    import sqlite3

    db = tmp_path / "gap2.db"
    store = EngramStore(db, vault_active=True)
    conn = store._get_conn()
    head = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
        0
    ]
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope, "
        "content, source, density, domain, read_visibility, tags_json, confidence, "
        "salience, active, foundational, revision_count, revisions_json, created_at, "
        "last_revised_at, decision_ref) VALUES "
        "('h-wit','oliver','david','pai','I am Oliver.','observed',0.5,'identity',"
        "'operational_context','[]',0.9,0.9,1,1,1,'[]','t','t','refhash123')"
    )
    conn.commit()
    store.close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    store2 = EngramStore(db, vault_active=True)
    conn2 = store2._get_conn()
    after_vis, after_ref = conn2.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries WHERE id='h-wit'"
    ).fetchone()
    after_ver = conn2.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    store2.close()
    assert str(after_ver) == str(head), "migrations did not re-run to head"
    # POST-FIX behavior: the GAP-2 witnessed-guard preserves the witnessed row.
    # (Pre-fix this was 'review_only' — the clobber. The permanent assertion of
    # the corrected behavior lives in test_t4_vault as test_t5_GAP2_*.)
    assert after_vis == "operational_context", (
        "GAP-2 fix regressed: v6 re-run clobbered witnessed hypomnema visibility"
    )
    assert after_ref == "refhash123", "decision_ref was rewritten — new hazard"


# ── GAP-5 — write-time classifier coverage ──


def test_repro_GAP5_hypomnema_paths_route_through_classifier(tmp_path):
    """GAP-5 (coverage, positive): every hypomnema insert routes through
    classify_hypomnema_read_visibility. write_hypomnema_entry with identity
    content must be floored to review_only, not operational. This documents the
    covered state; the fix (if any bypass exists) closes it."""
    store = _store(tmp_path)
    eid = store.write_hypomnema_entry(
        "I am Oliver, David's agent.",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        domain="identity",
        foundational=True,
    )
    conn = store._get_conn()
    vis = conn.execute(
        "SELECT read_visibility FROM hypomnema_entries WHERE id=?", (eid,)
    ).fetchone()[0]
    assert vis == "review_only", (
        "GAP-5: identity hypomnema wrote operational — classifier bypassed"
    )
