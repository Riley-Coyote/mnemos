"""What a mechanically-formed edge is allowed to claim.

Connection discovery has two paths. With an LLM client it classifies a
relation semantically. Without one — the local-first baseline most
installs run — it falls back to FTS keyword overlap.

Keyword overlap is correlation. Labelling it SUPPORTS asserts that one
memory independently reinforces another's conclusion, which nothing
established. `types.py` says so directly at the CO_ACTIVATED definition:
"Writing these as SUPPORTS would re-seed the relation-type monoculture
the discovery pass was built to fix."

The distinction is not cosmetic. `reactive.py` weights SUPPORTS at 1.0
and CO_ACTIVATED at 0.6 when propagating activation, so a mislabelled
edge does not just misdescribe the graph — it pulls retrieval toward
memories that only share vocabulary.
"""

from mnemos.consolidation.connection_discovery import run_connection_discovery
from mnemos.core.types import ConnectionRelation, SourceType


def _encode(encoder, content, tags, agent_id="reltest"):
    return encoder.encode(
        content=content,
        impact="",
        kind="semantic",
        tags=tags,
        source=SourceType.SESSION,
        agent_id=agent_id,
        skip_surprise_detection=True,
    )


def _discovered_relations(store, agent_id="reltest"):
    rows = store._get_conn().execute(
        "SELECT relation, formed_by FROM connections WHERE formed_by LIKE 'consolidation%'"
    ).fetchall()
    return [(row["relation"], row["formed_by"]) for row in rows]


class TestNoLlmFallback:
    def test_keyword_overlap_is_recorded_as_correlation_not_evidence(
        self, store, encoder
    ):
        """Without a model, discovered edges must be CO_ACTIVATED.

        Fails on the previous code, which wrote SUPPORTS here.
        """
        _encode(encoder, "The deploy pipeline runs migrations before rollout", ["deploy"])
        _encode(encoder, "Migrations are applied by the deploy pipeline nightly", ["deploy"])

        stats = run_connection_discovery(store, llm_client=None, agent_id="reltest")

        relations = _discovered_relations(store)
        assert stats["connections_created"] >= 1
        assert relations, "no connections were formed, so the test proves nothing"
        for relation, _formed_by in relations:
            assert relation == ConnectionRelation.CO_ACTIVATED, (
                f"a keyword-overlap edge claimed {relation!r}; without a model "
                f"nothing established that one memory supports the other"
            )

    def test_the_fallback_path_is_identifiable_after_the_fact(self, store, encoder):
        """Edges must record that no model classified them.

        A later pass can only reclassify or strip these if it can tell
        them apart from semantically classified ones.
        """
        _encode(encoder, "The deploy pipeline runs migrations before rollout", ["deploy"])
        _encode(encoder, "Migrations are applied by the deploy pipeline nightly", ["deploy"])

        run_connection_discovery(store, llm_client=None, agent_id="reltest")

        formed_by = {formed for _relation, formed in _discovered_relations(store)}
        assert formed_by == {"consolidation_no_llm"}, formed_by

    def test_no_supports_edge_is_ever_invented_without_a_model(self, store, encoder):
        """The regression guard, stated as the invariant that matters."""
        for i in range(4):
            _encode(
                encoder,
                f"Session notes about the retrieval scoring blend, part {i}",
                ["retrieval", "scoring"],
            )

        run_connection_discovery(store, llm_client=None, agent_id="reltest")

        relations = {relation for relation, _ in _discovered_relations(store)}
        assert ConnectionRelation.SUPPORTS not in relations
