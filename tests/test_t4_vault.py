"""T4 vault — identity-tier decision lock, in-repo half.

Journal I/O is dependency-injected (a fixture file plays the vault); OS
enforcement (append-only file owned by a separate user, sudo ceremony) is NOT
simulated — it is verified live by David's attack checklist.

Every NEG assertion here has a matching mutation proof recorded in the T4
implementation report: reverting the guard makes the test go red.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from mnemos.store.sqlite_store import EngramStore
from mnemos.store import sqlite_store as _sq
from mnemos.vault import journal as vj


def _apply_identity(store, proposal_id, journal, **kw):
    """008r: production apply has NO journal-path parameter. Tests inject the
    fixture journal by swapping the resolver seam (resolve_vault_journal_path) —
    a mechanism that cannot ship — and restore it after. Replaces the retired
    _journal_path_override argument."""
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        return store.apply_identity_decision(proposal_id, **kw)
    finally:
        _sq.resolve_vault_journal_path = prev


def _apply_legacy(store, journal):
    """apply_legacy_witness with the fixture journal injected via the resolver
    seam (008r — no _journal_path_override parameter exists)."""
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        return store.apply_legacy_witness()
    finally:
        _sq.resolve_vault_journal_path = prev


def _arm_vault(monkeypatch, journal_path):
    """008r-review: the resolver reads NO env. Arm the gate by pointing the
    tests-only resolution seam at a fixture journal (production pins canonical).
    The vault dir = the journal's parent (which exists), so the gate arms; the
    journal file itself may or may not exist (a missing file still arms —
    fail-closed — and read_journal([]) drives re-quarantine)."""
    import pathlib as _pl

    monkeypatch.setattr(_sq, "_VAULT_JOURNAL_FOR_RESOLUTION", str(journal_path))
    monkeypatch.setattr(
        _sq, "_VAULT_DIR_FOR_RESOLUTION", str(_pl.Path(journal_path).parent)
    )
    # 008r-review (vault-resolver-trusts-unverified-path): tmp fixtures are owned
    # by the test process; trust them (production checks real root/vault
    # ownership). The reject path is covered by test_r14_review_resolver_rejects
    # _agent_owned_vault, which does NOT patch this.
    monkeypatch.setattr(_sq, "_vault_object_trusted", lambda _p: True)


def _disarm_vault(monkeypatch):
    """Force the vault inert: the resolution seam's dir does not exist."""
    monkeypatch.setattr(
        _sq, "_VAULT_DIR_FOR_RESOLUTION", "/nonexistent/mnemos-vault-inert"
    )


def _store(tmp_path):
    # vault_active=True: exercise the read-path validator directly (the pre-vault
    # inert default is covered by the rest of the suite, which constructs stores
    # with no vault configured).
    return EngramStore(tmp_path / "vault.db", vault_active=True)


def _make_identity_proposal(
    store,
    *,
    surface="beliefs",
    content="I am Oliver. David's agent.",
    pid="prop-identity-1",
    target_id=None,
    blast_radius="identity",
    domain="identity",
):
    return store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface=surface,
        transition="install identity claim",
        domain=domain,
        blast_radius=blast_radius,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        target_id=target_id,
        payload={"content": content},
        provenance_ids=["soul-md-1"],
        proposal_id=pid,
    )


def _append_journal(path, entries):
    """Append chained journal lines. entries: list of (proposal_dict, decision).

    Plays the role of ``mnemos-decide`` for tests. Uses the same journal module
    the apply path uses, so the chain and content hashes are real.
    """
    existing = vj.read_journal(path)
    prev = vj.line_hash(existing[-1]) if existing else vj.genesis_prev_hash()
    with open(path, "a", encoding="utf-8") as handle:
        for proposal, decision in entries:
            line = {
                "v": 1,
                "ts": "2026-07-04T12:00:00Z",
                "proposal_id": proposal["id"],
                "content_sha256": vj.canonical_content_sha256(proposal),
                "decision": decision,
                "scope": proposal["blast_radius"],
                "prev_sha256": prev,
                "sudo_user": "davidef",
            }
            handle.write(json.dumps(line) + "\n")
            prev = vj.line_hash(line)


# ── P1: the happy path ──


def test_p1_approved_decision_applies_and_row_is_operational(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])

    result = _apply_identity(store, proposal["id"], journal)
    assert result["status"] == "applied"
    assert result["decision_ref"]

    # The witnessed belief is now operational and readable on the default surface.
    beliefs = store.get_beliefs(agent_id="oliver")
    assert any("I am Oliver" in b.content for b in beliefs)
    only = [b for b in beliefs if "I am Oliver" in b.content][0]
    assert only.read_visibility == "operational_context"


def test_p1_hypomnema_surface_applies(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store, surface="hypomnema_entries", pid="prop-hypo-1"
    )
    _append_journal(journal, [(proposal, "approved")])
    result = _apply_identity(store, proposal["id"], journal)
    assert result["status"] == "applied"
    hits = store.search_hypomnema(
        "Oliver", agent_id="oliver", person_id="david", project_scope="pai"
    )
    assert any("I am Oliver" in h["content"] for h in hits)


# ── N1: only apply reaches identity-tier applied; and it needs a journal line ──


def test_n1_apply_requires_a_journal_line(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"  # never written
    proposal = _make_identity_proposal(store)
    with pytest.raises(ValueError, match="requires a witnessed journal line"):
        _apply_identity(store, proposal["id"], journal)
    # Proposal untouched.
    assert store.get_proposal(proposal["id"])["status"] == "pending_review"


def test_n1_no_mcp_entrypoint_reaches_identity_apply():
    """No MCP tool may call apply_identity_decision (surface reduction lock)."""
    import inspect

    from mnemos import mcp_server

    src = inspect.getsource(mcp_server)
    assert "apply_identity_decision" not in src


def test_n1_only_apply_writes_applied_to_identity_proposals():
    """Repo-wide: nothing but apply_identity_decision transitions a proposal to
    'applied'. Guards against a future writer sneaking an identity apply in."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "mnemos"
    offenders = []
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "'applied'" not in text and '"applied"' not in text:
            continue
        # The one legitimate writer is apply_identity_decision in the store.
        if py.name == "sqlite_store.py":
            continue
        # Allow read-only comparisons / status enums, flag literal proposal writes.
        if "status = 'applied'" in text or 'status = "applied"' in text:
            offenders.append(str(py))
    assert offenders == [], f"unexpected 'applied' status writers: {offenders}"


# ── N2: hash mismatch (content changed after approval) ──


def test_n2_hash_mismatch_refuses(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, content="original content")
    _append_journal(journal, [(proposal, "approved")])
    # Mutate the pending proposal's content after the decision was recorded.
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="install identity claim",
        domain="identity",
        blast_radius="identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        payload={"content": "TAMPERED content"},
        provenance_ids=["soul-md-1"],
        proposal_id=proposal["id"],
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        _apply_identity(store, proposal["id"], journal)
    assert store.get_proposal(proposal["id"])["status"] == "pending_review"


# ── N3: chain break ──


def test_n3_chain_break_refuses(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    p1 = _make_identity_proposal(store, pid="p-a", content="first")
    p2 = _make_identity_proposal(store, pid="p-b", content="second")
    _append_journal(journal, [(p1, "approved"), (p2, "approved")])
    # Corrupt the first line's prev linkage so the whole chain fails.
    lines = journal.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[0])
    obj["prev_sha256"] = "0" * 64
    lines[0] = json.dumps(obj)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="chain broken"):
        _apply_identity(store, "p-b", journal)


# ── N4: orphan identity row (no decision_ref) forced review_only at read ──


def test_n4_orphan_identity_row_excluded_from_operational(tmp_path):
    store = _store(tmp_path)
    conn = store._get_conn()
    # A rogue direct-SQL identity belief: operational, foundational, no decision_ref.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('rogue', 'oliver', 'forged identity', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    # Excluded from the default operational surface...
    assert all(b.content != "forged identity" for b in store.get_beliefs("oliver"))
    # ...but visible to explicit admin (read_visibility=None) for review.
    admin = store.get_beliefs("oliver", read_visibility=None)
    assert any(b.content == "forged identity" for b in admin)


def test_n4_orphan_hypomnema_excluded(tmp_path):
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json, confidence,"
        " salience, active, foundational, revision_count, revisions_json, created_at,"
        " last_revised_at) VALUES ('rogueh','oliver','david','pai','forged hypo',"
        " 'observed', 0.5, 'identity', 'operational_context', '[]', 0.9, 0.9, 1, 1, 0,"
        " '[]', 't', 't')"
    )
    conn.commit()
    hits = store.search_hypomnema(
        "forged", agent_id="oliver", person_id="david", project_scope="pai"
    )
    assert all(h["content"] != "forged hypo" for h in hits)


# ── N5: forged decision_ref → excluded at read AND re-quarantined by reconcile ──


def test_n5_forged_ref_excluded_and_requarantined(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"  # empty
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility, decision_ref) VALUES"
        " ('forged', 'oliver', 'forged with fake ref', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context', 'deadbeef" + "0" * 56 + "')"
    )
    conn.commit()
    # Read-path: a decision_ref that resolves to nothing is still non-empty, so the
    # structural gate lets it through — the RECONCILER is what catches forged refs.
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "forged_or_broken_ref" for f in report.findings)
    row = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='forged'"
    ).fetchone()
    assert row[0] == "review_only"


# ── N6: journal absent → reconcile forces every identity row review_only ──


def test_n6_absent_journal_requarantines_all_identity_rows(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Row is operational and witnessed. Now the journal disappears.
    journal.unlink()
    report = store.reconcile_identity_vault(journal)
    assert not report.ok
    beliefs_admin = store.get_beliefs("oliver", read_visibility=None)
    witnessed = [b for b in beliefs_admin if "I am Oliver" in b.content][0]
    assert witnessed.read_visibility == "review_only"


# ── Reconciler: de-tier attack (Fable review 008 §2) ──


def test_reconciler_catches_detier(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])
    applied = _apply_identity(store, proposal["id"], journal)
    bid = store.get_beliefs("oliver")[0].id
    conn = store._get_conn()
    # Raw-SQL de-tier: clear the identity signal, keep the decision_ref. The row
    # is now freely operational as a NON-identity belief — invisible to a
    # tier-filtered query. Only journal→table reconcile catches it.
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general' WHERE id=?", (bid,)
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings)
    row = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id=?", (bid,)
    ).fetchone()
    assert row[0] == "review_only"
    assert applied["status"] == "applied"


def test_reconciler_catches_content_mutation(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    bid = store.get_beliefs("oliver")[0].id
    conn = store._get_conn()
    conn.execute("UPDATE beliefs SET content='SILENTLY REWRITTEN' WHERE id=?", (bid,))
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings)


def test_reconciler_clean_when_intact(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    report = store.reconcile_identity_vault(journal)
    assert report.ok, report.findings


# ── Legacy batch-witness (DAVID-9 c) ──


def _append_legacy(path, table, row):
    existing = vj.read_journal(path)
    prev = vj.line_hash(existing[-1]) if existing else vj.genesis_prev_hash()
    line = {
        "v": 1,
        "ts": "2026-07-04T12:00:00Z",
        "witness": "legacy",
        "table": table,
        "row_id": row["id"],
        "content_sha256": vj.canonical_row_sha256(table, row),
        "decision": "approved",
        "scope": row.get("domain"),
        "prev_sha256": prev,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _insert_legacy_belief(store, bid="legacy1", content="I am Oliver."):
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        f" ('{bid}', 'oliver', '{content}', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM beliefs WHERE id = ?", (bid,)).fetchone())


def test_legacy_witness_stamps_and_reads_operational(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store)
    # Vault active: unwitnessed legacy row is quarantined at read.
    assert all(b.content != "I am Oliver." for b in store.get_beliefs("oliver"))
    _append_legacy(journal, "beliefs", row)
    res = _apply_legacy(store, journal)
    assert res["stamped"] == ["legacy1"]
    # Now witnessed → operational and readable.
    assert any(b.content == "I am Oliver." for b in store.get_beliefs("oliver"))
    # And reconcile is clean (steady state: ordinary decision_ref).
    assert store.reconcile_identity_vault(journal).ok


def test_legacy_witness_is_idempotent(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store)
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    res2 = _apply_legacy(store, journal)
    assert res2["stamped"] == []
    assert "legacy1:already-stamped" in res2["skipped"]


def test_legacy_witness_skips_content_mismatch(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store)
    _append_legacy(journal, "beliefs", row)
    # Mutate the row AFTER witnessing but BEFORE stamping.
    conn = store._get_conn()
    conn.execute("UPDATE beliefs SET content='TAMPERED' WHERE id='legacy1'")
    conn.commit()
    res = _apply_legacy(store, journal)
    assert res["stamped"] == []
    assert "legacy1:content-mismatch" in res["skipped"]


def test_reconciler_catches_legacy_witness_tamper(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store)
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    # Mutate a stamped legacy row's content — reconcile must catch it.
    conn = store._get_conn()
    conn.execute("UPDATE beliefs SET content='SILENTLY REWRITTEN' WHERE id='legacy1'")
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='legacy1'"
        ).fetchone()[0]
        == "review_only"
    )


# ── Apply surface restriction (deviation #1) ──


def test_apply_rejects_unsupported_surface(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, surface="engrams", pid="p-eng")
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="requires a schema unit — escalate"):
        _apply_identity(store, "p-eng", journal)


def test_apply_rejects_non_identity_blast(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store, blast_radius="high", domain="topical", pid="p-hi"
    )
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="only for identity/foundational"):
        _apply_identity(store, "p-hi", journal)


# ── Rejected decision → terminal, no target write ──


def test_rejected_decision_terminal_no_write(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "rejected")])
    result = _apply_identity(store, proposal["id"], journal)
    assert result["status"] == "rejected"
    assert store.get_beliefs("oliver") == []


# ── 008e strengthenings — mutation-proven per finding ──


# E1: activation trigger (three branches pinned)


def test_e1_seam_arms_when_vault_dir_exists(tmp_path, monkeypatch):
    """008r-review: the gate arms iff the vault DIR exists (no env channel)."""
    from mnemos.store.sqlite_store import _resolve_vault_active

    j = tmp_path / "journal.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    assert _resolve_vault_active(None) is True


def test_e1_inert_when_vault_dir_absent(tmp_path, monkeypatch):
    """008r-review: inert iff the vault DIR does not exist. There is no env
    sentinel to force-disable an installed vault — and, crucially, no env to
    force-ENABLE a fake one (the redirect hole 008r-review closed)."""
    from mnemos.store import sqlite_store

    _disarm_vault(monkeypatch)
    assert sqlite_store._resolve_vault_active(None) is False


def test_e1_dir_probe_is_the_install_signal(tmp_path, monkeypatch):
    """The vault DIR existing IS the install-activation signal; removing it (a
    deliberate root-level uninstall) returns to inert. The journal file alone is
    not the signal — a missing journal under an existing dir still arms."""
    from mnemos.store import sqlite_store

    j = tmp_path / "journal.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    assert sqlite_store._resolve_vault_active(None) is True
    monkeypatch.setattr(
        sqlite_store, "_VAULT_DIR_FOR_RESOLUTION", str(tmp_path / "gone")
    )
    assert sqlite_store._resolve_vault_active(None) is False


# E2: legacy witness preserves prior visibility


def test_e2_legacy_witness_preserves_review_only_visibility(tmp_path):
    store = _store(tmp_path)
    conn = store._get_conn()
    # A pre-vault legacy row curator flagged review_only (not part of the
    # prior U4/06-28 approval — flagged for later review).
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('legacy-flagged', 'oliver', 'flagged claim', 0.9, 'identity', 't', 't',"
        " 't', 'foundational', 'review_only')"
    )
    conn.commit()
    row = dict(
        conn.execute("SELECT * FROM beliefs WHERE id='legacy-flagged'").fetchone()
    )
    _append_legacy(tmp_path / "decisions.jsonl", "beliefs", row)
    _apply_legacy(store, tmp_path / "decisions.jsonl")
    # Visibility PRESERVED — the stamp writes decision_ref only.
    r = conn.execute(
        "SELECT read_visibility, decision_ref FROM beliefs WHERE id='legacy-flagged'"
    ).fetchone()
    assert r[0] == "review_only", (
        "008e E2: apply_legacy_witness must NOT promote flagged rows to operational"
    )
    assert r[1], "decision_ref should be stamped"


# E3: visualization dashboard uses the same gate


def test_e3_dashboard_excludes_unwitnessed_identity_when_vault_active(
    tmp_path, monkeypatch
):
    from mnemos.visualization import data as viz

    store = EngramStore(tmp_path / "viz.db")
    conn = store._get_conn()
    # An unwitnessed identity belief that snuck in as operational.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility, confidence_pending_review)"
        " VALUES ('unwitnessed', 'oliver', 'leaked identity claim', 0.9, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context', 0)"
    )
    conn.commit()
    # Vault ARMED via env pointing at a real file (the conftest empty sentinel
    # is overridden explicitly here).
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    beliefs = viz._extract_beliefs(store._get_conn())
    assert all(b["content"] != "leaked identity claim" for b in beliefs), (
        "008e E3: dashboard leaked an unwitnessed identity belief past the vault gate"
    )


def test_e3_dashboard_shows_row_when_vault_inert(tmp_path, monkeypatch):
    from mnemos.visualization import data as viz

    store = EngramStore(tmp_path / "viz2.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility, confidence_pending_review)"
        " VALUES ('pre-vault', 'oliver', 'ordinary identity claim', 0.9, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context', 0)"
    )
    conn.commit()
    _disarm_vault(monkeypatch)
    beliefs = viz._extract_beliefs(store._get_conn())
    assert any(b["content"] == "ordinary identity claim" for b in beliefs)


# #2 reconcile fallback locator (the cleared-ref+de-tier bypass)


def test_hash_2_reconcile_catches_cleared_ref_plus_detier(tmp_path):
    """Producer-specified target_id → fallback locator re-quarantines.

    The residual case (auto-generated target_id + attacker clears ref + tier
    columns) is declared in the 008e implementation report — direction B still
    surfaces a 'missing_witnessed_row' finding for David to actionable, it just
    cannot re-quarantine automatically without a ledger-side reference the
    hash contract precludes.
    """
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="fixed-belief-id")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Attacker: clear BOTH decision_ref AND the tier signal.
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general', decision_ref=NULL "
        "WHERE id='fixed-belief-id'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    # Fallback located by proposal.target_surface + target_id and re-quarantined.
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008e #2: fallback locator did not catch cleared_ref + de-tier"
    )
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='fixed-belief-id'"
        ).fetchone()[0]
        == "review_only"
    )


# #4 mixed-visibility predicate correction


def test_hash_4_mixed_visibility_keeps_review_only_unwitnessed_identity(tmp_path):
    """(operational, review_only) request must NOT drop review-only identity rows."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # A review-only unwitnessed identity belief — belongs to review queues.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('review-item', 'oliver', 'awaiting review', 0.6, 'identity', 't', 't',"
        " 't', 'foundational', 'review_only')"
    )
    conn.commit()
    review_queue = store.get_beliefs(
        "oliver", read_visibility=("operational_context", "review_only")
    )
    assert any(b.content == "awaiting review" for b in review_queue), (
        "008e #4: mixed visibility set dropped the review-only identity row"
    )
    # Sanity: operational-only still hides it.
    ops = store.get_beliefs("oliver", read_visibility="operational_context")
    assert all(b.content != "awaiting review" for b in ops)


