"""Impacts the server writes itself.

An impact is what a memory changed, in the agent's own words. A few code paths
fill the field with a fixed phrase instead: a correction, a promotion, and the
older capture path that chose a phrase by domain. These phrases fill the column
but are not traces of how understanding changed. They must not become lessons,
or count as a memory having taught one. Newer rows also say so in
``impact_source="template"``. Older rows carry the phrase with no source
recorded, so the phrase itself is the test.
"""

from __future__ import annotations

TEMPLATED_IMPACTS = frozenset({
    "Foundational continuity for future interactions.",
    "Recurring pattern worth carrying across sessions.",
    "Long-arc context that should shape future work.",
    "Current working context for continuity.",
    "Preference to respect in future decisions.",
    "Durable continuity captured from the session.",
    "Stable scoped continuity promoted from hypomnema.",
    "Stable continuity promoted during simple maintenance.",
    "Correction to earlier continuity.",
    "Corrected continuity for future interactions.",
})


def is_templated(impact: str | None, impact_source: str | None = "") -> bool:
    """Whether an impact is one the server wrote rather than the agent."""
    return impact_source == "template" or (impact or "").strip() in TEMPLATED_IMPACTS
