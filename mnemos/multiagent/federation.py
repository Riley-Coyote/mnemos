"""
Cross-instance federation for memory synchronization.

Enables memory sharing across separate Mnemos instances (e.g., an
agent running on a desktop and the same agent running on a server).
Uses a pull-based sync model with conflict resolution.

Federation is opt-in and only syncs memories with visibility=PUBLIC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


class FederationClient:
    """Client for cross-instance memory federation.

    Usage:
        client = FederationClient(store=store, remote_url="https://...")
        client.sync()
    """

    def __init__(
        self,
        store: EngramStore,
        remote_url: str | None = None,
    ) -> None:
        self._store = store
        self._remote_url = remote_url

    def sync(self) -> dict[str, Any]:
        """Synchronize public memories with a remote instance.

        Returns:
            Sync statistics: {"pushed": N, "pulled": N, "conflicts": N}
        """
        unavailable("cross-instance federation")

    def push(self, engram_ids: list[str]) -> int:
        """Push specific memories to the remote instance.

        Args:
            engram_ids: IDs of engrams to push.

        Returns:
            Number of engrams successfully pushed.
        """
        unavailable("cross-instance federation push")

    def pull(self, since: str | None = None) -> int:
        """Pull new/updated memories from the remote instance.

        Args:
            since: ISO timestamp to pull changes since. If None, pulls all.

        Returns:
            Number of engrams pulled.
        """
        unavailable("cross-instance federation pull")
