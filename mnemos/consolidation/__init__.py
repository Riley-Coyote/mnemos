"""Consolidation: offline memory processing that runs between sessions.

Consolidation is the "sleeping brain" of Mnemos. It runs a series of passes
that maintain, transform, and enrich the memory store:

1. Decay — recalculate strength/stability/accessibility for all active engrams
2. Softening — LLM-mediated lossy compression of low-resolution memories
3. Belief Review — resolve explicit pending-review belief confidence queues
4. Reflection — gate low-stakes thoughts and refresh graph-derived identity
5. Connection Discovery — find and create new semantic connections

The daemon orchestrates these passes in order, respecting activity gates
and configuration for which passes are enabled.
"""

from .daemon import ConsolidationDaemon as ConsolidationDaemon
