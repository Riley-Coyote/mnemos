"""Shared enums and constants for Mnemos."""

from enum import Enum


class EngramKind(str, Enum):
    """Classification of memory type."""
    EPISODIC = "episodic"          # Specific experiences, events
    SEMANTIC = "semantic"          # Facts, knowledge, understanding
    PROCEDURAL = "procedural"     # How-to knowledge, skills, patterns
    PROSPECTIVE = "prospective"   # Future-directed (linked to Intentions)


class ConnectionRelation(str, Enum):
    """Typed semantic edges between engrams.

    Core taxonomy (7 types used by LLM classifier):
      SUPPORTS, CONTRADICTS, CAUSES, EXTENDS, PARALLELS, SYNTHESIZES, GROUNDS

    Legacy types (kept for backward compatibility with existing connections):
      ELABORATES, TEMPORAL_BEFORE, TEMPORAL_AFTER, PART_OF, INSTANCE_OF,
      ANALOGOUS_TO, INTERFERES_WITH, DISTILLED_INTO
    """
    # --- Core taxonomy (LLM-classified) ---
    SUPPORTS = "supports"           # Independently reinforces same conclusion
    CONTRADICTS = "contradicts"     # Genuine evidence against
    CAUSES = "causes"               # Temporal/causal chain
    EXTENDS = "extends"             # Adds new analysis, goes further
    PARALLELS = "parallels"         # Same pattern, different instances
    SYNTHESIZES = "synthesizes"     # Combines multiple sources into unified picture
    GROUNDS = "grounds"             # Provides foundational context giving meaning

    # --- Structural (mechanism-formed, not semantically classified) ---
    CO_ACTIVATED = "co_activated"   # Retrieved together — correlation, not evidence.
    # Created by retrieval reconsolidation; the connection discovery pass
    # may later upgrade it to a semantic relation (or remove it). Writing
    # these as SUPPORTS would re-seed the relation-type monoculture the
    # discovery pass was built to fix.

    # --- Legacy types (backward compatibility) ---
    ELABORATES = "elaborates"
    TEMPORAL_BEFORE = "temporal_before"
    TEMPORAL_AFTER = "temporal_after"
    PART_OF = "part_of"
    INSTANCE_OF = "instance_of"
    ANALOGOUS_TO = "analogous_to"
    INTERFERES_WITH = "interferes_with"
    DISTILLED_INTO = "distilled_into"


class EngramState(str, Enum):
    """Lifecycle state of an engram."""
    ACTIVE = "active"
    CONSOLIDATING = "consolidating"
    DORMANT = "dormant"
    ARCHIVED = "archived"


class ConfidenceSource(str, Enum):
    """How confident we are in a memory, and why."""
    USER_EXPLICIT = "user_explicit"    # 0.95-1.0: Direct, unambiguous statement
    USER_IMPLIED = "user_implied"      # 0.70-0.94: Strong inference from behavior
    MODEL_INFERRED = "model_inferred"  # 0.40-0.69: Pattern recognition
    SPECULATIVE = "speculative"        # 0.00-0.39: Tentative, needs verification


class EncodingDepth(str, Enum):
    """How deeply an engram was encoded."""
    SHALLOW = "shallow"        # Minimal metadata, no connections
    MODERATE = "moderate"      # Full metadata, basic connections
    DEEP = "deep"              # Rich connections, schema update
    ELABORATIVE = "elaborative"  # Above + questions + belief check


class Visibility(str, Enum):
    """Multi-agent visibility scope."""
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


class SourceType(str, Enum):
    """How a memory entered the system."""
    SESSION = "session"          # From user interaction
    BACKGROUND = "background"    # From autonomous thinking
    DREAM = "dream"              # From dream consolidation
    MERGE = "merge"              # From parallel session merge
    OBSERVER = "observer"        # From external observer
    BOOTSTRAP = "bootstrap"      # From initialization/migration
    REFLECTION = "reflection"    # From consolidation reflection pass
    BROWSER_EXTRACTION = "browser_extraction"  # From Sovereign Mind browser extension
    EXTERNAL = "external"                        # From external ingestion pipeline


