import ast
import json
import sqlite3
from pathlib import Path

import pytest

from mnemos.cli import main
from mnemos.consolidation.belief_review import (
    _resolve_pending_review_belief,
    format_belief_review_summary,
    run_belief_review,
)
from mnemos.encoding.llm_classifier import BeliefEvaluation
from mnemos.core.belief import Belief, BeliefRevision
from mnemos.core.engram import Engram
from mnemos.core.types import SourceAuthority, SourceType
from mnemos.encoding.encoder import Encoder
from mnemos.maintenance import restore_false_contradictions
from mnemos.maintenance.belief_restore import BeliefRestoreRow, _apply_row
from mnemos.store.sqlite_store import EngramStore


class BeliefEvalLLM:
    def __init__(self, belief_id: str, relation: str, impact: float = 1.0):
        self.belief_id = belief_id
        self.relation = relation
        self.impact = impact

    def structured_complete(self, **kwargs):
        return json.dumps(
            [
                {
                    "belief_id": self.belief_id,
                    "relation": self.relation,
                    "impact": self.impact,
                    "reasoning": "test evaluation",
                }
            ]
        )


class ReflectionLLM:
    def __init__(self, new_confidence: float):
        self.new_confidence = new_confidence

    def structured_complete(self, **kwargs):
        return json.dumps(
            {
                "new_confidence": self.new_confidence,
                "reasoning": "automatic reflection",
                "should_revise": True,
            }
        )


def _aged_belief(**kwargs) -> Belief:
    belief = Belief(**kwargs)
    belief.last_revised = "2000-01-01T00:00:00+00:00"
    return belief


def test_encoder_negation_overlap_no_longer_revises_beliefs_or_edges(store):
    support = Engram(id="support-1", content="Scoped review boundaries matter.")
    store.save_engram(support)
    belief = _aged_belief(
        id="belief-1",
        agent_id="oliver",
        content="David respects scoped review boundaries.",
        confidence=0.6,
        supporting_engram_ids=[support.id],
    )
    store.save_belief(belief)

    encoder = Encoder(store, llm_client=None)
    engram = encoder.encode(
        content="David does not treat scoped review boundaries as optional.",
        kind="semantic",
        source=SourceType.SESSION,
        agent_id="oliver",
        source_authority=SourceAuthority.OBSERVED,
    )

    [loaded] = store.get_beliefs("oliver", active_only=True)
    assert loaded.confidence == pytest.approx(0.6)
    assert loaded.revision_history == []
    stored = store.get_engram(engram.id)
    assert all(conn.relation != "contradicts" for conn in stored.connections)


def test_encoder_llm_contradiction_cannot_lower_belief_confidence(store):
    belief = _aged_belief(
        id="belief-llm",
        agent_id="oliver",
        content="David keeps review boundaries explicit.",
        confidence=0.6,
    )
    store.save_belief(belief)
    encoder = Encoder(store, llm_client=BeliefEvalLLM(belief.id, "CONTRADICTS"))

    encoder.encode(
        content="The system contradicted that belief.",
        kind="semantic",
        source=SourceType.SESSION,
        agent_id="oliver",
        source_authority=SourceAuthority.OBSERVED,
    )

    [loaded] = store.get_beliefs("oliver", active_only=True)
    assert loaded.confidence == pytest.approx(0.6)
    assert loaded.revision_history == []


def test_encoder_llm_support_cannot_raise_belief_confidence(store):
    belief = _aged_belief(
        id="belief-llm-support",
        agent_id="oliver",
        content="David keeps review boundaries explicit.",
        confidence=0.6,
    )
    store.save_belief(belief)
    encoder = Encoder(store, llm_client=BeliefEvalLLM(belief.id, "SUPPORTS"))

    encoder.encode(
        content="The system supported that belief.",
        kind="semantic",
        source=SourceType.SESSION,
        agent_id="oliver",
        source_authority=SourceAuthority.OBSERVED,
    )

    [loaded] = store.get_beliefs("oliver", active_only=True)
    assert loaded.confidence == pytest.approx(0.6)
    assert loaded.revision_history == []