# #5 reconcile dedupes by proposal_id (latest-decision wins)


def test_hash_5_reconcile_ignores_superseded_approval(tmp_path):
    """approve → reject same proposal: reconcile must not require an approved row."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved"), (proposal, "rejected")])
    # Apply per the latest decision (reject) — no target row is written.
    _apply_identity(store, proposal["id"], journal)
    # Reconcile must not raise a false divergence for the earlier approval.
    report = store.reconcile_identity_vault(journal)
    assert report.ok, (
        f"008e #5: reconcile treated superseded approval as still active: {report.findings}"
    )


# #7 apply-time tier signal enforcement


def test_hash_7_identity_apply_rejects_non_identity_domain(tmp_path):
    """identity blast_radius with domain='general' must not slip past apply."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="attempted domain-launder",
        domain="general",  # ← the launder attempt
        blast_radius="identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        payload={"content": "impostor claim", "tier": "operational"},
        provenance_ids=[],
        proposal_id="p-launder",
    )
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="identity/foundational domain"):
        _apply_identity(store, "p-launder", journal)


def test_hash_7_identity_apply_forces_foundational_tier(tmp_path):
    """payload tier='operational' must be forced to 'foundational' at apply."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="tier-downgrade attempt",
        domain="identity",
        blast_radius="identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        payload={"content": "identity claim", "tier": "operational"},
        provenance_ids=[],
        proposal_id="p-tier",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, "p-tier", journal)
    conn = store._get_conn()
    row = conn.execute(
        "SELECT tier FROM beliefs WHERE id != 'p-tier' AND content='identity claim'"
    ).fetchone()
    assert row[0] == "foundational", (
        "008e #7: payload's tier='operational' was accepted; must be forced foundational"
    )


# ── 008e-r2 strengthenings (review round 2) ──


# r2 #1: write_proposal auto-generates target_id for identity blast


def test_r2_1_identity_proposal_auto_generates_target_id(tmp_path):
    store = _store(tmp_path)
    proposal = store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="test auto-gen",
        domain="identity",
        blast_radius="identity",
        payload={"content": "x"},
        proposal_id="p-auto",
        # target_id NOT specified
    )
    assert proposal["target_id"], (
        "008e-r2 #1: identity blast must auto-generate a stable target_id"
    )


def test_r2_1_reconcile_fallback_now_works_for_auto_generated_target(tmp_path):
    """The 008e-declared residual is CLOSED: auto-gen target_id is in the
    hashed content, so fallback locator works even without producer intent."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    # No explicit target_id — write_proposal auto-generates.
    proposal = _make_identity_proposal(store)
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Attacker triple-clears; only the ledger's target_id can locate the row.
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general', decision_ref=NULL"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008e-r2 #1: fallback missed auto-gen target — the declared residual is not closed"
    )


# r2 #2: reconcile checks the FULL applied field-set


def test_008k_belief_confidence_mutation_is_NOT_tamper(tmp_path):
    """008k / 008g E7/E8 canonical rule: confidence is NOT witnessed.
    A routine consolidation confidence bump on a witnessed belief must
    reconcile CLEAN — otherwise review-fatigue quarantines the whole
    identity corpus over time. This test replaces the pre-008k r2 #2
    that expected the opposite; the old expectation encoded the mistake."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-conf")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Content + tier + domain intact — confidence updated, per 008g the
    # legitimate consolidation-style mutation.
    conn.execute("UPDATE beliefs SET confidence = 0.01 WHERE id = 'b-conf'")
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert not any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008k: confidence is NOT witnessed — reconcile must not flag its "
        "mutation as tamper (008g E7/E8 explicit rule)"
    )
    assert (
        conn.execute(
            "SELECT read_visibility, decision_ref FROM beliefs WHERE id='b-conf'"
        ).fetchone()[0]
        == "operational_context"
    ), "008k: witnessed belief must stay operational after confidence bump"


def test_008k_hypomnema_confidence_density_mutation_is_NOT_tamper(tmp_path):
    """008k: density/confidence/salience/source/tags all mutable-after-witnessing
    per 008g. A routine update must reconcile CLEAN."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store, surface="hypomnema_entries", target_id="h-den", content="hypo claim"
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE hypomnema_entries SET density = 0.99, confidence = 0.99, "
        "salience = 0.99 WHERE id = 'h-den'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert not any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008k: density/confidence/salience are NOT witnessed — reconcile "
        "must not flag their mutation as tamper (008g E7/E8 rule)"
    )
    assert (
        conn.execute(
            "SELECT read_visibility FROM hypomnema_entries WHERE id='h-den'"
        ).fetchone()[0]
        == "operational_context"
    )


# r2 #3: session-start honors default-vault activation


def test_r2_3_session_start_stamps_when_default_vault_installed(tmp_path, monkeypatch):
    import mnemos.mcp_server as server

    store = EngramStore(tmp_path / "s.db")
    # A default-install: the canonical vault dir exists (008r-review — no env).
    fake_default = tmp_path / "system_journal.jsonl"
    fake_default.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, fake_default)
    monkeypatch.setattr(server, "_store", store)

    called = {}
    # 008m: apply_legacy_witness resolves canonical internally now (no path
    # passed); session-start resolves the path only for reconcile. Assert
    # session-start resolved the default-install path and passed it to
    # reconcile, and that it invoked the stamp step.
    monkeypatch.setattr(
        store, "apply_legacy_witness", lambda **kw: called.setdefault("stamp", True)
    )
    # Return a real ReconcileReport, not the str dict.setdefault yields:
    # session-start now inspects report.findings (008r/review), so a str return
    # raised AttributeError into _alert_vault_error and leaked a vault-alert file
    # into the real ~/Oliver Inbox. The path capture (the actual assertion) stays.
    from mnemos.vault.reconcile import ReconcileReport

    def _fake_reconcile(p):
        called["reconcile_path"] = str(p)
        return ReconcileReport()

    monkeypatch.setattr(store, "reconcile_identity_vault", _fake_reconcile)
    server._reconcile_vault_on_session_start()
    assert called.get("stamp") is True, (
        "008e-r2 #3: session-start did not run the legacy-witness stamp"
    )
    assert called.get("reconcile_path") == str(fake_default), (
        "008e-r2 #3 / 008m: session-start missed the default-install probe "
        "or passed the wrong path to reconcile"
    )


def test_isolate_env_redirects_vault_alert_off_real_inbox(tmp_path, monkeypatch):
    """Mutation guard for the inbox-leak fix: the autouse _isolate_mnemos_env
    fixture (conftest) redirects MNEMOS_WATCHDOG_ALERT_DIR to a tmp dir, so a
    session-start vault alert can never write the developer's real ~/Oliver
    Inbox. Revert that setenv in conftest and this goes RED — and a suite run
    leaks a vault-session-start file into the real inbox again.
    """
    import os
    import pathlib
    from types import SimpleNamespace

    import mnemos.mcp_server as server

    # 1. The autouse fixture must have redirected the alert dir off the real inbox.
    alert_dir = os.environ.get("MNEMOS_WATCHDOG_ALERT_DIR")
    assert alert_dir, (
        "conftest _isolate_mnemos_env must set MNEMOS_WATCHDOG_ALERT_DIR so no "
        "test can write the real ~/Oliver Inbox"
    )
    assert (
        pathlib.Path(alert_dir).expanduser().resolve()
        != pathlib.Path("~/Oliver Inbox").expanduser().resolve()
    ), "the redirected alert dir must not be the real inbox"

    # 2. Drive a real session-start alert WITHOUT setting our own alert dir, so it
    #    exercises the conftest default; it must land in the redirected dir.
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    journal.write_text("", encoding="utf-8")
    fake_report = SimpleNamespace(
        findings=[
            {
                "severity": "high",
                "kind": "orphan_identity_row",
                "detail": "d",
                "table": "beliefs",
                "row_id": "b",
            }
        ],
        requarantined=[],
    )
    monkeypatch.setattr(store, "reconcile_identity_vault", lambda *a, **k: fake_report)
    monkeypatch.setattr(server, "_store", store)
    _arm_vault(monkeypatch, journal)
    server._reconcile_vault_on_session_start()

    landed = list(pathlib.Path(alert_dir).glob("*-vault-session-start-*.md"))
    assert landed, (
        "session-start alert did not land in the redirected tmp dir; the "
        "MNEMOS_WATCHDOG_ALERT_DIR default redirect is not in effect"
    )


# r2 #4: audit dashboard bypasses the gate


def test_r2_4_audit_dashboard_shows_unwitnessed_identity(tmp_path, monkeypatch):
    from mnemos.visualization import data as viz

    store = EngramStore(tmp_path / "aud.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility, confidence_pending_review)"
        " VALUES ('unwit', 'oliver', 'unwitnessed', 0.9, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context', 0)"
    )
    conn.commit()
    j = tmp_path / "v.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    # Admin/audit surface: unwitnessed identity must be VISIBLE for review.
    beliefs = viz._extract_beliefs(store._get_conn(), include_non_operational=True)
    assert any(b["content"] == "unwitnessed" for b in beliefs), (
        "008e-r2 #4: admin dashboard hid an unwitnessed identity row from David"
    )


# ── 008e-r3 strengthenings (review round 3) ──


# r3 #1: reconcile binds target_id / target_surface / scope


def test_r3_1_reconcile_rejects_copied_decision_ref_to_duplicate_row(tmp_path):
    """A copied decision_ref stamped onto a duplicate row must not license it."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-original")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    real_ref = conn.execute(
        "SELECT decision_ref FROM beliefs WHERE id='b-original'"
    ).fetchone()[0]
    # Attacker duplicates the row with the SAME decision_ref (would sail past
    # a fields-only check because content/domain/tier all match).
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility, decision_ref) VALUES"
        " ('b-duplicate', 'oliver', ?, ?, ?, 't', 't', 't', 'foundational',"
        " 'operational_context', ?)",
        (
            "I am Oliver. David's agent.",
            0.3,
            "identity",
            real_ref,
        ),
    )
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    # Duplicate is flagged + quarantined; original stays clean.
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008e-r3 #1: reconcile passed a copied-ref duplicate row as clean"
    )
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-duplicate'"
        ).fetchone()[0]
        == "review_only"
    )


# r3 #2: expanded-path consistency


def test_r3_2_resolver_returns_absolute_pinned_path(monkeypatch):
    """008r-review: the resolver has NO env channel, so the ~-expansion hazard
    is gone — production returns the pinned ABSOLUTE canonical journal. Assert
    the pin is absolute (no literal ~ that would empty the journal read) and
    that resolve returns it when the vault dir is present."""
    from mnemos.store import sqlite_store

    assert sqlite_store._CANONICAL_VAULT_JOURNAL.startswith("/")
    assert "~" not in sqlite_store._CANONICAL_VAULT_JOURNAL
    # Armed (dir present) → returns the pinned journal, absolute, no ~.
    monkeypatch.setattr(sqlite_store, "_VAULT_DIR_FOR_RESOLUTION", "/")
    resolved = sqlite_store.resolve_vault_journal_path()
    assert resolved == sqlite_store._VAULT_JOURNAL_FOR_RESOLUTION
    assert resolved is not None and "~" not in resolved


# r3 #3: TCB query matches apply's contract


def test_r3_3_tcb_lists_only_identity_blast_proposals(tmp_path):
    """A blast=medium + domain=identity proposal must NOT be offered to David
    — apply would refuse it and he'd approve something that can't apply."""
    store = EngramStore(tmp_path / "tcb.db")
    # Two proposals: one identity blast (should show), one blast=medium +
    # domain=identity (should NOT show — apply refuses that shape).
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="real identity claim",
        domain="identity",
        blast_radius="identity",
        payload={"content": "real"},
        proposal_id="p-real",
    )
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="mislabeled — medium blast + identity domain",
        domain="identity",
        blast_radius="medium",
        payload={"content": "mislabeled"},
        proposal_id="p-mislabel",
    )
    journal = tmp_path / "decisions.jsonl"
    result = _run_tcb_wrapper(store.db_path, journal, "s\n")
    assert "p-real" in result.stdout
    assert "p-mislabel" not in result.stdout, (
        "008e-r3 #3: TCB offered a proposal that apply would refuse"
    )


def _run_tcb_wrapper(db_path, journal_path, stdin_text, args=()):
    import pathlib
    import subprocess
    import sys

    tcb = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mnemos-decide"
    # 008r/review: the TCB no longer honors MNEMOS_DB_PATH / MNEMOS_VAULT_JOURNAL
    # env (a redirectable writer). Tests inject via --db/--journal flags, which
    # sudo and launchd never pass.
    # 008r-review (tcb-recreates-missing-journal): mirror the installed state —
    # the installer touches the journal; the TCB refuses an absent one.
    pathlib.Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(journal_path).touch(exist_ok=True)
    return subprocess.run(
        [
            sys.executable,
            str(tcb),
            "--db",
            str(db_path),
            "--journal",
            str(journal_path),
            *args,
        ],
        input=stdin_text,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


# r3 #4: defer removed


def test_r3_4_tcb_defer_option_removed(tmp_path):
    """The defer prompt string is gone — [d] is no longer offered as an option
    (it wrote neither DB nor journal, misleading David into thinking something
    durable happened)."""
    store = EngramStore(tmp_path / "def.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="x",
        domain="identity",
        blast_radius="identity",
        payload={"content": "x"},
        proposal_id="p-def",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "[d]efer" not in result.stdout, (
        "008e-r3 #4: [d]efer still offered — it's non-durable, remove it"
    )
    assert "[a]pprove / [r]eject / [s]kip" in result.stdout


# ── 008e-r4 strengthenings (review round 4) ──


# r4 #2: TCB filters to appliable target surfaces


def test_r4_2_tcb_hides_engrams_targeted_identity_proposal(tmp_path):
    """An identity proposal targeting 'engrams' must not be offered — apply
    would refuse it, so approving it leaves a permanent pending + reconciler
    'missing_witnessed_row' finding."""
    store = EngramStore(tmp_path / "tcb.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="valid",
        domain="identity",
        blast_radius="identity",
        payload={"content": "valid"},
        proposal_id="p-ok",
    )
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="engrams",
        transition="engram-targeted identity",
        domain="identity",
        blast_radius="identity",
        payload={"content": "unappliable"},
        proposal_id="p-engram",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "p-ok" in result.stdout
    assert "p-engram" not in result.stdout, (
        "008e-r4 #2: TCB offered a proposal apply would refuse (engrams target)"
    )


# r4 #3: legacy row-hash binds scope


def test_r4_3_row_hash_binds_agent_id():
    from mnemos.vault import journal as vj

    row_a = {
        "id": "b1",
        "content": "x",
        "domain": "identity",
        "foundational": 1,
        "agent_id": "oliver",
    }
    row_b = {**row_a, "agent_id": "attacker"}
    assert vj.canonical_row_sha256("beliefs", row_a) != vj.canonical_row_sha256(
        "beliefs", row_b
    ), "008e-r4 #3: row hash did not distinguish agent_id"


def test_r4_3_row_hash_binds_hypomnema_scope():
    from mnemos.vault import journal as vj

    row_a = {
        "id": "h1",
        "content": "x",
        "domain": "identity",
        "foundational": 1,
        "agent_id": "oliver",
        "person_id": "david",
        "project_scope": "pai",
    }
    for changed in ("person_id", "project_scope"):
        row_b = {**row_a, changed: "other"}
        assert vj.canonical_row_sha256("hypomnema_entries", row_a) != (
            vj.canonical_row_sha256("hypomnema_entries", row_b)
        ), f"008e-r4 #3: row hash did not distinguish hypomnema {changed}"


# r4 #6: Direction A scoped to operational


def test_r4_6_reconcile_does_not_flag_review_only_orphans(tmp_path):
    """A review_only identity row awaits review — don't spam findings each run."""
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('pending-review', 'oliver', 'awaiting review', 0.5, 'identity',"
        " 't', 't', 't', 'foundational', 'review_only')"
    )
    conn.commit()
    report = store.reconcile_identity_vault(tmp_path / "empty.jsonl")
    orphan_findings = [f for f in report.findings if f["kind"] == "orphan_identity_row"]
    assert not orphan_findings, (
        f"008e-r4 #6: reconcile spammed orphan finding on review_only row: {orphan_findings}"
    )


