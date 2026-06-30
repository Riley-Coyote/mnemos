"""Tests for reactive retrieval."""
import pytest

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.core.types import ConnectionRelation
from mnemos.interface.prompt_builder import PromptBuilder


class TestReactiveRetriever:
    """Retrieval tests."""

    def test_recall_empty_db(self, retriever):
        """Querying an empty store returns an empty list."""
        results = retriever.retrieve("anything at all")
        assert results == []

    def test_recall_finds_match(self, encoder, retriever):
        """Encode a memory then retrieve it by cue."""
        encoder.encode(
            content="The Mnemos project uses SQLite with FTS5 for full-text search",
            kind="semantic",
            tags=["mnemos", "architecture"],
        )

        results = retriever.retrieve("SQLite full-text search")
        assert len(results) >= 1
        assert any(
            "SQLite" in r.engram.content or "FTS5" in r.engram.content
            for r in results
        )

    def test_co_retrieval_edges_are_co_activated_not_supports(
        self, store, encoder, retriever
    ):
        """Co-activation is correlation, not evidence.

        Regression for the relation-type monoculture: retrieval used to
        write co-retrieval edges as `supports` at 0.30 unconditionally,
        re-seeding the very monoculture the discovery pass was built to
        fix. They must be `co_activated` until something actually
        classifies them.
        """
        for i in range(2):
            encoder.encode(
                content=f"Spreading activation tuning note number {i} for retrieval",
                kind="semantic",
                tags=["retrieval"],
            )

        results = retriever.retrieve("spreading activation tuning retrieval")
        assert len(results) >= 2, "need co-retrieval for the edge to form"

        retrieval_edges = [
            conn
            for e in store.get_active_engrams(limit=100)
            for conn in e.connections
            if conn.formed_by == "retrieval"
        ]
        assert retrieval_edges, "co-retrieval created no edges"
        assert all(c.relation == "co_activated" for c in retrieval_edges)
        assert not any(c.relation == "supports" for c in retrieval_edges)

    def test_retrieval_excludes_review_only_fts_and_propagation(
        self, store, retriever
    ):
        """Review-only engrams cannot enter retrieval as seeds or graph targets."""
        operational = Engram(
            content="Operational afferent membrane seed",
            owner_agent_id="default",
            read_visibility="operational_context",
        )
        review_seed = Engram(
            content="Review-only afferent membrane seed with hidden prose",
            owner_agent_id="default",
            read_visibility="review_only",
        )
        review_target = Engram(
            content="Review-only propagation target with hidden prose",
            owner_agent_id="default",
            read_visibility="review_only",
        )
        operational.add_connection(
            review_target.id,
            ConnectionRelation.SUPPORTS,
            strength=1.0,
            formed_by="test",
        )
        store.save_engram(review_target)
        store.save_engram(review_seed)
        store.save_engram(operational)

        results = retriever.retrieve(
            "afferent membrane seed",
            agent_id="default",
            max_results=10,
        )
        contents = [result.engram.content for result in results]

        assert "Operational afferent membrane seed" in contents
        assert "Review-only afferent membrane seed with hidden prose" not in contents
        assert "Review-only propagation target with hidden prose" not in contents

        review_results = retriever.retrieve(
            "afferent membrane seed",
            agent_id="default",
            max_results=10,
            read_visibility="review_only",
        )
        assert [result.engram.id for result in review_results] == [review_seed.id]

    def test_retrieval_does_not_bridge_through_review_only_engram(
        self, store, retriever
    ):
        operational_seed = Engram(
            content="Operational membrane bridge seed",
            owner_agent_id="default",
            read_visibility="operational_context",
        )
        review_bridge = Engram(
            content="Review-only membrane bridge hidden middle",
            owner_agent_id="default",
            read_visibility="review_only",
        )
        operational_downstream = Engram(
            content="Operational downstream should not rank",
            owner_agent_id="default",
            read_visibility="operational_context",
        )
        operational_seed.add_connection(
            review_bridge.id,
            ConnectionRelation.SUPPORTS,
            strength=1.0,
            formed_by="test",
        )
        review_bridge.add_connection(
            operational_downstream.id,
            ConnectionRelation.SUPPORTS,
            strength=1.0,
            formed_by="test",
        )
        store.save_engram(operational_downstream)
        store.save_engram(review_bridge)
        store.save_engram(operational_seed)

        results = retriever.retrieve(
            "membrane bridge seed",
            agent_id="default",
            max_results=10,
        )
        result_ids = {result.engram.id for result in results}

        assert operational_seed.id in result_ids
        assert review_bridge.id not in result_ids
        assert operational_downstream.id not in result_ids

    def test_prompt_builder_hides_review_only_beliefs_and_engrams(self, store):
        """PromptBuilder inherits operational visibility for all producer reads."""
        store.save_belief(
            Belief(
                content="Review-only belief must not enter the prompt",
                confidence=0.99,
                read_visibility="review_only",
            )
        )
        store.save_engram(
            Engram(
                content="Review-only prompt leak anchor",
                owner_agent_id="default",
                read_visibility="review_only",
            )
        )

        prompt = PromptBuilder(store).build(
            "prompt leak anchor",
            agent_id="default",
            token_budget=1000,
        )

        assert "Review-only belief" not in prompt
        assert "Review-only prompt leak anchor" not in prompt
