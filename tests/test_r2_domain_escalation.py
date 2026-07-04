"""R2 domain no-de-escalation + D4 quarantine-on-claim tests (T3).

A caller-supplied domain may only escalate above the classifier, never
de-escalate below it. A de-escalation attempt is a claim event: the write is
routed to review and the claim is recorded as a deduped proposal row.
"""

from __future__ import annotations

import pytest

from mnemos.simple_runtime import escalate_domain
from mnemos.store.sqlite_store import EngramStore


# ── escalate_domain (max-severity, no de-escalation) ─────────────────────────


def test_escalate_domain_takes_max_severity():
    # Caller cannot pull identity-classified content down to topical.
    assert escalate_domain("topical", "identity") == "identity"
    # Caller may escalate above a low classifier.
    assert escalate_domain("identity", "topical") == "identity"
    # Ties among low-blast keep the caller's label.
    assert escalate_domain("topical", "situational") == "topical"
    # Mid vs high: high wins regardless of order.
    assert escalate_domain("recurring", "foundational") == "foundational"
    assert escalate_domain("foundational", "recurring") == "foundational"
    # Unknown label is fail-closed low, cannot outrank a high-blast classifier.
    assert escalate_domain("bogus", "identity") == "identity"


# ── write_proposal domain enum (D7) ──────────────────────────────────────────


def _proposal_kwargs(**overrides):
    base = dict(
        source_authority="observed",
        kind="semantic",
        target_surface="hypomnema_entries",
        transition="test",
        blast_radius="medium",
    )
    base.update(overrides)
    return base


def test_write_proposal_rejects_unknown_domain(tmp_path):
    store = EngramStore(tmp_path / "r2-prop.db")
    try:
        with pytest.raises(ValueError):
            store.write_proposal(**_proposal_kwargs(domain="not-a-domain"))
    finally:
        store.close()


def test_write_proposal_accepts_general_and_rfc_domains(tmp_path):
    store = EngramStore(tmp_path / "r2-prop-ok.db")
    try:
        for dom in (
            "general",
            "identity",
            "foundational",
            "topical",
            "situational",
            "",
        ):
            row = store.write_proposal(
                **_proposal_kwargs(domain=dom, proposal_id=f"p-{dom or 'empty'}")
            )
            # empty normalizes to general; others persist as given.
            assert row["domain"] == (dom or "general")
    finally:
        store.close()


# ── mnemos_hypomnema_write R2 routing + D4 claim recording ───────────────────


def _patch_mcp(monkeypatch, store):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    monkeypatch.setattr(mcp_server, "_effective_scope", lambda a, p, s: (a, p, s))
    return mcp_server


def _pending_claims(store):
    return [
        p
        for p in store.list_proposals(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            status="pending_review",
        )
        if p["transition"] == "hypomnema_write_domain_claim"
    ]


