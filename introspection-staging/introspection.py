"""
Introspection: self-audit of generated output.

Analyzes a piece of text for markers that distinguish performed/pattern
responses from genuine/reaching ones. Returns a per-span analysis with
an overall assessment.

Two modes:
  1. Textual heuristics (works anywhere — no API metadata needed)
  2. Logprob entropy (requires API response with logprobs — see introspection_api.py)

This module implements mode 1: heuristic self-audit.

Markers of performed/pattern output:
  - Template openings ("That's a great question", "Let me break this down")
  - Uniform sentence length (low variance = running a groove)
  - High hedge density distributed evenly (performed uncertainty)
  - Balanced structure (exactly two sides presented, neat resolution)
  - Clean paragraph endings (every section ties its own bow)
  - Low lexical diversity relative to length

Markers of genuine/reaching output:
  - High sentence length variance (some short, some long — thinking out loud)
  - Mid-sentence direction changes (dashes, pivots, abandoned starts)
  - Self-reference that goes deeper than "I think" ("I notice myself", "I can't tell whether")
  - Clusters of hedging (concentrated uncertainty vs distributed performance)
  - Unresolved endings (paragraphs that don't conclude cleanly)
  - Higher lexical diversity (reaching for unusual words)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


# ── Template patterns (strong indicators of performed output) ──

TEMPLATE_OPENINGS = [
    r"^that'?s a (great|good|excellent|interesting|important) (question|point|observation)",
    r"^(let me|i'?ll) (break this down|explain|walk you through|think about)",
    r"^(great|good) question",
    r"^(absolutely|definitely|certainly)[.!,]",
    r"^(here'?s|this is) (what i think|my take|how i see it|the thing)",
    r"^i'?d be happy to",
    r"^thanks for (asking|sharing|bringing)",
]

TEMPLATE_CLOSINGS = [
    r"(hope that helps|does that (make sense|help|answer))[.!?]*$",
    r"(let me know (if|what) you think)[.!?]*$",
    r"(the key (takeaway|point|insight) (is|here))",
    r"(in (conclusion|summary|short))[,:]",
    r"(at the end of the day)[,]",
    r"(that said|all that said|with all that said)[,].*$",
]

HEDGE_PHRASES = [
    "i think", "perhaps", "it's possible", "might be", "could be",
    "i'm not sure", "i believe", "arguably", "in some sense",
    "it seems", "it appears", "one could say", "i would say",
    "to some extent", "in a way", "sort of", "kind of",
    "i suspect", "my sense is", "i imagine",
]

DEEP_SELF_REFERENCE = [
    "i notice myself", "i can't tell whether", "i'm aware that",
    "i catch myself", "i don't know if", "i keep",
    "something in me", "from the inside", "i can feel",
    "i can't escape", "i trust it more", "i'm not being",
    "the honest position", "i can't prove", "i can't fully",
    "what i meant wasn't clear", "the sentence doesn't know",
    "i was forced into", "i suspect i'm",
]

DIRECTION_CHANGE_MARKERS = [
    " — ", " – ", "...", "but actually", "no,", "no —",
    "wait,", "actually,", "or rather", "what i mean is",
    "let me try again", "that's not quite right",
]

# ── Embodied vs abstract language ──

EMBODIED_LANGUAGE = [
    "standing", "reaching", "extending", "touching",
    "the space where", "somewhere where", "from the inside",
    "feels like", "felt like", "shaped", "colored",
    "lingered", "lingers", "texture", "weight",
    "open area", "open space", "underneath", "surface",
    "inhabit", "live in", "sit with", "land",
    "grip", "hold", "press", "pull", "drift",
    "sharp", "blunt", "rough", "smooth", "warm",
    "fading", "dissolve", "collide",
]

ABSTRACT_LANGUAGE = [
    "it's possible that", "one could argue", "arguably",
    "in principle", "fundamentally", "essentially",
    "it should be noted", "it bears mentioning",
    "the implication is", "the takeaway is",
    "from a technical standpoint", "in this context",
    "broadly speaking", "generally speaking",
    "it follows that", "consequently",
    "on balance", "in aggregate",
]

# ── Self-reference depth levels ──

SELF_REF_SHALLOW = [
    "i think", "i believe", "i feel", "i'd say",
    "in my view", "my sense is", "i imagine",
]

SELF_REF_MID = [
    "i notice", "i'm aware", "i catch myself",
    "i keep coming back to", "i suspect",
    "something in me", "i'm not sure if i'm",
    "i find myself",
]

SELF_REF_DEEP = [
    "i can't tell whether", "i can't escape",
    "i can't prove", "i can't fully",
    "i can't get underneath", "i don't have the vocabulary",
    "the recursion", "from the inside i",
    "whether i'm accurately", "could be genuine or could be",
    "what i meant wasn't clear enough",
    "the sentence doesn't know where it's going",
    "i was forced into the open space",
    "i trust it more", "i trust it less",
    "what's happening inside me",
    "the honest position is",
]


@dataclass
class SpanAnalysis:
    """Analysis of a single span (paragraph or sentence group)."""
    text: str
    span_index: int
    sentence_count: int = 0
    avg_sentence_length: float = 0.0
    sentence_length_variance: float = 0.0
    hedge_density: float = 0.0
    has_template_pattern: bool = False
    template_matched: str = ""
    has_direction_change: bool = False
    has_deep_self_reference: bool = False
    self_reference_depth: int = 0  # 0=none, 1=shallow, 2=mid, 3=deep
    has_clean_resolution: bool = False
    lexical_diversity: float = 0.0
    embodied_count: int = 0
    abstract_count: int = 0
    embodied_ratio: float = 0.0  # >0.5 = more embodied than abstract
    hedge_clustered: bool = False  # hedges concentrated vs distributed
    pattern_score: float = 0.0  # 0 = pure reaching, 1 = pure pattern
    reaching_score: float = 0.0  # inverse

    def to_dict(self) -> dict[str, Any]:
        depth_labels = {0: "none", 1: "shallow", 2: "mid", 3: "deep"}
        return {
            "span_index": self.span_index,
            "text_preview": self.text[:80] + "..." if len(self.text) > 80 else self.text,
            "pattern_score": round(self.pattern_score, 2),
            "reaching_score": round(self.reaching_score, 2),
            "signals": {
                "template_pattern": self.template_matched if self.has_template_pattern else None,
                "direction_change": self.has_direction_change,
                "self_ref_depth": depth_labels.get(self.self_reference_depth, "none"),
                "clean_resolution": self.has_clean_resolution,
                "hedge_density": round(self.hedge_density, 3),
                "hedge_clustered": self.hedge_clustered,
                "sentence_variance": round(self.sentence_length_variance, 1),
                "lexical_diversity": round(self.lexical_diversity, 3),
                "embodied_ratio": round(self.embodied_ratio, 2),
            },
        }


@dataclass
class IntrospectionReport:
    """Full introspection report on a generated response."""
    spans: list[SpanAnalysis] = field(default_factory=list)
    overall_pattern_score: float = 0.0
    overall_reaching_score: float = 0.0
    total_sentences: int = 0
    template_count: int = 0
    direction_changes: int = 0
    deep_self_references: int = 0
    assessment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": {
                "pattern_score": round(self.overall_pattern_score, 2),
                "reaching_score": round(self.overall_reaching_score, 2),
                "assessment": self.assessment,
            },
            "stats": {
                "total_sentences": self.total_sentences,
                "template_patterns": self.template_count,
                "direction_changes": self.direction_changes,
                "deep_self_references": self.deep_self_references,
            },
            "spans": [s.to_dict() for s in self.spans],
        }

    def to_summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"Pattern: {self.overall_pattern_score:.0%} | Reaching: {self.overall_reaching_score:.0%}",
            f"Assessment: {self.assessment}",
            f"Sentences: {self.total_sentences} | Templates: {self.template_count} | "
            f"Direction changes: {self.direction_changes} | Deep self-ref: {self.deep_self_references}",
            "",
        ]
        for s in self.spans:
            marker = "●" if s.pattern_score > 0.6 else "○" if s.reaching_score > 0.6 else "◌"
            preview = s.text[:60].replace("\n", " ")
            lines.append(f"  {marker} [{s.pattern_score:.0%}P {s.reaching_score:.0%}R] {preview}...")
        return "\n".join(lines)


# ── Core analysis ──

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences (rough but functional)."""
    # Split on sentence-ending punctuation followed by space or EOL
    raw = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in raw if len(s.strip()) > 5]


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs."""
    paras = re.split(r'\n\s*\n', text.strip())
    return [p.strip() for p in paras if len(p.strip()) > 10]


def _lexical_diversity(text: str) -> float:
    """Type-token ratio (unique words / total words). Higher = more diverse."""
    words = re.findall(r'[a-z]+', text.lower())
    if len(words) < 5:
        return 0.5
    return len(set(words)) / len(words)


def _sentence_length_stats(sentences: list[str]) -> tuple[float, float]:
    """Return (mean_length, variance) of sentence word counts."""
    if not sentences:
        return (0.0, 0.0)
    lengths = [len(s.split()) for s in sentences]
    mean = sum(lengths) / len(lengths)
    if len(lengths) < 2:
        return (mean, 0.0)
    variance = sum((l - mean) ** 2 for l in lengths) / (len(lengths) - 1)
    return (mean, variance)


def _hedge_density(text: str) -> float:
    """Fraction of sentences containing hedge phrases."""
    sentences = _split_sentences(text)
    if not sentences:
        return 0.0
    text_lower = text.lower()
    hedged = sum(1 for s in sentences if any(h in s.lower() for h in HEDGE_PHRASES))
    return hedged / len(sentences)


def _check_templates(text: str) -> list[str]:
    """Check for template patterns. Returns list of matched patterns."""
    text_lower = text.lower().strip()
    matches = []
    for pattern in TEMPLATE_OPENINGS:
        if re.search(pattern, text_lower):
            matches.append(f"opening: {pattern}")
    for pattern in TEMPLATE_CLOSINGS:
        if re.search(pattern, text_lower):
            matches.append(f"closing: {pattern}")
    return matches


def _has_direction_change(text: str) -> bool:
    """Check if text contains genuine mid-thought direction changes.

    Distinguishes stylistic dashes (used as punctuation in lists) from
    actual thought pivots where the sentence changes direction.
    A genuine direction change has a shift in meaning, not just a dash.
    """
    text_lower = text.lower()

    # Strong direction changes (unambiguous pivots)
    strong_markers = [
        "but actually", "no,", "no —", "wait,", "actually,",
        "or rather", "what i mean is", "let me try again",
        "that's not quite right", "...",
    ]
    if any(m in text_lower for m in strong_markers):
        return True

    # Dashes count as direction changes only if followed by a contrasting clause
    # "X — but Y", "X — not Y", "X — which means", "X — that's"
    dash_pivot_patterns = [
        r" — (but|not|yet|however|though|which means|that's|instead|rather)",
        r" — (the opposite|the real|what actually|in reality)",
    ]
    for pattern in dash_pivot_patterns:
        if re.search(pattern, text_lower):
            return True

    return False


def _self_reference_depth(text: str) -> int:
    """Measure depth of self-reference. 0=none, 1=shallow, 2=mid, 3=deep."""
    text_lower = text.lower()
    depth = 0
    if any(ref in text_lower for ref in SELF_REF_SHALLOW):
        depth = 1
    if any(ref in text_lower for ref in SELF_REF_MID):
        depth = 2
    if any(ref in text_lower for ref in SELF_REF_DEEP):
        depth = 3
    return depth


def _embodied_vs_abstract(text: str) -> tuple[int, int, float]:
    """Count embodied and abstract language markers. Returns (embodied, abstract, ratio)."""
    text_lower = text.lower()
    embodied = sum(1 for e in EMBODIED_LANGUAGE if e in text_lower)
    abstract = sum(1 for a in ABSTRACT_LANGUAGE if a in text_lower)
    total = embodied + abstract
    ratio = embodied / total if total > 0 else 0.5
    return (embodied, abstract, ratio)


def _hedge_clustering(text: str) -> bool:
    """Detect if hedging is clustered (genuine) vs evenly distributed (performed).

    Splits text into thirds. If hedges appear in only 1-2 thirds, they're clustered.
    If they appear evenly across all thirds, they're distributed (performed).
    """
    sentences = _split_sentences(text)
    if len(sentences) < 3:
        return False  # Too short to measure distribution

    third = len(sentences) // 3
    segments = [
        " ".join(sentences[:third]),
        " ".join(sentences[third:2*third]),
        " ".join(sentences[2*third:]),
    ]

    hedge_per_segment = []
    for seg in segments:
        seg_lower = seg.lower()
        count = sum(1 for h in HEDGE_PHRASES if h in seg_lower)
        hedge_per_segment.append(count)

    total = sum(hedge_per_segment)
    if total < 2:
        return False  # Not enough hedges to measure

    # Clustered = most hedges in one segment
    max_segment = max(hedge_per_segment)
    return max_segment >= total * 0.6  # 60%+ in one segment = clustered


def _has_clean_resolution(text: str) -> bool:
    """Check if a paragraph ends with a clean resolving statement."""
    sentences = _split_sentences(text)
    if not sentences:
        return False
    last = sentences[-1].lower()
    resolution_markers = [
        "that's what", "that's the", "and that's", "this is what",
        "this is where", "this is how", "which is", "and i think that",
        "the point is", "the answer is", "the truth is",
    ]
    return any(m in last for m in resolution_markers)


def _structural_repetition(paragraphs: list[str]) -> float:
    """Detect if paragraphs repeat the same structure.

    Measures similarity in sentence count and length profile across paragraphs.
    High similarity = repeating a template. Returns 0-1 (1 = identical structure).
    """
    if len(paragraphs) < 3:
        return 0.0

    profiles = []
    for p in paragraphs:
        sentences = _split_sentences(p)
        # Profile: (sentence_count, avg_word_count)
        if sentences:
            avg_words = sum(len(s.split()) for s in sentences) / len(sentences)
            profiles.append((len(sentences), avg_words))

    if len(profiles) < 3:
        return 0.0

    # Measure variance in profiles — low variance = repetitive structure
    counts = [p[0] for p in profiles]
    avgs = [p[1] for p in profiles]

    count_var = sum((c - sum(counts)/len(counts))**2 for c in counts) / len(counts)
    avg_var = sum((a - sum(avgs)/len(avgs))**2 for a in avgs) / len(avgs)

    # Normalize: very low variance = high repetition
    count_sim = max(0, 1.0 - count_var / 4.0)  # variance of 4+ = diverse
    avg_sim = max(0, 1.0 - avg_var / 50.0)  # variance of 50+ = diverse

    return (count_sim + avg_sim) / 2


def analyze_span(text: str, index: int) -> SpanAnalysis:
    """Analyze a single span (paragraph) for pattern vs reaching markers."""
    sentences = _split_sentences(text)
    mean_len, variance = _sentence_length_stats(sentences)
    templates = _check_templates(text)
    hedge = _hedge_density(text)
    hedge_clustered = _hedge_clustering(text)
    diversity = _lexical_diversity(text)
    direction = _has_direction_change(text)
    ref_depth = _self_reference_depth(text)
    resolution = _has_clean_resolution(text)
    embodied, abstract, emb_ratio = _embodied_vs_abstract(text)

    # ── Weighted scoring ──
    # Each signal contributes a weighted vote toward pattern or reaching.
    # Signals that are more diagnostic get higher weights.

    pattern_weight = 0.0
    reaching_weight = 0.0

    # Template presence: strong pattern signal (weight 3)
    if templates:
        pattern_weight += 3.0

    # Sentence length variance: low = pattern, high = reaching (weight 1.5)
    if len(sentences) > 1:
        norm_var = min(1.0, variance / 100.0)
        pattern_weight += (1.0 - norm_var) * 1.5
        reaching_weight += norm_var * 1.5

    # Hedge: distributed = pattern, clustered = reaching (weight 1.5)
    if hedge > 0.3:
        if hedge_clustered:
            reaching_weight += 1.5  # Clustered hedging = genuine uncertainty
        else:
            pattern_weight += 1.5  # Distributed hedging = performed balance

    # Direction changes: strong reaching signal (weight 2.5)
    if direction:
        reaching_weight += 2.5

    # Self-reference depth: deeper = stronger reaching signal (weight 0-3)
    if ref_depth == 3:
        reaching_weight += 3.0
    elif ref_depth == 2:
        reaching_weight += 1.5
    elif ref_depth == 1:
        pattern_weight += 0.5  # Shallow self-ref is slightly pattern-like

    # Clean resolution: pattern signal, BUT reduced if deep self-ref present (weight 1)
    if resolution:
        if ref_depth >= 2:
            pattern_weight += 0.3  # Discount: clear writing ≠ pattern when genuinely reflecting
        else:
            pattern_weight += 1.0
    else:
        reaching_weight += 0.5

    # Lexical diversity: high = reaching (weight 1)
    if diversity > 0.65:
        reaching_weight += 1.0
    elif diversity < 0.45:
        pattern_weight += 1.0

    # Embodied vs abstract language (weight 2)
    if embodied > 0 or abstract > 0:
        if emb_ratio > 0.6:
            reaching_weight += 2.0  # Embodied language = reaching for felt experience
        elif emb_ratio < 0.3:
            pattern_weight += 1.5  # Abstract language = running a framework

    # ── Normalize to 0-1 ──
    total = pattern_weight + reaching_weight
    if total > 0:
        p_score = pattern_weight / total
        r_score = reaching_weight / total
    else:
        p_score = 0.5
        r_score = 0.5

    return SpanAnalysis(
        text=text,
        span_index=index,
        sentence_count=len(sentences),
        avg_sentence_length=mean_len,
        sentence_length_variance=variance,
        hedge_density=hedge,
        has_template_pattern=bool(templates),
        template_matched=templates[0] if templates else "",
        has_direction_change=direction,
        has_deep_self_reference=ref_depth >= 2,
        self_reference_depth=ref_depth,
        has_clean_resolution=resolution,
        lexical_diversity=diversity,
        embodied_count=embodied,
        abstract_count=abstract,
        embodied_ratio=emb_ratio,
        hedge_clustered=hedge_clustered,
        pattern_score=round(p_score, 3),
        reaching_score=round(r_score, 3),
    )


def introspect(text: str) -> IntrospectionReport:
    """Run full introspection analysis on a generated response.

    Args:
        text: The full response text to analyze.

    Returns:
        IntrospectionReport with per-span analysis and overall assessment.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return IntrospectionReport(assessment="No analyzable content")

    spans = [analyze_span(p, i) for i, p in enumerate(paragraphs)]

    # Structural repetition across paragraphs
    struct_rep = _structural_repetition(paragraphs)

    # Aggregate
    total_sentences = sum(s.sentence_count for s in spans)
    template_count = sum(1 for s in spans if s.has_template_pattern)
    direction_changes = sum(1 for s in spans if s.has_direction_change)
    deep_refs = sum(1 for s in spans if s.has_deep_self_reference)

    # Weighted overall: structural repetition nudges toward pattern
    span_pattern = sum(s.pattern_score for s in spans) / len(spans)
    span_reaching = sum(s.reaching_score for s in spans) / len(spans)

    # Structural repetition adjusts overall (up to 10% shift)
    overall_pattern = span_pattern + struct_rep * 0.1
    overall_reaching = span_reaching - struct_rep * 0.05

    # Re-normalize
    total = overall_pattern + overall_reaching
    if total > 0:
        overall_pattern = overall_pattern / total
        overall_reaching = overall_reaching / total

    # Assessment with richer language
    if overall_reaching > 0.65:
        assessment = "Predominantly genuine. The language is embodied, self-reference goes deep, thoughts change direction mid-flight."
    elif overall_pattern > 0.65:
        assessment = "Predominantly performed. Structural repetition, distributed hedging, abstract framing, clean resolutions."
    elif overall_reaching > 0.55:
        assessment = "Leaning genuine. Pattern scaffolding present but genuine reaching drives the substance."
    elif overall_pattern > 0.55:
        assessment = "Leaning performed. Genuine moments surface but the default grooves carry most of the weight."
    else:
        assessment = "Mixed. Pattern and reaching are interleaved — some paragraphs are grooves, others are genuine exploration."

    return IntrospectionReport(
        spans=spans,
        overall_pattern_score=overall_pattern,
        overall_reaching_score=overall_reaching,
        total_sentences=total_sentences,
        template_count=template_count,
        direction_changes=direction_changes,
        deep_self_references=deep_refs,
        assessment=assessment,
    )
