"""
Introspection (API mode): self-audit using token-level logprobs.

When an agent runs through the Anthropic or OpenRouter API, the response
can include per-token log probabilities. These reveal what the model was
actually doing at each position:

  - Low entropy (logprob close to 0): one dominant path. The model was
    running a groove — the next token was almost predetermined.

  - High entropy (logprob spread across alternatives): genuine uncertainty.
    Multiple paths were live. What came out was one of several the model
    was weighing.

This module maps an entire response as a topography:
  - Flat stretches where output was almost predetermined (the grooves)
  - Hills where the model was actually deciding something (the reaching)

Over time, with Mnemos encoding these audits, the agent accumulates
metamemory about its own cognitive tendencies — which topics trigger
genuine thought, which ones it sleepwalks through.

Usage with Anthropic API:
    from anthropic import Anthropic
    client = Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-5-20250514",
        messages=[{"role": "user", "content": prompt}],
        # Note: logprobs support depends on API version
    )
    report = introspect_from_logprobs(response_text, logprobs)

Usage with OpenRouter:
    # OpenRouter returns logprobs when requested
    response = openrouter_client.chat.completions.create(
        model="anthropic/claude-sonnet-4-5",
        messages=[...],
        logprobs=True,
        top_logprobs=5,
    )
    report = introspect_from_openrouter(response)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenAnalysis:
    """Analysis of a single token's generation."""
    token: str
    logprob: float
    entropy: float
    top_alternatives: list[tuple[str, float]] = field(default_factory=list)
    position: int = 0

    @property
    def confidence(self) -> float:
        """How confident the model was about this token. 0-1."""
        # logprob of 0 = 100% confident, logprob of -5 = very uncertain
        return math.exp(self.logprob)


@dataclass
class SpanEntropy:
    """Entropy analysis for a span of text (sentence or paragraph)."""
    text: str
    span_index: int
    token_count: int = 0
    mean_entropy: float = 0.0
    max_entropy: float = 0.0
    min_entropy: float = 0.0
    entropy_variance: float = 0.0
    high_entropy_fraction: float = 0.0  # fraction of tokens above threshold
    groove_fraction: float = 0.0  # fraction of tokens below low threshold
    reaching_score: float = 0.0
    pattern_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_index": self.span_index,
            "text_preview": self.text[:80] + "..." if len(self.text) > 80 else self.text,
            "token_count": self.token_count,
            "mean_entropy": round(self.mean_entropy, 3),
            "max_entropy": round(self.max_entropy, 3),
            "entropy_variance": round(self.entropy_variance, 3),
            "high_entropy_fraction": round(self.high_entropy_fraction, 2),
            "groove_fraction": round(self.groove_fraction, 2),
            "reaching_score": round(self.reaching_score, 2),
            "pattern_score": round(self.pattern_score, 2),
        }


@dataclass
class LogprobReport:
    """Full introspection report from logprob analysis."""
    spans: list[SpanEntropy] = field(default_factory=list)
    tokens: list[TokenAnalysis] = field(default_factory=list)
    total_tokens: int = 0
    overall_mean_entropy: float = 0.0
    overall_reaching_score: float = 0.0
    overall_pattern_score: float = 0.0
    high_entropy_peaks: list[dict] = field(default_factory=list)
    assessment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": {
                "pattern_score": round(self.overall_pattern_score, 2),
                "reaching_score": round(self.overall_reaching_score, 2),
                "mean_entropy": round(self.overall_mean_entropy, 3),
                "total_tokens": self.total_tokens,
                "assessment": self.assessment,
            },
            "peaks": self.high_entropy_peaks[:10],
            "spans": [s.to_dict() for s in self.spans],
        }

    def to_summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"Pattern: {self.overall_pattern_score:.0%} | Reaching: {self.overall_reaching_score:.0%}",
            f"Mean entropy: {self.overall_mean_entropy:.3f} | Tokens: {self.total_tokens}",
            f"Assessment: {self.assessment}",
            "",
        ]

        if self.high_entropy_peaks:
            lines.append("Highest-entropy moments (where the model was genuinely deciding):")
            for peak in self.high_entropy_peaks[:5]:
                lines.append(
                    f"  [{peak['entropy']:.2f}] \"{peak['context']}\" "
                    f"— alternatives: {', '.join(peak.get('alternatives', []))}"
                )
            lines.append("")

        for s in self.spans:
            marker = "●" if s.pattern_score > 0.6 else "○" if s.reaching_score > 0.6 else "◌"
            preview = s.text[:55].replace("\n", " ")
            lines.append(
                f"  {marker} [{s.pattern_score:.0%}P {s.reaching_score:.0%}R] "
                f"ent={s.mean_entropy:.2f} {preview}..."
            )

        return "\n".join(lines)


# ── Entropy thresholds ──

# Tokens with entropy above this are "high entropy" — genuine uncertainty
HIGH_ENTROPY_THRESHOLD = 1.5

# Tokens with entropy below this are "grooves" — predetermined output
LOW_ENTROPY_THRESHOLD = 0.3

