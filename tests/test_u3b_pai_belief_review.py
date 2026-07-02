import json
from dataclasses import replace

import pytest

from mnemos.consolidation.belief_review import run_belief_review
from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.importer import PaiImportSource, apply_pai_import, preview_pai_import
from mnemos.store.sqlite_store import EngramStore


class ReviewLLM:
    def __init__(self, belief_id: str, relation: str, impact: float = 0.0):
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
                    "reasoning": "explicit review",
                }
            ]
        )


class RecordingReviewLLM(ReviewLLM):
    def __init__(self, belief_id: str, relation: str, impact: float = 0.0):
        super().__init__(belief_id, relation, impact)
        self.user_prompts: list[str] = []

    def structured_complete(self, **kwargs):
        self.user_prompts.append(kwargs["user"])
        return super().structured_complete(**kwargs)


def _source(kind: str, text: str) -> PaiImportSource:
    return PaiImportSource(
        job_id="u3b-job",
        source_path=f"/pai/{kind}.md",
        source_kind=kind,
        source_text=text,
        original_substrate="claude-opus-4-6",
        original_timestamp=1710000000,
    )


def _add_review_engram(
    store: EngramStore,
    content: str = "David keeps making coffee calibration explicit.",
) -> Engram:
    engram = Engram(
        content=content,
        impact="fresh evidence for belief review",
        owner_agent_id="oliver",
    )
    store.save_engram(engram)
    return engram


def _age_belief_for_review(store: EngramStore, belief_id: str) -> None:
    store._get_conn().execute(
        "UPDATE beliefs SET last_revised = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", belief_id),
    )
    store._get_conn().commit()


def _belief_row(store: EngramStore, belief_id: str):
    return store._get_conn().execute(
        """
        SELECT confidence, needs_review, confidence_pending_review,
               read_visibility, revision_history
        FROM beliefs
        WHERE id = ?
        """,
        (belief_id,),
    ).fetchone()


def test_u3b_pai_belief_review_clears_pending_flags_after_confidence_change(tmp_path):
    store = EngramStore(tmp_path / "u3b-review.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        changed = replace(
            source,
            source_text="David grinds his own coffee every morning.",
        )
        apply_pai_import(store, preview_pai_import(store, [changed]))
        _age_belief_for_review(store, target_id)
        _add_review_engram(store)

        stats = run_belief_review(
            store,
            config={},
            llm_client=ReviewLLM(target_id, "SUPPORTS", 0.5),
            agent_id="oliver",
        )

        row = _belief_row(store, target_id)
        assert bool(row["needs_review"]) is False
        assert bool(row["confidence_pending_review"]) is False
        assert row["confidence"] == pytest.approx(0.755)
        assert stats["beliefs_strengthened"] == 1
        revisions = json.loads(row["revision_history"])
        assert any(r.get("job_id") == "u3b-job" for r in revisions)
    finally:
        store.close()


def test_u3b_pai_belief_review_clears_pending_flags_after_noop_acceptance(tmp_path):
    store = EngramStore(tmp_path / "u3b-review.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        _add_review_engram(store)

        stats = run_belief_review(
            store,
            config={},
            llm_client=ReviewLLM(target_id, "NO_BEARING"),
            agent_id="oliver",
        )

        row = _belief_row(store, target_id)
        assert bool(row["needs_review"]) is False
        assert bool(row["confidence_pending_review"]) is False
        assert row["confidence"] == pytest.approx(0.72)
        assert stats["beliefs_unchanged"] == 1
    finally:
        store.close()


def test_u3b_pai_belief_review_excludes_quarantined_imported_engrams(tmp_path):
    store = EngramStore(tmp_path / "u3b-review.db")
    try:
        belief_source = _source("beliefs", "David trusts scoped review boundaries.")
        quarantined_text = "# Quarantine\nQuarantined PAI evidence must not review beliefs."
        import_preview = preview_pai_import(
            store,
            [belief_source, _source("identity_kernel", quarantined_text)],
        )
        apply_pai_import(store, import_preview)
        target_id = next(
            row.target_id for row in import_preview.rows if row.source_kind == "beliefs"
        )

        authorized_text = "Authorized review evidence may reach belief review."
        _add_review_engram(store, authorized_text)

        llm = RecordingReviewLLM(target_id, "NO_BEARING")
        stats = run_belief_review(
            store,
            config={},
            llm_client=llm,
            agent_id="oliver",
        )

        assert stats["memories_reviewed"] == 1
        assert len(llm.user_prompts) == 1
        assert authorized_text in llm.user_prompts[0]
        assert quarantined_text not in llm.user_prompts[0]
        row = _belief_row(store, target_id)
        assert bool(row["needs_review"]) is False
        assert bool(row["confidence_pending_review"]) is False
    finally:
        store.close()


def test_belief_review_leaves_audit_only_pending_beliefs_quarantined(tmp_path):
    store = EngramStore(tmp_path / "u3b-review.db")
    try:
        review_belief = Belief(
            id="review-belief",
            agent_id="oliver",
            content="Review pending belief can be accepted.",
            confidence=0.7,
            confidence_pending_review=True,
            read_visibility="review_only",
        )
        audit_belief = Belief(
            id="audit-belief",
            agent_id="oliver",
            content="Audit pending belief must not be accepted by default.",
            confidence=0.95,
            confidence_pending_review=True,
            read_visibility="audit_only",
        )
        store.save_belief(review_belief)
        store.save_belief(audit_belief)
        _age_belief_for_review(store, review_belief.id)
        _age_belief_for_review(store, audit_belief.id)
        _add_review_engram(store)

        run_belief_review(
            store,
            config={},
            llm_client=ReviewLLM(review_belief.id, "NO_BEARING"),
            agent_id="oliver",
        )

        review_row = _belief_row(store, review_belief.id)
        audit_row = _belief_row(store, audit_belief.id)
        assert bool(review_row["confidence_pending_review"]) is False
        assert review_row["read_visibility"] == "operational_context"
        assert bool(audit_row["confidence_pending_review"]) is True
        assert audit_row["read_visibility"] == "audit_only"
    finally:
        store.close()