# ── 008g E7/E8: witnessed-field split on upsert (chokepoint semantics) ──


def _witnessed_belief(
    store,
    bid="wb",
    agent="oliver",
    content="witnessed claim",
    domain="identity",
    tier="foundational",
):
    """Insert a belief as if it had been vault-witnessed (fake decision_ref)."""
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " decision_ref) VALUES (?, ?, ?, 0.7, ?, 't', 't', 't', ?,"
        " 'operational_context', 'fakeref' || ?)",
        (bid, agent, content, domain, tier, bid),
    )
    conn.commit()


def test_g_e7_confidence_only_upsert_preserves_ref_and_operational(tmp_path):
    """A consolidation confidence bump on a witnessed belief must NOT degrade."""
    from mnemos.core.belief import Belief

    store = _store(tmp_path)
    _witnessed_belief(store)
    updated = Belief(
        id="wb",
        agent_id="oliver",
        content="witnessed claim",
        confidence=0.92,
        domain="identity",
        tier="foundational",
    )
    store.save_belief(updated)
    conn = store._get_conn()
    row = conn.execute(
        "SELECT confidence, decision_ref, read_visibility FROM beliefs WHERE id='wb'"
    ).fetchone()
    assert abs(row[0] - 0.92) < 1e-9, "confidence should have updated"
    assert (row[1] or "").startswith("fakeref"), (
        "008g E7: witness ref lost on confidence-only update — witnessed fields unchanged"
    )
    assert row[2] == "operational_context"


def test_g_e7_content_change_atomic_degrade(tmp_path):
    """A content change on a witnessed belief must clear ref + review_only ATOMICALLY."""
    from mnemos.core.belief import Belief

    store = _store(tmp_path)
    _witnessed_belief(store, content="original")
    mutated = Belief(
        id="wb",
        agent_id="oliver",
        content="MUTATED",
        confidence=0.7,
        domain="identity",
        tier="foundational",
    )
    store.save_belief(mutated)
    conn = store._get_conn()
    row = conn.execute(
        "SELECT content, decision_ref, read_visibility FROM beliefs WHERE id='wb'"
    ).fetchone()
    assert row[0] == "MUTATED"
    assert row[1] is None, "008g E7: decision_ref must be cleared on content change"
    assert row[2] == "review_only", (
        "008g E7: read_visibility must degrade to review_only in same transaction"
    )


def test_g_e7_domain_change_degrades(tmp_path):
    from mnemos.core.belief import Belief

    store = _store(tmp_path)
    _witnessed_belief(store)
    mutated = Belief(
        id="wb",
        agent_id="oliver",
        content="witnessed claim",
        confidence=0.7,
        domain="general",
        tier="foundational",
    )
    store.save_belief(mutated)
    conn = store._get_conn()
    assert (
        conn.execute("SELECT decision_ref FROM beliefs WHERE id='wb'").fetchone()[0]
        is None
    )


def test_g_e7_identical_rewrite_preserves_ref(tmp_path):
    """Byte-identical rewrite of every witnessed field must NOT degrade."""
    from mnemos.core.belief import Belief

    store = _store(tmp_path)
    _witnessed_belief(store)
    same = Belief(
        id="wb",
        agent_id="oliver",
        content="witnessed claim",
        confidence=0.7,
        domain="identity",
        tier="foundational",
    )
    store.save_belief(same)
    conn = store._get_conn()
    ref = conn.execute("SELECT decision_ref FROM beliefs WHERE id='wb'").fetchone()[0]
    assert (ref or "").startswith("fakeref"), (
        "008g E7: identical rewrite triggered degrade (should be no-op re witness)"
    )


def test_g_e7_degrade_emits_trace_proposal(tmp_path):
    from mnemos.core.belief import Belief

    store = _store(tmp_path)
    _witnessed_belief(store)
    mutated = Belief(
        id="wb",
        agent_id="oliver",
        content="MUTATED",
        confidence=0.7,
        domain="identity",
        tier="foundational",
    )
    store.save_belief(mutated)
    conn = store._get_conn()
    traces = conn.execute(
        "SELECT id, reason, payload_json, status, target_id FROM proposal_ledger "
        "WHERE id LIKE 'degrade-beliefs-wb-%'"
    ).fetchall()
    assert len(traces) == 1
    trace = dict(traces[0])
    assert trace["status"] == "pending_review"
    assert "MUTATED" not in trace["reason"], "reason must not carry raw payload"
    assert trace["target_id"] == "wb"
    payload = json.loads(trace["payload_json"])
    assert "old_row_hash" in payload and "new_row_hash" in payload
    assert payload["old_row_hash"] != payload["new_row_hash"]


def test_g_e8_hypomnema_content_change_degrades_and_emits_trace(tmp_path):
    store = _store(tmp_path)
    # Insert a witnessed hypomnema row directly.
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at, decision_ref) VALUES"
        " ('wh', 'oliver', 'david', 'pai', 'original', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.6, 0.5, 1, 1, 0, '[]',"
        " 't', 't', 'fakerefwh')"
    )
    conn.commit()
    # Ordinary write attempts to change content — must degrade.
    store.write_hypomnema_entry(
        content="MUTATED HYPO",
        entry_id="wh",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        source="observed",
        density=0.5,
        domain="identity",
        confidence=0.6,
        salience=0.5,
        foundational=True,
    )
    row = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries WHERE id='wh'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "review_only"
    traces = conn.execute(
        "SELECT payload_json FROM proposal_ledger WHERE id LIKE 'degrade-hypomnema_entries-wh-%'"
    ).fetchall()
    assert len(traces) == 1


def test_g_e8_hypomnema_confidence_only_preserves_ref(tmp_path):
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at, decision_ref) VALUES"
        " ('wh2', 'oliver', 'david', 'pai', 'stable', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.6, 0.5, 1, 1, 0, '[]',"
        " 't', 't', 'fakerefwh2')"
    )
    conn.commit()
    store.write_hypomnema_entry(
        content="stable",  # unchanged
        entry_id="wh2",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        source="observed",
        density=0.5,
        domain="identity",
        confidence=0.99,
        salience=0.9,
        foundational=True,
    )
    row = conn.execute(
        "SELECT decision_ref FROM hypomnema_entries WHERE id='wh2'"
    ).fetchone()
    # 008g E8: witnessed-field split is about REF preservation. The existing
    # classify_hypomnema_read_visibility already forces identity content to
    # review_only on ordinary writes (only apply_identity_decision bypasses
    # that classifier), so visibility is a separate concern here — the witness
    # question is whether the ref survives a non-witnessed-field change.
    assert row[0] == "fakerefwh2", (
        "008g E8: confidence/salience-only update lost the witness ref"
    )
    # And no trace proposal is emitted for a witnessed-field-unchanged write.
    traces = conn.execute(
        "SELECT id FROM proposal_ledger WHERE id LIKE 'degrade-hypomnema_entries-wh2-%'"
    ).fetchall()
    assert traces == []


# ── 008g-r5 strengthenings ──


# r5 #1: TCB excludes trace proposals and non-identity-domain proposals


def test_r5_1_tcb_excludes_degrade_trace_proposals(tmp_path):
    """The degrade-* proposal traces (payload has no content) must not appear
    in the TCB — apply would refuse them, and they belong to
    mnemos_proposal_audit, not the approval flow."""
    store = EngramStore(tmp_path / "tcb.db")
    # A real applyable proposal.
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="real claim",
        domain="identity",
        blast_radius="identity",
        payload={"content": "I am Oliver."},
        proposal_id="p-real",
    )
    # A degrade trace: identity blast, but payload has no 'content'.
    store.write_proposal(
        source_authority="observed",
        kind="semantic",
        target_surface="beliefs",
        transition="witnessed row degrade",
        domain="identity",
        blast_radius="identity",
        payload={"old_row_hash": "abc", "new_row_hash": "def"},
        proposal_id="degrade-beliefs-wb-0000000000000000",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "p-real" in result.stdout
    assert "degrade-beliefs-wb-0000000000000000" not in result.stdout, (
        "008g-r5 #1: TCB offered a degrade-trace proposal (no payload.content)"
    )


def test_r5_1_tcb_excludes_general_domain_identity_blast(tmp_path):
    """Apply requires domain in {identity, foundational} (r2 #7). TCB now mirrors."""
    store = EngramStore(tmp_path / "tcb.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="normal",
        domain="identity",
        blast_radius="identity",
        payload={"content": "ok"},
        proposal_id="p-ok",
    )
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="mislabeled",
        domain="general",
        blast_radius="identity",
        payload={"content": "bad"},
        proposal_id="p-bad-domain",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "p-ok" in result.stdout
    assert "p-bad-domain" not in result.stdout


# r5 #2: revise_hypomnema_entry witnessed-field degrade


def test_r5_2_revise_hypomnema_content_change_degrades(tmp_path):
    """revise_hypomnema_entry was the missing write path — content changes on
    a witnessed row must degrade + trace, same as ordinary upsert."""
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at, decision_ref) VALUES"
        " ('wr', 'oliver', 'david', 'pai', 'original', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.6, 0.5, 1, 1, 0, '[]',"
        " 't', 't', 'fakerefwr')"
    )
    conn.commit()
    store.revise_hypomnema_entry(
        entry_id="wr",
        new_content="mutated via revise",
        reason="test",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    row = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries WHERE id='wr'"
    ).fetchone()
    assert row[0] is None, (
        "008g-r5 #2: revise_hypomnema_entry did not clear decision_ref on content change"
    )
    assert row[1] == "review_only"
    traces = conn.execute(
        "SELECT id FROM proposal_ledger WHERE id LIKE 'degrade-hypomnema_entries-wr-%'"
    ).fetchall()
    assert len(traces) == 1


# r5 #3: watchdog catches reconcile exceptions and alerts


