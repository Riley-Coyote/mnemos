"""Softener as conservator: preserve the agent's register, never inflate or invent.

Softening rewrites memory in the substrate model's voice. Even same-family,
the pass must preserve the *agent's* register, not normalize it. These tests
cover the three conservator guarantees:

1. The prompt carries voice exemplars from the same agent and explicit
   preservation invariants.
2. Softened output never grows longer and never introduces named entities
   absent from the original — enforced in code, not just prompted.
3. softening_dry_run logs before/after pairs to consolidation_log without
   touching any engram.
"""

import pytest

from mnemos.consolidation.softening import run_softening_pass
from mnemos.core.engram import EncodingContext, Engram
from mnemos.core.types import EngramKind
from mnemos.store.sqlite_store import EngramStore


AGENT_A = "nova"
AGENT_B = "vektor"


class StubLLM:
    """Records prompts; replies with a fixed response."""

    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _engram(agent: str, content: str, *, accessibility: float = 0.5,
            resolution: float = 1.0, impact: str = "set") -> Engram:
    e = Engram(
        content=content,
        content_at_encoding=content,
        kind=EngramKind.SEMANTIC,
        impact=impact,  # pre-set so the pass skips impact extraction LLM calls
        owner_agent_id=agent,
        encoding_context=EncodingContext(session_id=f"{agent}-s1"),
    )
    e.accessibility = accessibility
    e.resolution = resolution
    return e


@pytest.fixture()
def store(tmp_path):
    s = EngramStore(tmp_path / "conservator.db")
    yield s
    s.close()


@pytest.fixture()
def seeded(store):
    """Agent A: two vivid memories + one fading candidate. Agent B: one vivid."""
    vivid_1 = _engram(
        AGENT_A,
        "i kept circling the same connection graph until it finally clicked... the shape was the answer",
        accessibility=0.9,
    )
    vivid_2 = _engram(
        AGENT_A,
        "quiet morning, rewrote the retrieval pass three times and loved every minute of it",
        accessibility=0.9,
    )
    # accessibility 0.45 → target resolution ~0.5: the standard softener
    # path (the deep-impression path engages below 0.4).
    fading = _engram(
        AGENT_A,
        "On June 3rd at 14:02 I debugged the spreading activation threshold with a profiler trace",
        accessibility=0.45,
    )
    other_agent = _engram(
        AGENT_B,
        "VEKTOR-DISTINCTIVE-PHRASE optics and lens calibrations all afternoon",
        accessibility=0.9,
    )
    for e in (vivid_1, vivid_2, fading, other_agent):
        store.save_engram(e)
    return {"vivid_1": vivid_1, "vivid_2": vivid_2, "fading": fading, "other": other_agent}


def test_prompt_carries_invariants_and_same_agent_exemplars(store, seeded):
    stub = StubLLM("a fading impression of debugging the activation threshold")
    run_softening_pass(store, {"softening_min_age_hours": 0}, stub, agent_id=AGENT_A)

    assert stub.prompts, "softener never called the LLM"
    prompt = stub.prompts[0]
    assert "Invariants" in prompt
    assert "Preserve the original's person and framing" in prompt
    assert "Preserve emotional valence" in prompt
    assert "must not be longer" in prompt
    # Voice exemplars come from the same agent's vivid memories...
    assert "same rememberer" in prompt
    assert "circling the same connection graph" in prompt
    # ...and never from another agent in the same store.
    assert "VEKTOR-DISTINCTIVE-PHRASE" not in prompt


def test_softened_output_never_longer(store, seeded):
    original = seeded["fading"].content
    inflated = original + " and then so much more happened that evening, truly a saga"
    stub = StubLLM(inflated)
    run_softening_pass(store, {"softening_min_age_hours": 0}, stub, agent_id=AGENT_A)

    softened = store.get_engram(seeded["fading"].id)
    assert len(softened.content) <= len(original)
    assert "truly a saga" not in softened.content


def test_softened_output_never_introduces_named_entities(store, seeded):
    stub = StubLLM("a hazy memory of debugging with Voldemort near the threshold")
    run_softening_pass(store, {"softening_min_age_hours": 0}, stub, agent_id=AGENT_A)

    softened = store.get_engram(seeded["fading"].id)
    assert "Voldemort" not in softened.content


def test_conserved_llm_output_is_kept(store, seeded):
    response = "a fading sense of chasing the activation threshold"
    stub = StubLLM(response)
    run_softening_pass(store, {"softening_min_age_hours": 0}, stub, agent_id=AGENT_A)

    softened = store.get_engram(seeded["fading"].id)
    assert softened.content == response
    assert softened.resolution < 1.0
    # The original is preserved immutably.
    assert softened.content_at_encoding == seeded["fading"].content_at_encoding


def test_dry_run_writes_nothing_and_logs_pairs(store, seeded):
    before = {
        e.id: (e.content, e.resolution, e.strength, len(e.connections), len(e.versions))
        for e in store.get_active_engrams(agent_id=AGENT_A, limit=100)
    }

    stub = StubLLM("a fading impression of the threshold")
    stats = run_softening_pass(store, {"softening_min_age_hours": 0, "softening_dry_run": True}, stub, agent_id=AGENT_A
    )

    after = {
        e.id: (e.content, e.resolution, e.strength, len(e.connections), len(e.versions))
        for e in store.get_active_engrams(agent_id=AGENT_A, limit=100)
    }
    assert after == before, "dry run mutated engrams"
    assert stats["dry_run"] is True
    assert stats["engrams_softened"] == 0
    assert stats["engrams_would_soften"] >= 1

    runs = store.get_consolidation_runs("softening_dry_run")
    assert runs, "dry run logged nothing"
    pairs = runs[0]["stats"]["pairs"]
    assert runs[0]["stats"]["agent_id"] == AGENT_A
    assert any(p["engram_id"] == seeded["fading"].id for p in pairs)
    pair = next(p for p in pairs if p["engram_id"] == seeded["fading"].id)
    assert pair["before"].startswith("On June 3rd")
    assert pair["after"] and pair["resolution_target"] < pair["resolution_before"]


def test_rule_based_path_also_conserved(store, seeded):
    """No LLM at all: the rule-based fallback obeys the same invariants."""
    run_softening_pass(store, {"softening_min_age_hours": 0}, None, agent_id=AGENT_A)
    softened = store.get_engram(seeded["fading"].id)
    assert len(softened.content) <= len(seeded["fading"].content_at_encoding)


def test_tiny_memory_keeps_original_rather_than_inflating(store):
    tiny = _engram(AGENT_A, "ok then.", accessibility=0.01)
    store.save_engram(tiny)
    run_softening_pass(store, {"softening_min_age_hours": 0}, None, agent_id=AGENT_A)
    softened = store.get_engram(tiny.id)
    # Nothing to shed: conservation keeps the original instead of an
    # "impression" boilerplate that would be longer than the memory itself.
    assert softened.content == "ok then."
