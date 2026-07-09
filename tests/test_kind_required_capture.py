"""kind is REQUIRED at the MCP capture surface — no default in either direction.

Ruling context (h7-bond-thickness §5.4): the store went 296/301 semantic because
mnemos_remember / mnemos_ingest defaulted kind="semantic", silently overriding
the encoder's own EPISODIC default. Absence of a declaration must not become a
declaration — the caller (an LLM) has to choose.

Mutation proofs (each assertion class has a mutation that turns it red):
- Restore `kind: str = "semantic"` on either tool -> the no-kind TypeError
  tests and the schema-required tests go red.
- Remove/weaken `_validate_kind` -> the invalid-kind tests go red.
- Change any of the four ruled-defensible hardcoded kind="semantic" sites
  (setup seeds, setup project context, domain-claim proposal, hypomnema
  promotion) -> the corresponding regression pin goes red.
- Reintroduce SourceType.USER_EXPLICIT at any ruled site -> the source mapping
  assertions or enum-resolution guard go red.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.server.fastmcp")


def _patch_mcp(monkeypatch, store):
    from mnemos import mcp_server
    from mnemos.encoding.encoder import Encoder

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_encoder", Encoder(store))
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    monkeypatch.setattr(mcp_server, "_effective_agent_id", lambda a: a)
    monkeypatch.setattr(mcp_server, "_effective_scope", lambda a, p, s: (a, p, s))
    return mcp_server


# ── (a) omitting kind is rejected at the signature layer ─────────────────────


def test_mnemos_remember_without_kind_is_a_typeerror(monkeypatch, store):
    """Mutation proof: restoring `kind: str = "semantic"` makes this go red."""
    mcp_server = _patch_mcp(monkeypatch, store)
    with pytest.raises(TypeError):
        mcp_server.mnemos_remember(content="a memory with no declared kind")


def test_mnemos_ingest_without_kind_is_a_typeerror(monkeypatch, store):
    """Mutation proof: restoring `kind: str = "semantic"` makes this go red."""
    mcp_server = _patch_mcp(monkeypatch, store)
    with pytest.raises(TypeError):
        mcp_server.mnemos_ingest(content="ingested content with no declared kind")


def test_capture_tools_have_no_kind_default_in_signature():
    """Belt-and-braces on the same class: no default exists to fall back to."""
    from mnemos import mcp_server

    for fn in (mcp_server.mnemos_remember, mcp_server.mnemos_ingest):
        param = inspect.signature(fn).parameters["kind"]
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__} must not default kind — absence of a declaration "
            "must not become a declaration (h7-bond-thickness §5.4)"
        )


# ── (a) the MCP tool schema marks kind required ──────────────────────────────


def _tool_schema(name: str) -> dict:
    from mnemos.mcp_server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    return tools[name].inputSchema


@pytest.mark.parametrize("tool_name", ["mnemos_remember", "mnemos_ingest"])
def test_capture_tool_schema_requires_kind(tool_name):
    """The MCP schema is what the LLM caller sees: kind must be declared
    required there, with no default advertised.

    Mutation proof: restoring the kind default drops it from `required`.
    """
    schema = _tool_schema(tool_name)
    assert "kind" in schema.get("properties", {})
    assert "kind" in schema.get("required", []), (
        f"{tool_name} schema must mark kind required"
    )
    assert "default" not in schema["properties"]["kind"], (
        f"{tool_name} schema must not advertise a kind default"
    )


# ── (b) explicit kind round-trips to the stored row ──────────────────────────


def test_mnemos_remember_explicit_episodic_round_trips(monkeypatch, store):
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_remember(
        content="We debugged the encoder together this afternoon.",
        kind="episodic",
        agent_id="oliver",
    )
    engram_id = out.split("Remembered: ", 1)[1].splitlines()[0].strip()
    engram = store.get_engram(engram_id, read_visibility=None)
    assert engram is not None
    assert engram.kind == "episodic"


def test_mnemos_ingest_explicit_episodic_round_trips(monkeypatch, store):
    mcp_server = _patch_mcp(monkeypatch, store)
    out = mcp_server.mnemos_ingest(
        content="Session log: the afternoon debugging run and what happened.",
        kind="episodic",
        agent_id="oliver",
    )
    engram_id = out.split("Ingested: ", 1)[1].splitlines()[0].strip()
    engram = store.get_engram(engram_id, read_visibility=None)
    assert engram is not None
    assert engram.kind == "episodic"


# ── (c) invalid kind -> clear error, nothing stored ──────────────────────────


@pytest.mark.parametrize("bad_kind", ["semantik", "event", "", "EPISODIC "])
def test_mnemos_remember_rejects_unknown_kind(monkeypatch, store, bad_kind):
    mcp_server = _patch_mcp(monkeypatch, store)
    before = store.count_engrams(agent_id="oliver", read_visibility=None)
    out = mcp_server.mnemos_remember(
        content="should not be stored",
        kind=bad_kind,
        agent_id="oliver",
    )
    assert "invalid kind" in out
    # The error teaches the valid choices.
    for valid in ("episodic", "semantic", "procedural", "prospective"):
        assert valid in out
    assert store.count_engrams(agent_id="oliver", read_visibility=None) == before


def test_mnemos_ingest_rejects_unknown_kind(monkeypatch, store):
    mcp_server = _patch_mcp(monkeypatch, store)
    before = store.count_engrams(agent_id="oliver", read_visibility=None)
    out = mcp_server.mnemos_ingest(
        content="should not be stored",
        kind="knowledge",
        agent_id="oliver",
    )
    assert "invalid kind" in out
    assert store.count_engrams(agent_id="oliver", read_visibility=None) == before


# ── (d) regression pins: the four ruled-defensible hardcoded semantic sites ──


class _RecordingEncoder:
    """Captures encode() kwargs; returns a minimal engram-shaped stub."""

    def __init__(self):
        self.calls: list[dict] = []

    def encode(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="engram_test_pin",
            connections=[],
            tags=[],
            source=SimpleNamespace(confidence=0.9),
        )


def _patch_setup(monkeypatch, store, encoder, config):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_encoder", encoder)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "save_config", lambda updated: None)
    monkeypatch.setattr(mcp_server, "_config_invalidate", lambda: None)
    return mcp_server


def test_setup_seed_engrams_stay_semantic(monkeypatch, store):
    """Pin: bootstrap seeds (mnemos_setup step 3) are deliberate fact-writes."""
    encoder = _RecordingEncoder()
    config = {
        "setup_step": 3,
        "agent_id": "vektor",
        "person_id": "riley",
        "agent_name": "Vektor",
        "user_name": "Riley",
    }
    mcp_server = _patch_setup(monkeypatch, store, encoder, config)

    mcp_server.mnemos_setup("Riley is a careful collaborator who values evidence.")

    assert encoder.calls, "setup step 3 should encode seed engrams"
    assert all(call["kind"] == "semantic" for call in encoder.calls)
    assert all(call["source"] == "bootstrap" for call in encoder.calls)
    assert all(call["allow_auto_share"] is False for call in encoder.calls)


def test_setup_project_context_stays_semantic(monkeypatch, store):
    """Pin: project-context writes (mnemos_setup step 4) are deliberate
    fact-writes."""
    encoder = _RecordingEncoder()
    config = {
        "setup_step": 4,
        "agent_id": "vektor",
        "person_id": "riley",
        "agent_name": "Vektor",
        "user_name": "Riley",
        "onboarding_session_id": None,
    }
    mcp_server = _patch_setup(monkeypatch, store, encoder, config)

    mcp_server.mnemos_setup("mnemos, pai")

    assert encoder.calls, "setup step 4 should encode project context"
    assert all(call["kind"] == "semantic" for call in encoder.calls)
    assert all(call["source"] == "bootstrap" for call in encoder.calls)
    assert all(call["allow_auto_share"] is False for call in encoder.calls)


def test_domain_claim_proposal_row_stays_semantic(monkeypatch, store):
    """Pin: the hypomnema_write domain-claim proposal row is a deliberate
    semantic fact-write about the claim, not a capture."""
    mcp_server = _patch_mcp(monkeypatch, store)
    mcp_server.mnemos_hypomnema_write(
        content="This is about who i am — my identity and selfhood.",
        domain="topical",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    claims = [
        p
        for p in store.list_proposals(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            status="pending_review",
        )
        if p["transition"] == "hypomnema_write_domain_claim"
    ]
    assert len(claims) == 1
    assert claims[0]["kind"] == "semantic"


def test_hypomnema_promotion_stays_semantic(monkeypatch, store):
    """Pin: hypomnema promotion encodes stable distilled continuity — a
    deliberate semantic write. A real encoder runs (the promoted-engram FK
    needs a real row), wrapped to record kwargs at the ruled call site."""
    from mnemos.encoding.encoder import Encoder

    inner = Encoder(store)
    encoder = _RecordingEncoder()
    encoder.encode = lambda **kwargs: (  # record, then really encode
        encoder.calls.append(kwargs),
        inner.encode(**kwargs),
    )[1]
    mcp_server = _patch_mcp(monkeypatch, store)
    monkeypatch.setattr(mcp_server, "_encoder", encoder)
    entry_id = store.write_hypomnema_entry(
        "Riley prefers evidence-first reviews.",
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        confidence=0.9,
        salience=0.8,
    )
    out = mcp_server.mnemos_hypomnema_promote(
        entry_id,
        dry_run=False,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
    )
    assert "Hypomnema promoted" in out, out
    assert len(encoder.calls) == 1
    assert encoder.calls[0]["kind"] == "semantic"
    assert encoder.calls[0]["source"] == "session"
    assert encoder.calls[0]["origin_stamp_override"] == "inference"
    assert encoder.calls[0]["allow_auto_share"] is False
    # And the stored row carries the ruled kind.
    engram_id = out.split("Engram: ", 1)[1].splitlines()[0].strip()
    engram = store.get_engram(engram_id, read_visibility=None)
    assert engram is not None
    assert engram.kind == "semantic"
    assert engram.origin_stamp == "inference"
    assert engram.visibility == "private"
