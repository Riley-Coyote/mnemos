"""Identity diff: computed identity (graph) vs declared identity (SOUL.md).

The system's deepest claim — identity is computed from the graph — is only
proven when the computed identity can be compared against the declared one
and the collision is handled deliberately. These tests seed a graph and a
soul file with known alignments, divergences (both directions), and
emergences, and verify the full loop: diff → surface at mnemos_context →
accept → epoch transition.
"""

import json

import pytest

from mnemos.core.belief import Belief
from mnemos.core.engram import EncodingContext, Engram
from mnemos.core.types import EngramKind
from mnemos.identity_diff import (
    IdentityDiffReport,
    accept_divergence,
    compute_graph_identity,
    diff_identity,
    load_latest_report,
    parse_soul_file,
    persist_report,
    write_divergence_note,
)
from mnemos.simple_runtime import MnemosScope
from mnemos.store.sqlite_store import EngramStore


AGENT = "nova"
PERSON = "riley"
PROJECT = "studio"

SOUL_TEXT = """# Soul — Nova

<!-- a comment that must not become claims -->

## Essence

Nova is a cartographer of typography and the spaces between letters.

## What Makes Me Me

- typography is how I think
- I prefer quiet systematic exploration

## Philosophy

### On Work

I always write tests before code.

### On Collaboration

I pair with humans on every decision when designing.

## Voice

...
"""


def _engram(content: str, tags: list[str], impact: str = "", recon: int = 0) -> Engram:
    e = Engram(
        content=content,
        content_at_encoding=content,
        kind=EngramKind.SEMANTIC,
        impact=impact,
        owner_agent_id=AGENT,
        encoding_context=EncodingContext(session_id="seed"),
    )
    e.tags = list(tags)
    e.reconsolidation_count = recon
    return e


@pytest.fixture()
def soul_path(tmp_path):
    p = tmp_path / "SOUL.md"
    p.write_text(SOUL_TEXT, encoding="utf-8")
    return p


@pytest.fixture()
def seeded_store(tmp_path):
    store = EngramStore(tmp_path / "identity.db")

    # Concern: typography (x4) — aligns with the soul.
    for i in range(4):
        store.save_engram(
            _engram(f"Explored typography rhythm in grid {i}", ["typography"])
        )
    # Concern: mcp-servers (x3) — emergence, never declared.
    for i in range(3):
        store.save_engram(
            _engram(f"Debugged the mcp transport layer {i}", ["mcp-servers"])
        )
    # Preoccupation: heavily reconsolidated — emergence.
    store.save_engram(
        _engram(
            "The migration keeps returning to me",
            ["migration"],
            impact="the unfinished migration question",
            recon=5,
        )
    )
    # Contradiction pair: the graph holds evidence against the declared
    # pairing trait.
    pairing = _engram(
        "I pair with humans on every decision when designing", ["collaboration"]
    )
    solo = _engram(
        "Solo deep work produced the strongest outcomes repeatedly", ["solo-work"]
    )
    store.save_engram(solo)
    pairing.add_connection(
        target_id=solo.id, relation="contradicts", strength=0.6, formed_by="seed"
    )
    store.save_engram(pairing)
    # Filler so the graph is not "too young" (>= 10 engrams).
    store.save_engram(_engram("Read about activation spreading", ["retrieval"]))

    # Beliefs: a confident value that aligns, a confident value that
    # emerged undeclared, and an uncertain belief the soul declares settled.
    store.save_belief(
        Belief(agent_id=AGENT, content="typography reveals how systems think", confidence=0.9)
    )
    store.save_belief(
        Belief(
            agent_id=AGENT,
            content="honest uncertainty beats confident wrongness",
            confidence=0.75,
        )
    )
    store.save_belief(
        Belief(agent_id=AGENT, content="writing tests before code pays off", confidence=0.35)
    )

    yield store
    store.close()


@pytest.fixture()
def scope(seeded_store):
    return MnemosScope(
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        db_path=str(seeded_store.db_path),
    )


def _diff(seeded_store, soul_path) -> IdentityDiffReport:
    soul = parse_soul_file(soul_path)
    graph = compute_graph_identity(seeded_store, AGENT)
    return diff_identity(soul, graph, llm_client=None)


# ── soul parsing ──


def test_parse_soul_sections_and_claims(soul_path):
    doc = parse_soul_file(soul_path)
    assert "Essence" in doc.sections
    assert "Philosophy > On Work" in doc.sections
    texts = [c.text for c in doc.real_claims]
    assert "typography is how I think" in texts
    assert "I always write tests before code." in texts
    assert not any("comment that must not" in t for t in texts)


