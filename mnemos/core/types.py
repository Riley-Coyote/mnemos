"""Shared enums and constants for Mnemos."""

from enum import Enum


class EngramKind(str, Enum):
    """Classification of memory type."""

    EPISODIC = "episodic"  # Specific experiences, events
    SEMANTIC = "semantic"  # Facts, knowledge, understanding
    PROCEDURAL = "procedural"  # How-to knowledge, skills, patterns
    PROSPECTIVE = "prospective"  # Future-directed (linked to Intentions)


class ConnectionRelation(str, Enum):
    """Typed semantic edges between engrams.

    Core taxonomy (7 types used by LLM classifier):
      SUPPORTS, CONTRADICTS, CAUSES, EXTENDS, PARALLELS, SYNTHESIZES, GROUNDS

    Legacy types (kept for backward compatibility with existing connections):
      ELABORATES, TEMPORAL_BEFORE, TEMPORAL_AFTER, PART_OF, INSTANCE_OF,
      ANALOGOUS_TO, INTERFERES_WITH, DISTILLED_INTO
    """

    # --- Core taxonomy (LLM-classified) ---
    SUPPORTS = "supports"  # Independently reinforces same conclusion
    CONTRADICTS = "contradicts"  # Genuine evidence against
    CAUSES = "causes"  # Temporal/causal chain
    EXTENDS = "extends"  # Adds new analysis, goes further
    PARALLELS = "parallels"  # Same pattern, different instances
    SYNTHESIZES = "synthesizes"  # Combines multiple sources into unified picture
    GROUNDS = "grounds"  # Provides foundational context giving meaning

    # --- Structural (mechanism-formed, not semantically classified) ---
    CO_ACTIVATED = "co_activated"  # Retrieved together — correlation, not evidence.
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

    USER_EXPLICIT = "user_explicit"  # 0.95-1.0: Direct, unambiguous statement
    USER_IMPLIED = "user_implied"  # 0.70-0.94: Strong inference from behavior
    MODEL_INFERRED = "model_inferred"  # 0.40-0.69: Pattern recognition
    SPECULATIVE = "speculative"  # 0.00-0.39: Tentative, needs verification


class EncodingDepth(str, Enum):
    """How deeply an engram was encoded."""

    SHALLOW = "shallow"  # Minimal metadata, no connections
    MODERATE = "moderate"  # Full metadata, basic connections
    DEEP = "deep"  # Rich connections, schema update
    ELABORATIVE = "elaborative"  # Above + questions + belief check


class Visibility(str, Enum):
    """Multi-agent visibility scope."""

    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


class SourceType(str, Enum):
    """How a memory entered the system."""

    SESSION = "session"  # From user interaction
    BACKGROUND = "background"  # From autonomous thinking
    DREAM = "dream"  # From dream consolidation
    MERGE = "merge"  # From parallel session merge
    OBSERVER = "observer"  # From external observer
    BOOTSTRAP = "bootstrap"  # From initialization/migration
    REFLECTION = "reflection"  # From consolidation reflection pass
    BROWSER_EXTRACTION = "browser_extraction"  # From Sovereign Mind browser extension
    EXTERNAL = "external"  # From external ingestion pipeline


class SourceAuthority(str, Enum):
    """Harness-stamped authority for a durable-affecting write (RFC-R1).

    Authority is derived from the *channel* a write arrives on — which tool,
    which surface, which importer — never from caller parameters or payload
    content. A payload or caller claiming a higher authority is evidence of
    risk, not authority (R1). ``user_stated`` is never mintable by any ingest
    channel; it is reserved for David's reviewed-decision path (U4).
    """

    USER_STATED = "user_stated"  # David asserted it through a reviewed surface
    IMPORTED = "imported"  # Curated-history importer (DAVID-7)
    OBSERVED = "observed"  # Agent/runtime observed it (model-reachable channels)
    GENERATED = "generated"  # Autonomous producer synthesized it


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

    CONFIRMED = "confirmed"  # Upward crossing — belief strengthened past a tier
    CONTRADICTED = "contradicted"  # Downward crossing — belief eroded past a tier
    NO_CROSSING = "no_crossing"  # Change stayed within the same tier band


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
