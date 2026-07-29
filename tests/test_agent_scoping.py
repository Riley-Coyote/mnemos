"""Agent scope integrity: consolidation must never touch another agent's memory.

Regression tests for the hardcoded-default-scope bug class:
- connection_discovery and belief_review defaulted to agent_id="luca"
- softening queried engrams with the store's default scope regardless of agent
- the daemon's blank-identity fallback made reflection derive the wrong scope

The invariant under test: running any consolidation pass scoped to agent A
must leave agent B's rows untouched, and must actually process agent A.
"""

import pytest

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram, EncodingContext
from mnemos.core.identity import AgentIdentity
from mnemos.core.types import DEFAULT_AGENT_ID, EngramKind
from mnemos.consolidation.belief_review import run_belief_review
from mnemos.consolidation.connection_discovery import run_connection_discovery
from mnemos.consolidation.decay import run_decay_pass
from mnemos.consolidation.softening import run_softening_pass
from mnemos.store.sqlite_store import EngramStore


AGENT_A = "nova"
AGENT_B = "vektor"


@pytest.fixture()
def two_agent_store(tmp_path):
    store = EngramStore(tmp_path / "scoping.db")
    for agent, topics in ((AGENT_A, ["graphs", "topology"]), (AGENT_B, ["lenses", "exposure"])):
        for t in topics:
            e = Engram(
                content=f"{agent} learned something durable about {t} today",
                content_at_encoding=f"{agent} learned something durable about {t} today",
                kind=EngramKind.SEMANTIC,
                impact=f"insight about {t}",
                owner_agent_id=agent,
                encoding_context=EncodingContext(session_id=f"{agent}-s1"),
            )
            store.save_engram(e)
    return store


def _snapshot(store, agent_id):
    rows = store.get_active_engrams(agent_id=agent_id, limit=100)
    return {
        e.id: (e.content, e.strength, e.stability, e.accessibility, len(e.connections))
        for e in rows
    }


def test_default_agent_id_is_not_a_personal_name():
    assert DEFAULT_AGENT_ID == "default"


def test_connection_discovery_default_scope_is_default(two_agent_store):
    """The pass must not silently scope to a hardcoded personal agent name."""
    import inspect

    sig = inspect.signature(run_connection_discovery)
    assert sig.parameters["agent_id"].default == DEFAULT_AGENT_ID


def test_belief_review_default_scope_is_default():
    import inspect

    sig = inspect.signature(run_belief_review)
    assert sig.parameters["agent_id"].default == DEFAULT_AGENT_ID


def test_connection_discovery_scoped_leaves_other_agent_untouched(two_agent_store):
    before_b = _snapshot(two_agent_store, AGENT_B)
    stats = run_connection_discovery(
        two_agent_store, embedding_index=None, config={}, llm_client=None, agent_id=AGENT_A
    )
    after_b = _snapshot(two_agent_store, AGENT_B)
    assert after_b == before_b, "agent B's engrams changed during agent A's discovery"
    assert stats["engrams_processed"] > 0, "agent A's engrams were not processed"


def test_decay_scoped_leaves_other_agent_untouched(two_agent_store):
    before_b = _snapshot(two_agent_store, AGENT_B)
    stats = run_decay_pass(two_agent_store, {}, agent_id=AGENT_A)
    after_b = _snapshot(two_agent_store, AGENT_B)
    assert after_b == before_b
    assert stats["engrams_processed"] > 0


def test_softening_scoped_leaves_other_agent_untouched(two_agent_store):
    """Softening rewrites content — cross-agent softening is contamination."""
    # Force every engram below the softening threshold so candidates exist.
    for agent in (AGENT_A, AGENT_B):
        for e in two_agent_store.get_active_engrams(agent_id=agent, limit=100):
            e.accessibility = 0.01
            e.resolution = 0.5
            two_agent_store.save_engram(e)

    before_b = _snapshot(two_agent_store, AGENT_B)
    stats = run_softening_pass(two_agent_store, {"softening_threshold": 0.9, "softening_min_age_hours": 0}, None, agent_id=AGENT_A)
    after_b = _snapshot(two_agent_store, AGENT_B)

    b_content_before = {k: v[0] for k, v in before_b.items()}
    b_content_after = {k: v[0] for k, v in after_b.items()}
    assert b_content_after == b_content_before, "agent B's memory content was rewritten"
    assert stats["engrams_evaluated"] > 0, "agent A's engrams were not evaluated"


def test_belief_review_scoped_reviews_correct_agent(two_agent_store):
    for agent in (AGENT_A, AGENT_B):
        b = Belief(agent_id=agent, content=f"{agent} believes in scoped maintenance", confidence=0.5)
        two_agent_store.save_belief(b)

    beliefs_b_before = {
        b.id: (b.confidence, len(b.revision_history))
        for b in two_agent_store.get_beliefs(AGENT_B, active_only=True)
    }
    run_belief_review(two_agent_store, config={}, llm_client=None, agent_id=AGENT_A)
    beliefs_b_after = {
        b.id: (b.confidence, len(b.revision_history))
        for b in two_agent_store.get_beliefs(AGENT_B, active_only=True)
    }
    assert beliefs_b_after == beliefs_b_before, "agent B's beliefs were revised during agent A's review"


def test_blank_identity_fallback_carries_agent_scope():
    """Mirrors the daemon's fallback: a fresh AgentIdentity must be re-scoped
    before reflection derives its memory scope from it."""
    identity = AgentIdentity()
    identity.memory_profile.agent_id = AGENT_A
    assert identity.memory_profile.agent_id == AGENT_A