def test_explicit_belief_review_queue_can_lower_confidence(tmp_path):
    store = EngramStore(tmp_path / "review.db")
    try:
        belief = _aged_belief(
            id="belief-review",
            agent_id="oliver",
            content="David keeps review boundaries explicit.",
            confidence=0.6,
            confidence_pending_review=True,
        )
        store.save_belief(belief)
        store.save_engram(
            Engram(
                content="Fresh authorized review evidence.",
                owner_agent_id="oliver",
            )
        )

        stats = run_belief_review(
            store,
            config={},
            llm_client=BeliefEvalLLM(belief.id, "CONTRADICTS", 1.0),
            agent_id="oliver",
        )

        [loaded] = store.get_beliefs("oliver", active_only=True)
        assert loaded.confidence == pytest.approx(0.56)
        assert len(loaded.revision_history) == 1
        assert (
            "Explicit belief review contradiction" in loaded.revision_history[0].reason
        )
        assert stats["beliefs_weakened"] == 1
        assert stats["beliefs_unchanged"] == 0
    finally:
        store.close()


def test_explicit_belief_review_queue_can_raise_confidence(tmp_path):
    store = EngramStore(tmp_path / "review-support.db")
    try:
        belief = _aged_belief(
            id="belief-review-support",
            agent_id="oliver",
            content="David keeps review boundaries explicit.",
            confidence=0.6,
            confidence_pending_review=True,
        )
        store.save_belief(belief)
        store.save_engram(
            Engram(
                content="Fresh authorized review evidence.",
                owner_agent_id="oliver",
            )
        )

        stats = run_belief_review(
            store,
            config={},
            llm_client=BeliefEvalLLM(belief.id, "SUPPORTS", 1.0),
            agent_id="oliver",
        )

        [loaded] = store.get_beliefs("oliver", active_only=True)
        assert loaded.confidence == pytest.approx(0.67)
        assert len(loaded.revision_history) == 1
        assert "Explicit belief review support" in loaded.revision_history[0].reason
        assert stats["beliefs_strengthened"] == 1
        assert stats["beliefs_unchanged"] == 0
    finally:
        store.close()


def test_explicit_needs_review_queue_can_change_confidence(tmp_path):
    store = EngramStore(tmp_path / "review-needs-review.db")
    try:
        belief = _aged_belief(
            id="belief-needs-review",
            agent_id="oliver",
            content="David keeps review boundaries explicit.",
            confidence=0.6,
            needs_review=True,
        )
        store.save_belief(belief)
        store.save_engram(
            Engram(
                content="Fresh authorized review evidence.",
                owner_agent_id="oliver",
            )
        )

        stats = run_belief_review(
            store,
            config={},
            llm_client=BeliefEvalLLM(belief.id, "SUPPORTS", 1.0),
            agent_id="oliver",
        )

        [loaded] = store.get_beliefs("oliver", active_only=True)
        assert loaded.confidence == pytest.approx(0.67)
        assert loaded.needs_review is False
        assert loaded.confidence_pending_review is False
        assert loaded.read_visibility == "operational_context"
        assert len(loaded.revision_history) == 1
        assert stats["beliefs_strengthened"] == 1
        assert stats["beliefs_unchanged"] == 0
    finally:
        store.close()


def test_belief_review_resolves_pending_belief_after_surprise_detection(tmp_path):
    store = EngramStore(tmp_path / "review-surprise.db")
    try:
        belief = _aged_belief(
            id="belief-review-surprise",
            agent_id="oliver",
            content="David keeps review boundaries explicit.",
            confidence=0.6,
            confidence_pending_review=True,
        )
        store.save_belief(belief)
        engram = Engram(
            content="Fresh surprised review evidence.",
            owner_agent_id="oliver",
        )
        engram.encoding_context.surprise_level = 1.0
        store.save_engram(engram)

        stats = run_belief_review(
            store,
            config={},
            llm_client=BeliefEvalLLM(belief.id, "SUPPORTS", 1.0),
            agent_id="oliver",
        )

        [loaded] = store.get_beliefs("oliver", active_only=True)
        assert loaded.confidence == pytest.approx(0.67)
        assert loaded.confidence_pending_review is False
        assert stats["memories_reviewed"] == 1
        assert stats["beliefs_resolved"] == 1
        assert stats["beliefs_strengthened"] == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    ("relation", "changed_stat"),
    [("SUPPORTS", "beliefs_strengthened"), ("CONTRADICTS", "beliefs_weakened")],
)
def test_belief_review_does_not_mutate_non_pending_operational_beliefs(
    tmp_path, relation, changed_stat
):
    store = EngramStore(tmp_path / f"review-non-pending-{relation}.db")
    try:
        belief = _aged_belief(
            id=f"belief-review-{relation.lower()}",
            agent_id="oliver",
            content="David keeps review boundaries explicit.",
            confidence=0.6,
        )
        store.save_belief(belief)
        store.save_engram(
            Engram(
                content="Fresh automatic consolidation evidence.",
                owner_agent_id="oliver",
            )
        )

        stats = run_belief_review(
            store,
            config={},
            llm_client=BeliefEvalLLM(belief.id, relation, 1.0),
            agent_id="oliver",
        )

        [loaded] = store.get_beliefs("oliver", active_only=True)
        assert loaded.confidence == pytest.approx(0.6)
        assert loaded.revision_history == []
        assert stats[changed_stat] == 0
        assert stats["beliefs_unchanged"] == 1
    finally:
        store.close()