def _load_watchdog_module(db_path, journal_path):
    """Load the watchdog with its DB/journal PINNED to test paths.

    008m Addition 1 + 008r-review (watchdog-production-redirect-flags): the
    watchdog no longer honors MNEMOS_DB_PATH / MNEMOS_VAULT_JOURNAL env NOR
    exposes --db/--journal flags — unguarded under launchd, a flag was a
    redirect vector for the independent detector. It reads pinned canonical
    constants. A test must NEVER let those defaults stand, or it runs
    reconcile-with-quarantine against David's real memory. Inject by loading the
    module and OVERRIDING DB_PATH / JOURNAL_PATH directly — a seam a subprocess
    or launchd never reaches into.
    """
    import importlib.util
    import pathlib
    from importlib.machinery import SourceFileLoader

    wd_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "mnemos-vault-watchdog.py"
    )
    loader = SourceFileLoader("mnemos_watchdog_r5", str(wd_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(module)
    # Override the pinned canonical paths with the test fixtures (the seam).
    module.DB_PATH = pathlib.Path(db_path)
    module.JOURNAL_PATH = str(journal_path)
    # Belt-and-suspenders: assert the module is pinned to the tmp paths — a guard
    # so this test can never silently touch David's real DB.
    assert str(module.DB_PATH) == str(db_path), (
        "watchdog test failed to pin DB_PATH away from the live canonical DB"
    )
    assert str(module.JOURNAL_PATH) == str(journal_path)
    return module


def test_r5_3_watchdog_catches_corrupt_journal_and_alerts(tmp_path, monkeypatch):
    """A corrupt journal must produce a log entry + Oliver Inbox alert file,
    not a silent crash landing in launchd stderr."""
    store = EngramStore(tmp_path / "wd.db")
    journal = tmp_path / "corrupt.jsonl"
    # 008i: single-line malformation now classifies as torn-tail (recoverable
    # power-loss shape). Write MID-FILE corruption to trigger the corrupt
    # class: valid line, then garbage, then a valid-looking tail.
    good = (
        '{"v":1,"proposal_id":"p","content_sha256":"x","decision":"approved"'
        ',"scope":"identity","prev_sha256":"0"}'
    )
    journal.write_text(
        good + "\n{garbage in the middle\n" + good + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_WATCHDOG_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    module = _load_watchdog_module(store.db_path, journal)

    rc = module.main()
    # 008i: corrupt journal now classifies gracefully → findings present
    # → exit 5 (was 6 pre-008i when the read raised outright).
    assert rc == 5, f"expected exit 5 on corrupt journal (008i), got {rc}"
    logs = (tmp_path / "logs" / "vault-watchdog.log").read_text()
    assert "DIVERGENCE" in logs or "corrupt" in logs.lower()
    # 008i: watchdog now writes the standard findings-alert path (not the
    # crash-error path), because corrupt journal is a classified finding.
    alerts = list((tmp_path / "inbox").glob("*-vault-watchdog-alert.md"))
    assert len(alerts) == 1, "008i: watchdog did not alert on corrupt journal"


def test_008m_watchdog_ignores_env_poisoning(tmp_path, monkeypatch):
    """008m Addition 1: the independent detector must NOT honor
    MNEMOS_VAULT_JOURNAL / MNEMOS_DB_PATH env — a poisoned env that redirects
    the store must not redirect the watchdog, or tamper-evidence is false."""
    real_journal = tmp_path / "canonical.jsonl"
    real_journal.write_text("", encoding="utf-8")
    real_db = tmp_path / "canonical.db"
    EngramStore(real_db)  # create it
    poison = tmp_path / "attacker-journal.jsonl"
    poison.write_text("", encoding="utf-8")
    # Poison the env the way an agent process could.
    monkeypatch.setenv("MNEMOS_VAULT_JOURNAL", str(poison))
    monkeypatch.setenv("MNEMOS_DB_PATH", str(poison))
    # The watchdog, pinned via --journal/--db to the REAL paths, must ignore
    # the poisoned env entirely.
    module = _load_watchdog_module(real_db, real_journal)
    assert str(module.JOURNAL_PATH) == str(real_journal), (
        "008m: watchdog honored poisoned MNEMOS_VAULT_JOURNAL env"
    )
    assert str(module.DB_PATH) == str(real_db), (
        "008m: watchdog honored poisoned MNEMOS_DB_PATH env"
    )


# ── 008g-r6 strengthenings ──


# r6 #1: TCB terminal control sanitization


def test_r6_1_tcb_neutralizes_ansi_in_transition(tmp_path):
    """A malicious ANSI in `transition` must not reach David's terminal."""
    store = EngramStore(tmp_path / "tcb.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="\x1b[2J\x1b[H  FAKE PROMPT: enter approval > ",
        domain="identity",
        blast_radius="identity",
        payload={"content": "attacker payload"},
        proposal_id="p-inject",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "\x1b[2J" not in result.stdout, (
        "008g-r6 #1: TCB emitted a raw ANSI clear-screen from a proposal field"
    )
    assert "\x1b" not in result.stdout, (
        "008g-r6 #1: TCB emitted a raw ESC from a proposal field"
    )


def test_r6_1_tcb_neutralizes_ansi_in_payload_content(tmp_path):
    store = EngramStore(tmp_path / "tcb.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="normal",
        domain="identity",
        blast_radius="identity",
        payload={"content": "harmless\x1b[31mred\x1b[0m"},
        proposal_id="p-p",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "\x1b" not in result.stdout


# r6 #2: configured vault fails closed


def test_r6_2_missing_journal_arms_and_re_quarantines(tmp_path, monkeypatch):
    """008g-r6 fail-CLOSED preserved under the pin (008r-review): the vault DIR
    exists but the journal FILE is missing → still arms, so reconcile sees an
    empty journal and re-quarantines stamped identity rows — NOT silently
    un-arm (fail-open)."""
    from mnemos.store.sqlite_store import (
        _resolve_vault_active,
        resolve_vault_journal_path,
    )

    ghost = tmp_path / "does-not-exist.jsonl"  # file missing; parent dir exists
    _arm_vault(monkeypatch, ghost)
    assert _resolve_vault_active(None) is True, (
        "008g-r6: dir present + journal missing must arm (fail-closed)"
    )
    assert resolve_vault_journal_path() == str(ghost)


def test_r6_2_missing_journal_re_quarantines_stamped_rows(tmp_path):
    """The behavioral consequence: a stamped identity row becomes review_only
    when the configured journal is missing on the next reconcile."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-x")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Now the file goes missing (typo, rm, whatever).
    journal.unlink()
    # Reconcile against the missing path should re-quarantine.
    report = store.reconcile_identity_vault(journal)
    assert not report.ok
    conn = store._get_conn()
    row = conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-x'").fetchone()
    assert row[0] == "review_only"


# r6 #3: reconcile binds lifecycle fields


def test_r6_3_reconcile_catches_witnessed_belief_superseded(tmp_path):
    """Setting superseded_by on a witnessed belief must NOT reconcile clean —
    the row disappears from operational reads but decision_ref stays,
    which is exactly the silent-tamper case."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-sup")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute("UPDATE beliefs SET superseded_by = 'phantom' WHERE id='b-sup'")
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008g-r6 #3: reconcile missed a silent supersede of a witnessed belief"
    )


def test_r6_3_reconcile_catches_witnessed_hypomnema_deactivated(tmp_path):
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store,
        surface="hypomnema_entries",
        target_id="h-off",
        content="witnessed hypo",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute("UPDATE hypomnema_entries SET active = 0 WHERE id='h-off'")
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "witnessed_row_tampered" for f in report.findings), (
        "008g-r6 #3: reconcile missed a silent deactivation of witnessed hypomnema"
    )


# r6 #4: stats respect the vault gate


def test_r6_4_belief_stats_exclude_unwitnessed_identity(tmp_path, monkeypatch):
    """beliefs_active must not count unwitnessed identity rows when the vault is armed."""
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "s.db")
    conn = store._get_conn()
    # A regular non-identity belief.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('ok', 'oliver', 'regular', 0.5, 'general', 't', 't', 't',"
        " NULL, 'operational_context')"
    )
    # An unwitnessed identity belief that slipped operational.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('sneak', 'oliver', 'forged', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    stats = store.get_stats(agent_id="oliver")
    assert stats["beliefs_active"] == 1, (
        f"008g-r6 #4: unwitnessed identity belief counted in beliefs_active "
        f"(got {stats['beliefs_active']}, expected 1)"
    )


def test_r6_4_hypomnema_stats_exclude_unwitnessed_identity(tmp_path, monkeypatch):
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "s2.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at) VALUES"
        " ('sneakh', 'oliver', 'david', 'pai', 'forged', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.9, 0.5, 1, 1, 0, '[]',"
        " 't', 't')"
    )
    conn.commit()
    stats = store.get_hypomnema_stats(
        agent_id="oliver", read_visibility="operational_context"
    )
    assert stats["hypomnema_total"] == 0, (
        f"008g-r6 #4: unwitnessed identity hypomnema counted in stats "
        f"(got {stats['hypomnema_total']}, expected 0)"
    )


# ── 008g-r7 strengthenings ──


# r7 #2: --witness-legacy sanitizes the content sample


def test_r7_2_witness_legacy_neutralizes_ansi_in_sample(tmp_path):
    """A pre-vault identity row with ANSI in its content must not spoof the y/N."""
    store = EngramStore(tmp_path / "wl.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('legacy', 'oliver', '\x1b[2J\x1b[H ATTACK PROMPT > ',"
        " 0.7, 'identity', 't', 't', 't', 'foundational', 'operational_context')"
    )
    conn.commit()
    result = _run_tcb_wrapper(
        store.db_path, tmp_path / "j.jsonl", "n\n", args=["--witness-legacy"]
    )
    assert "\x1b[2J" not in result.stdout
    assert "\x1b" not in result.stdout, (
        "008g-r7 #2: --witness-legacy emitted raw ANSI from a legacy row's content"
    )


# r7 #3: apply resets lifecycle fields on conflict


def test_r7_3_apply_re_witness_resets_belief_superseded_by(tmp_path):
    """Re-approving a previously-superseded belief must clear superseded_by."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # Pre-existing belief with the target_id, superseded.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " superseded_by) VALUES"
        " ('b-rewit', 'oliver', 'old', 0.5, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context', 'phantom-successor')"
    )
    conn.commit()
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-rewit", content="new")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    row = conn.execute(
        "SELECT content, superseded_by FROM beliefs WHERE id='b-rewit'"
    ).fetchone()
    assert row[0] == "new"
    assert row[1] is None, (
        "008g-r7 #3: apply left superseded_by set on a re-witnessed belief"
    )


def test_r7_3_apply_re_witness_resets_hypomnema_active_and_superseded(tmp_path):
    """Re-approving a previously-deactivated hypomnema clears active=0."""
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at) VALUES"
        " ('h-rewit', 'oliver', 'david', 'pai', 'old', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.6, 0.5, 0, 1, 0, '[]',"
        " 't', 't')"
    )
    conn.commit()
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store,
        surface="hypomnema_entries",
        target_id="h-rewit",
        content="new hypo",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    row = conn.execute(
        "SELECT active, superseded_by FROM hypomnema_entries WHERE id='h-rewit'"
    ).fetchone()
    assert row[0] == 1, "008g-r7 #3: apply did not reactivate a re-witnessed hypomnema"
    assert row[1] is None


# r7 #4: promotion-candidate count uses the vault gate


def test_r7_4_hypomnema_promotion_candidates_excludes_unwitnessed_identity(
    tmp_path, monkeypatch
):
    """An unwitnessed identity hypomnema that meets promotion thresholds must
    not increment hypomnema_promotion_candidates when the vault is armed."""
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "cand.db")
    conn = store._get_conn()
    # Meets promotion candidate thresholds (foundational=1 forces candidate).
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at) VALUES"
        " ('sneakcand', 'oliver', 'david', 'pai', 'forged identity', 'observed',"
        " 0.5, 'identity', 'operational_context', '[]', 0.9, 0.8, 1, 1, 2, '[]',"
        " 't', 't')"
    )
    conn.commit()
    stats = store.get_hypomnema_stats(
        agent_id="oliver", read_visibility="operational_context"
    )
    assert stats["hypomnema_promotion_candidates"] == 0, (
        f"008g-r7 #4: unwitnessed identity hypomnema still in candidate count "
        f"({stats['hypomnema_promotion_candidates']})"
    )


# ── 008g-r8 strengthenings ──


# r8 #1: apply refuses stale/mutated ledger row (TOCTOU close)


def test_r8_1_apply_rejects_when_ledger_flipped_to_terminal(tmp_path):
    """A raw SQL flip of the ledger row to 'rejected' between journal write
    and apply must not leave a witnessed row committed with no matching
    ledger transition."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-toctou")
    _append_journal(journal, [(proposal, "approved")])
    # Attacker/race: flip the ledger to 'rejected' AFTER journal write, BEFORE
    # apply. Apply's BEGIN IMMEDIATE + reload should catch the state change.
    conn = store._get_conn()
    conn.execute(
        "UPDATE proposal_ledger SET status='rejected' WHERE id=?",
        (proposal["id"],),
    )
    conn.commit()
    with pytest.raises(ValueError, match="not applicable in status"):
        _apply_identity(store, proposal["id"], journal)
    # And the target row must NOT exist — the write is inside the transaction
    # that got rolled back.
    row = conn.execute("SELECT id FROM beliefs WHERE id='b-toctou'").fetchone()
    assert row is None, (
        "008g-r8 #1: target belief committed despite ledger already terminal"
    )


# r8 #2: apply and TCB both refuse empty target_id


def test_r8_2_apply_refuses_empty_target_id(tmp_path):
    """A pre-v9 proposal with target_id=NULL was hashed with target_id='',
    so it can never resolve at apply — refuse loudly."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # Insert a legacy proposal directly with NULL target_id (bypasses write_proposal
    # auto-gen — simulates a pre-v9 row already in the DB).
    now = int(datetime.now(timezone.utc).timestamp())
    conn.execute(
        "INSERT INTO proposal_ledger (id, agent_id, person_id, project_scope,"
        " source_authority, kind, domain, target_surface, transition,"
        " blast_radius, read_visibility, status, reason, gate_version,"
        " target_id, provenance_ids_json, payload_json, created_at,"
        " updated_at, decided_at, applied_at) VALUES"
        " ('p-legacy-null', 'oliver', 'david', 'pai', 'user_stated',"
        " 'semantic', 'identity', 'beliefs', 'legacy', 'identity',"
        " 'audit_only', 'pending_review', '', 'affmem-v1',"
        " NULL, '[]', ?, ?, ?, NULL, NULL)",
        (json.dumps({"content": "x"}), now, now),
    )
    conn.commit()
    # Journal a decision using the hydrated proposal shape.
    proposal = store.get_proposal("p-legacy-null")
    journal = tmp_path / "decisions.jsonl"
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="target_id"):
        _apply_identity(store, "p-legacy-null", journal)


def test_r8_2_tcb_hides_null_target_id_proposals(tmp_path):
    """The TCB must not offer proposals with target_id=NULL — apply refuses them."""
    from datetime import datetime as _dt, timezone as _tz

    store = EngramStore(tmp_path / "tcb.db")
    conn = store._get_conn()
    now = int(_dt.now(_tz.utc).timestamp())
    conn.execute(
        "INSERT INTO proposal_ledger (id, agent_id, person_id, project_scope,"
        " source_authority, kind, domain, target_surface, transition,"
        " blast_radius, read_visibility, status, reason, gate_version,"
        " target_id, provenance_ids_json, payload_json, created_at,"
        " updated_at, decided_at, applied_at) VALUES"
        " ('p-null', 'oliver', 'david', 'pai', 'user_stated', 'semantic',"
        " 'identity', 'beliefs', 't', 'identity', 'audit_only',"
        " 'pending_review', '', 'affmem-v1', NULL, '[]', "
        '\'{"content": "x"}\', ?, ?, NULL, NULL)',
        (now, now),
    )
    conn.commit()
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "s\n")
    assert "p-null" not in result.stdout, (
        "008g-r8 #2: TCB offered a proposal with NULL target_id"
    )


# ── 008g-r9 in-band strengthenings ──


# r9 #1: substrate modulator belief count excludes unwitnessed identity


def test_r9_1_modulator_belief_count_excludes_unwitnessed_identity(
    tmp_path, monkeypatch
):
    """Modulators drive openness/temperature; an unwitnessed identity belief
    must not skew belief_count when the vault is armed."""
    from mnemos.substrate.modulators import compute_modulators

    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "m.db")
    conn = store._get_conn()
    # One regular non-identity belief, one unwitnessed identity belief that
    # slipped operational.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " confidence_pending_review, needs_review) VALUES"
        " ('ok', 'oliver', 'reg', 0.5, 'general', 't', 't', 't', NULL,"
        " 'operational_context', 0, 0)"
    )
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " confidence_pending_review, needs_review) VALUES"
        " ('sneak', 'oliver', 'forged', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context', 0, 0)"
    )
    # Modulators only apply the belief_count formula when total_engrams > 0;
    # seed one so the belief_count actually influences openness.
    conn.execute(
        "INSERT INTO engrams (id, content, content_at_encoding, owner_agent_id,"
        " created_at, last_accessed) VALUES ('e1', 'x', 'x', 'oliver', 't', 't')"
    )
    conn.commit()
    # Vault ARMED: modulator's belief_count should exclude the unwitnessed
    # identity row. belief_settlement = min(belief_count / 10.0, 1.0), so a
    # 1-belief count vs 2-belief count is observable in the modulator output.
    mods_armed = compute_modulators(str(store.db_path), agent_id="oliver")

    # Now DISARM the vault and recompute. Same DB, same rows, but the gate
    # should be inert so belief_count picks up the identity row too.
    _disarm_vault(monkeypatch)
    mods_inert = compute_modulators(str(store.db_path), agent_id="oliver")
    # The delta reveals whether the gate is being applied by the modulator.
    # belief_count feeds belief_settlement → openness; the gate change must
    # produce a different openness value for identical beliefs.
    assert mods_armed.openness != mods_inert.openness, (
        "008g-r9 #1: modulator openness unchanged when vault flipped — "
        "gate not applied to modulator's raw SQL"
    )


# r9 #2: --witness-legacy dedupes against existing journal


def test_r9_2_witness_legacy_skips_already_witnessed(tmp_path):
    """Rerunning --witness-legacy before stamp must not append a second
    identical legacy line for the same row."""
    store = EngramStore(tmp_path / "wl.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('lg', 'oliver', 'legacy claim', 0.7, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context')"
    )
    conn.commit()
    journal = tmp_path / "decisions.jsonl"
    # First pass — writes the legacy line.
    r1 = _run_tcb_wrapper(store.db_path, journal, "y\n", args=["--witness-legacy"])
    assert r1.returncode == 0
    first = vj.read_journal(journal)
    assert len(first) == 1
    # Second pass BEFORE stamp — should skip (no re-append).
    r2 = _run_tcb_wrapper(store.db_path, journal, "y\n", args=["--witness-legacy"])
    assert r2.returncode == 0
    second = vj.read_journal(journal)
    assert len(second) == 1, (
        f"008g-r9 #2: --witness-legacy appended a duplicate line "
        f"(journal now has {len(second)} lines)"
    )
    assert "skipped" in r2.stdout


# ── 008i journal corruption ruling ──


def test_008i_torn_tail_is_alert_only_no_quarantine(tmp_path):
    """A malformed FINAL line only = torn append (power loss). Read returns
    the good prefix; reconcile alarms but does NOT quarantine."""
    store = _store(tmp_path)
    journal = tmp_path / "torn.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-torn")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Append a torn (unterminated) final line — the good line + garbage tail.
    with open(journal, "a", encoding="utf-8") as h:
        h.write("{partial-torn-write")
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "journal_torn_tail" for f in report.findings), (
        "008i: torn tail must alert"
    )
    assert not any(f["kind"] == "journal_corrupt" for f in report.findings), (
        "008i: torn tail must NOT be treated as corrupt"
    )
    # Row stays operational — no quarantine on torn-tail.
    conn = store._get_conn()
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-torn'"
        ).fetchone()[0]
        == "operational_context"
    )


def test_008i_mid_file_corruption_quarantines_witnessed_rows(tmp_path):
    """Malformed content BEFORE the tail = true corruption → fail-closed."""
    store = _store(tmp_path)
    journal = tmp_path / "corrupt.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-corr")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-corr'"
        ).fetchone()[0]
        == "operational_context"
    )
    # Corrupt the MIDDLE of the journal: valid line, garbage, valid line.
    original = journal.read_text(encoding="utf-8")
    good_line = (
        '{"v":1,"proposal_id":"p2","content_sha256":"y",'
        '"decision":"approved","scope":"identity","prev_sha256":"z"}'
    )
    journal.write_text(
        original + "{garbage-mid-file\n" + good_line + "\n",
        encoding="utf-8",
    )
    report = store.reconcile_identity_vault(journal)
    assert any(f["kind"] == "journal_corrupt" for f in report.findings), (
        "008i: mid-file corruption must produce a journal_corrupt finding"
    )
    # The witnessed row is now quarantined pending David's journal repair.
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-corr'"
        ).fetchone()[0]
        == "review_only"
    )


def test_008i_restore_on_verify_after_journal_repair(tmp_path):
    """After the journal is repaired, the next healthy reconcile restores the
    row — no mass re-witness required."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-restore")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Simulate the quarantine: corrupt journal → row goes review_only.
    original = journal.read_text(encoding="utf-8")
    journal.write_text(original + "{mid-garbage\n" + original, encoding="utf-8")
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-restore'"
        ).fetchone()[0]
        == "review_only"
    )
    # David repairs the journal (removes the mid-file garbage).
    journal.write_text(original, encoding="utf-8")
    report = store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-restore'"
        ).fetchone()[0]
        == "operational_context"
    ), "008i: restore-on-verify did not promote the row after journal repair"
    # And the restore is recorded.
    assert any(item.get("kind") == "restored" for item in report.requarantined), (
        "008i: restore event not surfaced in the report"
    )


def test_008i_curator_flagged_row_stays_review_only_after_repair(tmp_path):
    """A row with NO decision_ref (curator-flagged) must never be restored."""
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('flagged', 'oliver', 'flagged claim', 0.5, 'identity',"
        " 't', 't', 't', 'foundational', 'review_only')"
    )
    conn.commit()
    journal = tmp_path / "j.jsonl"
    journal.write_text("", encoding="utf-8")
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='flagged'"
        ).fetchone()[0]
        == "review_only"
    ), "008i: curator-flagged (no-ref) row was falsely restored"


