"""
Working memory: soft attention gradient with nominal capacity.

Unlike a hard-capped queue, working memory uses a soft attention gradient
where items beyond the nominal capacity (default 7) receive progressively
less attention, rather than being evicted.

Working memory contents influence:
- Encoding context (what was active during encoding)
- Retrieval scoring (WM items get a co-activation bonus)
- Attention gate (WM load affects encoding depth)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..experimental import unavailable


@dataclass
class WorkingMemoryItem:
    """An item in working memory with an attention weight."""

    engram_id: str
    content_preview: str = ""
    attention_weight: float = 1.0
    entered_at: str = ""


class WorkingMemory:
    """Soft-capacity working memory with attention gradient.

    Items beyond nominal capacity receive exponentially decaying
    attention weights rather than being evicted.

    Usage:
        wm = WorkingMemory(nominal_capacity=7)
        wm.attend("engram_123", "some content preview")
        snapshot = wm.snapshot()  # list of engram IDs with attention weights
    """

    def __init__(self, nominal_capacity: int = 7) -> None:
        self._capacity = nominal_capacity
        self._items: list[WorkingMemoryItem] = []

    def attend(self, engram_id: str, content_preview: str = "") -> None:
        """Bring an engram into working memory focus."""
        unavailable("experimental working memory")

    def release(self, engram_id: str) -> None:
        """Release an engram from working memory."""
        unavailable("experimental working memory")

    def snapshot(self) -> list[str]:
        """Get current WM engram IDs for encoding context."""
        unavailable("experimental working memory")

    def get_attention_weights(self) -> dict[str, float]:
        """Get attention weights for all WM items."""
        unavailable("experimental working memory")

    def current_load(self) -> float:
        """Get current WM load as fraction of capacity (can exceed 1.0)."""
        unavailable("experimental working memory")