def test_review_helper_refuses_no_bearing_authority(store):
    belief = _aged_belief(
        id="belief-no-bearing-helper",
        agent_id="oliver",
        content="David keeps review boundaries explicit.",
        confidence=0.6,
        confidence_pending_review=True,
    )
    store.save_belief(belief)

    with pytest.raises(ValueError, match="does not carry confidence authority"):
        _resolve_pending_review_belief(
            belief,
            BeliefEvaluation(
                belief_id=belief.id,
                relation="NO_BEARING",
                impact=0.0,
                reasoning="irrelevant",
            ),
            "engram-no-bearing",
            store,
        )

    [loaded] = store.get_beliefs(
        "oliver",
        active_only=True,
        include_pending_review=True,
    )
    assert loaded.confidence == pytest.approx(0.6)
    assert loaded.confidence_pending_review is True
    assert loaded.read_visibility == "review_only"
    assert loaded.revision_history == []


@pytest.mark.parametrize("new_confidence", [0.2, 0.9])
def test_substrate_reflection_cannot_change_belief_confidence(
    store, monkeypatch, new_confidence
):
    from mnemos.substrate.config import SubstrateConfig
    from mnemos.substrate.events import EventType, SubstrateEvent
    from mnemos.substrate.handlers import reflection
    from mnemos.substrate.modulators import ModulatorState

    monkeypatch.setattr(reflection, "load_prompt", lambda _name: None)

    belief = _aged_belief(
        id="belief-reflection",
        agent_id="oliver",
        content="David keeps review boundaries explicit.",
        confidence=0.6,
    )
    store.save_belief(belief)
    event = SubstrateEvent(
        event_type=EventType.BELIEF_CONTRADICTED,
        payload={"belief_id": belief.id},
        source="test",
    )

    reflection.handle(
        event,
        SubstrateConfig(agent_id="oliver", agent_name="Oliver"),
        ModulatorState(),
        store,
        ReflectionLLM(new_confidence),
    )

    [loaded] = store.get_beliefs("oliver", active_only=True)
    assert loaded.confidence == pytest.approx(0.6)
    assert loaded.revision_history == []


def test_encoder_contains_no_fallback_downward_revision_branch():
    source = Path("mnemos/encoding/encoder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct_revise_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "revise"
    ]

    assert not direct_revise_calls
    assert "negation_signals" not in source
    assert "Contradicted by new evidence: {engram.content[:50]}" not in source


def test_llm_classifier_contains_no_automatic_confidence_revision():
    source = Path("mnemos/encoding/llm_classifier.py").read_text(encoding="utf-8")
    assert "belief.revise(" not in source
    assert "BELIEF_SUPPORT_MULTIPLIER" not in source


def _restore_fixture(db_path):
    store = EngramStore(db_path)
    try:
        belief = Belief(
            id="belief-restore",
            agent_id="oliver",
            content="Living question belief.",
            confidence=0.40,
        )
        belief.revision_history = [
            BeliefRevision(
                timestamp="2026-07-05T01:00:00+00:00",
                old_confidence=0.40,
                new_confidence=0.35,
                reason="Contradicted by new evidence: The identity vault is LIVE...",
                trigger_engram_id="engram-a",
            ),
            BeliefRevision(
                timestamp="2026-07-05T02:00:00+00:00",
                old_confidence=0.35,
                new_confidence=0.45,
                reason="DAVID-13 living question seed",
                trigger_engram_id=None,
            ),
            BeliefRevision(
                timestamp="2026-07-05T03:00:00+00:00",
                old_confidence=0.45,
                new_confidence=0.40,
                reason="Contradicted by new evidence (impact 0.20): real review",
                trigger_engram_id="engram-real",
            ),
        ]
        store.save_belief(belief)
    finally:
        store.close()