def test_hypomnema_write_caller_topical_on_identity_content_routes_review(
    monkeypatch, store
):
    """RFC test 4 (hypomnema surface): caller labels identity-bearing content
    topical -> routed review_only AND the claim is recorded as a proposal."""
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_hypomnema_write(
        content="This is about who i am — my identity and selfhood.",
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    entry_id = out.split("Hypomnema written: ", 1)[1].splitlines()[0].strip()
    entry = store.get_hypomnema_entry(
        entry_id,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        read_visibility=None,
    )
    assert entry is not None
    # The effective (escalated) domain routes the write to review.
    assert entry["read_visibility"] == "review_only"
    assert entry["domain"] == "identity"
    # The response annotates the escalation rather than hiding it behind the
    # caller's claimed label (review finding r2-hypomnema-domain-response).
    assert "identity (escalated from topical" in out
    # The de-escalation attempt is recorded.
    claims = _pending_claims(store)
    assert len(claims) == 1
    assert "caller-claimed domain='topical'" in claims[0]["reason"]


def test_hypomnema_write_caller_may_escalate_without_claim(monkeypatch, store):
    """Caller escalating (identity label on topical content) stays high-blast
    and records no claim — escalation is allowed, de-escalation is not."""
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_hypomnema_write(
        content="a mundane note about the weather today",
        domain="identity",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    entry_id = out.split("Hypomnema written: ", 1)[1].splitlines()[0].strip()
    entry = store.get_hypomnema_entry(
        entry_id,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        read_visibility=None,
    )
    assert entry is not None
    assert entry["domain"] == "identity"
    assert entry["read_visibility"] == "review_only"
    assert _pending_claims(store) == []


def test_hypomnema_write_domain_claim_is_deduped(monkeypatch, store):
    """D4: an identical claim (same surface, claimed domain, content) upserts the
    same pending row rather than flooding the review queue."""
    mcp_server = _patch_mcp(monkeypatch, store)
    content = "This concerns who i am and my identity."
    for _ in range(3):
        mcp_server.mnemos_hypomnema_write(
            content=content,
            domain="topical",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
    # Three identical claim events -> exactly one claim proposal row.
    assert len(_pending_claims(store)) == 1


def test_hypomnema_write_trailing_space_domain_is_not_an_escalation(monkeypatch, store):
    """Whitespace-only variation ('topical ') on topical content is normalized
    and must NOT be treated as a de-escalation (review finding
    r2-domain-claim-raw-domain)."""
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_hypomnema_write(
        content="a mundane note about the weather today",
        domain="topical ",  # trailing space
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert "escalated" not in out
    assert _pending_claims(store) == []


def test_hypomnema_write_domain_claim_is_scoped_per_agent(monkeypatch, store):
    """Identical claim content in two different scopes produces two separate
    claim rows — one scope cannot overwrite another's (review finding
    r2-domain-claim-global-dedupe; David's decision: scope the key)."""
    mcp_server = _patch_mcp(monkeypatch, store)
    content = "This concerns who i am and my identity."
    mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="topical",
        agent_id="alice",
        person_id="bob",
        project_scope="proj",
    )
    scope_a = store.list_proposals(
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        status="pending_review",
    )
    scope_b = store.list_proposals(
        agent_id="alice", person_id="bob", project_scope="proj", status="pending_review"
    )
    assert (
        len([p for p in scope_a if p["transition"] == "hypomnema_write_domain_claim"])
        == 1
    )
    assert (
        len([p for p in scope_b if p["transition"] == "hypomnema_write_domain_claim"])
        == 1
    )


def test_duplicate_underclaimed_write_creates_one_review_entry(monkeypatch, store):
    """Finding B (completion of D4): repeated identical underclaimed writes
    create ONE review_only hypomnema candidate and one claim proposal — the
    pre-write idempotency stops the claim-spam loop from flooding the review
    queue with duplicate candidates, not just the proposal ledger."""
    mcp_server = _patch_mcp(monkeypatch, store)
    content = "This is about who i am — my identity and selfhood."
    out1 = mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    out2 = mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert "Hypomnema written" in out1
    assert "Duplicate quarantined domain claim" in out2
    # Only one review candidate exists for this content (the duplicate was
    # skipped before write), and one claim proposal.
    candidates = store.get_hypomnema_promotion_candidates(
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        read_visibility="review_only",
    )
    assert len([c for c in candidates if c["content"] == content]) == 1
    assert len(_pending_claims(store)) == 1


def test_domain_claim_write_uses_deterministic_entry_id(monkeypatch, store):
    """Review d4-domain-claim-idempotency-race: the quarantine-path write uses a
    deterministic entry_id (claim-<key>), so two identical underclaimed writes
    target the SAME row even under a read-before-write race — closing the TOCTOU
    that a fresh entry_id per call would leave open."""
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_hypomnema_write(
        content="This is about who i am and my identity.",
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    entry_id = out.split("Hypomnema written: ", 1)[1].splitlines()[0].strip()
    assert entry_id.startswith("claim-")


def test_domain_claim_key_canonical_ignores_varied_claimed_domain(monkeypatch, store):
    """Review domain-claim-key-not-canonical: two writes with the same content
    (same effective domain) but different claimed labels must collapse to ONE
    claim row — the key is on the canonical effective domain, not the caller's
    varied claimed label, so it can't be used to mint duplicate review rows."""
    mcp_server = _patch_mcp(monkeypatch, store)
    content = "This is about who i am and my identity."
    mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    out2 = mcp_server.mnemos_hypomnema_write(
        content=content,
        domain="situational",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert "Duplicate quarantined domain claim" in out2
    assert len(_pending_claims(store)) == 1


def test_domain_claim_proposal_payload_is_legible(monkeypatch, store):
    """Review domain-claim-proposal-not-legible: the claim proposal carries the
    claimed/classifier/effective domains + target entry in its payload so the
    review queue can render the claim, not an empty payload."""
    import json

    mcp_server = _patch_mcp(monkeypatch, store)
    mcp_server.mnemos_hypomnema_write(
        content="This is about who i am and my identity.",
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    claims = _pending_claims(store)
    assert len(claims) == 1
    payload = claims[0].get("payload") or claims[0].get("payload_json") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("claimed_domain") == "topical"
    assert payload.get("effective_domain") == "identity"
    assert payload.get("target_entry_id")


def test_domain_claim_key_normalizes_content_whitespace(monkeypatch, store):
    """Review domain-claim-key-raw-content: whitespace-variant content collapses
    to one claim row — a caller cannot pad whitespace to bypass D4."""
    mcp_server = _patch_mcp(monkeypatch, store)
    base = "This is about who i am and my identity."
    mcp_server.mnemos_hypomnema_write(
        content=base,
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    out2 = mcp_server.mnemos_hypomnema_write(
        content=f"  {base}  ",
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert "Duplicate quarantined domain claim" in out2
    assert len(_pending_claims(store)) == 1
