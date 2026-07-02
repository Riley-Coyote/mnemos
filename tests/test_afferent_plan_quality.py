from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MAIN_PLAN = ROOT / "docs/plans/2026-06-30-001-feat-afferent-membrane-v1-plan.md"
REPAIR_PLAN = ROOT / "docs/plans/2026-06-30-002-fix-afferent-u2-5-safety-ledger-plan.md"
QUALITY_GATE = ROOT / "docs/plans/afferent-membrane-plan-quality-gate.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_no_operative_r9_or_r10(text: str) -> None:
    for match in re.finditer(r"\bRFC-R(?:9|10)\b", text):
        window = text[max(0, match.start() - 80) : match.end() + 120].lower()
        assert (
            "there is no" in window
            or "do not exist" in window
            or "does not use" in window
            or "no operative" in window
            or "rejects operative" in window
        ), window


def test_afferent_main_plan_keeps_rfc_rule_ledger_and_modulation_boundary():
    text = _read(MAIN_PLAN)

    assert "The RFC is the safety ledger and source of truth" in text
    assert "this plan cannot override, renumber, or weaken it" in text
    assert "U3 through U6b remain planned follow-up stages" in text
    assert (
        "Do not start U3 until `no-mistakes` passes for the same short SHA returned by `git rev-parse --short HEAD`"
        in text
    )
    assert (
        "SHA-256 `4c0a0b46534365023be89c328e6647b257bb431e5d4a5e346b74a8c56e1f976a`"
        in text
    )

    expected_mappings = [
        ("RFC-R1", "U3"),
        ("RFC-R2", "U3"),
        ("RFC-R3", "U2"),
        ("RFC-R4", "U4"),
        ("RFC-R5", "U2"),
        ("RFC-R6", "U1, extended by U2"),
        ("RFC-R7", "U5/U6"),
        ("RFC-R8", "U5/U6"),
    ]
    for rule, unit in expected_mappings:
        assert f"| {rule} |" in text
        assert unit in text

    assert "U7" not in text
    _assert_no_operative_r9_or_r10(text)
    assert "U5 alone must not actively shape live salience/retrieval" in text
    assert "active influence waits for U6 so both RFC-R7 bounds exist" in text
    assert "docs/plans/afferent-membrane-plan-quality-gate.md" in text


def test_afferent_main_plan_has_state_surface_and_regression_ledgers():
    text = _read(MAIN_PLAN)

    for heading in (
        "## Rule Ledger",
        "## State Ledger",
        "## Surface Ledger",
        "## Actor/Auth Ledger",
        "## Regression Ledger",
    ):
        assert heading in text

    required_rule_ledger = [
        "RFC rule | Implementation unit | Code chokepoints | Positive tests | Negative tests",
        "mnemos/store/sqlite_store.py::write_proposal",
        "mnemos/store/read_visibility.py::classify_hypomnema_read_visibility",
        "tests/test_store.py::TestEngramStore::test_proposal_upsert_rejects_rejected_terminal_conflict",
        "tests/test_context_packet.py::test_operational_proposal_count_uses_total_not_limited_reference_sample",
        "planned `tests/test_dynamic_modulation.py::test_modulation_decays_to_baseline_within_ttl`",
        "planned `tests/test_experience_tick.py::test_high_salience_single_tick_cannot_mint_semantic_truth`",
    ]
    for phrase in required_rule_ledger:
        assert phrase in text

    required_state_ledger = [
        "Allowed states / visibility",
        "Defaults and omitted fields",
        "Upsert behavior",
        "Terminal states",
        "Allowed transitions",
        "Rejected transitions",
        "duplicate write against terminal row is rejected without mutating reason, provenance, payload, status, or visibility",
        "fresh high-confidence/foundational write cannot become operational",
    ]
    for phrase in required_state_ledger:
        assert phrase in text

    required_surface_ledger = [
        "Context packet JSON and prompt",
        "MCP context/review/audit",
        "CLI inspect/direct-ID",
        "Retrieval/search/prompt builder",
        "Store aggregate/count/status",
        "Visual/dashboard",
        "Migration/backfill",
        "Tag/dream/substrate producers",
        "review/audit rows satisfying operational bootstrap thresholds",
    ]
    for phrase in required_surface_ledger:
        assert phrase in text

    required_actor_auth_ledger = [
        "This ledger prevents review/audit visibility from becoming a read permission grant",
        "Default/simple MCP agent",
        "Advanced MCP operator surface",
        "David-only review decision",
        "model/MCP callers cannot self-stamp `user_stated` or `imported`",
        "U4 must add negative tests rejecting forged David approvals",
    ]
    for phrase in required_actor_auth_ledger:
        assert phrase in text

    required_regression_ledger = [
        "Each invariant below requires at least one positive and one negative test",
        "Fresh high-confidence/foundational omitted-visibility hypomnema is review-only",
        "Duplicate/upsert cannot promote or downgrade quarantined hypomnema",
        "Terminal ProposalLedger rows are immutable",
        "Proposal sample limits do not alter total queue count",
        "Aggregates/bootstrap counts ignore quarantined rows",
    ]
    for phrase in required_regression_ledger:
        assert phrase in text


