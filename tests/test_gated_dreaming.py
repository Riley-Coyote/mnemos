from mnemos.core.engram import Engram
from mnemos.core.types import ConfidenceSource, SourceType, Visibility
from mnemos.store.sqlite_store import EngramStore
from mnemos.substrate.config import SubstrateConfig
from mnemos.substrate.events import EventType, SubstrateEvent
from mnemos.substrate.handlers import dreaming
from mnemos.substrate.modulators import ModulatorState


class StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def structured_complete(self, system: str, user: str, temperature: float) -> str:
        self.prompts.append(user)
        return self.response


def _config(db_path, tmp_path, **overrides) -> SubstrateConfig:
    return SubstrateConfig(
        agent_id="oliver",
        db_path=str(db_path),
        log_dir=str(tmp_path / "logs"),
        dreaming_collision_threshold=0.1,
        **overrides,
    )


def _seed_collision_pair(store: EngramStore) -> tuple[Engram, Engram]:
    softened = Engram(
        content="AUTHORIZED-DREAM-FADING-SOURCE",
        impact="fading impact",
        owner_agent_id="oliver",
        accessibility=0.2,
        strength=0.5,
    )
    vivid = Engram(
        content="AUTHORIZED-DREAM-VIVID-SOURCE",
        impact="vivid impact",
        owner_agent_id="oliver",
        accessibility=0.95,
        strength=0.95,
    )
    store.save_engram(softened)
    store.save_engram(vivid)
    return softened, vivid


def _event(softened: Engram) -> SubstrateEvent:
    return SubstrateEvent(
        event_type=EventType.MEMORY_SOFTENED,
        payload={"engram_id": softened.id},
        source="test",
    )


def test_dreaming_writes_passed_recombination_as_low_stakes_only(tmp_path):
    db_path = tmp_path / "gated-dreaming-write.db"
    store = EngramStore(db_path)
    softened, vivid = _seed_collision_pair(store)
    stub = StubLLM('{"dream": "authorized synthesis", "significance": "set"}')
    try:
        dreaming.handle(
            _event(softened),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert "AUTHORIZED-DREAM-FADING-SOURCE" in stub.prompts[0]
        assert "AUTHORIZED-DREAM-VIVID-SOURCE" in stub.prompts[0]
        # Finding A: low-stakes output is audit_only, absent from operational reads.
        generated = [
            engram
            for engram in store.get_active_engrams(
                agent_id="oliver", limit=10, read_visibility="audit_only"
            )
            if "low-stakes" in engram.tags
        ]
        assert len(generated) == 1
        engram = generated[0]
        assert engram.content == "[dream] authorized synthesis"
        assert engram.read_visibility == "audit_only"
        assert engram.source.type == SourceType.DREAM
        assert engram.source.confidence_source == ConfidenceSource.SPECULATIVE
        assert engram.visibility == Visibility.PRIVATE
        assert engram.voice_exemplar_eligible is False
        assert engram.consolidation_authorized is False
        assert engram.lineage.parents == [softened.id, vivid.id]
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
        assert rows[1]["source_ids"] == [softened.id, vivid.id, engram.id]
    finally:
        store.close()


def test_dreaming_drops_metrics_only_dream_before_memory_write(tmp_path):
    db_path = tmp_path / "gated-dreaming-metrics.db"
    store = EngramStore(db_path)
    softened, _vivid = _seed_collision_pair(store)
    stub = StubLLM(
        '{"dream": "connections_created=2; engrams_softened=1; passes_run=cycle", '
        '"significance": "metrics"}'
    )
    try:
        dreaming.handle(
            _event(softened),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert store.count_engrams(agent_id="oliver") == 2
        rows = store.get_inner_life_events(
            agent_id="oliver",
            event_type="skip",
            rollout_tag="u6.6",
        )
        assert len(rows) == 1
        assert rows[0]["process_name"] == "narrative-gate"
        assert rows[0]["gate_decision"] == "drop:metrics_only_dream"
    finally:
        store.close()


def test_dreaming_time_gate_counts_prior_low_stakes_dream(tmp_path):
    db_path = tmp_path / "gated-dreaming-time.db"
    store = EngramStore(db_path)
    softened, _vivid = _seed_collision_pair(store)
    previous = Engram(
        content="[dream] already surfaced",
        impact="",
        tags=["internal", "low-stakes", "generated", "dream"],
        owner_agent_id="oliver",
        consolidation_authorized=False,
        read_visibility="audit_only",  # Finding A: prior low-stakes are audit_only
    )
    store.save_engram(previous)
    stub = StubLLM('{"dream": "should not run", "significance": "none"}')
    try:
        dreaming.handle(
            _event(softened),
            _config(db_path, tmp_path),
            ModulatorState(),
            store,
            stub,
        )

        assert stub.prompts == []
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == 3
    finally:
        store.close()