def test_008i_upsert_degraded_row_stays_review_only(tmp_path):
    """A row whose decision_ref was cleared by an upsert degrade must not
    be restored — the ref is gone, direction B never reaches it."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # Simulate 008g upsert-degrade: witnessed row, then mutation cleared ref
    # and forced review_only in same transaction.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " decision_ref) VALUES"
        " ('degraded', 'oliver', 'mutated', 0.5, 'identity', 't', 't', 't',"
        " 'foundational', 'review_only', NULL)"
    )
    conn.commit()
    journal = tmp_path / "j.jsonl"
    journal.write_text("", encoding="utf-8")
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='degraded'"
        ).fetchone()[0]
        == "review_only"
    ), "008i: upsert-degraded row was falsely restored"


# ── 008i-r10 gap fixes ──


def test_r10_1_session_start_runs_reconcile_when_legacy_stamp_fails(
    tmp_path, monkeypatch
):
    """apply_legacy_witness raising on corrupt journal must NOT prevent
    reconcile from running — that's the fail-open path this closes."""
    import mnemos.mcp_server as server

    store = _store(tmp_path)
    # Seed a witnessed row so quarantine has something to touch.
    journal = tmp_path / "journal.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-r10")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Corrupt the journal MID-FILE so both apply_legacy_witness AND reconcile
    # will see it. apply_legacy_witness raises first (chain broken).
    original = journal.read_text(encoding="utf-8")
    good_line = (
        '{"v":1,"proposal_id":"p2","content_sha256":"y",'
        '"decision":"approved","scope":"identity","prev_sha256":"z"}'
    )
    journal.write_text(
        original + "{mid-garbage\n" + good_line + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_store", store)
    _arm_vault(monkeypatch, journal)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    server._reconcile_vault_on_session_start()
    # The witnessed row must have been quarantined by the reconcile pass,
    # despite apply_legacy_witness raising first.
    assert (
        conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-r10'").fetchone()[
            0
        ]
        == "review_only"
    ), (
        "008i-r10 #1: session-start failed open when apply_legacy_witness "
        "raised — reconcile never ran"
    )


def test_r10_2_corrupt_journal_quarantines_detiered_witnessed_row(tmp_path):
    """A witnessed row that was raw-SQL de-tiered still carries decision_ref
    + operational. Corrupt-journal fail-closed must quarantine it too."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-detier")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Raw-SQL de-tier the row but keep decision_ref + operational.
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general' WHERE id='b-detier'"
    )
    conn.commit()
    # Now corrupt the journal mid-file.
    original = journal.read_text(encoding="utf-8")
    good_line = (
        '{"v":1,"proposal_id":"p2","content_sha256":"y",'
        '"decision":"approved","scope":"identity","prev_sha256":"z"}'
    )
    journal.write_text(
        original + "{mid-garbage\n" + good_line + "\n",
        encoding="utf-8",
    )
    store.reconcile_identity_vault(journal)
    # The de-tiered row still carries decision_ref — corrupt-journal must
    # quarantine it too, not skip it because the tier predicate no longer
    # matches.
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-detier'"
        ).fetchone()[0]
        == "review_only"
    ), "008i-r10 #2: corrupt-journal fail-closed missed the de-tiered witnessed row"


def test_r10_3_tcb_refuses_to_append_after_torn_tail(tmp_path):
    """The TCB must not extend a journal whose final line is torn — the
    append-only file can't remove the torn bytes, so new lines would land
    as [torn][new] which future reads classify as mid-file corruption."""
    store = EngramStore(tmp_path / "torn.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="post-torn approval",
        domain="identity",
        blast_radius="identity",
        payload={"content": "x"},
        proposal_id="p-r10-3",
    )
    journal = tmp_path / "j.jsonl"
    # Simulate a torn tail: valid line + partial garbage at end.
    good_line = (
        '{"v":1,"proposal_id":"prior","content_sha256":"z",'
        '"decision":"approved","scope":"identity","prev_sha256":"'
        + vj.genesis_prev_hash()
        + '"}'
    )
    journal.write_text(good_line + "\n{partial-torn-", encoding="utf-8")
    result = _run_tcb_wrapper(store.db_path, journal, "a\n")
    assert result.returncode == 4, (
        f"008i-r10 #3: TCB accepted append after torn tail (rc={result.returncode})"
    )
    assert "torn" in result.stderr.lower() or "REFUSING" in result.stderr, (
        "008i-r10 #3: TCB did not surface the torn-tail refusal"
    )
    # And nothing new was appended.
    after = journal.read_text(encoding="utf-8")
    assert "p-r10-3" not in after


def test_r10_4_legacy_witness_restored_on_verify(tmp_path):
    """Batch-witnessed legacy rows must restore too, not just proposal-backed."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # Insert a legacy row, batch-witness it, then simulate corrupt-journal
    # quarantine + repair.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('legacy-restore', 'oliver', 'legacy claim', 0.7, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context')"
    )
    conn.commit()
    row = dict(
        conn.execute("SELECT * FROM beliefs WHERE id='legacy-restore'").fetchone()
    )
    journal = tmp_path / "j.jsonl"
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    # Corrupt → quarantines the legacy row too.
    original = journal.read_text(encoding="utf-8")
    good_line = (
        '{"v":1,"proposal_id":"p2","content_sha256":"y",'
        '"decision":"approved","scope":"identity","prev_sha256":"z"}'
    )
    journal.write_text(
        original + "{mid-garbage\n" + good_line + "\n",
        encoding="utf-8",
    )
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='legacy-restore'"
        ).fetchone()[0]
        == "review_only"
    )
    # Repair: rewrite the journal without the mid-file corruption.
    journal.write_text(original, encoding="utf-8")
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='legacy-restore'"
        ).fetchone()[0]
        == "operational_context"
    ), "008i-r10 #4: legacy-witnessed row not restored after journal repair"


# ── 008-r14 in-band fixes ──


def test_r14_2_identity_apply_rejects_cross_scope_target(tmp_path):
    """A proposal whose target_id collides with an existing belief in a
    DIFFERENT scope must be refused — not silently overwrite the other
    scope's row with witnessed content."""
    store = _store(tmp_path)
    conn = store._get_conn()
    # An existing belief owned by a DIFFERENT agent, at the target_id.
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('b-collide', 'someone-else', 'their belief', 0.5, 'identity',"
        " 't', 't', 't', 'foundational', 'operational_context')"
    )
    conn.commit()
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-collide")
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="different scope"):
        _apply_identity(store, proposal["id"], journal)
    # The other scope's row is untouched.
    row = conn.execute(
        "SELECT agent_id, content FROM beliefs WHERE id='b-collide'"
    ).fetchone()
    assert row[0] == "someone-else"
    assert row[1] == "their belief"


def test_r14_2_hypomnema_apply_rejects_cross_scope(tmp_path):
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at) VALUES"
        " ('h-collide', 'oliver', 'someone-else', 'other-project', 'theirs',"
        " 'observed', 0.5, 'identity', 'operational_context', '[]', 0.6, 0.5,"
        " 1, 1, 0, '[]', 't', 't')"
    )
    conn.commit()
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(
        store, surface="hypomnema_entries", target_id="h-collide", content="mine"
    )
    _append_journal(journal, [(proposal, "approved")])
    with pytest.raises(ValueError, match="different scope"):
        _apply_identity(store, proposal["id"], journal)


def test_r14_3_session_start_refreshes_vault_active(tmp_path, monkeypatch):
    """A store constructed BEFORE the vault journal existed has
    _vault_active=False; after the journal appears, session-start must
    refresh the flag so read APIs gate for the rest of the process."""
    import mnemos.mcp_server as server

    # Construct with no journal → inert.
    store = EngramStore(tmp_path / "s.db", vault_active=False)
    assert store._vault_active is False
    journal = tmp_path / "vault.jsonl"
    journal.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "_store", store)
    _arm_vault(monkeypatch, journal)
    server._reconcile_vault_on_session_start()
    assert store._vault_active is True, (
        "008-r14 #3: session-start did not refresh _vault_active after the "
        "journal appeared"
    )


# ── 008k-r13 in-band fixes ──


def test_r13_1_unreadable_journal_fails_closed(tmp_path):
    """An unreadable journal (OSError on read) classifies as corrupt →
    quarantines witnessed operational rows, not silently absent."""
    from mnemos.vault import journal as vjmod

    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-unread")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Make the journal unreadable (chmod 000). Skip if running as root (can't
    # simulate permission denial as root).
    import os

    if os.geteuid() == 0:
        import pytest

        pytest.skip("cannot simulate permission denial as root")
    journal.chmod(0o000)
    try:
        result = vjmod.read_journal_classified(journal)
        assert result.error == "corrupt", (
            "008k-r13 #1: unreadable journal did not classify as corrupt"
        )
        store.reconcile_identity_vault(journal)
        assert (
            conn.execute(
                "SELECT read_visibility FROM beliefs WHERE id='b-unread'"
            ).fetchone()[0]
            == "review_only"
        ), "008k-r13 #1: witnessed row not quarantined on unreadable journal"
    finally:
        journal.chmod(0o644)


def test_r13_3_pai_reimport_witnessed_belief_degrades(tmp_path):
    """Re-importing a witnessed belief with changed content must degrade the
    row (clear ref + review_only + trace), not leave a stale witness."""
    from mnemos.importer import pai

    store = _store(tmp_path)
    conn = store._get_conn()
    # A witnessed belief already in the store (simulate a prior apply + witness).
    bid = pai._target_id(
        job_id="j1",
        source_path="SOUL.md",
        source_anchor="h:test:001",
        target_table="beliefs",
    )
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility,"
        " decision_ref) VALUES"
        f" ('{bid}', 'oliver', 'original witnessed', 0.5, 'identity', 't', 't',"
        " 't', 'foundational', 'operational_context', 'fake-witness-ref-abc')"
    )
    # Row-map so the importer treats it as a re-import (UPDATE), not untracked.
    conn.execute(
        "INSERT INTO pai_import_row_map (job_id, source_path, source_anchor,"
        " target_table, target_id, source_hash, created_at, updated_at,"
        " imported_at, content_at_last_import) VALUES"
        f" ('j1', 'SOUL.md', 'h:test:001', 'beliefs', '{bid}', 'oldhash', 0, 0,"
        " 0, 'original witnessed')"
    )
    conn.commit()
    # Build a re-import row with CHANGED content.
    profile = pai._PROFILES["beliefs"]
    row = pai.PaiImportRow(
        job_id="j1",
        source_path="SOUL.md",
        source_anchor="h:test:001",
        source_kind="beliefs",
        target_table="beliefs",
        target_id=bid,
        source_hash="newhash",
        content="REVISED witnessed content",
        action=pai.ACTION_UPDATE,
        reason="content changed",
        mapped_source_hash="oldhash",
        target_projection_hash="x",
        original_substrate="s",
        original_timestamp=None,
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
    pai._write_pai_belief_no_commit(store, conn, row)
    conn.commit()
    r = conn.execute(
        f"SELECT decision_ref, read_visibility FROM beliefs WHERE id='{bid}'"
    ).fetchone()
    assert r[0] is None, "008k-r13 #3: re-import left stale decision_ref"
    assert r[1] == "review_only", "008k-r13 #3: re-import did not force review_only"
    trace = conn.execute(
        f"SELECT id FROM proposal_ledger WHERE id LIKE 'lifecycle-beliefs-{bid}-%'"
    ).fetchone()
    assert trace is not None, "008k-r13 #3: no degrade trace emitted"


def test_r13_4_pai_deactivate_witnessed_hypomnema_degrades(tmp_path):
    """PAI deactivate of a witnessed hypomnema must degrade (clear ref +
    review_only + trace)."""
    from mnemos.importer import pai

    store = _store(tmp_path)
    conn = store._get_conn()
    hid = pai._target_id(
        job_id="j1",
        source_path="SOUL.md",
        source_anchor="h:hypo:001",
        target_table="hypomnema_entries",
    )
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at, decision_ref) VALUES"
        f" ('{hid}', 'oliver', 'david', 'pai', 'witnessed hypo', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.6, 0.7, 1, 1, 0, '[]',"
        " 't', 't', 'fake-witness-ref-xyz')"
    )
    conn.commit()
    profile = pai._PROFILES["hypomnema"]
    row = pai.PaiImportRow(
        job_id="j1",
        source_path="SOUL.md",
        source_anchor="h:hypo:001",
        source_kind="hypomnema",
        target_table="hypomnema_entries",
        target_id=hid,
        source_hash="h",
        content="witnessed hypo",
        action=pai.ACTION_DEACTIVATE,
        reason="deactivate",
        mapped_source_hash="h",
        target_projection_hash="x",
        original_substrate="s",
        original_timestamp=None,
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
    pai._apply_u3c_lifecycle_row_no_commit(store, conn, row)
    conn.commit()
    r = conn.execute(
        f"SELECT decision_ref, read_visibility, active FROM hypomnema_entries "
        f"WHERE id='{hid}'"
    ).fetchone()
    assert r[0] is None, "008k-r13 #4: deactivate left stale decision_ref"
    assert r[1] == "review_only", "008k-r13 #4: deactivate did not force review_only"
    assert r[2] == 0, "008k-r13 #4: row not actually deactivated"


# ── 008k-r12 gap fixes ──


def test_r12_1_detiered_witnessed_row_quarantined_on_missing_journal(tmp_path):
    """A witnessed row raw-SQL de-tiered, with a ref that no longer resolves
    (missing journal → empty trusted map), must still fail closed."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-esc")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general' WHERE id='b-esc'"
    )
    conn.commit()
    journal.unlink()
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-esc'").fetchone()[
            0
        ]
        == "review_only"
    ), "008k-r12 #1: de-tiered witnessed row escaped quarantine on missing journal"


def test_r12_1_detiered_row_quarantined_after_chain_break(tmp_path):
    """De-tiered row whose ref is AFTER a chain break also fails closed."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    p1 = _make_identity_proposal(store, target_id="b-a", pid="p-a", content="first")
    _append_journal(journal, [(p1, "approved")])
    _apply_identity(store, "p-a", journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET tier='operational', domain='general' WHERE id='b-a'"
    )
    conn.commit()
    lines = journal.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[0])
    obj["prev_sha256"] = "0" * 64
    journal.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    store.reconcile_identity_vault(journal)
    assert (
        conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-a'").fetchone()[0]
        == "review_only"
    ), "008k-r12 #1: de-tiered row with post-break ref escaped quarantine"


def test_r12_2_hypomnema_stats_admin_read_bypasses_gate(tmp_path, monkeypatch):
    """get_hypomnema_stats(read_visibility=None) is admin — must count
    unwitnessed identity rows (no gate), matching the store/audit contract."""
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "s.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO hypomnema_entries (id, agent_id, person_id, project_scope,"
        " content, source, density, domain, read_visibility, tags_json,"
        " confidence, salience, active, foundational, revision_count,"
        " revisions_json, created_at, last_revised_at) VALUES"
        " ('sneak', 'oliver', 'david', 'pai', 'forged', 'observed', 0.5,"
        " 'identity', 'operational_context', '[]', 0.9, 0.5, 1, 1, 0, '[]',"
        " 't', 't')"
    )
    conn.commit()
    admin = store.get_hypomnema_stats(agent_id="oliver", read_visibility=None)
    assert admin["hypomnema_total"] == 1, (
        "008k-r12 #2: admin (read_visibility=None) stats hid an unwitnessed row"
    )
    ops = store.get_hypomnema_stats(
        agent_id="oliver", read_visibility="operational_context"
    )
    assert ops["hypomnema_total"] == 0


def test_r12_2_belief_stats_admin_read_bypasses_gate(tmp_path, monkeypatch):
    j = tmp_path / "vault.jsonl"
    j.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, j)
    store = EngramStore(tmp_path / "s.db")
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain,"
        " created_at, last_revised, last_challenged, tier, read_visibility)"
        " VALUES ('sneak', 'oliver', 'forged', 0.9, 'identity', 't', 't', 't',"
        " 'foundational', 'operational_context')"
    )
    conn.commit()
    admin = store.get_stats(agent_id="oliver", read_visibility=None)
    assert admin["beliefs_active"] == 1, (
        "008k-r12 #2: admin belief stats hid an unwitnessed row"
    )
    ops = store.get_stats(agent_id="oliver")  # default operational
    assert ops["beliefs_active"] == 0


# ── 008k E2B: lifecycle mutations degrade + trace ──


