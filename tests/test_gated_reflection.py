from mnemos.consolidation.reflection import run_reflection_pass
from mnemos.core.emotional_state import EmotionalState
from mnemos.core.engram import Engram
from mnemos.core.identity import AgentIdentity
from mnemos.core.types import ConfidenceSource, SourceType, Visibility
from mnemos.store.sqlite_store import EngramStore


class StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _identity() -> AgentIdentity:
    identity = AgentIdentity()
    identity.memory_profile.agent_id = "oliver"
    return identity


def _seed_reflection_memories(store: EngramStore) -> list[Engram]:
    engrams = [
        Engram(
            content="verified change one stayed inside the branch boundary",
            impact="proof before narrative",
            tags=["verified-theme"],
            owner_agent_id="oliver",
        ),
        Engram(
            content="verified change two kept live database writes blocked",
            impact="live auth boundary held",
            tags=["verified-theme"],
            owner_agent_id="oliver",
        ),
        Engram(
            content="verified change three added regression tests",
            impact="tests guard the new behavior",
            tags=["verified-theme"],
            owner_agent_id="oliver",
        ),
    ]
    for engram in engrams:
        store.save_engram(engram)
    return engrams


def test_gated_reflection_drops_manufactured_thought_but_computes_identity(tmp_path):
    store = EngramStore(tmp_path / "gated-reflection-drop.db")
    try:
        _seed_reflection_memories(store)
        llm = StubLLM("I feel alive in a deeply meaningful inner chamber.")
        identity = _identity()

        stats = run_reflection_pass(
            store,
            identity,
            EmotionalState(),
            llm,
            {
                "reflection_lookback_hours": 999999,
                "inner_life_person_id": "david",
                "inner_life_project_scope": "pai",
                "inner_life_rollout_tag": "u6.6-test",
            },
        )

        assert llm.prompts
        assert stats["identity_computed"] is True
        assert stats["thoughts_generated"] == 0
        assert stats["thoughts_dropped"] == 1
        assert stats["generated_memory_writes"] == 0
        assert store.count_engrams(agent_id="oliver") == 3

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
            rollout_tag="u6.6-test",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "narrative-gate"
        assert rows[0]["gate_decision"] == "drop:manufactured_inner_state"
        assert "verified-theme" in identity.epoch_state.self_summary
    finally:
        store.close()


def test_gated_reflection_writes_passed_thought_as_low_stakes_only(tmp_path):
    store = EngramStore(tmp_path / "gated-reflection-write.db")
    try:
        seeds = _seed_reflection_memories(store)
        llm = StubLLM("The verified changes form a pattern: proof before narrative.")

        stats = run_reflection_pass(
            store,
            _identity(),
            EmotionalState(),
            llm,
            {
                "reflection_lookback_hours": 999999,
                "inner_life_person_id": "david",
                "inner_life_project_scope": "pai",
                "inner_life_rollout_tag": "u6.6-test",
            },
        )

        assert stats["identity_computed"] is True
        assert stats["thoughts_generated"] == 1
        assert stats["thoughts_dropped"] == 0
        assert stats["generated_memory_writes"] == 1
        assert stats["belief_writes"] == 0
        assert stats["identity_patches"] == 0
        assert stats["shared_pool_writes"] == 0
        assert store.get_beliefs(agent_id="oliver") == []

        generated = [
            engram
            for engram in store.get_active_engrams(agent_id="oliver", limit=10)
            if "low-stakes" in engram.tags
        ]
        assert len(generated) == 1
        engram = generated[0]
        assert engram.source.type == SourceType.REFLECTION
        assert engram.source.confidence_source == ConfidenceSource.SPECULATIVE
        assert engram.visibility == Visibility.PRIVATE
        assert engram.voice_exemplar_eligible is False
        assert engram.consolidation_authorized is False
        assert engram.lineage.parents == [seed.id for seed in seeds]

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )
        assert [row["process_name"] for row in rows] == [
            "narrative-gate",
            "low-stakes-writer",
        ]
        assert rows[0]["gate_decision"] == "pass"
        assert rows[1]["gate_decision"] == "written:low_stakes"
    finally:
        store.close()


def test_gated_reflection_introspection_rejects_before_memory_write(tmp_path):
    store = EngramStore(tmp_path / "gated-reflection-introspect.db")
    try:
        _seed_reflection_memories(store)
        llm = StubLLM("The verified changes should stay provisional.")

        stats = run_reflection_pass(
            store,
            _identity(),
            EmotionalState(),
            llm,
            {
                "reflection_lookback_hours": 999999,
                "inner_life_person_id": "david",
                "inner_life_project_scope": "pai",
                "inner_life_rollout_tag": "u6.6-test",
                "inner_life_introspector": lambda _content: {
                    "verdict": "reject",
                    "performed": True,
                },
            },
        )

        assert stats["identity_computed"] is True
        assert stats["thoughts_generated"] == 0
        assert stats["thoughts_dropped"] == 1
        assert store.count_engrams(agent_id="oliver") == 3

        rows = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            event_type="skip",
            rollout_tag="u6.6-test",
        )
        assert rows[0]["gate_decision"] == "drop:introspection_reject"
        assert rows[0]["metadata"]["introspection_report"]["verdict"] == "reject"
    finally:
        store.close()
