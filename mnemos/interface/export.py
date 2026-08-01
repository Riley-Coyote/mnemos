"""
Portable memory export and import.

Provides functions to export an agent's entire memory to a portable JSON
format and import it back. Compatible with OpenClaw's file-based memory
format for interoperability.

Export format is a single JSON file containing:
- Agent identity (kernel, profile, epoch state)
- All engrams with connections and version history
- All beliefs with revision history
- Emotional state history
- Metadata (export timestamp, version, engram count)

The format is designed to be:
- Human-readable (pretty-printed JSON)
- Self-contained (no external references)
- Version-tagged (for forward compatibility)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..experimental import unavailable

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore


def export_memory(
    store: EngramStore,
    agent_id: str,
    output_path: str | Path,
) -> None:
    """Export an agent's complete memory to a portable JSON file.

    Creates a self-contained JSON file with all of an agent's memories,
    beliefs, identity, and emotional history. Suitable for backup,
    migration, or sharing.

    The export includes:
    - Agent identity (kernel, profile, epoch state/history)
    - All engrams (active + dormant, with connections and versions)
    - All beliefs (with revision history)
    - Recent emotional state history
    - Export metadata (timestamp, version, counts)

    Args:
        store: The engram store to export from.
        agent_id: Which agent's memories to export.
        output_path: Path to write the JSON file. Parent directory must exist.

    Raises:
        FileNotFoundError: If parent directory of output_path doesn't exist.
    """
    unavailable("portable JSON memory export")


def import_memory(
    store: EngramStore,
    input_path: str | Path,
) -> dict[str, Any]:
    """Import memories from a portable JSON file into the store.

    Reads a previously exported memory file and imports all engrams,
    beliefs, identity, and emotional history into the store. Handles
    ID conflicts by generating new IDs and updating references.

    Args:
        store: The engram store to import into.
        input_path: Path to the JSON file to import.

    Returns:
        Import statistics:
        {
            "engrams_imported": int,
            "beliefs_imported": int,
            "connections_imported": int,
            "identity_imported": bool,
            "conflicts_resolved": int,
        }

    Raises:
        FileNotFoundError: If input_path doesn't exist.
        ValueError: If file format is invalid or version is incompatible.
    """
    unavailable("portable JSON memory import")