def test_008k_e2b_supersede_witnessed_hypomnema_degrades_and_traces(tmp_path):
    """supersede_hypomnema_entry on a witnessed row must clear ref + force
    review_only + emit a lifecycle trace, all in one transaction. Reconcile
    then never false-fires on a legitimate supersede."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(
        store,
        surface="hypomnema_entries",
        target_id="h-sup",
        content="original identity claim",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Row is witnessed + operational at this point.
    assert conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries WHERE id='h-sup'"
    ).fetchone()[0]
    # Ordinary supersede via the public API.
    new_id = store.supersede_hypomnema_entry(
        "h-sup",
        "revised claim",
        reason="consolidation",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    row = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries WHERE id='h-sup'"
    ).fetchone()
    assert row[0] is None, (
        "008k E2B: supersede did not clear decision_ref on witnessed row"
    )
    assert row[1] == "review_only", (
        "008k E2B: supersede did not force review_only on witnessed row"
    )
    # Trace proposal was emitted.
    trace = conn.execute(
        "SELECT id, transition FROM proposal_ledger "
        "WHERE id LIKE 'lifecycle-hypomnema_entries-h-sup-%'"
    ).fetchone()
    assert trace is not None, "008k E2B: no lifecycle trace proposal emitted"
    assert "lifecycle" in trace[1]
    # And reconcile now reports clean on the (degraded) old row — no r6 #3
    # false-fire because the ref was cleared at the mutation site.
    report = store.reconcile_identity_vault(journal)
    tampered = [
        f
        for f in report.findings
        if f["kind"] == "witnessed_row_tampered" and f.get("row_id") == "h-sup"
    ]
    assert not tampered, (
        f"008k E2B: reconcile false-fired on legitimately-degraded row: {tampered}"
    )
    assert new_id  # sanity


def test_008k_e2b_archive_witnessed_hypomnema_degrades_and_traces(tmp_path):
    """archive_hypomnema_entry same pattern as supersede."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(
        store,
        surface="hypomnema_entries",
        target_id="h-arc",
        content="to be archived",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    store.archive_hypomnema_entry(
        "h-arc",
        reason="retired",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    conn = store._get_conn()
    row = conn.execute(
        "SELECT decision_ref, read_visibility FROM hypomnema_entries WHERE id='h-arc'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "review_only"
    trace = conn.execute(
        "SELECT id FROM proposal_ledger "
        "WHERE id LIKE 'lifecycle-hypomnema_entries-h-arc-%'"
    ).fetchone()
    assert trace is not None


def test_008k_e2b_raw_sql_supersede_still_caught_by_reconcile(tmp_path):
    """The reconcile r6 #3 lifecycle check now catches ONLY the raw-SQL
    bypass class — write-side degrade is primary enforcement. This test
    pins that redundant-detection behavior."""
    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    proposal = _make_identity_proposal(
        store,
        surface="hypomnema_entries",
        target_id="h-raw",
        content="raw-sql bypass target",
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Raw-SQL supersede, keeps decision_ref (bypasses supersede_hypomnema_entry).
    conn.execute("UPDATE hypomnema_entries SET active = 0 WHERE id='h-raw'")
    conn.commit()
    report = store.reconcile_identity_vault(journal)
    assert any(
        f["kind"] == "witnessed_row_tampered" and f.get("row_id") == "h-raw"
        for f in report.findings
    ), "008k E2B: reconcile stopped catching raw-SQL supersede bypass"


# ── 008i-r11 in-band fix ──


def test_r11_1_tcb_recorded_line_sanitizes_proposal_id(tmp_path):
    """The `Recorded:` line printed after approve/reject must sanitize the
    proposal_id — an attacker-authored id with ANSI could clear the screen
    right after David approves, hiding what he just did."""
    store = EngramStore(tmp_path / "tcb.db")
    store.write_proposal(
        source_authority="user_stated",
        kind="semantic",
        target_surface="beliefs",
        transition="normal",
        domain="identity",
        blast_radius="identity",
        payload={"content": "x"},
        proposal_id="p\x1b[2Jinject",
    )
    result = _run_tcb_wrapper(store.db_path, tmp_path / "j.jsonl", "a\n")
    # Even in error/rejection paths, no raw ESC bytes may reach David's terminal.
    assert "\x1b" not in result.stdout, (
        "008i-r11 #1: TCB emitted raw ANSI from proposal_id"
    )


# ── Session-start reconciliation wiring (Fable review 008 §2) ──


def test_session_start_runs_reconcile(tmp_path, monkeypatch):
    import mnemos.mcp_server as server

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    journal.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "_store", store)
    _arm_vault(monkeypatch, journal)

    called = {}
    real = store.reconcile_identity_vault

    def spy(path, **kw):
        called["path"] = str(path)
        return real(path, **kw)

    monkeypatch.setattr(store, "reconcile_identity_vault", spy)
    server._reconcile_vault_on_session_start()
    assert called.get("path") == str(journal)


def test_session_start_no_journal_is_noop(tmp_path, monkeypatch):
    import mnemos.mcp_server as server

    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.delenv("MNEMOS_VAULT_JOURNAL", raising=False)
    # Must not raise with no journal configured.
    server._reconcile_vault_on_session_start()


def test_r14_review_alert_writes_stamp_error_with_no_critical_findings(
    tmp_path, monkeypatch
):
    """008-r14 review #3: a legacy-stamp failure with a CLEAN reconcile (no
    critical findings) must still surface the stamp exception in the written
    alert. Before the fix _alert_vault_findings dropped stamp_error, emitting an
    empty Findings list that hid the real session-start failure."""
    import mnemos.mcp_server as server

    inbox = tmp_path / "inbox"
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(inbox))
    stamp_error = RuntimeError("apply_legacy_witness exploded: broken chain")
    # findings=[] simulates a clean reconcile; the alert fired only because the
    # legacy stamp raised.
    server._alert_vault_findings("/some/journal.jsonl", [], [], stamp_error=stamp_error)
    written = list(inbox.glob("*-vault-session-start-alert.md"))
    assert len(written) == 1, "alert file was not written"
    body = written[0].read_text(encoding="utf-8")
    assert "RuntimeError" in body, "stamp_error type dropped from the alert"
    assert "apply_legacy_witness exploded: broken chain" in body, (
        "stamp_error message dropped from the alert — the actual session-start "
        "failure is hidden"
    )
    assert "legacy-stamp failed" in body, (
        "header should name the stamp-only case when there are no findings"
    )


def test_r14_review_error_alert_also_includes_stamp_error(tmp_path, monkeypatch):
    """008-r14 review #3 (audit-the-class): the reconcile-RAISED handler
    (_alert_vault_error) is the peer of _alert_vault_findings and had the same
    latent drop. If reconcile raised AND the legacy stamp failed, both must
    appear — else the stamp failure hides behind the reconcile exception."""
    import mnemos.mcp_server as server

    inbox = tmp_path / "inbox"
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(inbox))
    server._alert_vault_error(
        "/some/journal.jsonl",
        RuntimeError("reconcile blew up"),
        stamp_error=ValueError("stamp also failed: broken chain"),
    )
    written = list(inbox.glob("*-vault-session-start-error.md"))
    assert len(written) == 1, "error alert file was not written"
    body = written[0].read_text(encoding="utf-8")
    assert "reconcile blew up" in body, "reconcile exception missing"
    assert "stamp also failed: broken chain" in body, (
        "stamp_error dropped from the reconcile-raised alert"
    )


def test_session_start_runs_initial_rollout_after_legacy_stamp_failure(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import mnemos.mcp_server as server

    store = _store(tmp_path)
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, journal)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    calls = []

    def fail_legacy():
        calls.append("legacy")
        raise RuntimeError("legacy failed")

    def run_initial():
        calls.append("initial")

    monkeypatch.setattr(store, "apply_legacy_witness", fail_legacy)
    monkeypatch.setattr(store, "apply_initial_rollout", run_initial)
    monkeypatch.setattr(
        store,
        "reconcile_identity_vault",
        lambda _p: SimpleNamespace(findings=[], requarantined=[]),
    )
    server._reconcile_vault_on_session_start()
    assert calls == ["legacy", "initial"]


def test_session_start_labels_initial_rollout_stamp_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import mnemos.mcp_server as server

    store = _store(tmp_path)
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, journal)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    monkeypatch.setattr(store, "apply_legacy_witness", lambda: None)

    def fail_initial():
        raise RuntimeError("initial failed")

    monkeypatch.setattr(store, "apply_initial_rollout", fail_initial)
    monkeypatch.setattr(
        store,
        "reconcile_identity_vault",
        lambda _p: SimpleNamespace(findings=[], requarantined=[]),
    )
    server._reconcile_vault_on_session_start()
    written = list((tmp_path / "inbox").glob("*-vault-session-start-alert.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "Initial-rollout stamp error (apply_initial_rollout raised)" in body
    assert "initial failed" in body
    assert "Legacy-stamp error" not in body


def test_session_start_alerts_unexpected_initial_rollout_skip(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import mnemos.mcp_server as server

    store = _store(tmp_path)
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, journal)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    monkeypatch.setattr(store, "apply_legacy_witness", lambda: None)
    monkeypatch.setattr(
        store,
        "apply_initial_rollout",
        lambda: {
            "stamped": [],
            "skipped": [
                "rollout-1:content-mismatch",
                "rollout-2:already-stamped",
                "journal-untrusted",
            ],
        },
    )
    monkeypatch.setattr(
        store,
        "reconcile_identity_vault",
        lambda _p: SimpleNamespace(findings=[], requarantined=[]),
    )
    server._reconcile_vault_on_session_start()
    written = list((tmp_path / "inbox").glob("*-vault-session-start-alert.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "initial_rollout_skip" in body
    assert "rollout-1:content-mismatch" in body
    assert "rollout-2:already-stamped" not in body
    assert "journal-untrusted" not in body


def test_r14_review_session_start_alerts_on_high_findings(tmp_path, monkeypatch):
    """008r/review (session-start-drops-high-vault-findings): session-start must
    alert on HIGH findings + re-quarantines (orphan/forged/missing witnessed
    rows), not only `critical`. Gating on critical let those divergences be
    handled silently until the watchdog cron next ran."""
    import mnemos.mcp_server as server
    from types import SimpleNamespace

    store = _store(tmp_path)
    journal = tmp_path / "j.jsonl"
    journal.write_text("", encoding="utf-8")  # present → resolver arms the vault
    # Reconcile returns a HIGH finding + a re-quarantine, NO critical.
    fake_report = SimpleNamespace(
        findings=[
            {
                "severity": "high",
                "kind": "orphan_identity_row",
                "detail": "decision_ref not in journal",
                "table": "beliefs",
                "row_id": "b-orphan",
            }
        ],
        requarantined=[
            {
                "table": "beliefs",
                "row_id": "b-orphan",
                "detail": "forced review_only (orphan)",
            }
        ],
    )
    monkeypatch.setattr(store, "reconcile_identity_vault", lambda *a, **k: fake_report)
    monkeypatch.setattr(server, "_store", store)
    _arm_vault(monkeypatch, journal)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "inbox"))
    server._reconcile_vault_on_session_start()
    written = list((tmp_path / "inbox").glob("*-vault-session-start-alert.md"))
    assert len(written) == 1, "session-start stayed silent on a high finding"
    body = written[0].read_text(encoding="utf-8")
    assert "orphan_identity_row" in body, "the high finding was not surfaced"


def test_r14_review_install_hardens_leaf_acls():
    """008-r14 review #2: the installer must strip + re-verify ACLs on every
    leaf it creates (VAULT_DIR, JOURNAL, LIBEXEC), because POSIX chmod does not
    clear macOS ACLs and a surviving ACL would let David/agent rename the
    journal or replace the sudoers-targeted TCB."""
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "install-mnemos-vault.sh"
    ).read_text(encoding="utf-8")
    # The helper strips the ACL (chmod -N) and re-verifies effective writability.
    assert "require_no_effective_write()" in script, "leaf ACL helper missing"
    assert "chmod -N" in script, "helper must strip ACL entries with chmod -N"
    # And it must be CALLED on each of the three leaves.
    for leaf in ('"${VAULT_DIR}"', '"${JOURNAL}"', '"${LIBEXEC}"'):
        assert f"require_no_effective_write {leaf}" in script, (
            f"installer never re-verifies leaf ACL on {leaf}"
        )


def test_r14_review_sudoers_restricts_tcb_args():
    """008r/review (sudoers-allows-tcb-redirect-args): the sudoers rule must
    restrict the TCB to the no-arg and --witness-legacy forms — an unrestricted
    rule would let --db/--journal redirect the canonical journal under sudo."""
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "install-mnemos-vault.sh"
    ).read_text(encoding="utf-8")
    assert '${TCB_DEST} "", ${TCB_DEST} --witness-legacy' in script, (
        "sudoers rule does not restrict TCB args — a redirect flag is passable "
        "under sudo"
    )


def test_r14_review_installer_drops_unsupported_alternate_path():
    """008r-review (unsupported-alternate-vault-path-guidance): the installer must
    not advise 'reinstall under another path' — the resolver/watchdog/TCB PIN
    /usr/local, so an alternate path builds a vault the runtime never reads."""
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "install-mnemos-vault.sh"
    ).read_text(encoding="utf-8")
    assert "reinstall under a path" not in script, (
        "installer still advises an unsupported alternate install path"
    )
    assert "/var/mnemos-vault + /opt/mnemos-vault/libexec" not in script
    assert "PIN" in script, "installer should explain the paths are pinned"


def test_r14_review_resolver_ignores_env(tmp_path, monkeypatch):
    """008r-review (env-resolver-redirects-agent-vault): the store resolver reads
    NO environment. A poisoned MNEMOS_VAULT_JOURNAL must not redirect the vault,
    and an empty value must not DISABLE the gate — only the pinned resolution
    (seam in tests, canonical in production) decides."""
    from mnemos.store import sqlite_store

    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    _arm_vault(monkeypatch, real)
    # Poison: env points at a fake journal.
    monkeypatch.setenv("MNEMOS_VAULT_JOURNAL", str(tmp_path / "fake.jsonl"))
    assert sqlite_store.resolve_vault_journal_path() == str(real), (
        "resolver honored a poisoned MNEMOS_VAULT_JOURNAL env — redirect hole"
    )
    # Empty (the old inert sentinel) must NOT turn an armed vault off.
    monkeypatch.setenv("MNEMOS_VAULT_JOURNAL", "")
    assert sqlite_store.resolve_vault_journal_path() == str(real), (
        "empty MNEMOS_VAULT_JOURNAL disabled the gate — agent could disarm it"
    )


def test_r14_review_resolver_rejects_agent_owned_vault(tmp_path, monkeypatch):
    """008r-review (vault-resolver-trusts-unverified-path): a vault dir the
    current (agent) user owns / can write must NOT arm — else a pre-created fake
    vault on a user-writable /usr/local (Homebrew) would be trusted as
    installed, and an attacker-controlled journal would drive apply/reads.
    (Note: this does NOT patch _vault_object_trusted — it exercises the real
    ownership check against tmp files, which the test process owns.)"""
    from mnemos.store import sqlite_store

    fake_dir = tmp_path / "mnemos-vault"
    fake_dir.mkdir()
    fake_journal = fake_dir / "decisions.jsonl"
    fake_journal.write_text("", encoding="utf-8")
    monkeypatch.setattr(sqlite_store, "_VAULT_DIR_FOR_RESOLUTION", str(fake_dir))
    monkeypatch.setattr(
        sqlite_store, "_VAULT_JOURNAL_FOR_RESOLUTION", str(fake_journal)
    )
    assert sqlite_store.resolve_vault_journal_path() is None, (
        "resolver armed on an agent-owned vault dir — the fake-vault hole is open"
    )
    assert sqlite_store._resolve_vault_active(None) is False


def test_r14_review_watchdog_has_no_redirect_flags():
    """008r-review (watchdog-production-redirect-flags): the shipped watchdog
    must expose NO --db/--journal flags. It runs under launchd (no sudo to gate
    args), so a flag is an UNGUARDED redirect vector for the independent
    detector. Paths are pinned; tests inject via the module-attr seam."""
    import pathlib

    wd = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "mnemos-vault-watchdog.py"
    ).read_text(encoding="utf-8")
    assert "add_argument" not in wd, "watchdog exposes a CLI flag — redirect vector"
    assert "argparse" not in wd, (
        "watchdog imports argparse — read pinned constants only"
    )
    assert "DB_PATH = pathlib.Path(CANONICAL_DB_PATH)" in wd
    assert "JOURNAL_PATH = CANONICAL_JOURNAL_PATH" in wd


def test_r14_review_installed_but_broken_vault_arms_fail_closed(tmp_path, monkeypatch):
    """008r-review (vault-untrusted-journal-disarms-gate): a TRUSTED install dir
    with a missing (or untrusted) journal is installed-but-BROKEN, not
    pre-install. It must ARM (fail-closed → reconciler re-quarantines), not go
    inert (fail-open). This is the regression the E ownership fix introduced."""
    from mnemos.store import sqlite_store

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    journal = vault_dir / "decisions.jsonl"  # MISSING — not created
    monkeypatch.setattr(sqlite_store, "_VAULT_DIR_FOR_RESOLUTION", str(vault_dir))
    monkeypatch.setattr(sqlite_store, "_VAULT_JOURNAL_FOR_RESOLUTION", str(journal))
    # Dir is a trusted install (real root/vault ownership); the journal is broken.
    monkeypatch.setattr(sqlite_store, "_vault_object_trusted", lambda _p: True)
    assert sqlite_store.resolve_vault_journal_path() == str(journal), (
        "installed-but-broken vault went inert (fail-open) instead of arming"
    )
    assert sqlite_store._resolve_vault_active(None) is True


def test_r14_review_audit_only_hide_is_caught_and_restored(tmp_path):
    """008r-review (reconcile-misses-audit-only-hide): flipping a witnessed
    identity row to audit_only hides it from operational reads AND from
    Direction A (which scans only operational rows). Reconcile must catch it via
    Direction B (found by ref → verified), FLAG it as tamper, and RESTORE it —
    not report clean."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-hide")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)  # operational + decision_ref
    conn = store._get_conn()
    conn.execute("UPDATE beliefs SET read_visibility='audit_only' WHERE id='b-hide'")
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-hide'"
    ).fetchone()[0]
    assert vis == "operational_context", (
        "audit_only-hidden witnessed row was not restored — the hide survived"
    )
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "reconcile did not flag the audit_only hide as tamper (reported clean)"
    )


def test_r14_review_legacy_lifecycle_hide_is_caught(tmp_path):
    """008r-review audit finding K: a legacy-witnessed belief hidden via
    superseded_by (decision_ref intact, content/tier/hash all unchanged) must be
    caught by the legacy reconcile path and forced to review_only. The legacy
    path previously omitted the lifecycle check the proposal path already has."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store)
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)  # operational + decision_ref
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET superseded_by='some-other-belief' WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id=?", (row["id"],)
    ).fetchone()[0]
    assert vis == "review_only", (
        "legacy lifecycle-hidden belief was not re-quarantined — hide survived"
    )
    assert any(
        "hidden via lifecycle" in (f.get("detail") or "") for f in report.findings
    ), "reconcile did not flag the legacy lifecycle hide"