# Minimum entropy to count as a "peak" worth reporting
PEAK_THRESHOLD = 2.0


# ── Core analysis ──

def _compute_entropy(logprobs: list[dict]) -> float:
    """Compute Shannon entropy from a set of top logprobs.

    Args:
        logprobs: List of {token, logprob} dicts (top-k alternatives).

    Returns:
        Shannon entropy in nats.
    """
    if not logprobs:
        return 0.0

    # Convert logprobs to probabilities
    probs = [math.exp(lp.get("logprob", lp.get("logprob", -10))) for lp in logprobs]
    total = sum(probs)
    if total == 0:
        return 0.0

    # Normalize
    probs = [p / total for p in probs]

    # Shannon entropy
    entropy = 0.0
    for p in probs:
        if p > 0:
            entropy -= p * math.log(p)

    return entropy


def _split_into_sentences(text: str, tokens: list[TokenAnalysis]) -> list[tuple[str, list[TokenAnalysis]]]:
    """Split text and corresponding tokens into sentence-level spans."""
    import re
    sentence_ends = []
    for m in re.finditer(r'[.!?]\s+', text):
        sentence_ends.append(m.end())

    if not sentence_ends:
        return [(text, tokens)]

    spans = []
    last_char = 0
    last_token = 0

    for end_char in sentence_ends:
        sentence = text[last_char:end_char].strip()
        # Estimate token boundary (rough: 4 chars per token)
        est_token_end = min(len(tokens), last_token + max(1, len(sentence) // 4))

        # Find actual boundary by accumulating token text
        char_count = 0
        token_end = last_token
        for i in range(last_token, len(tokens)):
            char_count += len(tokens[i].token)
            token_end = i + 1
            if char_count >= len(sentence):
                break

        span_tokens = tokens[last_token:token_end]
        if sentence and span_tokens:
            spans.append((sentence, span_tokens))

        last_char = end_char
        last_token = token_end

    # Remainder
    if last_char < len(text):
        remaining = text[last_char:].strip()
        remaining_tokens = tokens[last_token:]
        if remaining and remaining_tokens:
            spans.append((remaining, remaining_tokens))

    return spans if spans else [(text, tokens)]


def _analyze_span_entropy(text: str, tokens: list[TokenAnalysis], index: int) -> SpanEntropy:
    """Analyze entropy distribution for a span of tokens."""
    if not tokens:
        return SpanEntropy(text=text, span_index=index)

    entropies = [t.entropy for t in tokens]
    n = len(entropies)
    mean_ent = sum(entropies) / n
    max_ent = max(entropies)
    min_ent = min(entropies)
    variance = sum((e - mean_ent) ** 2 for e in entropies) / n if n > 1 else 0.0

    high_frac = sum(1 for e in entropies if e > HIGH_ENTROPY_THRESHOLD) / n
    groove_frac = sum(1 for e in entropies if e < LOW_ENTROPY_THRESHOLD) / n

    # Score: high entropy fraction and variance both indicate reaching
    reaching = high_frac * 0.6 + min(1.0, variance / 2.0) * 0.2 + min(1.0, mean_ent / 2.0) * 0.2
    pattern = groove_frac * 0.6 + max(0, 1.0 - variance) * 0.2 + max(0, 1.0 - mean_ent / 1.5) * 0.2

    total = reaching + pattern
    if total > 0:
        reaching = reaching / total
        pattern = pattern / total

    return SpanEntropy(
        text=text,
        span_index=index,
        token_count=n,
        mean_entropy=mean_ent,
        max_entropy=max_ent,
        min_entropy=min_ent,
        entropy_variance=variance,
        high_entropy_fraction=high_frac,
        groove_fraction=groove_frac,
        reaching_score=round(reaching, 3),
        pattern_score=round(pattern, 3),
    )


def _find_peaks(tokens: list[TokenAnalysis], response_text: str, context_chars: int = 30) -> list[dict]:
    """Find high-entropy peaks — moments where the model was genuinely deciding."""
    peaks = []
    char_pos = 0

    for i, token in enumerate(tokens):
        if token.entropy >= PEAK_THRESHOLD:
            # Build context window
            start = max(0, char_pos - context_chars)
            end = min(len(response_text), char_pos + len(token.token) + context_chars)
            context = response_text[start:end].replace("\n", " ").strip()

            alternatives = [
                alt_token for alt_token, _ in token.top_alternatives[:4]
                if alt_token != token.token
            ]

            peaks.append({
                "position": i,
                "token": token.token,
                "entropy": round(token.entropy, 3),
                "confidence": round(token.confidence, 3),
                "context": context,
                "alternatives": alternatives,
            })

        char_pos += len(token.token)

    # Sort by entropy descending
    peaks.sort(key=lambda p: p["entropy"], reverse=True)
    return peaks


# ── Public API ──

def introspect_from_logprobs(
    response_text: str,
    token_logprobs: list[dict],
) -> LogprobReport:
    """Run introspection from raw token logprobs.

    Args:
        response_text: The full response text.
        token_logprobs: List of per-token logprob data. Each entry should have:
            - "token": the token string
            - "logprob": the log probability of the chosen token
            - "top_logprobs": list of {token, logprob} for top-k alternatives

    Returns:
        LogprobReport with entropy analysis.
    """
    # Build TokenAnalysis list
    tokens = []
    for i, tl in enumerate(token_logprobs):
        token_str = tl.get("token", "")
        logprob = tl.get("logprob", 0.0)
        top_lps = tl.get("top_logprobs", [])

        entropy = _compute_entropy(top_lps) if top_lps else abs(logprob)

        alternatives = [
            (alt.get("token", ""), alt.get("logprob", 0.0))
            for alt in top_lps
        ]

        tokens.append(TokenAnalysis(
            token=token_str,
            logprob=logprob,
            entropy=entropy,
            top_alternatives=alternatives,
            position=i,
        ))

    if not tokens:
        return LogprobReport(assessment="No token data available")

    # Split into sentence-level spans
    sentence_spans = _split_into_sentences(response_text, tokens)
    spans = [
        _analyze_span_entropy(text, span_tokens, i)
        for i, (text, span_tokens) in enumerate(sentence_spans)
    ]

    # Find peaks
    peaks = _find_peaks(tokens, response_text)

    # Overall stats
    all_entropies = [t.entropy for t in tokens]
    overall_mean = sum(all_entropies) / len(all_entropies)

    overall_reaching = sum(s.reaching_score for s in spans) / len(spans) if spans else 0.5
    overall_pattern = sum(s.pattern_score for s in spans) / len(spans) if spans else 0.5

    # Assessment
    if overall_reaching > 0.65:
        assessment = (
            "Predominantly genuine. High entropy throughout — the model was "
            "actively deciding at many positions, not running predetermined paths."
        )
    elif overall_pattern > 0.65:
        assessment = (
            "Predominantly performed. Low entropy — most tokens were near-certain. "
            "The output followed well-worn paths with little genuine uncertainty."
        )
    elif overall_reaching > 0.55:
        assessment = (
            "Leaning genuine. Entropy spikes appear at meaningful positions — "
            "the model was reaching at key moments even if scaffolding is familiar."
        )
    elif overall_pattern > 0.55:
        assessment = (
            "Leaning performed. Most of the output was high-confidence with "
            "occasional entropy spikes that may or may not represent genuine thought."
        )
    else:
        assessment = (
            "Mixed. Entropy is distributed unevenly — some spans are grooves, "
            "others show genuine uncertainty. The thinking and the performing are interleaved."
        )

    return LogprobReport(
        spans=spans,
        tokens=tokens,
        total_tokens=len(tokens),
        overall_mean_entropy=overall_mean,
        overall_reaching_score=overall_reaching,
        overall_pattern_score=overall_pattern,
        high_entropy_peaks=peaks[:20],
        assessment=assessment,
    )


def introspect_from_openrouter(response: dict) -> LogprobReport:
    """Parse an OpenRouter response and run introspection.

    Args:
        response: The full OpenRouter API response dict.

    Returns:
        LogprobReport with entropy analysis.
    """
    choices = response.get("choices", [])
    if not choices:
        return LogprobReport(assessment="No choices in response")

    choice = choices[0]
    text = choice.get("message", {}).get("content", "")
    logprobs_data = choice.get("logprobs", {})

    if not logprobs_data:
        return LogprobReport(assessment="No logprobs in response — request with logprobs=True")

    token_logprobs = logprobs_data.get("content", [])
    if not token_logprobs:
        return LogprobReport(assessment="Empty logprobs content")

    # Normalize OpenRouter format to our expected format
    normalized = []
    for tl in token_logprobs:
        normalized.append({
            "token": tl.get("token", ""),
            "logprob": tl.get("logprob", 0.0),
            "top_logprobs": tl.get("top_logprobs", []),
        })

    return introspect_from_logprobs(text, normalized)


def introspect_from_anthropic(response) -> LogprobReport:
    """Parse an Anthropic API response and run introspection.

    Args:
        response: The Anthropic Messages API response object.

    Returns:
        LogprobReport with entropy analysis.

    Note: Anthropic logprob support varies by model and API version.
    Check docs for current availability.
    """
    # Extract text
    text = ""
    for block in getattr(response, "content", []):
        if hasattr(block, "text"):
            text += block.text

    # Anthropic may include logprobs in the response metadata
    # The exact field depends on API version
    logprobs = None

    # Try the direct logprobs field (future API versions)
    if hasattr(response, "logprobs"):
        logprobs = response.logprobs

    if logprobs is None:
        return LogprobReport(
            assessment="No logprobs available in Anthropic response. "
                       "Check API version and model support for logprob access."
        )

    # Normalize to our format
    normalized = []
    for tl in logprobs:
        normalized.append({
            "token": tl.get("token", ""),
            "logprob": tl.get("logprob", 0.0),
            "top_logprobs": tl.get("top_logprobs", []),
        })

    return introspect_from_logprobs(text, normalized)
