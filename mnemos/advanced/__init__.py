"""Experimental Mnemos prototypes (unsupported, opt-in).

These modules remain importable for research and compatibility. They are not
production-supported. Unfinished operations raise an explicit error instead
of returning placeholder success.

Modules:
- working_memory: Soft attention gradient working memory (capacity ~7 items)
- schema: Cognitive schemas for structured encoding and retrieval
- attention_gate: Attention-gated encoding (filter what gets encoded)
- schema_matcher: Match incoming content against active schemas
- predictive: Predictive retrieval (pre-fetch likely-needed memories)
- spreading_activation: Activation spreading through connection graph
- interference: Interference modeling (similar memories competing)
- intention: Prospective memory (future-directed intentions with triggers)
- intention_sweep: Periodic check for triggered intentions
- metamemory: Knowing what you know (and what you don't)
- metamemory_update: Update metamemory state after cognitive events
- schema_maintenance: Schema evolution and pruning
- interference_resolution: Resolve interference between competing memories
- observer: External multi-model observer for calibration
- dreaming: Dream-like consolidation for creative connection discovery
"""

# All imports are lazy — modules are only loaded when enabled in config.
# This avoids import-time costs for disabled features.

__all__ = [
    "working_memory",
    "schema",
    "attention_gate",
    "schema_matcher",
    "predictive",
    "spreading_activation",
    "interference",
    "intention",
    "intention_sweep",
    "metamemory",
    "metamemory_update",
    "schema_maintenance",
    "interference_resolution",
    "observer",
    "dreaming",
]