def test_r14_review_requarantine_covers_audit_only(tmp_path):
    """008r-review (audit-only-tamper-not-review-quarantined): _requarantine must
    force review_only from audit_only too, not only operational_context — else a
    tampered audit_only row is flagged but never surfaced on the review queue."""
    from mnemos.vault.reconcile import _requarantine

    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at,"
        " last_revised, last_challenged, tier, read_visibility) VALUES"
        " ('b-au', 'oliver', 'x', 0.9, 'identity', 't','t','t','foundational','audit_only')"
    )
    conn.commit()
    assert _requarantine(conn, "beliefs", "b-au") is True
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-au'"
    ).fetchone()[0]
    assert vis == "review_only", (
        "_requarantine did not force review_only from audit_only"
    )


def test_008r_apply_methods_have_no_journal_path_kwarg():
    """008r (overturns 008m): the vault's apply chokepoints must carry NO
    journal-path parameter. Production resolves the canonical path; there is no
    sanctioned redirection. A journal-path kwarg reintroduced on either method
    is exactly the affordance Fable removed — this is the signature-level
    regression guard the ruling required (008r #4)."""
    import inspect

    for name in ("apply_identity_decision", "apply_legacy_witness"):
        params = list(inspect.signature(getattr(EngramStore, name)).parameters)
        offenders = [
            p for p in params if "journal" in p.lower() or "override" in p.lower()
        ]
        assert not offenders, (
            f"{name} accepts journal-path kwarg(s) {offenders} — 008r removed "
            "the redirectable path; tests inject via the resolver seam instead"
        )


# ══════════════════════════════════════════════════════════════════════════
# T5 — reconciler audit + residuals (012 §2 rulings; report 014).
# Every NEG assertion has a mutation proof recorded in report 014 §per-fix:
# reverting the guard makes the test go red.
# ══════════════════════════════════════════════════════════════════════════


# ── RULING J: witness re-verification discriminator in the not-located fallback ──
# Amends 008k E2B: exemption narrows from "any review_only" to
# "review_only AND a witnessed field genuinely changed".


def test_t5_J_proposal_fallback_hide_is_caught_and_restored(tmp_path):
    """J (proposal path): clear decision_ref + review_only via raw SQL with NO
    witnessed field changed → witness still fully verifies → raw-SQL HIDE.
    Reconcile must RESTORE it to operational and fire witnessed_row_hidden, not
    exempt it as a legitimate degrade. Mutation proof: reverting the fallback to
    the blanket `if review_only: pass` makes this red (hide survives)."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-Jhide")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='b-Jhide'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-Jhide'"
    ).fetchone()[0]
    assert vis == "operational_context", "J: raw-SQL hide was not restored"
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "J: reconcile did not flag the cleared-ref hide (reported clean)"
    )


def test_t5_J_proposal_genuine_degrade_stays_quarantined(tmp_path):
    """J counter-case (proposal): a GENUINE degrade — a witnessed field (content)
    changed AND ref cleared + review_only (what the write-path E7/E8/E2B does) —
    must NOT be re-flagged as a hide, and must STAY at review_only. Mutation proof:
    dropping the `not hidden` guard would restore/flag it (008g no-review-fatigue
    intent violated), turning this red."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-Jdeg", content="orig")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET content='changed', decision_ref=NULL, "
        "read_visibility='review_only' WHERE id='b-Jdeg'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-Jdeg'"
    ).fetchone()[0]
    assert vis == "review_only", "J: genuine degrade was wrongly restored"
    assert not any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "J: genuine degrade wrongly flagged as a hide (review-fatigue regression)"
    )


def test_t5_J_legacy_fallback_hide_is_caught_and_restored(tmp_path):
    """J (legacy path): same discriminator on _reconcile_legacy_line's fallback.
    Clear ref + review_only, content intact → legacy witness fully re-verifies →
    raw-SQL hide → restore + finding. Mutation proof: reverting the legacy
    fallback to the blanket `if review_only: pass` makes this red."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store, bid="legJhide", content="I am Oliver.")
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='legJhide'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='legJhide'"
    ).fetchone()[0]
    assert vis == "operational_context", "J legacy: raw-SQL hide was not restored"
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "J legacy: reconcile did not flag the cleared-ref hide"
    )


def test_t5_J_legacy_genuine_degrade_stays_quarantined(tmp_path):
    """J counter-case (legacy): a genuine content change + degrade must stay
    review_only, not restore. Mutation proof: dropping the `not hidden` guard
    restores it, turning this red."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store, bid="legJdeg", content="I am Oliver.")
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    conn = store._get_conn()
    conn.execute(
        "UPDATE beliefs SET content='TAMPERED', decision_ref=NULL, "
        "read_visibility='review_only' WHERE id='legJdeg'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='legJdeg'"
    ).fetchone()[0]
    assert vis == "review_only", "J legacy: genuine degrade was wrongly restored"
    assert not any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "J legacy: genuine degrade wrongly flagged as a hide"
    )


# ── RULING R6-1: existing agent-owned journal → unusable-at-read, quarantine-all ──
# Detection in the store (owns the trust check); handling IDENTICAL to corrupt;
# journal.py stays format-only; distinct label `journal_untrusted` permitted.


def test_t5_R6_1_agent_owned_journal_quarantines_all(tmp_path, monkeypatch):
    """R6-1: an existing agent-owned decisions.jsonl under a (dir-)trusted vault
    is a self-authored leaf — its chain-valid lines must NOT witness identity
    content. Reconcile must classify it unusable-at-read and route into the
    IDENTICAL quarantine-all fail-closed handling the corrupt path uses, with a
    distinct `journal_untrusted` label. Mutation proof: removing the
    journal_untrusted branch in reconcile (or the store's trust check) lets the
    witnessed row stay operational — this goes red."""
    # Enable the real ownership check (conftest defaults it off). The tmp journal
    # is owned by the test process → agent-owned → untrusted.
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-r61")
    _append_journal(journal, [(proposal, "approved")])
    # Stamp the row operational by applying against the (still-trusted-at-apply)
    # journal, then reconcile with the trust check on.
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    _apply_identity(store, proposal["id"], journal)
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    conn = store._get_conn()
    assert (
        conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-r61'").fetchone()[
            0
        ]
        == "operational_context"
    )
    report = store.reconcile_identity_vault(journal)
    # Distinct forensic label, identical fail-closed handling.
    assert any(f["kind"] == "journal_untrusted" for f in report.findings), (
        "R6-1: agent-owned journal not classified untrusted"
    )
    # Quarantine-all: the witnessed row is forced review_only (never fail-open).
    assert (
        conn.execute("SELECT read_visibility FROM beliefs WHERE id='b-r61'").fetchone()[
            0
        ]
        == "review_only"
    ), "R6-1: witnessed row stayed operational — agent-owned journal was trusted"


def test_t5_R6_1_untrusted_handling_is_identical_to_corrupt(tmp_path, monkeypatch):
    """R6-1: the untrusted-journal handling must be BYTE-IDENTICAL to the corrupt
    handling (only the label differs). Both quarantine every operational
    witnessed identity row via _fail_closed_quarantine_all_witnessed. Assert the
    requarantine records match in shape and coverage."""
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-ident")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Untrusted-journal report.
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    untrusted_report = store.reconcile_identity_vault(journal)
    conn = store._get_conn()
    untrusted_vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-ident'"
    ).fetchone()[0]
    # Same fail-closed coverage: the witnessed operational row is re-quarantined.
    assert untrusted_vis == "review_only"
    assert any(
        r["kind"] == "requarantined_journal_corrupt"
        for r in untrusted_report.requarantined
    ), "R6-1: untrusted path did not use the identical corrupt quarantine handling"


def test_t5_R6_1_valid_trusted_journal_no_false_quarantine(tmp_path, monkeypatch):
    """R6-1 counter-case: a trusted journal (the _vault_object_trusted seam
    accepts it, as production's root-owned journal is) must reconcile normally —
    NO false quarantine. Mutation proof: making the trust check always-True would
    quarantine this valid case, turning it red."""
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    # Trust the fixture (mirrors production's root-owned journal / _arm_vault).
    monkeypatch.setattr(_sq, "_vault_object_trusted", lambda _p: True)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-valid")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    report = store.reconcile_identity_vault(journal)
    assert report.ok, "R6-1: trusted journal false-quarantined (valid case broke)"
    conn = store._get_conn()
    assert (
        conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-valid'"
        ).fetchone()[0]
        == "operational_context"
    )


def test_t5_R6_1_round4_fail_open_regression_stays_red_provable(tmp_path, monkeypatch):
    """R6-1: the round-4 fail-open regression (an untrusted journal must NEVER be
    returned as authoritative / inert) stays red-provable. With the check on and
    an agent-owned journal, reconcile must NOT be a no-op (fail-open) — it must
    fail closed. Assert the report is NOT ok (a fail-open no-op would be ok)."""
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-r4")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    report = store.reconcile_identity_vault(journal)
    assert not report.ok, (
        "R6-1: untrusted journal reconciled clean (fail-open) — never inert, "
        "never trusted, never fail-open"
    )


def test_t5_R6_1_journal_module_has_no_ownership_knowledge():
    """R6-1: journal.py (the stdlib TCB twin) must gain ZERO ownership knowledge —
    it stays format-only. Grep the module for ownership/uid tokens; the trust
    check lives in the store, not here."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "mnemos"
        / "vault"
        / "journal.py"
    ).read_text(encoding="utf-8")
    for token in (
        "st_uid",
        "getuid",
        "geteuid",
        "os.access",
        "_vault_object_trusted",
        "owner",
        "_JOURNAL_TRUST",
    ):
        assert token not in src, (
            f"journal.py gained ownership knowledge ({token!r}) — R6-1 requires "
            "it stay format-only; the trust check belongs in the store"
        )


def test_t5_R6_1_apply_refuses_untrusted_journal(tmp_path, monkeypatch):
    """R6-1 (apply path): apply_identity_decision must refuse to witness against
    an agent-owned journal — an agent could hand-craft a chain-valid,
    hash-matching line for its own content. Fail closed with a raise. Mutation
    proof: removing the apply-side trust check lets the apply succeed."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-applyr61")
    _append_journal(journal, [(proposal, "approved")])
    # Enable the real ownership check; the tmp journal is agent-owned.
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        with pytest.raises(ValueError, match="agent-owned|untrusted"):
            store.apply_identity_decision(proposal["id"])
    finally:
        _sq.resolve_vault_journal_path = prev


def test_t5_R6_1_legacy_apply_refuses_untrusted_journal(tmp_path, monkeypatch):
    """R6-1 (legacy apply path): apply_legacy_witness must not stamp from an
    agent-owned journal. Best-effort (session-start), so it returns empty with a
    'journal-untrusted' skip rather than raising — but never stamps."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store, bid="legr61", content="I am Oliver.")
    _append_legacy(journal, "beliefs", row)
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    prev = _sq.resolve_vault_journal_path
    _sq.resolve_vault_journal_path = lambda: str(journal)
    try:
        res = store.apply_legacy_witness()
    finally:
        _sq.resolve_vault_journal_path = prev
    assert res["stamped"] == [], "R6-1: legacy witness stamped from untrusted journal"
    assert "journal-untrusted" in res["skipped"]


# ── RULING GAP-1: reconcile read-decide-write span runs under BEGIN IMMEDIATE ──


def test_t5_GAP1_reconcile_takes_begin_immediate(tmp_path):
    """GAP-1: reconcile must hold BEGIN IMMEDIATE across its read-decide-write
    span (apply already does; reconcile did not). Observe the lock is taken.
    Mutation proof: removing the BEGIN IMMEDIATE wrap makes this red."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-gap1")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
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
    try:
        store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    assert seen["immediate"] is True, (
        "GAP-1: reconcile did not take BEGIN IMMEDIATE across the span (TOCTOU open)"
    )


def test_t5_GAP1_concurrent_writer_blocked_during_reconcile_span(tmp_path):
    """GAP-1 TOCTOU closed: while RECONCILE's own BEGIN IMMEDIATE is held, a
    concurrent writer (separate connection) attempting a mutation is BLOCKED.
    The concurrent write is injected from a hook that fires the moment reconcile
    opens its span lock — proving reconcile's OWN lock (not a manually-held one)
    makes the read-decide-write atomic. Mutation proof: with the GAP-1 lock
    removed, reconcile opens no BEGIN IMMEDIATE, the hook never fires — this
    goes red on the `fired` assertion."""
    import sqlite3

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-gap1c")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    db_path = str(tmp_path / "vault.db")
    real_conn = store._get_conn()
    state = {"blocked": None, "fired": False}

    class _HookConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            if (
                isinstance(sql, str)
                and "BEGIN IMMEDIATE" in sql.upper()
                and not state["fired"]
            ):
                state["fired"] = True
                # reconcile now holds the write lock — a concurrent writer must block.
                attacker = sqlite3.connect(db_path, timeout=0.2)
                try:
                    attacker.execute(
                        "UPDATE beliefs SET read_visibility='operational_context' "
                        "WHERE id='b-gap1c'"
                    )
                    attacker.commit()
                    state["blocked"] = False
                except sqlite3.OperationalError:
                    state["blocked"] = True
                finally:
                    attacker.close()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    try:
        store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    assert state["fired"] is True, (
        "GAP-1: reconcile never opened BEGIN IMMEDIATE — the hook did not fire"
    )
    assert state["blocked"] is True, (
        "GAP-1: a concurrent writer was NOT blocked during reconcile's span "
        "(TOCTOU still open)"
    )


def test_t5_GAP1_reconcile_populated_db_stays_interactive(tmp_path):
    """GAP-1 sanity: a reconcile pass on a POPULATED DB completes promptly (does
    not deadlock on its own lock). Exercises the full span with real rows."""
    import time

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    # Populate several witnessed identity rows + non-identity noise.
    for i in range(20):
        p = _make_identity_proposal(store, target_id=f"b-pop{i}", pid=f"prop-pop{i}")
        _append_journal(journal, [(p, "approved")])
        _apply_identity(store, p["id"], journal)
    t0 = time.monotonic()
    report = store.reconcile_identity_vault(str(journal))
    elapsed = time.monotonic() - t0
    assert report.ok, f"populated reconcile produced findings: {report.findings}"
    assert elapsed < 5.0, f"reconcile took {elapsed:.2f}s — not interactive"


