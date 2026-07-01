"""
Connection discovery pass: find semantic connections between engrams
that were missed during encoding.

Phase 2 upgrade:
- Uses embedding similarity (Gemini 3072-dim) alongside FTS5 for candidate discovery
- Uses LLM classifier for typed relationship assignment (7 types + NONE)
- Can reclassify existing low-quality connections (old all-"supports" monoculture)
- Cross-session connections: memories from different conversations that relate
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..core.types import ConnectionRelation, DEFAULT_AGENT_ID
from ..encoding.llm_classifier import classify_connections
from ..store.read_visibility import READ_VISIBILITY_OPERATIONAL

if TYPE_CHECKING:
    from ..store.sqlite_store import EngramStore

log = logging.getLogger("mnemos.consolidation.connections")


def run_connection_discovery(
    store: EngramStore,
    embedding_index: Any | None = None,
    config: dict[str, Any] | None = None,
    llm_client: Any | None = None,
    agent_id: str = DEFAULT_AGENT_ID,
) -> dict[str, Any]:
    """Find semantically related engrams and create/reclassify connections.

    Two modes:
    1. **New connections** — find engrams with few connections and discover new ones
    2. **Reclassify** — upgrade old "supports" connections using LLM classification

    Uses both FTS5 and embedding similarity for candidate discovery.
    Uses LLM classifier for typed relationship assignment.

    Args:
        store: The engram store.
        embedding_index: Embedding index for semantic search (Phase 1.5+).
        config: Configuration dict with discovery parameters.
        llm_client: LLM client for typed classification. Falls back to
            SUPPORTS if not available.

    Returns:
        Statistics dict.
    """
    config = config or {}
    max_per_pass = config.get("max_engrams_per_discovery_pass", 50)
    max_per_engram = config.get("max_connections_per_engram", 10)
    reclassify_batch = config.get("reclassify_batch_size", 20)

    stats = {
        "engrams_processed": 0,
        "connections_created": 0,
        "connections_reclassified": 0,
        "connections_removed": 0,
        "connections_strengthened": 0,
        "embedding_candidates": 0,
        "fts_candidates": 0,
    }

    all_active = store.get_active_engrams(
        agent_id=agent_id,
        limit=max_per_pass * 2,
        require_consolidation_authorized=True,
    )

    # ── Phase A: Discover new connections for underconnected engrams ──
    underconnected = []
    for engram in all_active:
        existing = store.get_connections(
            engram.id,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
        if len(existing) < max_per_engram:
            underconnected.append((engram, existing))

    for engram, existing_connections in underconnected[:max_per_pass]:
        stats["engrams_processed"] += 1
        existing_target_ids = {c.target_id for c in existing_connections}
        candidates = []

        # 1. Embedding-based candidates (if available)
        if embedding_index and embedding_index.available:
            emb_results = embedding_index.search(
                engram.content, k=10, exclude_ids={engram.id},
            )
            for eid, score in emb_results:
                if eid not in existing_target_ids and score > 0.3:
                    candidate = store.get_engram(
                        eid,
                        read_visibility="operational_context",
                    )
                    if _same_mutable_scope(engram, candidate):
                        candidates.append(candidate)
                        stats["embedding_candidates"] += 1

        # 2. FTS5 candidates (supplement, catches keyword matches embeddings miss)
        words = [w for w in engram.content.split() if len(w) > 2 and w.isalnum()]
        if words:
            query = " OR ".join(f'"{w}"' for w in words[:8])
            try:
                fts_results = store.search_fts(
                    query,
                    limit=10,
                    read_visibility=READ_VISIBILITY_OPERATIONAL,
                )
                for match in fts_results:
                    if (
                        match.id != engram.id
                        and match.id not in existing_target_ids
                        and _same_mutable_scope(engram, match)
                    ):
                        # Don't duplicate embedding candidates
                        if not any(c.id == match.id for c in candidates):
                            candidates.append(match)
                            stats["fts_candidates"] += 1
            except Exception:
                pass

        if not candidates:
            continue

        # 3. Classify relationships via LLM (or fallback)
        if llm_client:
            classifications = classify_connections(llm_client, engram, candidates)
            for cls in classifications:
                tag_overlap = 0
                candidate = next((c for c in candidates if c.id == cls.candidate_id), None)
                if candidate:
                    tag_overlap = len(set(engram.tags) & set(candidate.tags))

                strength = min(0.95, cls.confidence + 0.05 * tag_overlap)

                engram.add_connection(
                    target_id=cls.candidate_id,
                    relation=cls.relation,
                    strength=round(strength, 3),
                    formed_by="consolidation",
                )
                stats["connections_created"] += 1
        else:
            # Fallback when no LLM client is available: FTS keyword overlap
            # is correlation, not evidence of support. Write CO_ACTIVATED so
            # later passes can reclassify (or strip) these edges rather than
            # locking the graph into a SUPPORTS monoculture from keyword hits.
            for match in candidates[:5]:
                tag_overlap = len(set(engram.tags) & set(match.tags))
                strength = min(0.8, 0.3 + 0.1 * tag_overlap)
                engram.add_connection(
                    target_id=match.id,
                    relation=ConnectionRelation.CO_ACTIVATED,
                    strength=strength,
                    formed_by="consolidation_no_llm",
                )
                stats["connections_created"] += 1

        store.save_engram(engram)

    # ── Phase B: Reclassify unclassified connections ──
    if llm_client:
        _reclassify_old_connections(
            store, llm_client, all_active,
            batch_size=reclassify_batch, stats=stats,
        )

    return stats


# Relations that were mechanically formed rather than semantically
# classified: legacy "supports" monoculture edges and the structural
# co-activation edges written by retrieval reconsolidation.
_RECLASSIFIABLE_RELATIONS = (
    ConnectionRelation.SUPPORTS,
    "supports",
    ConnectionRelation.CO_ACTIVATED,
    "co_activated",
)


def _reclassify_old_connections(
    store: EngramStore,
    llm_client: Any,
    all_active: list,
    batch_size: int,
    stats: dict,
) -> None:
    """Reclassify mechanically-formed connections using LLM classification.

    Finds connections that carry no real semantic judgment — the legacy
    "supports" monoculture and retrieval's "co_activated" edges — and
    classifies them. NONE results → remove the connection (false positive).
    """
    # Find engrams with unclassified connections
    reclassified_count = 0

    for engram in all_active:
        if reclassified_count >= batch_size:
            break

        connections = store.get_connections(
            engram.id,
            read_visibility=READ_VISIBILITY_OPERATIONAL,
        )
        raw_connections = [
            c for c in connections
            if c.relation in _RECLASSIFIABLE_RELATIONS
        ]

        if not raw_connections:
            continue

        # Load the target engrams
        targets = []
        for conn in raw_connections:
            target = store.get_engram(
                conn.target_id,
                read_visibility="operational_context",
            )
            if _same_mutable_scope(engram, target):
                targets.append(target)

        if not targets:
            continue

        # Classify the batch
        classifications = classify_connections(llm_client, engram, targets)
        classified_ids = {cls.candidate_id for cls in classifications}

        for cls in classifications:
            # Find the existing connection to update
            for conn in raw_connections:
                if conn.target_id == cls.candidate_id:
                    # Update the connection type and strength
                    conn.relation = cls.relation
                    conn.strength = round(cls.confidence, 3)
                    conn.formed_by = "consolidation_reclassified"
                    store.save_connection(engram.id, conn)
                    stats["connections_reclassified"] += 1
                    reclassified_count += 1
                    break

        # Remove connections where LLM returned NONE (false positives)
        for conn in raw_connections:
            if conn.target_id not in classified_ids:
                # Target wasn't classified → check if it was in our targets list
                if any(t.id == conn.target_id for t in targets):
                    # It was sent to the LLM and came back NONE → remove
                    store.remove_connection(engram.id, conn.target_id)
                    stats["connections_removed"] += 1


def _same_mutable_scope(source: Any, candidate: Any | None) -> bool:
    if candidate is None:
        return False
    return (
        getattr(source, "read_visibility", "operational_context") == "operational_context"
        and getattr(candidate, "read_visibility", "operational_context") == "operational_context"
        and bool(candidate.consolidation_authorized)
        and candidate.owner_agent_id == source.owner_agent_id
    )