def test_parse_soul_detects_placeholders(soul_path):
    doc = parse_soul_file(soul_path)
    assert "Voice" in doc.placeholder_sections
    # The Philosophy H2 has no body of its own (only H3 children).
    assert "Philosophy" in doc.placeholder_sections
    assert "Essence" not in doc.placeholder_sections


def test_parse_template_soul_is_mostly_placeholder():
    from pathlib import Path

    template = Path(__file__).parent.parent / "templates" / "SOUL.md"
    doc = parse_soul_file(template)
    assert "Essence" in doc.placeholder_sections
    assert "Voice" in doc.placeholder_sections
    assert len(doc.real_claims) <= 2


# ── graph profile thresholds ──


def test_graph_identity_applies_audit_thresholds(seeded_store):
    graph = compute_graph_identity(seeded_store, AGENT)

    values = {i.text for i in graph.facet("value")}
    assert "typography reveals how systems think" in values
    assert "honest uncertainty beats confident wrongness" in values

    questions = {i.text for i in graph.facet("question")}
    assert "writing tests before code pays off" in questions

    concerns = {i.text: i.weight for i in graph.facet("concern")}
    assert concerns.get("typography") == 4.0
    assert concerns.get("mcp-servers") == 3.0

    preoccupations = [i.text for i in graph.facet("preoccupation")]
    assert "the unfinished migration question" in preoccupations

    assert any(
        "Solo deep work" in contradicting
        for _, contradicting in graph.contradiction_edges
    )


# ── diff buckets ──


