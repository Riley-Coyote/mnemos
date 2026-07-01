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
        ), window


def test_afferent_main_plan_keeps_rfc_rule_ledger_and_modulation_boundary():
    text = _read(MAIN_PLAN)

    assert "The RFC is the safety ledger and source of truth" in text
    assert "this plan cannot override, renumber, or weaken it" in text

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

    _assert_no_operative_r9_or_r10(text)
    assert "U5 alone must not actively shape live salience/retrieval" in text
    assert "active influence waits for U6 so both RFC-R7 bounds exist" in text
    assert "docs/plans/afferent-membrane-plan-quality-gate.md" in text


def test_u2_5_repair_plan_records_quality_gate_and_deliberate_deviation():
    text = _read(REPAIR_PLAN)

    assert "The reusable prevention gate for later Afferent work lives at" in text
    assert "no-mistakes should validate an already rigorous safety ledger" in text
    assert "Any future no-mistakes `ask-user` policy decision must be folded back" in text
    assert "Hypomnema promotion candidates may intentionally use `review_only`" in text
    assert "deliberate recorded deviation" in text
    assert "ProposalLedger rows should follow the RFC default" in text
    assert "`audit_only`" in text
    _assert_no_operative_r9_or_r10(text)


def test_plan_quality_gate_blocks_error_amplification_patterns():
    text = _read(QUALITY_GATE)

    required_phrases = [
        "The RFC remains the source of truth",
        "RFC-R1 through RFC-R8",
        "RFC-R9 and RFC-R10 do not exist",
        "Chokepoint inventory",
        "Default and upsert semantics",
        "Terminal-state policy",
        "terminal conflicts are immutable",
        "Operational, review, and audit surface matrix",
        "Migration and legacy-row policy",
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
