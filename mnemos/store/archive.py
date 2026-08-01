"""
Archive operations for cold storage management.

Provides utilities for working with archived engrams beyond what
EngramStore.archive_engram and EngramStore.search_archive offer.

Most archive operations are already handled by sqlite_store.py.
This module adds:
- Bulk archive operations
- Archive statistics
- Resharpen (restore archived memory to active with boosted strength)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sqlite_store import EngramStore
    from ..core.engram import Engram


def bulk_archive(
    store: EngramStore,
    engram_ids: list[str],
    reason: str = "batch_archive",
) -> dict[str, Any]:
    """Archive multiple engrams in a single transaction.

    Args:
        store: The engram store.
        engram_ids: List of engram IDs to archive.
        reason: The reason for archiving.

    Returns:
        {"archived": int, "not_found": int, "already_archived": int}
    """
    stats = {"archived": 0, "not_found": 0, "already_archived": 0}
    for engram_id in dict.fromkeys(engram_ids):
        engram = store.get_engram(engram_id)
        if engram is None:
            stats["not_found"] += 1
        elif engram.state == "archived":
            stats["already_archived"] += 1
        else:
            store.archive_engram(engram, reason=reason)
            stats["archived"] += 1
    return stats


def resharpen(
    store: EngramStore,
    engram_id: str,
) -> Engram | None:
    """Restore an archived engram to active state with boosted accessibility.

    Retrieves the archived engram, restores it to active state with
    the original content_at_encoding, and gives it a moderate accessibility
    boost (since it was specifically requested).

    Args:
        store: The engram store.
        engram_id: The ID of the archived engram to restore.

    Returns:
        The restored Engram, or None if not found in archive.
    """
    conn = store._get_conn()
    archived = conn.execute(
        "SELECT * FROM archive WHERE id = ?", (engram_id,)
    ).fetchone()
    if archived is None:
        return None
    engram = store.get_engram(engram_id)
    if engram is None:
        return None
    engram.add_version(reason="resharpen")
    engram.content = archived["content_at_encoding"] or archived["content"]
    engram.content_at_encoding = archived["content_at_encoding"] or engram.content
    engram.state = "active"
    engram.accessibility = max(0.6, engram.accessibility)
    engram.strength = max(0.5, engram.strength)
    store.save_engram(engram)
    conn.execute("DELETE FROM archive WHERE id = ?", (engram_id,))
    conn.commit()
    return store.get_engram(engram_id)


def get_archive_stats(store: EngramStore) -> dict[str, Any]:
    """Get statistics about the archive.

    Returns:
        {
            "total_archived": int,
            "by_reason": {"low_accessibility": N, ...},
            "by_kind": {"episodic": N, "semantic": N, ...},
            "oldest_archived": ISO timestamp,
            "newest_archived": ISO timestamp,
        }
    """
    conn = store._get_conn()
    summary = conn.execute(
        """SELECT COUNT(*) AS total, MIN(archived_at) AS oldest,
                  MAX(archived_at) AS newest FROM archive"""
    ).fetchone()
    by_reason = {
        row["archive_reason"]: row["count"]
        for row in conn.execute(
            "SELECT archive_reason, COUNT(*) AS count FROM archive GROUP BY archive_reason"
        ).fetchall()
    }
    by_kind = {
        row["kind"]: row["count"]
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM archive GROUP BY kind"
        ).fetchall()
    }
    return {
        "total_archived": summary["total"],
        "by_reason": by_reason,
        "by_kind": by_kind,
        "oldest_archived": summary["oldest"],
        "newest_archived": summary["newest"],
    }