# Constants
DEFAULT_STRENGTH = 0.5
DEFAULT_STABILITY = 0.1
DEFAULT_ACCESSIBILITY = 0.5
BOOTSTRAP_STRENGTH = 0.7
BOOTSTRAP_STABILITY = 0.3
CONFIDENCE_THRESHOLD = 0.3  # Below this, don't act on memory

# Belief tier boundaries (descending order)
# Crossing these thresholds triggers events in the substrate layer.
BELIEF_TIERS = (0.7, 0.5, 0.3)


class BeliefChangeKind(str, Enum):
    """Result of classifying a belief confidence change against tier boundaries."""
    CONFIRMED = "confirmed"        # Upward crossing — belief strengthened past a tier
    CONTRADICTED = "contradicted"  # Downward crossing — belief eroded past a tier
    NO_CROSSING = "no_crossing"    # Change stayed within the same tier band


def classify_belief_change(
    previous_confidence: float,
    current_confidence: float,
    tiers: tuple[float, ...] = BELIEF_TIERS,
) -> BeliefChangeKind:
    """Classify a belief confidence change against tier boundaries.

    Only tier crossings produce CONFIRMED or CONTRADICTED — small changes
    within a tier band return NO_CROSSING. This prevents cascade loops
    where every minor revision triggers a reflection.

    The critical semantic distinction:
      - Downward crossing (previous >= tier > current) -> CONTRADICTED
        Genuine erosion. Should trigger reflection.
      - Upward crossing (previous < tier <= current) -> CONFIRMED
        Belief strengthening. Should NOT trigger challenge/reflection.

    Bug this fixes: previously, both directions fired CONTRADICTED,
    creating a death spiral where strengthening a belief triggered
    reflection that weakened it.

    Args:
        previous_confidence: Belief confidence at last check.
        current_confidence: Belief confidence now.
        tiers: Tier boundary thresholds (descending).

    Returns:
        BeliefChangeKind indicating what happened.
    """
    for tier in tiers:
        if previous_confidence >= tier > current_confidence:
            return BeliefChangeKind.CONTRADICTED
        if previous_confidence < tier <= current_confidence:
            return BeliefChangeKind.CONFIRMED
    return BeliefChangeKind.NO_CROSSING


# Canonical default agent scope. Library code must never default to a
# specific agent's name — scope leaks between agents are identity
# contamination, not just bugs.
DEFAULT_AGENT_ID = "default"


# Tags that Mnemos stamps on memories itself — classifier buckets, domain and
# kind labels, indexer bookkeeping, consolidation markers. They are how the
# pipeline files a memory, not anything the agent or human meant by it.
#
# This set exists because identity ("what you keep returning to") was being
# computed by counting tags, and these are on nearly everything by
# construction — `continuity` is stamped on every capture, `trace-type:*` on
# every indexed transcript line. The result read back "Persistent concerns:
# session-indexed, trace-type:fact, decision", which is a histogram of the
# classifier, not a self. Anything OUTSIDE this vocabulary came from a human or
# from content (a project name, a topic) and is a truer signal of a concern.
STRUCTURAL_TAGS: frozenset[str] = frozenset(
    {
        # _simple_tags classifier labels (mnemos/simple_runtime.py)
        "continuity", "preference", "decision", "project", "identity", "correction",
        # hypomnema domains
        "foundational", "recurring", "long-arc", "topical", "situational",
        # engram kinds
        "episodic", "semantic", "procedural", "prospective",
        # consolidation / reflection markers
        "lesson", "distilled", "reflection", "synthesized",
        # session indexer bookkeeping
        "session-indexed",
    }
)

#: Prefixes marking a tag as pipeline-generated regardless of its suffix.
_STRUCTURAL_TAG_PREFIXES: tuple[str, ...] = ("trace-type:",)


def is_structural_tag(tag: str) -> bool:
    """Whether a tag is Mnemos bookkeeping rather than a meaningful concern.

    Used wherever tags are surfaced as though they described the agent (most
    importantly the identity profile). Kept in one place so a new pipeline tag
    is excluded everywhere at once, instead of leaking back into "who you are"
    the way ``trace-type:fact`` did.
    """
    t = tag.strip().lower()
    if t in STRUCTURAL_TAGS:
        return True
    return any(t.startswith(prefix) for prefix in _STRUCTURAL_TAG_PREFIXES)
