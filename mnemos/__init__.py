"""
Mnemos: Living Memory Architecture for Autonomous AI Agents

Memory is not a feature of the agent. Memory IS the agent.

Mnemos provides a cognitive memory layer that sits beneath agent platforms
like OpenClaw, replacing passive note-storage with active, living memory
that encodes at varying depths, forgets naturally, predicts what it'll need,
and changes its memories every time it touches them.

Core features (always active):
- Engrams with dual-trace model (S0 strength/stability/accessibility)
- Typed connections (supports, contradicts, causes, elaborates, etc.)
- Confidence scoring on every memory
- Reconsolidation (retrieval updates access metadata, stability, and links)
- Decay + softening (LLM-mediated lossy compression)
- Emotional state (6 dimensions influencing retrieval)
- Beliefs with confidence tracking and revision history
- Narrative identity generation
- OpenClaw-compatible file export

Advanced modules (opt-in):
- Working memory with soft attention gradient
- Schemas and schema-based encoding
- Attention-gated encoding
- Predictive retrieval
- Interference modeling
- Prospective memory (intentions with triggers)
- Metamemory (knowing what you know)
- External multi-model observer
- Multi-agent federation
"""

__version__ = "0.1.0"

# Public API
from .store.sqlite_store import EngramStore
from .encoding.encoder import Encoder
from .retrieval.reactive import ReactiveRetriever
from .consolidation.daemon import ConsolidationDaemon
from .bridge import MnemosBridge
from .config.loader import load_config, save_config

__all__ = [
    "EngramStore",
    "Encoder",
    "ReactiveRetriever",
    "ConsolidationDaemon",
    "MnemosBridge",
    "load_config",
    "save_config",
]
