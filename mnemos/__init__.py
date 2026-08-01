"""
Mnemos: Living Memory Architecture for Autonomous AI Agents

Memory is not a feature of the agent. Memory IS the agent.

Mnemos provides a cognitive memory layer that sits beneath agent platforms
like OpenClaw, replacing passive note-storage with active, living memory
that encodes at varying depths, forgets naturally, predicts what it'll need,
and changes its memories every time it touches them.

Core features (always active):
- Engrams with dual-trace model (strength/stability/accessibility)
- Typed connections (supports, contradicts, causes, elaborates, etc.)
- Confidence scoring on every memory
- Reconsolidation (every retrieval updates the memory)
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

# Read from installed package metadata so there is exactly one source of
# truth. Hardcoding it here meant this said 0.1.0 while the distribution
# said 0.2.0, and nothing failed — a released package can misreport its own
# version to every user and to every bug report, and PyPI versions cannot be
# replaced once uploaded. The fallback covers running from a source tree
# that was never installed.
try:  # pragma: no cover - trivial branch
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    __version__ = _pkg_version("mnemos-continuity")
except (ImportError, PackageNotFoundError):  # pragma: no cover
    __version__ = "0.0.0+unknown"

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
