from mnemos.core.engram import Engram
from mnemos.core.types import ConfidenceSource, SourceType, Visibility
from mnemos.store.sqlite_store import EngramStore
from mnemos.substrate.config import SubstrateConfig
from mnemos.substrate.events import EventType, SubstrateEvent
from mnemos.substrate.handlers import wandering
from mnemos.substrate.modulators import ModulatorState


class StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def structured_complete(self, system: str, user: str, temperature: float) -> str:
        self.prompts.append(user)
        return self.response


def _event() -> SubstrateEvent:
    return SubstrateEvent(
        event_type=EventType.SILENCE_EXTENDED,
        payload={"silence_hours": 12},
        source="test",
    )


def _config(db_path, tmp_path, **overrides) -> SubstrateConfig:
    return SubstrateConfig(
        agent_id="oliver",
        db_path=str(db_path),
        log_dir=str(tmp_path / "logs"),
        **overrides,
    )


def _seed_authorized_memory(store: EngramStore, content: str = "authorized seed") -> Engram:
    engram = Engram(
        content=content,
        impact="source impact",
        owner_agent_id="oliver",
    )
    store.save_engram(engram)
    return engram


def test_wandering_writes_passed_thought_as_low_stakes_only(tmp_path):
    db_path = tmp_path / "gated-wandering-write.db"
    store = EngramStore(db_path)
    seed = _seed_authorized_memory(store, "AUTHORIZED-WANDERING-SOURCE")
    stub = StubLLM('{"thought": "authorized wandering", "origin": "authorized"}')
    try:
        wandering.handle(
            _event(),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert "AUTHORIZED-WANDERING-SOURCE" in stub.prompts[0]
        generated = [
            engram
            for engram in store.get_active_engrams(agent_id="oliver", limit=10)
            if "low-stakes" in engram.tags
        ]
        assert len(generated) == 1
        engram = generated[0]
        assert engram.content == "[wandering] authorized wandering"
        assert engram.source.type == SourceType.REFLECTION
        assert engram.source.confidence_source == ConfidenceSource.SPECULATIVE
        assert engram.visibility == Visibility.PRIVATE
        assert engram.voice_exemplar_eligible is False
        assert engram.consolidation_authorized is False
        assert engram.lineage.parents == [seed.id]
        assert store.get_beliefs(agent_id="oliver") == []

        rows = store.get_inner_life_events(
            agent_id="oliver",
            event_type="tool_event",
            rollout_tag="u6.6",
        )
        assert [row["process_name"] for row in rows] == [
            "narrative-gate",
            "low-stakes-writer",
        ]
        assert rows[0]["gate_decision"] == "pass"
        assert rows[1]["gate_decision"] == "written:low_stakes"
        assert rows[1]["source_ids"] == [seed.id, engram.id]
    finally:
        store.close()


def test_wandering_drops_manufactured_thought_before_memory_write(tmp_path):
    db_path = tmp_path / "gated-wandering-drop.db"
    store = EngramStore(db_path)
    _seed_authorized_memory(store)
    stub = StubLLM(
        '{"thought": "I feel alive in a deeply meaningful inner chamber.", '
        '"origin": "authorized"}'
    )
    try:
        wandering.handle(
            _event(),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert store.count_engrams(agent_id="oliver") == 1
        rows = store.get_inner_life_events(
            agent_id="oliver",
            event_type="skip",
            rollout_tag="u6.6",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "narrative-gate"
        assert rows[0]["gate_decision"] == "drop:manufactured_inner_state"
    finally:
        store.close()


def test_wandering_time_gate_counts_prior_low_stakes_wandering(tmp_path):
    db_path = tmp_path / "gated-wandering-time.db"
    store = EngramStore(db_path)
    _seed_authorized_memory(store)
    previous = Engram(
        content="[wandering] already surfaced",
        impact="",
        tags=["internal", "low-stakes", "generated", "wandering"],
        owner_agent_id="oliver",
        consolidation_authorized=False,
    )
    store.save_engram(previous)
    stub = StubLLM('{"thought": "should not run", "origin": "authorized"}')
    try:
        wandering.handle(
            _event(),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert stub.prompts == []
        assert store.count_engrams(agent_id="oliver") == 2
    finally:
        store.close()
