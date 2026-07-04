import pytest

from mnemos.core.engram import Engram, MemorySource
from mnemos.core.types import (
    ConfidenceSource,
    EncodingDepth,
    EngramKind,
    SourceType,
    Visibility,
)
from mnemos.encoding.encoder import should_auto_share
from mnemos.inner_life.low_stakes import write_low_stakes_record
from mnemos.store.sqlite_store import EngramStore


def test_low_stakes_writer_persists_private_generated_record_only(tmp_path):
    store = EngramStore(tmp_path / "low-stakes.db")
    try:
        result = write_low_stakes_record(
            store,
            gate_result={
                "allowed": True,
                "content": "  Turn evidence shows the gate stayed grounded. ",
                "source_ids": ["session-1", "turn-4"],
            },
            candidate_kind="reflection",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["written"] == 1
        assert result["generated_memory_writes"] == 1
        assert result["belief_writes"] == 0
        assert result["identity_patches"] == 0
        assert result["shared_pool_writes"] == 0

        # Low-stakes records are written audit_only; inspect via admin opt-in (R5/D8-A).
        engram = store.get_engram(result["engram_id"], read_visibility=None)
        assert engram is not None
        assert engram.content == "Turn evidence shows the gate stayed grounded."
        assert engram.impact == ""
        assert engram.kind == EngramKind.EPISODIC
        assert engram.tags == [
            "internal",
            "low-stakes",
            "generated",
            "u6.6",
            "rollout:u6.6-test",
            "reflection",
        ]
        assert engram.strength == pytest.approx(0.25)
        assert engram.stability == pytest.approx(0.05)
        assert engram.accessibility == pytest.approx(0.20)
        assert engram.encoding_context.wm_snapshot == ["session-1", "turn-4"]
        assert engram.encoding_context.encoding_depth == EncodingDepth.SHALLOW
        assert engram.encoding_context.session_id == "session-1"
        assert engram.encoding_context.surprise_level == pytest.approx(0.0)
        assert engram.source.type == SourceType.REFLECTION
        assert engram.source.confidence == pytest.approx(0.35)
        assert engram.source.confidence_source == ConfidenceSource.SPECULATIVE
        assert engram.lineage.parents == ["session-1", "turn-4"]
        assert engram.owner_agent_id == "oliver"
        assert engram.visibility == Visibility.PRIVATE
        assert engram.voice_exemplar_eligible is False
        assert engram.softening_protected is False
        assert engram.consolidation_authorized is False
        assert engram.decay_protected is False

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="tool_event",
            rollout_tag="u6.6-test",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "low-stakes-writer"
        assert rows[0]["gate_decision"] == "written:low_stakes"
        assert rows[0]["source_ids"] == ["session-1", "turn-4", engram.id]
        assert rows[0]["metadata"]["writes_memory"] is True
        assert rows[0]["metadata"]["generated_memory_writes"] == 1
        assert rows[0]["metadata"]["belief_writes"] == 0
        assert rows[0]["metadata"]["identity_patches"] == 0
        assert rows[0]["metadata"]["shared_pool_writes"] == 0
        assert rows[0]["metadata"]["voice_exemplar_eligible"] is False
        assert rows[0]["metadata"]["visibility"] == Visibility.PRIVATE

        assert store.get_beliefs(agent_id="oliver") == []
        assert (
            store.get_hypomnema_stats(
                agent_id="oliver",
                person_id="david",
                project_scope="pai",
            )["hypomnema_promotion_candidates"]
            == 0
        )
        assert (
            store.get_active_engrams(
                agent_id="oliver",
                require_consolidation_authorized=True,
            )
            == []
        )
    finally:
        store.close()


def test_low_stakes_writer_records_gate_skip_without_memory(tmp_path):
    store = EngramStore(tmp_path / "low-stakes-skip.db")
    try:
        result = write_low_stakes_record(
            store,
            gate_result={
                "allowed": False,
                "reason": "introspection_reject",
                "source_ids": ["turn-5"],
            },
            candidate_kind="dream",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert result["written"] == 0
        assert result["skipped"] == 1
        assert result["reason"] == "introspection_reject"
        assert store.count_engrams(agent_id="oliver") == 0

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
            rollout_tag="u6.6-test",
        )
        assert len(rows) == 1
        assert rows[0]["gate_decision"] == "skip:introspection_reject"
        assert rows[0]["metadata"]["writes_memory"] is False
    finally:
        store.close()


def test_low_stakes_idempotency_is_key_based_not_scope_window_based(tmp_path):
    store = EngramStore(tmp_path / "low-stakes-idempotency.db")
    gate_result = {
        "allowed": True,
        "content": "The same grounded candidate should write once.",
        "source_ids": ["session-2", "turn-9"],
    }
    try:
        first = write_low_stakes_record(
            store,
            gate_result=gate_result,
            candidate_kind="wander",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )
        store.upsert_inner_life_event(
            idempotency_key="unrelated-default-global-row",
            event_type="skip",
            process_name="low-stakes-writer",
        )
        second = write_low_stakes_record(
            store,
            gate_result=gate_result,
            candidate_kind="wander",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )

        assert first["written"] == 1
        assert second["written"] == 0
        assert second["duplicates"] == 1
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == 1
        engram = store.get_engram(first["engram_id"], read_visibility=None)
        assert engram is not None
        assert engram.source.type == SourceType.REFLECTION
    finally:
        store.close()


def test_observer_source_stays_private_even_with_auto_share_tags():
    observer = Engram(
        content="Observer found a possible lesson.",
        impact="",
        kind=EngramKind.SEMANTIC,
        tags=["discovery", "lesson"],
        source=MemorySource(
            type=SourceType.OBSERVER,
            confidence=0.95,
            confidence_source=ConfidenceSource.MODEL_INFERRED,
            authority="observed",
        ),
    )
    session = Engram(
        content="Session found a possible lesson.",
        impact="",
        kind=EngramKind.SEMANTIC,
        tags=["discovery", "lesson"],
        source=MemorySource(
            type=SourceType.SESSION,
            confidence=0.95,
            confidence_source=ConfidenceSource.USER_IMPLIED,
            authority="observed",
        ),
    )

    assert should_auto_share(observer) is False
    assert should_auto_share(session) is True