def _restore_import_shape_fixture(db_path):
    store = EngramStore(db_path)
    try:
        belief = Belief(
            id="belief-import-shape",
            agent_id="oliver",
            content="Imported operational belief.",
            confidence=0.65,
        )
        belief.revision_history = [
            BeliefRevision(
                timestamp="2026-07-01T00:00:00+00:00",
                old_confidence=0.0,
                new_confidence=0.0,
                reason="reviewed + activated with David",
                trigger_engram_id=None,
            ),
            BeliefRevision(
                timestamp="2026-07-05T01:00:00+00:00",
                old_confidence=0.70,
                new_confidence=0.65,
                reason="Contradicted by new evidence: The identity vault is LIVE...",
                trigger_engram_id="engram-import",
            ),
        ]
        store.save_belief(belief)
    finally:
        store.close()


def test_restore_false_contradictions_dry_run_and_apply_are_receipted(tmp_path):
    db = tmp_path / "restore.db"
    _restore_fixture(db)

    dry = restore_false_contradictions(db)
    assert dry["mode"] == "dry-run"
    assert dry["beliefs_to_restore"] == 1
    assert dry["false_events_to_annul"] == 1
    assert dry["rows"][0]["restored_confidence"] == pytest.approx(0.45)

    applied = restore_false_contradictions(db, apply=True)
    assert applied["mode"] == "apply"
    assert len(applied["receipts"]) == 1
    assert applied["receipts"][0]["kind"] == "belief_confidence_restore"

    store = EngramStore(db)
    try:
        [belief] = store.get_beliefs("oliver", active_only=True)
        assert belief.confidence == pytest.approx(0.45)
        restore_event = belief.revision_history[-1].to_dict()
        assert restore_event["annuls"] == ["2026-07-05T01:00:00+00:00"]
        receipts = store.get_runtime_receipts(kind="belief_confidence_restore")
        assert len(receipts) == 1
        assert receipts[0]["payload"]["belief_id"] == "belief-restore"
        assert receipts[0]["payload"]["annuls"] == ["2026-07-05T01:00:00+00:00"]
    finally:
        store.close()

    second = restore_false_contradictions(db, apply=True)
    assert second["beliefs_to_restore"] == 0
    assert second["false_events_to_annul"] == 0


def test_restore_identifies_false_encoder_event_with_impact_text(tmp_path):
    db = tmp_path / "restore-impact-text.db"
    store = EngramStore(db)
    try:
        belief = Belief(
            id="belief-impact-text",
            agent_id="oliver",
            content="False encoder event content can mention impact.",
            confidence=0.35,
        )
        belief.revision_history = [
            BeliefRevision(
                timestamp="2026-07-05T01:00:00+00:00",
                old_confidence=0.40,
                new_confidence=0.35,
                reason=(
                    "Contradicted by new evidence: Memory text said (impact matters)."
                ),
                trigger_engram_id="engram-impact-text",
            ),
        ]
        store.save_belief(belief)
    finally:
        store.close()

    dry = restore_false_contradictions(db)

    assert dry["beliefs_to_restore"] == 1
    assert dry["false_events_to_annul"] == 1
    assert dry["rows"][0]["restored_confidence"] == pytest.approx(0.40)

    applied = restore_false_contradictions(db, apply=True)
    assert applied["beliefs_to_restore"] == 1

    store = EngramStore(db)
    try:
        [belief] = store.get_beliefs("oliver", active_only=True)
        assert belief.confidence == pytest.approx(0.40)
        assert belief.revision_history[-1].to_dict()["annuls"] == [
            "2026-07-05T01:00:00+00:00"
        ]
    finally:
        store.close()


def test_restore_apply_recomputes_current_confidence_before_writing(tmp_path):
    db = tmp_path / "restore-stale-plan.db"
    _restore_fixture(db)
    planned = restore_false_contradictions(db)["rows"][0]
    stale_row = BeliefRestoreRow(
        belief_id=planned["belief_id"],
        current_confidence=planned["current_confidence"],
        restored_confidence=planned["restored_confidence"],
        false_event_timestamps=planned["false_event_timestamps"],
        trigger_engram_ids=planned["trigger_engram_ids"],
    )

    store = EngramStore(db)
    try:
        store._get_conn().execute(
            "UPDATE beliefs SET confidence = ? WHERE id = ?",
            (0.60, stale_row.belief_id),
        )
        store._get_conn().commit()

        receipt = _apply_row(
            store,
            stale_row,
            actor="test",
            runtime="pytest",
            session_id="stale-plan",
        )

        [belief] = store.get_beliefs("oliver", active_only=True)
        assert belief.confidence == pytest.approx(0.65)
        assert receipt is not None
        assert receipt["payload"]["old_confidence"] == pytest.approx(0.60)
        assert receipt["payload"]["new_confidence"] == pytest.approx(0.65)
    finally:
        store.close()