def test_u2_5_repair_plan_records_quality_gate_and_deliberate_deviation():
    text = _read(REPAIR_PLAN)

    assert "The reusable prevention gate for later Afferent work lives at" in text
    assert "no-mistakes should validate an already rigorous safety ledger" in text
    assert (
        "Any future no-mistakes `ask-user` policy decision must be folded back" in text
    )
    assert "Hypomnema promotion candidates may intentionally use `review_only`" in text
    assert "deliberate recorded deviation" in text
    assert "ProposalLedger rows should follow the RFC default" in text
    assert "`audit_only`" in text
    _assert_no_operative_r9_or_r10(text)


def test_afferent_main_plan_encodes_evidence_diversity_and_dream_graduation():
    """T2.6 plan repair: amendments A (AM-U4 evidence diversity) + B (AM-U6c dream graduation) pins.

    Each assertion is anchored to a specific plan-file addition. Mutation-check
    confirms each pin goes red when the corresponding plan edit is stashed.
    """
    text = _read(MAIN_PLAN)

    # AM-U4 Evidence Diversity subsection + review-surface folds
    assert "#### AM-U4 Evidence Diversity" in text
    assert "minimum pairwise dissimilarity across supporting instances" in text
    assert "Instance count is never a diversity signal" in text
    assert "Fail-toward-review" in text
    assert "dedupe-then-diversity" in text
    assert "Consistency-anomaly score" in text
    assert "Salience as queue-ordering only" in text
    assert "Development-health metric set" in text

    # Ledger extensions carrying the AM-U4 definition (Requirements + Rule Ledger both point at the subsection)
    assert text.count("evidence-diversity definition per AM-U4 subsection") >= 2
    assert "`mnemos/review/evidence_diversity.py::compute_evidence_diversity`" in text
    assert "supporting_instance_ids" in text
    assert "evidence_diversity" in text
    assert "consistency_anomaly" in text

    # A-regression rows (three, aligned to 3-col schema)
    assert "Evidence-diversity gate qualifies genuinely diverse support" in text
    assert (
        "Paraphrase-set never qualifies as evidence-diverse (false-consensus)" in text
    )
    assert "Single or incomputable support routes to review" in text

    # AM-U6c Dream Graduation charter
    assert "### U6c. Dream Graduation" in text
    assert "non-accumulating" in text
    assert "vividness-ratio cap" in text
    assert "three conjunctive tests" in text
    assert "adaptive decay" in text
    assert "MIN_RECURRENCES" in text
    assert "VIVIDNESS_RATIO_CAP" in text
    assert "drop:high_blast_generated" in text

    # R3/R7/R8 U6c extensions (Requirements + Rule Ledger carry U6c, 6 total)
    assert text.count("extended by U6c (dream graduation)") >= 6

    # Deferral-≠-re-graduation binding note
    assert "rejecting it is terminal for the pattern" in text
    assert "graduation is one-time" in text

    # State Ledger dream-pattern lifecycle row
    assert "Dream pattern (U6c)" in text
    assert "`stored/recurring/stabilized/graduated/decayed`" in text
    assert "no state reaches `graduated` without the three conjunctive tests" in text

    # Surface Ledger pattern-store row
    assert "Pattern store (U6c:" in text
    assert "dream-pattern prose never appears as operational context" in text

    # Actor/Auth Ledger dream-graduation producer row
    assert "Dream graduation (U6c pattern-store producer)" in text
    assert "no path in U6c mints anything above `generated`" in text

    # B-regression rows (four)
    assert "Authentic recurrence graduates as a proposal exactly once" in text
    assert "False-consensus variant-spam does not graduate" in text
    assert "A single vivid dream does not graduate (vividness-ratio cap)" in text
    assert "Identity-domain dream content drops regardless of stability" in text

    # Invariants that must survive the amendments
    assert "U7" not in text
    _assert_no_operative_r9_or_r10(text)


def test_plan_quality_gate_blocks_error_amplification_patterns():
    text = _read(QUALITY_GATE)

    required_phrases = [
        "The RFC remains the source of truth",
        "Rule Ledger",
        "State Ledger",
        "Surface Ledger",
        "Actor/Auth Ledger",
        "Regression Ledger",
        "RFC-R1 through RFC-R8",
        "RFC-R9 and RFC-R10 do not exist",
        "RFC rule -> implementation unit -> chokepoint -> positive test -> negative test",
        "allowed states, defaults, omitted-field behavior, upsert behavior, terminal states, allowed transitions, and rejected transitions",
        "terminal conflicts are immutable",
        "operational, review, audit, admin, migration, direct-ID, aggregate/count, visual, MCP, and context paths",
        "caller classes, authority values, proof artifacts, and negative tests",
        "David-only decisions require a non-model proof artifact",
        "Migration and legacy-row policy",
        "Every invariant needs at least one positive test and one negative test",
        "DynamicModulation bound check",
        "TTL/decay/magnitude",
        "valence floor/deadband/fail-toward-width",
        "No-mistakes error budget",
        "stop and repair the plan first",
        "bootstrap counts influenced by quarantined engrams",
        "stale documentation of schema defaults versus write-time classification",
    ]
    for phrase in required_phrases:
        assert phrase in text
