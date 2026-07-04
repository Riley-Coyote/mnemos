"""Integration test — full memory lifecycle."""

from mnemos.store.sqlite_store import EngramStore
from mnemos.encoding.encoder import Encoder
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.consolidation.daemon import ConsolidationDaemon
from mnemos.core.types import SourceType


class TestFullRoundtrip:
    """End-to-end: init -> encode -> recall -> consolidate -> recall -> verify."""

    def test_full_roundtrip(self, tmp_db):
        """Full lifecycle test with 3 memories."""
        store = EngramStore(tmp_db)
        encoder = Encoder(store, llm_client=None)
        retriever = ReactiveRetriever(store)
        daemon = ConsolidationDaemon(store=store, config={}, llm_client=None)

        # 1. Encode 3 memories
        e1 = encoder.encode(
            content="Mnemos uses spreading activation for memory retrieval",
            kind="semantic",
            tags=["architecture", "retrieval"],
            source=SourceType.SESSION,
            source_authority="observed",
        )
        encoder.encode(
            content="Connection types include supports, contradicts, causes, and extends",
            kind="semantic",
            tags=["architecture", "connections"],
            source=SourceType.SESSION,
            source_authority="observed",
        )
        encoder.encode(
            content="Deployed the MCP server to production successfully",
            kind="episodic",
            tags=["deployment", "mcp"],
            source=SourceType.SESSION,
            source_authority="observed",
        )

        assert store.count_engrams() >= 3

        # 2. Recall
        results_before = retriever.retrieve("spreading activation retrieval")
        assert len(results_before) >= 1

        # 3. Consolidate (shallow — no LLM needed)
        stats = daemon.run_cycle(deep=False)
        assert isinstance(stats, dict)

        # 4. Recall again after consolidation
        results_after = retriever.retrieve("spreading activation retrieval")
        assert len(results_after) >= 1

        # 5. Verify the engram still exists and is accessible
        loaded = store.get_engram(e1.id)
        assert loaded is not None
        assert loaded.content == e1.content

        store.close()