def test_diff_detects_alignment(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    aligned_claims = {f.soul_claim for f in report.alignments}
    assert "typography is how I think" in aligned_claims
    typo = next(f for f in report.alignments if f.soul_claim == "typography is how I think")
    assert typo.confidence_label in ("strong", "moderate")
    assert typo.soul_section == "What Makes Me Me"


def test_diff_detects_declared_not_grown(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    declared = [f for f in report.divergences if f.kind == "divergence_declared"]
    assert any(
        f.soul_claim == "I prefer quiet systematic exploration" for f in declared
    )


def test_diff_detects_grown_contradicting_via_uncertain_belief(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    grown = [f for f in report.divergences if f.kind == "divergence_grown"]
    inversion = [f for f in grown if f.graph_facet == "question"]
    assert inversion, "settledness inversion not detected"
    assert inversion[0].soul_claim == "I always write tests before code."
    assert "uncertain" in inversion[0].note


def test_diff_detects_grown_contradicting_via_contradicts_edge(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    grown = [f for f in report.divergences if f.kind == "divergence_grown"]
    edges = [f for f in grown if f.graph_facet == "contradiction"]
    assert edges, "contradicts-edge divergence not detected"
    assert "Solo deep work" in edges[0].graph_item
    assert edges[0].confidence_label == "weak"


def test_claim_lands_in_exactly_one_bucket(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    aligned = {f.soul_claim for f in report.alignments}
    diverged = {f.soul_claim for f in report.divergences if f.soul_claim}
    assert not aligned & diverged


def test_diff_detects_emergence(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    emerged = {f.graph_item for f in report.emergences}
    assert "mcp-servers" in emerged
    assert "the unfinished migration question" in emerged
    assert "honest uncertainty beats confident wrongness" in emerged


def test_diff_deterministic_without_llm(seeded_store, soul_path):
    a = _diff(seeded_store, soul_path).to_dict()
    b = _diff(seeded_store, soul_path).to_dict()
    a.pop("computed_at")
    b.pop("computed_at")
    assert a == b
    assert "lexical" in _diff(seeded_store, soul_path).signal_quality


# ── persistence + surfacing ──


def test_report_persists_and_loads(seeded_store, soul_path):
    report = _diff(seeded_store, soul_path)
    persist_report(seeded_store, report)
    loaded = load_latest_report(seeded_store, AGENT)
    assert loaded is not None
    assert loaded.agent_id == AGENT
    assert [f.finding_id for f in loaded.divergences] == [
        f.finding_id for f in report.divergences
    ]
    runs = seeded_store.get_consolidation_runs("identity_diff")
    assert runs and runs[0]["stats"]["agent_id"] == AGENT


def test_identity_diff_note_defaults_review_only_until_acceptance(
    seeded_store, soul_path, scope
):
    report = _diff(seeded_store, soul_path)
    note_id = write_divergence_note(seeded_store, scope, report)
    assert note_id is not None

    from mnemos.simple_runtime import MnemosRuntime

    runtime = MnemosRuntime(
        db_path=scope.db_path,
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        use_dedicated_model=False,
    )
    try:
        packet = runtime.context()
    finally:
        runtime.close()
    operational = seeded_store.get_hypomnema_entry(
        note_id,
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        read_visibility="operational_context",
    )
    review = seeded_store.get_hypomnema_entry(
        note_id,
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        read_visibility="review_only",
    )

    assert operational is None
    assert review is not None
    assert review["read_visibility"] == "review_only"
    assert "Identity tension" not in packet
    assert "mnemos identity diff" not in packet


def test_rerun_supersedes_prior_note(seeded_store, soul_path, scope):
    report = _diff(seeded_store, soul_path)
    first = write_divergence_note(seeded_store, scope, report)
    second = write_divergence_note(seeded_store, scope, report)
    assert first != second

    entries = seeded_store.search_hypomnema(
        "",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        limit=50,
        include_inactive=True,
        read_visibility="review_only",
    )
    tagged = [e for e in entries if "identity-diff" in (e.get("tags") or [])]
    active = [e for e in tagged if e["active"]]
    assert len(active) == 1
    assert active[0]["id"] == second
    superseded = next(e for e in tagged if e["id"] == first)
    assert superseded["superseded_by"] == second


# ── accept / epoch wiring ──


def test_accept_transitions_epoch_and_persists(seeded_store, soul_path, scope):
    report = _diff(seeded_store, soul_path)
    persist_report(seeded_store, report)
    write_divergence_note(seeded_store, scope, report)

    result = accept_divergence(seeded_store, scope, 1, note="growth is real")
    assert result["epoch_before"] == 0
    assert result["epoch_after"] == 1

    identity = seeded_store.get_identity(AGENT)
    assert identity is not None
    assert identity.epoch_state.epoch_number == 1
    assert len(identity.epoch_history) == 1
    archived = identity.epoch_history[0]
    assert report.divergences[0].finding_id in archived.trigger_event
    assert "growth is real" in archived.trigger_event

    # The tension note resolves into a resolution note.
    entries = seeded_store.search_hypomnema(
        "",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=PROJECT,
        limit=50,
        read_visibility="review_only",
    )
    tagged = [e for e in entries if "identity-diff" in (e.get("tags") or [])]
    assert len(tagged) == 1
    assert "accepted" in tagged[0]["content"]


def test_accept_without_diff_errors(tmp_path):
    store = EngramStore(tmp_path / "fresh.db")
    scope = MnemosScope(
        agent_id=AGENT, person_id=PERSON, project_scope=PROJECT,
        db_path=str(tmp_path / "fresh.db"),
    )
    try:
        with pytest.raises(ValueError, match="Run `mnemos identity diff` first"):
            accept_divergence(store, scope, 1)
    finally:
        store.close()


def test_accept_out_of_range_errors(seeded_store, soul_path, scope):
    report = _diff(seeded_store, soul_path)
    persist_report(seeded_store, report)
    with pytest.raises(ValueError, match="out of range"):
        accept_divergence(seeded_store, scope, 99)


# ── CLI ──


def _cli_args(soul_path, db_path):
    return [
        "--soul", str(soul_path),
        "--db-path", str(db_path),
        "--agent-id", AGENT,
        "--person-id", PERSON,
        "--project-scope", PROJECT,
        "--no-enrich",
    ]


def test_cli_identity_diff_smoke(seeded_store, soul_path, capsys):
    from mnemos.cli import main

    rc = main(["identity", "diff", *_cli_args(soul_path, seeded_store.db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALIGNMENTS" in out and "DIVERGENCES" in out and "EMERGENCES" in out
    assert "explicit identity review/acceptance surfaces" in out
    assert "surface at next mnemos_context" not in out

    rc = main(
        ["identity", "diff", "--json", *_cli_args(soul_path, seeded_store.db_path)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_id"] == AGENT
    assert all("finding_id" in f for f in payload["divergences"])


def test_cli_identity_accept_smoke(seeded_store, soul_path, capsys):
    from mnemos.cli import main

    assert main(["identity", "diff", *_cli_args(soul_path, seeded_store.db_path)]) == 0
    capsys.readouterr()

    rc = main(
        [
            "identity", "accept", "--divergence", "1",
            "--db-path", str(seeded_store.db_path),
            "--agent-id", AGENT,
            "--person-id", PERSON,
            "--project-scope", PROJECT,
        ]
    )
    assert rc == 0
    assert "Epoch 0 -> 1" in capsys.readouterr().out

    identity = seeded_store.get_identity(AGENT)
    assert identity is not None and identity.epoch_state.epoch_number == 1


def test_cli_identity_diff_missing_soul(tmp_path, capsys):
    from mnemos.cli import main

    rc = main(
        [
            "identity", "diff",
            "--soul", str(tmp_path / "nope" / "SOUL.md"),
            "--db-path", str(tmp_path / "x.db"),
            "--agent-id", AGENT,
            "--no-enrich",
        ]
    )
    assert rc == 2
