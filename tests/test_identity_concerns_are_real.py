"""Identity must not report its own bookkeeping as who the agent is.

Shift 5 computes identity from what the agent keeps returning to. It did that
by counting tags — but Mnemos stamps a fixed vocabulary of classifier, domain,
kind and indexer tags on nearly every memory by construction. `continuity` is
on every capture; `trace-type:fact` and `session-indexed` on every indexed
transcript line. So the "persistent concerns" the agent read back about itself
were:

    Persistent concerns: session-indexed, trace-type:fact, decision

— a histogram of the pipeline, surfaced as a self. Being told that is who you
are is worse than being told nothing.

A concern must be something a human or the content supplied, never a tag the
pipeline stamped. When nothing meaningful remains, the profile carries no
concerns and the summary omits the line rather than narrate bookkeeping.
"""

from __future__ import annotations

import pytest

from mnemos.core.identity import AgentIdentity, IdentityProfile
from mnemos.core.types import is_structural_tag
from mnemos.simple_runtime import MnemosRuntime


class _FakeEngram:
    def __init__(self, tags):
        self.tags = tags
        self.connections = []
        self.impact = ""
        self.content = ""
        self.kind = "semantic"


def _profile(tag_lists):
    from mnemos.consolidation.reflection import compute_identity_profile

    engrams = [_FakeEngram(tags) for tags in tag_lists]
    identity = AgentIdentity()
    identity.memory_profile.agent_id = "t"

    class _NoBeliefs:
        def get_beliefs(self, *a, **k):
            return []

    return compute_identity_profile(_NoBeliefs(), engrams, identity)


class TestStructuralTagVocabulary:
    @pytest.mark.parametrize(
        "tag",
        [
            "continuity", "preference", "decision", "project", "identity",
            "correction", "foundational", "recurring", "long-arc", "topical",
            "situational", "episodic", "semantic", "procedural", "prospective",
            "lesson", "distilled", "reflection", "synthesized", "session-indexed",
            "trace-type:fact", "trace-type:decision", "TRACE-TYPE:Fact",
        ],
    )
    def test_pipeline_tags_are_structural(self, tag):
        assert is_structural_tag(tag)

    @pytest.mark.parametrize("tag", ["vektor", "sourdough", "mnemos-redesign", "riley"])
    def test_meaningful_tags_are_not_structural(self, tag):
        assert not is_structural_tag(tag)


class TestPersistentConcernsExcludeBookkeeping:
    def test_a_store_of_only_pipeline_tags_yields_no_concerns(self):
        """The exact shape of a default install: every tag is classifier output."""
        profile = _profile([
            ["continuity", "decision", "trace-type:decision", "session-indexed"],
            ["continuity", "preference", "trace-type:fact", "session-indexed"],
        ])
        assert profile.persistent_concerns == [], profile.persistent_concerns
        assert "Persistent concerns" not in profile.to_summary()

    def test_a_genuine_concern_survives_among_the_noise(self):
        """A project the agent keeps returning to is a real concern; tags are not."""
        profile = _profile([
            ["continuity", "decision", "trace-type:fact", "vektor"],
            ["continuity", "project", "session-indexed", "vektor"],
            ["continuity", "trace-type:decision", "vektor"],
        ])
        concerns = dict(profile.persistent_concerns)
        assert set(concerns) == {"vektor"}, concerns
        assert concerns["vektor"] == 3
        assert "Persistent concerns: vektor" in profile.to_summary()

    def test_the_summary_omits_the_line_when_empty(self):
        """No fabricated self — the guard in to_summary already handles absence."""
        empty = IdentityProfile(persistent_concerns=[])
        assert "Persistent concerns" not in empty.to_summary()


class TestOnARealisticCapturePath:
    def test_captured_memories_do_not_make_classifier_labels_into_concerns(self, tmp_path):
        """End to end: capture through the product, compute identity, check the line.

        `mnemos_capture` runs `_simple_tags`, which stamps `continuity` plus a
        classifier label on everything. Those must not come back as concerns.
        """
        rt = MnemosRuntime(
            db_path=str(tmp_path / "id.db"),
            agent_id="t", person_id="p", project_scope="g",
        )
        rt.capture(content="Riley prefers concise answers with no preamble")
        rt.capture(content="Riley decided to ship the continuity layer first")

        from mnemos.consolidation.reflection import run_identity_pass

        run_identity_pass(store=rt._store, agent_id="t")
        identity = rt._store.get_identity("t")
        assert identity is not None

        summary = identity.epoch_state.self_summary
        for bookkeeping in ("continuity", "trace-type", "session-indexed", "preference", "decision"):
            assert bookkeeping not in summary, (
                f"identity summary surfaced the pipeline tag {bookkeeping!r} as a "
                f"concern:\n{summary}"
            )