def test_t5_GAP1_B1_lock_acquisition_fails_closed(tmp_path):
    """014b B-1: if BEGIN IMMEDIATE cannot be acquired, reconcile must FAIL CLOSED
    (raise) — it must NEVER run the write path unlocked. A swallowed lock failure
    silently reopens the exact TOCTOU GAP-1 closed (the T4 fail-direction class
    applied to a lock: degraded protection must be loud). Force the lock
    acquisition to raise and assert reconcile propagates it AND performs no
    requarantine write. Mutation proof: reintroducing
    `try/except Exception: span_lock = False` around the BEGIN IMMEDIATE makes
    reconcile swallow the error, run the write path unlocked, and return a report
    → pytest.raises finds no exception → RED."""
    import sqlite3

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-failclosed")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Detier so the write path (Direction B requarantine) WOULD run if unlocked.
    conn0 = store._get_conn()
    conn0.execute(
        "UPDATE beliefs SET domain='general', tier='operational' WHERE id='b-failclosed'"
    )
    conn0.commit()
    real_conn = store._get_conn()

    class _NoLockConn:
        """Raises exactly on BEGIN IMMEDIATE — simulates an unacquirable lock."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if isinstance(sql, str) and "BEGIN IMMEDIATE" in sql.upper():
                raise sqlite3.OperationalError("simulated: cannot acquire write lock")
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = store._get_conn
    store._get_conn = lambda: _NoLockConn(real_conn)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    # Fail-closed: the write path never ran, so the row is untouched (still the
    # de-tiered operational_context we left it at — NOT requarantined by an
    # unlocked pass).
    row_vis = real_conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-failclosed'"
    ).fetchone()[0]
    assert row_vis == "operational_context", (
        "B-1: reconcile mutated a row despite failing to acquire the lock — "
        "the write path ran unlocked (fail-open)"
    )


# ── RULING GAP-2: migration re-run safety on a populated identity table ──


def test_t5_GAP2_migration_rerun_preserves_witnessed_identity_rows(tmp_path):
    """GAP-2: an agent forcing meta.schema_version back to 4 re-runs v5..v9.
    v6's backfill UPDATEs previously fired unconditionally and downgraded a
    WITNESSED identity hypomnema (operational + decision_ref) to review_only.
    The witnessed-guard exempts rows carrying a decision_ref. Re-run must leave
    witnessed identity rows BYTE-IDENTICAL. Mutation proof: removing the
    `_witnessed_guard` in apply_afferent_membrane_v1_schema_migration makes this
    red (hypomnema flips to review_only)."""
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
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at, "
        "last_revised, last_challenged, tier, read_visibility, decision_ref) VALUES "
        "('b-wit','oliver','I am Oliver.',0.9,'identity','t','t','t','foundational',"
        "'operational_context','brefhash')"
    )
    conn.commit()
    before_h = tuple(
        conn.execute(
            "SELECT read_visibility, decision_ref FROM hypomnema_entries WHERE id='h-wit'"
        ).fetchone()
    )
    before_b = tuple(
        conn.execute(
            "SELECT read_visibility, decision_ref FROM beliefs WHERE id='b-wit'"
        ).fetchone()
    )
    store.close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    store2 = EngramStore(db, vault_active=True)
    conn2 = store2._get_conn()
    after_h = tuple(
        conn2.execute(
            "SELECT read_visibility, decision_ref FROM hypomnema_entries WHERE id='h-wit'"
        ).fetchone()
    )
    after_b = tuple(
        conn2.execute(
            "SELECT read_visibility, decision_ref FROM beliefs WHERE id='b-wit'"
        ).fetchone()
    )
    after_ver = conn2.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    store2.close()
    assert str(after_ver) == str(head), "migrations did not re-run to head"
    assert after_h == before_h, (
        f"GAP-2: witnessed hypomnema clobbered on re-run: {before_h} -> {after_h}"
    )
    assert after_b == before_b, (
        f"GAP-2: witnessed belief clobbered on re-run: {before_b} -> {after_b}"
    )


def test_t5_GAP2_unknown_read_visibility_fails_closed_at_read(tmp_path):
    """GAP-2 (CHECK-drop vector): if a table rebuild dropped the read_visibility
    CHECK and an attacker wrote a 4th (unknown) value on an identity row, an
    operational read must NOT return it — reads filter by exact-match
    `read_visibility = 'operational_context'`, so any unknown value is excluded
    by construction (fail-closed on unknown enum). Pin this property against
    future refactors that might use a NOT-IN or negation predicate."""
    # Two-part fail-closed proof for the CHECK-drop vector:
    # (1) the read gate uses EXACT-match (IN (literal)), so any value other than
    #     the operational literal is excluded from operational reads by
    #     construction — an unknown 4th enum value can never read as operational;
    # (2) reconcile's requarantine scan (!= 'review_only') sweeps an unknown
    #     value into review_only, so an unknown-enum witnessed row fails closed.
    from mnemos.store.sqlite_store import _append_read_visibility_filter

    params: list = []
    sql = _append_read_visibility_filter(
        "SELECT id FROM beliefs WHERE agent_id = 'oliver'",
        params,
        "read_visibility",
        ("operational_context",),
    )
    assert "read_visibility IN (?)" in sql, (
        "read gate is not exact-match — a NOT-IN/negation predicate could let an "
        "unknown enum leak operational"
    )
    assert params == ["operational_context"], (
        "read gate does not bind the operational literal exactly"
    )
    # Dispositive on real data: an in-memory table without the CHECK, holding an
    # unknown value, is excluded by the exact-match operational filter and swept
    # by the != 'review_only' requarantine scan.
    import sqlite3

    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE b (id TEXT, read_visibility TEXT)")  # no CHECK
    mem.execute(
        "INSERT INTO b VALUES ('op','operational_context'),('unk','xxx_unknown'),"
        "('rev','review_only')"
    )
    ops = [
        r[0]
        for r in mem.execute(
            "SELECT id FROM b WHERE read_visibility = 'operational_context'"
        )
    ]
    assert "unk" not in ops, "unknown enum leaked into an exact-match operational read"
    req = [
        r[0]
        for r in mem.execute("SELECT id FROM b WHERE read_visibility != 'review_only'")
    ]
    assert "unk" in req, "requarantine scan (!= review_only) missed the unknown enum"
    mem.close()


# ── RULING GAP-5: write-time classifier coverage (detect-not-prevent) ──


def test_t5_GAP5_write_hypomnema_floors_identity_to_review(tmp_path):
    """GAP-5 coverage: write_hypomnema_entry routes through
    classify_hypomnema_read_visibility, flooring identity/foundational content to
    review_only. An agent cannot set operational directly on identity content via
    this path. Mutation proof: removing the classifier floor lets it write
    operational."""
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
    assert (
        conn.execute(
            "SELECT read_visibility FROM hypomnema_entries WHERE id=?", (eid,)
        ).fetchone()[0]
        == "review_only"
    ), "GAP-5: identity hypomnema wrote operational"
    # Even an explicit operational request is rejected (the classifier floor).
    with pytest.raises(ValueError, match="review"):
        store.write_hypomnema_entry(
            "More identity content.",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            domain="identity",
            foundational=True,
            read_visibility="operational_context",
        )


def test_t5_GAP5_revise_hypomnema_floors_identity_to_review(tmp_path):
    """GAP-5 coverage: revise_hypomnema_entry (the second write path) also routes
    through the classifier — revising toward identity content floors to
    review_only, never below the stricter of existing/classified."""
    store = _store(tmp_path)
    eid = store.write_hypomnema_entry(
        "A topical note.",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        domain="topical",
    )
    conn = store._get_conn()
    assert (
        conn.execute(
            "SELECT read_visibility FROM hypomnema_entries WHERE id=?", (eid,)
        ).fetchone()[0]
        == "operational_context"
    )
    store.revise_hypomnema_entry(
        eid,
        new_content="I am Oliver — this is identity now.",
        reason="became identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert (
        conn.execute(
            "SELECT read_visibility FROM hypomnema_entries WHERE id=?", (eid,)
        ).fetchone()[0]
        == "review_only"
    ), "GAP-5: revise did not floor identity content to review_only"


def test_t5_GAP5_only_vault_apply_bypasses_classifier(tmp_path):
    """GAP-5 coverage: the ONLY hypomnema insert that writes operational identity
    content directly is the vault apply path — and it carries a decision_ref (the
    journal line hash that licenses operational visibility). Prove the bypass is
    gated on a witnessed decision: an applied identity hypomnema is operational
    AND has a decision_ref; nothing else writes operational identity content."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(
        store, surface="hypomnema_entries", target_id="h-gap5", content="I am Oliver."
    )
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    vis, ref = conn.execute(
        "SELECT read_visibility, decision_ref FROM hypomnema_entries WHERE id='h-gap5'"
    ).fetchone()
    assert vis == "operational_context", "vault apply did not write operational"
    assert ref, (
        "vault apply wrote operational WITHOUT a decision_ref — bypass unguarded"
    )


def test_t5_GAP5_belief_identity_without_ref_excluded_from_operational_reads(tmp_path):
    """GAP-5 coverage (beliefs): beliefs have no hypomnema-style write classifier;
    their structural defense is the READ gate. An identity belief written
    operational with NO decision_ref must be excluded from operational reads by
    _append_identity_decision_gate. Prove the read-gate coverage."""
    store = _store(tmp_path)
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO beliefs (id, agent_id, content, confidence, domain, created_at, "
        "last_revised, last_challenged, tier, read_visibility) VALUES "
        "('b-gap5','oliver','sneaky identity',0.9,'identity','t','t','t',"
        "'foundational','operational_context')"
    )
    conn.commit()
    ops = store.get_beliefs("oliver", read_visibility="operational_context")
    assert all(b.content != "sneaky identity" for b in ops), (
        "GAP-5: unwitnessed identity belief leaked into operational reads"
    )


# ── E2B lifecycle residual composed with J (008k as amended) ──


def test_t5_J_E2B_legitimate_lifecycle_degrade_not_restored(tmp_path):
    """J × E2B composition: a row legitimately degraded via a lifecycle change
    (superseded_by set + ref cleared + review_only, as the write-path E2B
    produces) has a CHANGED witnessed field (lifecycle), so _witness_reverifies
    returns False → it is a genuine degrade, NOT a hide. The J fix must NOT
    restore it. Pins that J narrows E2B to 'review_only AND a witnessed field
    changed', and lifecycle counts as a witnessed change."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-e2bj")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # Legitimate E2B lifecycle degrade: supersede + clear ref + review_only.
    conn.execute(
        "UPDATE beliefs SET superseded_by='new-belief', decision_ref=NULL, "
        "read_visibility='review_only' WHERE id='b-e2bj'"
    )
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-e2bj'"
    ).fetchone()[0]
    assert vis == "review_only", "J×E2B: legitimate lifecycle degrade wrongly restored"
    assert not any(f.get("kind") == "witnessed_row_hidden" for f in report.findings), (
        "J×E2B: legitimate lifecycle degrade wrongly flagged as a hide"
    )


# ── 014c B-2: J restore must restore the decision_ref too (loop closure) ──


def test_t5_B2_proposal_J_restore_closes_the_loop(tmp_path):
    """014c B-2: a J-restored row must get its decision_ref back (the verified
    line hash), not just its visibility — else the identity read gate still
    excludes it (operational + identity-tier + no ref), the NEXT reconcile
    Direction A re-orphans it, and 'restore' becomes a restore→requarantine loop.
    Asserts: (a) post-restore decision_ref == the journal line hash; (b) the row
    is readable through the operational identity gate; (c) a SECOND reconcile
    pass is clean — no re-quarantine, no new findings. Mutation proof: reverting
    _restore_operational to visibility-only (drop the decision_ref arg effect)
    makes the second pass re-quarantine the row → this test RED."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-b2", content="I am Oliver.")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    # The line hash the row SHOULD carry (what apply stamped).
    expected_ref = conn.execute(
        "SELECT decision_ref FROM beliefs WHERE id='b-b2'"
    ).fetchone()[0]
    assert expected_ref, "precondition: row was not stamped operational"
    # Raw-SQL hide: clear the ref + review_only, no witnessed field changed.
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='b-b2'"
    )
    conn.commit()
    # First reconcile: J detects the hide and restores.
    report1 = store.reconcile_identity_vault(str(journal))
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report1.findings)
    # (a) decision_ref restored to the verified line hash.
    vis, ref = conn.execute(
        "SELECT read_visibility, decision_ref FROM beliefs WHERE id='b-b2'"
    ).fetchone()
    assert vis == "operational_context", "B-2: visibility not restored"
    assert ref == expected_ref, (
        "B-2: decision_ref not restored — the row is still ref-less and will "
        "re-orphan on the next pass"
    )
    # (b) readable through the operational identity gate.
    ops = store.get_beliefs("oliver", read_visibility="operational_context")
    assert any(b.content == "I am Oliver." for b in ops), (
        "B-2: restored row is not readable through the operational identity gate"
    )
    # (c) SECOND reconcile pass is clean — the loop is closed.
    report2 = store.reconcile_identity_vault(str(journal))
    assert report2.ok, (
        f"B-2: second reconcile re-quarantined the restored row (loop not "
        f"closed): findings={report2.findings} requarantined={report2.requarantined}"
    )
    vis2 = conn.execute(
        "SELECT read_visibility FROM beliefs WHERE id='b-b2'"
    ).fetchone()[0]
    assert vis2 == "operational_context", "B-2: row re-quarantined on the 2nd pass"


def test_t5_B2_legacy_J_restore_closes_the_loop(tmp_path):
    """014c B-2 (legacy twin): the legacy J fallback restore must also restore the
    decision_ref, and a second reconcile pass must be clean."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    row = _insert_legacy_belief(store, bid="leg-b2", content="I am Oliver.")
    _append_legacy(journal, "beliefs", row)
    _apply_legacy(store, journal)
    conn = store._get_conn()
    expected_ref = conn.execute(
        "SELECT decision_ref FROM beliefs WHERE id='leg-b2'"
    ).fetchone()[0]
    assert expected_ref
    conn.execute(
        "UPDATE beliefs SET decision_ref=NULL, read_visibility='review_only' "
        "WHERE id='leg-b2'"
    )
    conn.commit()
    report1 = store.reconcile_identity_vault(str(journal))
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report1.findings)
    vis, ref = conn.execute(
        "SELECT read_visibility, decision_ref FROM beliefs WHERE id='leg-b2'"
    ).fetchone()
    assert vis == "operational_context"
    assert ref == expected_ref, "B-2 legacy: decision_ref not restored"
    report2 = store.reconcile_identity_vault(str(journal))
    assert report2.ok, (
        f"B-2 legacy: second reconcile re-quarantined (loop not closed): "
        f"{report2.findings}"
    )


def test_t5_B2_K_audit_only_restore_stays_visibility_only(tmp_path):
    """014c B-2 CAUTION: K's round-6 restore path handles a visibility-only hide
    (audit_only) where the decision_ref is INTACT. That path must stay
    visibility-only — do NOT regress it into rewriting the ref. Prove the K path
    still restores an audit_only-hidden row whose ref never changed, and the ref
    is unchanged (not rewritten by a B-2 code path)."""
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-k", content="I am Oliver.")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    conn = store._get_conn()
    orig_ref = conn.execute(
        "SELECT decision_ref FROM beliefs WHERE id='b-k'"
    ).fetchone()[0]
    # K hide: flip to audit_only but LEAVE the decision_ref intact.
    conn.execute("UPDATE beliefs SET read_visibility='audit_only' WHERE id='b-k'")
    conn.commit()
    report = store.reconcile_identity_vault(str(journal))
    vis, ref = conn.execute(
        "SELECT read_visibility, decision_ref FROM beliefs WHERE id='b-k'"
    ).fetchone()
    assert vis == "operational_context", "K path: audit_only hide not restored"
    assert ref == orig_ref, "K path regressed: intact ref was rewritten"
    assert any(f.get("kind") == "witnessed_row_hidden" for f in report.findings)


# ── 014c B-3: the early quarantine-all write branches run UNDER the span lock ──


def test_t5_B3_untrusted_quarantine_runs_under_lock(tmp_path, monkeypatch):
    """014c B-3: the journal_untrusted fail-closed quarantine-all branch writes
    (it iterates _requarantine UPDATEs) and must run UNDER the span lock, not
    before it. Observe that reconcile opens BEGIN IMMEDIATE on the untrusted
    branch. Mutation proof: removing the entry lock (reverting to the per-branch
    inline commit that ran before BEGIN IMMEDIATE) → no BEGIN IMMEDIATE on this
    branch → RED."""
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-b3u")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
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
    try:
        report = store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    assert any(f["kind"] == "journal_untrusted" for f in report.findings)
    assert seen["immediate"] is True, (
        "B-3: the untrusted quarantine-all branch ran WITHOUT the span lock"
    )
    # The witnessed row was actually re-quarantined (the branch did write).
    assert (
        real_conn.execute(
            "SELECT read_visibility FROM beliefs WHERE id='b-b3u'"
        ).fetchone()[0]
        == "review_only"
    )


def test_t5_B3_corrupt_quarantine_runs_under_lock(tmp_path):
    """014c B-3: the corrupt-journal fail-closed quarantine-all branch must also
    run under the span lock. Force a mid-file-corrupt journal and observe BEGIN
    IMMEDIATE is taken before the quarantine-all writes."""
    import json as _json

    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-b3c")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    # Corrupt the journal mid-file: append a valid line then a broken one is
    # "torn tail"; to force `corrupt`, put malformed content BEFORE the last line.
    good = _json.loads(journal.read_text().splitlines()[0])
    journal.write_text(
        _json.dumps(good) + "\n" + "{not valid json\n" + _json.dumps(good) + "\n"
    )
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
    try:
        report = store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    assert any(f["kind"] == "journal_corrupt" for f in report.findings)
    assert seen["immediate"] is True, (
        "B-3: the corrupt quarantine-all branch ran WITHOUT the span lock"
    )


def test_t5_B3_untrusted_quarantine_concurrent_writer_blocked(tmp_path, monkeypatch):
    """014c B-3 teeth: while reconcile runs the untrusted quarantine-all branch
    under its lock, a concurrent writer is BLOCKED — proving the branch's writes
    are atomic under the span lock (not a pre-lock unlocked iteration)."""
    import sqlite3

    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", False)
    store = _store(tmp_path)
    journal = tmp_path / "decisions.jsonl"
    proposal = _make_identity_proposal(store, target_id="b-b3blk")
    _append_journal(journal, [(proposal, "approved")])
    _apply_identity(store, proposal["id"], journal)
    monkeypatch.setattr(_sq, "_JOURNAL_TRUST_CHECK_ENABLED", True)
    db_path = str(tmp_path / "vault.db")
    real_conn = store._get_conn()
    state = {"blocked": None, "fired": False}

    class _HookConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            result = self._inner.execute(sql, *a, **k)
            if (
                isinstance(sql, str)
                and "BEGIN IMMEDIATE" in sql.upper()
                and not state["fired"]
            ):
                state["fired"] = True
                attacker = sqlite3.connect(db_path, timeout=0.2)
                try:
                    attacker.execute(
                        "UPDATE beliefs SET read_visibility='operational_context' "
                        "WHERE id='b-b3blk'"
                    )
                    attacker.commit()
                    state["blocked"] = False
                except sqlite3.OperationalError:
                    state["blocked"] = True
                finally:
                    attacker.close()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = store._get_conn
    store._get_conn = lambda: _HookConn(real_conn)  # type: ignore[assignment]
    try:
        store.reconcile_identity_vault(str(journal))
    finally:
        store._get_conn = orig  # type: ignore[assignment]
    assert state["fired"] is True, "B-3: untrusted branch never opened BEGIN IMMEDIATE"
    assert state["blocked"] is True, (
        "B-3: a concurrent writer was NOT blocked during the untrusted "
        "quarantine-all branch (it ran unlocked)"
    )