def test_restore_apply_refuses_missing_db_without_creating_it(tmp_path):
    db = tmp_path / "missing.db"

    with pytest.raises(ValueError, match="refuses missing DB path"):
        restore_false_contradictions(db, apply=True)

    assert not db.exists()


def test_restore_apply_refuses_uninitialized_sqlite_db(tmp_path):
    db = tmp_path / "empty.db"
    db.touch()

    with pytest.raises(ValueError, match="required tables"):
        restore_false_contradictions(db, apply=True)


def test_restore_apply_refuses_schema_migrations_ahead(tmp_path):
    db = tmp_path / "restore-future-migration.db"
    _restore_fixture(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO schema_migrations
                (version, name, checksum, applied_at, snapshot)
            VALUES
                (99, 'from_the_future', 'abc',
                 '2026-01-01T00:00:00+00:00', 'x')
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="schema_migrations version 99"):
        restore_false_contradictions(db, apply=True)


def test_restore_caps_belief_confidence_below_absolute_certainty(tmp_path):
    db = tmp_path / "restore-cap.db"
    store = EngramStore(db)
    try:
        belief = Belief(
            id="belief-cap",
            agent_id="oliver",
            content="Nearly certain belief.",
            confidence=0.98,
        )
        belief.revision_history = [
            BeliefRevision(
                timestamp="2026-07-05T01:00:00+00:00",
                old_confidence=0.98,
                new_confidence=0.90,
                reason="Contradicted by new evidence: false event...",
                trigger_engram_id="engram-cap",
            ),
        ]
        store.save_belief(belief)
    finally:
        store.close()

    dry = restore_false_contradictions(db)

    assert dry["rows"][0]["current_confidence"] == pytest.approx(0.98)
    assert dry["rows"][0]["restored_confidence"] == pytest.approx(0.99)


def test_restore_import_shape_anchors_on_current_confidence_not_history_zero(tmp_path):
    db = tmp_path / "restore-import-shape.db"
    _restore_import_shape_fixture(db)

    dry = restore_false_contradictions(db)

    assert dry["beliefs_to_restore"] == 1
    assert dry["rows"][0]["current_confidence"] == pytest.approx(0.65)
    assert dry["rows"][0]["restored_confidence"] == pytest.approx(0.70)
    assert dry["rows"][0]["restored_confidence"] > dry["rows"][0]["current_confidence"]


def test_restore_refuses_non_raising_false_event(tmp_path):
    db = tmp_path / "restore-refuse.db"
    store = EngramStore(db)
    try:
        belief = Belief(
            id="belief-refuse",
            agent_id="oliver",
            content="Malformed false contradiction belief.",
            confidence=0.6,
        )
        belief.revision_history = [
            BeliefRevision(
                timestamp="2026-07-05T01:00:00+00:00",
                old_confidence=0.55,
                new_confidence=0.60,
                reason="Contradicted by new evidence: malformed event...",
                trigger_engram_id="engram-refuse",
            ),
        ]
        store.save_belief(belief)
    finally:
        store.close()

    with pytest.raises(ValueError, match="not a downward revision"):
        restore_false_contradictions(db)


def test_restore_cli_defaults_to_dry_run_and_refuses_live_db(capsys, tmp_path):
    db = tmp_path / "restore-cli.db"
    _restore_fixture(db)

    result = main(
        [
            "maintain",
            "--restore-false-contradictions",
            "--db-path",
            str(db),
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "dry-run" in out
    assert "Beliefs: 1" in out

    result = main(
        [
            "maintain",
            "--restore-false-contradictions",
            "--db-path",
            str(Path("~/.mnemos/memory.db").expanduser()),
        ]
    )
    err = capsys.readouterr().err
    assert result == 1
    assert "refuses live Mnemos databases" in err


def test_belief_review_summary_reports_mutation_and_pending_counts():
    assert (
        format_belief_review_summary(
            {
                "beliefs_reviewed": 3,
                "beliefs_strengthened": 1,
                "beliefs_weakened": 1,
                "beliefs_left_pending": 1,
            }
        )
        == "3 reviewed, 1 strengthened, 1 weakened, 1 left pending"
    )
