"""Interface layer: the packet an agent wakes with, and what it exports.

- context_packet: the briefing a session starts from
  (``build_context_packet``, ``format_context_packet``)
- visual_snapshot: an inline Mermaid picture of a scope's memory
- openclaw_export: the OpenClaw workspace files (MEMORY.md and the rest)
"""

from .context_packet import build_context_packet, format_context_packet
from .visual_snapshot import build_memory_visual_snapshot
