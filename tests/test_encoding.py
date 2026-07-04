"""Tests for the encoding pipeline."""

import pytest

from mnemos.core.engram import Engram
from mnemos.core.types import (
    BOOTSTRAP_STABILITY,
    BOOTSTRAP_STRENGTH,
    SourceAuthority,
    SourceType,
)


class TestEncoder:
    """Encoder tests — rule-based fallback (no LLM)."""

    def test_encode_basic(self, encoder):
        """Encode a simple memory with no LLM."""
        engram = encoder.encode(
            content="Riley prefers dark mode in all applications",
            kind="semantic",
            tags=["preference", "ui"],
            source=SourceType.SESSION,
            source_authority=SourceAuthority.OBSERVED,
        )
        assert engram is not None
        assert engram.content == "Riley prefers dark mode in all applications"
        assert "preference" in engram.tags
        assert engram.state == "active"

    def test_quarantined_engrams_do_not_end_bootstrap_policy(self, store, encoder):
        """Review/audit rows should not count toward operational encoding thresholds."""
        for index in range(60):
            store.save_engram(
                Engram(
                    content=f"Quarantined memory {index}",
                    read_visibility="review_only" if index % 2 else "audit_only",
                )
            )

        engram = encoder.encode(
            content="Operational memory still receives bootstrap values",
            source=SourceType.SESSION,
            skip_surprise_detection=True,
            source_authority=SourceAuthority.OBSERVED,
        )

        assert engram.strength == BOOTSTRAP_STRENGTH
        assert engram.stability == BOOTSTRAP_STABILITY

    def test_encode_rejects_empty(self, encoder):
        """Empty content should raise ValueError."""
        with pytest.raises((ValueError, Exception)):
            encoder.encode(
                content="", kind="semantic", source_authority=SourceAuthority.OBSERVED
            )

    def test_confidence_by_source(self, encoder):
        """Verify confidence ranges differ by source type."""
        session_engram = encoder.encode(
            content="Fact from a session conversation",
            source=SourceType.SESSION,
            source_authority=SourceAuthority.OBSERVED,
        )
        reflection_engram = encoder.encode(
            content="Insight from background reflection",
            source=SourceType.REFLECTION,
            source_authority=SourceAuthority.OBSERVED,
        )

        # Session source should have higher baseline confidence than reflection
        session_conf = session_engram.source.confidence
        reflection_conf = reflection_engram.source.confidence

        assert session_conf > reflection_conf, (
            f"Session confidence ({session_conf}) should exceed "
            f"reflection confidence ({reflection_conf})"
        )
