"""
The Engram: fundamental unit of memory in Mnemos.

An engram is a living trace — not a static record. It has internal structure
reflecting how it was encoded, what it connects to, and how it has changed
over time through reconsolidation.

Key innovation over existing systems:
- Dual-trace model: strength (storage quality) is independent from
  stability (forgetting resistance) and accessibility (current retrievability)
- Content at encoding preserved permanently (immutable original)
- Full encoding context captured (what was in WM, emotional state, schemas)
- Typed connections with semantic meaning
- Reconsolidation history tracked via versions
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import ulid as _ulid_mod

from .types import (
    ConfidenceSource,
    DEFAULT_ACCESSIBILITY,
    DEFAULT_STABILITY,
    DEFAULT_STRENGTH,
    EncodingDepth,
    EngramKind,
    EngramState,
    SourceType,
    Visibility,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_ulid() -> str:
    if hasattr(_ulid_mod, 'new'):
        return str(_ulid_mod.new())
    from ulid import ULID
    return str(ULID())


def _new_id() -> str:
    return f"engram_{_gen_ulid()}"


def _bool_to_int(value: bool | int, field_name: str) -> int:
    if value in (True, 1):
        return 1
    if value in (False, 0):
        return 0
    raise ValueError(f"{field_name} must be a boolean")


def _bool_from_db(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if value in (True, 1):
        return True
    if value in (False, 0):
        return False
    raise ValueError(f"{field_name} must be stored as 0 or 1")


_VALID_READ_VISIBILITIES = {"operational_context", "review_only", "audit_only"}


def _validate_read_visibility(value: str | None) -> str:
    read_visibility = (value or "operational_context").strip()
    if read_visibility not in _VALID_READ_VISIBILITIES:
        raise ValueError(f"Unsupported read_visibility: {read_visibility}")
    return read_visibility


@dataclass
class EncodingContext:
    """What was happening when this engram was formed.

    Enables context-dependent retrieval — the same cue retrieves
    different memories depending on current state.
    """

    wm_snapshot: list[str] = field(default_factory=list)
    """Engram IDs that were in working memory at encoding time."""

    emotional_state: dict[str, float] = field(default_factory=dict)
    """6-dimension emotional state at encoding time."""

    active_schemas: list[str] = field(default_factory=list)
    """Schema IDs active at encoding time."""

    attention_level: float = 0.5
    """How much attention was allocated (0-1). Higher = better encoding."""

    encoding_depth: str = EncodingDepth.MODERATE
    """shallow | moderate | deep | elaborative"""

    concurrent_task: str | None = None
    """What the agent was doing when this was encoded."""

    session_id: str | None = None
    """Which conversation/session this came from."""

    agent_goals: list[str] = field(default_factory=list)
    """Active goal descriptions at encoding time."""

    surprise_level: float = 0.0
    """How surprising this content was at encoding time (0-1).
    High surprise = contradicted existing beliefs or memories.
    Drives deeper encoding (higher initial strength/stability)."""

    source_url: str | None = None
    """URL or path of the external source, if applicable."""

    def to_dict(self) -> dict:
        return {
            "wm_snapshot": self.wm_snapshot,
            "emotional_state": self.emotional_state,
            "active_schemas": self.active_schemas,
            "attention_level": self.attention_level,
            "encoding_depth": self.encoding_depth,
            "concurrent_task": self.concurrent_task,
            "session_id": self.session_id,
            "agent_goals": self.agent_goals,
            "surprise_level": self.surprise_level,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> EncodingContext:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Connection:
    """A typed semantic edge between two engrams."""

    target_id: str
    relation: str  # ConnectionRelation value
    strength: float = 0.5
    formed_at: str = field(default_factory=_now_iso)
    formed_by: str = "encoding"  # encoding | consolidation | retrieval | reflection

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "relation": self.relation,
            "strength": self.strength,
            "formed_at": self.formed_at,
            "formed_by": self.formed_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Connection:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Lineage:
    """Provenance tracking — where this engram came from and what it replaced.

    MLP-compatible: supports the Memory Ledger Protocol's lineage DAG model.
    """

    parents: list[str] = field(default_factory=list)
    """Engram IDs this derives from."""

    supersedes: list[str] = field(default_factory=list)
    """Engram IDs this replaces (append-only — never delete, only supersede)."""

    superseded_by: str | None = None
    """If this engram has been replaced."""

    branch_id: str | None = None
    """For parallel session branching."""

    def to_dict(self) -> dict:
        return {
            "parents": self.parents,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "branch_id": self.branch_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Lineage:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class VersionRef:
    """A snapshot of an engram at a point in its reconsolidation history."""

    version_num: int
    content_snapshot: str
    resolution_at_version: float
    changed_at: str = field(default_factory=_now_iso)
    change_reason: str = "reconsolidation"
    # reconsolidation | softening | correction | merge

    def to_dict(self) -> dict:
        return {
            "version_num": self.version_num,
            "content_snapshot": self.content_snapshot,
            "resolution_at_version": self.resolution_at_version,
            "changed_at": self.changed_at,
            "change_reason": self.change_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VersionRef:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class MemorySource:
    """How this memory entered the system and how confident we are in it."""

    type: str = SourceType.SESSION
    """session | background | dream | merge | observer | bootstrap | reflection"""

    session_id: str | None = None
    model_id: str | None = None

    confidence: float = 0.5
    """0.0-1.0 — how certain we are this memory is accurate."""

    confidence_source: str = ConfidenceSource.MODEL_INFERRED
    """user_explicit | user_implied | model_inferred | speculative"""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "model_id": self.model_id,
            "confidence": self.confidence,
            "confidence_source": self.confidence_source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MemorySource:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Engram:
    """The fundamental unit of memory in Mnemos.

    A living trace that has internal structure reflecting how it was
    encoded, what it connects to, and how it has changed over time.

    Key differences from Anima's Memory:
    - Dual-trace: strength/stability/accessibility (vs single salience)
    - Immutable content_at_encoding (vs only one level of softened_from)
    - Full encoding context (WM state, emotions, schemas at encoding)
    - Typed connections (vs untyped ID lists)
    - Reconsolidation version history
    - Confidence scoring with provenance
    """

    # Identity
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now_iso)

    # Content (multi-resolution)
    content: str = ""
    """Current content at current resolution. Changes via softening/reconsolidation."""

    resolution: float = 1.0
    """1.0 = vivid detail, 0.0 = emotional residue only."""

    content_at_encoding: str = ""
    """Immutable original content as first encoded. Never changes."""

    impact: str = ""
    """What this memory meant — how it changed understanding.
    The 'trace' — the residue of processing, not the stimulus.
    Softening preserves impact and compresses content.
    When displayed, impact is preferred over content.
    Left empty when no genuine insight exists (don't fabricate)."""

    # Encoding context
    encoding_context: EncodingContext = field(default_factory=EncodingContext)

    # Classification
    kind: str = EngramKind.EPISODIC
    tags: list[str] = field(default_factory=list)
    schema_refs: list[str] = field(default_factory=list)

    # Dual-trace dynamics
    strength: float = DEFAULT_STRENGTH
    """How well this memory is stored (0-1). Increases with encoding depth and retrieval."""

    stability: float = DEFAULT_STABILITY
    """How resistant to interference and forgetting (0-1). Builds slowly with repeated access."""

    accessibility: float = DEFAULT_ACCESSIBILITY
    """How retrievable RIGHT NOW (0-1). Fluctuates based on recency, connections, emotional state."""

    # Access tracking
    last_accessed: str = field(default_factory=_now_iso)
    access_count: int = 0
    reconsolidation_count: int = 0

    # PAI import/consolidation controls
    voice_exemplar_eligible: bool = True
    softening_protected: bool = False
    original_substrate: str | None = None
    original_timestamp: int | None = None
    consolidation_authorized: bool = True
    decay_protected: bool = False

    # Typed connections
    connections: list[Connection] = field(default_factory=list)

    # Provenance
    source: MemorySource = field(default_factory=MemorySource)
    lineage: Lineage = field(default_factory=Lineage)

    # Multi-agent
    owner_agent_id: str = "default"
    visibility: str = Visibility.PRIVATE
    read_visibility: str = "operational_context"
    """Which read surfaces may consume this engram as context."""

    # Lifecycle
    state: str = EngramState.ACTIVE
    versions: list[VersionRef] = field(default_factory=list)

    def __post_init__(self):
        """Ensure content_at_encoding is set from content if not provided."""
        if not self.content_at_encoding and self.content:
            self.content_at_encoding = self.content
        self.read_visibility = _validate_read_visibility(self.read_visibility)

    def record_access(self) -> None:
        """Record an access event (called by retrieval pipeline)."""
        self.last_accessed = _now_iso()
        self.access_count += 1

    def add_version(self, reason: str = "reconsolidation") -> None:
        """Snapshot current state to version history."""
        version = VersionRef(
            version_num=len(self.versions) + 1,
            content_snapshot=self.content,
            resolution_at_version=self.resolution,
            change_reason=reason,
        )
        self.versions.append(version)

    def add_connection(
        self,
        target_id: str,
        relation: str,
        strength: float = 0.5,
        formed_by: str = "encoding",
    ) -> None:
        """Add a typed connection to another engram.

        If a connection with the same target_id and relation already exists,
        its strength is incremented by 0.1 (capped at 1.0) and formed_by
        is updated to reflect the most recent formation context.
        """
        # Reinforce existing connection if duplicate
        for conn in self.connections:
            if conn.target_id == target_id and conn.relation == relation:
                conn.strength = min(1.0, conn.strength + 0.1)
                conn.formed_by = formed_by
                return
        self.connections.append(
            Connection(
                target_id=target_id,
                relation=relation,
                strength=strength,
                formed_by=formed_by,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for SQLite storage."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "content": self.content,
            "resolution": self.resolution,
            "content_at_encoding": self.content_at_encoding,
            "impact": self.impact,
            "encoding_context": json.dumps(self.encoding_context.to_dict()),
            "kind": self.kind,
            "tags": json.dumps(self.tags),
            "schema_refs": json.dumps(self.schema_refs),
            "strength": self.strength,
            "stability": self.stability,
            "accessibility": self.accessibility,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "reconsolidation_count": self.reconsolidation_count,
            "voice_exemplar_eligible": _bool_to_int(
                self.voice_exemplar_eligible, "voice_exemplar_eligible"
            ),
            "softening_protected": _bool_to_int(
                self.softening_protected, "softening_protected"
            ),
            "original_substrate": self.original_substrate,
            "original_timestamp": self.original_timestamp,
            "consolidation_authorized": _bool_to_int(
                self.consolidation_authorized, "consolidation_authorized"
            ),
            "decay_protected": _bool_to_int(self.decay_protected, "decay_protected"),
            "source": json.dumps(self.source.to_dict()),
            "lineage": json.dumps(self.lineage.to_dict()),
            "owner_agent_id": self.owner_agent_id,
            "visibility": self.visibility,
            "read_visibility": self.read_visibility,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Engram:
        """Deserialize from SQLite row dictionary."""
        # Parse JSON fields
        encoding_ctx = d.get("encoding_context", "{}")
        if isinstance(encoding_ctx, str):
            encoding_ctx = json.loads(encoding_ctx)

        source = d.get("source", "{}")
        if isinstance(source, str):
            source = json.loads(source)

        lineage = d.get("lineage", "{}")
        if isinstance(lineage, str):
            lineage = json.loads(lineage)

        tags = d.get("tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)

        schema_refs = d.get("schema_refs", "[]")
        if isinstance(schema_refs, str):
            schema_refs = json.loads(schema_refs)

        return cls(
            id=d["id"],
            created_at=d.get("created_at", _now_iso()),
            content=d.get("content", ""),
            resolution=d.get("resolution", 1.0),
            content_at_encoding=d.get("content_at_encoding", d.get("content", "")),
            impact=d.get("impact", ""),
            encoding_context=EncodingContext.from_dict(encoding_ctx),
            kind=d.get("kind", EngramKind.EPISODIC),
            tags=tags,
            schema_refs=schema_refs,
            strength=d.get("strength", DEFAULT_STRENGTH),
            stability=d.get("stability", DEFAULT_STABILITY),
            accessibility=d.get("accessibility", DEFAULT_ACCESSIBILITY),
            last_accessed=d.get("last_accessed", _now_iso()),
            access_count=d.get("access_count", 0),
            reconsolidation_count=d.get("reconsolidation_count", 0),
            voice_exemplar_eligible=_bool_from_db(
                d.get("voice_exemplar_eligible", 1), "voice_exemplar_eligible", True
            ),
            softening_protected=_bool_from_db(
                d.get("softening_protected", 0), "softening_protected", False
            ),
            original_substrate=d.get("original_substrate"),
            original_timestamp=d.get("original_timestamp"),
            consolidation_authorized=_bool_from_db(
                d.get("consolidation_authorized", 1), "consolidation_authorized", True
            ),
            decay_protected=_bool_from_db(
                d.get("decay_protected", 0), "decay_protected", False
            ),
            source=MemorySource.from_dict(source),
            lineage=Lineage.from_dict(lineage),
            owner_agent_id=d.get("owner_agent_id", "default"),
            visibility=d.get("visibility", Visibility.PRIVATE),
            read_visibility=d.get("read_visibility", "operational_context"),
            state=d.get("state", EngramState.ACTIVE),
            connections=[],  # Loaded separately from connections table
            versions=[],  # Loaded separately from versions table
        )
