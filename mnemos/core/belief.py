"""
Beliefs: stable convictions that form from repeated memory patterns.

Ported from Anima's beliefs.py and enhanced with:
- Supporting engram IDs (evidence trail)
- Trigger engram on revisions (what caused the change)
- Confidence never reaches 1.0 (epistemic humility)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import ulid as _ulid_mod

def _gen_ulid() -> str:
    if hasattr(_ulid_mod, 'new'):
        return str(_ulid_mod.new())
    from ulid import ULID
    return str(ULID())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VALID_BELIEF_TIERS = {"foundational", "operational", "tactical"}


def _bool_to_int(value: bool | int, field_name: str) -> int:
    if value in (True, 1):
        return 1
    if value in (False, 0):
        return 0
    raise ValueError(f"{field_name} must be a boolean")


def _bool_from_db(value, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if value in (True, 1):
        return True
    if value in (False, 0):
        return False
    raise ValueError(f"{field_name} must be stored as 0 or 1")


def _validate_tier(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _VALID_BELIEF_TIERS:
        raise ValueError(f"Unsupported belief tier: {value}")
    return value


@dataclass
class BeliefRevision:
    """A record of how a belief changed."""

    timestamp: str = field(default_factory=_now_iso)
    old_confidence: float = 0.0
    new_confidence: float = 0.0
    reason: str = ""
    trigger_engram_id: str | None = None
    _extra_fields: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict:
        data = dict(self._extra_fields)
        data.update(
            {
                "timestamp": self.timestamp,
                "old_confidence": self.old_confidence,
                "new_confidence": self.new_confidence,
                "reason": self.reason,
                "trigger_engram_id": self.trigger_engram_id,
            }
        )
        return data

    @classmethod
    def from_dict(cls, d: dict) -> BeliefRevision:
        fields = {
            "timestamp",
            "old_confidence",
            "new_confidence",
            "reason",
            "trigger_engram_id",
        }
        return cls(
            **{k: v for k, v in d.items() if k in fields},
            _extra_fields={k: v for k, v in d.items() if k not in fields},
        )


@dataclass
class Belief:
    """A stable conviction formed from repeated memory patterns.

    Confidence never reaches 1.0 — epistemic humility is built in.
    Beliefs that go unchallenged for stagnation_threshold_days get
    flagged for review by the belief_review consolidation pass.
    """

    id: str = field(default_factory=lambda: f"belief_{_gen_ulid()}")
    agent_id: str = "default"

    content: str = ""
    """The belief statement."""

    confidence: float = 0.3
    """0.0-0.99. New beliefs start tentative. Never reaches 1.0."""

    domain: str = "general"
    """Category: technical, social, self, project, etc."""

    created_at: str = field(default_factory=_now_iso)
    last_revised: str = field(default_factory=_now_iso)
    last_challenged: str = field(default_factory=_now_iso)

    revision_history: list[BeliefRevision] = field(default_factory=list)
    superseded_by: str | None = None

    supporting_engram_ids: list[str] = field(default_factory=list)
    """Engrams that provide evidence for this belief."""

    tier: str | None = None
    """foundational | operational | tactical; used by IdentityProfile weighting."""

    needs_review: bool = False
    """Set when dual-life source changes make this belief stale."""

    confidence_pending_review: bool = False
    """Set when confidence should be excluded or degraded until review."""

    def revise(
        self,
        new_confidence: float,
        reason: str,
        trigger_engram_id: str | None = None,
    ) -> None:
        """Update confidence with full audit trail."""
        revision = BeliefRevision(
            old_confidence=self.confidence,
            new_confidence=min(0.99, max(0.0, new_confidence)),
            reason=reason,
            trigger_engram_id=trigger_engram_id,
        )
        self.revision_history.append(revision)
        self.confidence = revision.new_confidence
        self.last_revised = _now_iso()

    def challenge(self) -> None:
        """Mark as challenged (resets stagnation timer)."""
        self.last_challenged = _now_iso()

    def supersede(self, new_belief_id: str) -> None:
        """Mark this belief as superseded by a new one."""
        self.superseded_by = new_belief_id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "content": self.content,
            "confidence": self.confidence,
            "domain": self.domain,
            "created_at": self.created_at,
            "last_revised": self.last_revised,
            "last_challenged": self.last_challenged,
            "revision_history": json.dumps(
                [r.to_dict() for r in self.revision_history]
            ),
            "superseded_by": self.superseded_by,
            "supporting_engram_ids": json.dumps(self.supporting_engram_ids),
            "tier": _validate_tier(self.tier),
            "needs_review": _bool_to_int(self.needs_review, "needs_review"),
            "confidence_pending_review": _bool_to_int(
                self.confidence_pending_review, "confidence_pending_review"
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Belief:
        revisions = d.get("revision_history", "[]")
        if isinstance(revisions, str):
            revisions = json.loads(revisions)

        supporting = d.get("supporting_engram_ids", "[]")
        if isinstance(supporting, str):
            supporting = json.loads(supporting)

        return cls(
            id=d["id"],
            agent_id=d.get("agent_id", "default"),
            content=d.get("content", ""),
            confidence=d.get("confidence", 0.3),
            domain=d.get("domain", "general"),
            created_at=d.get("created_at", _now_iso()),
            last_revised=d.get("last_revised", _now_iso()),
            last_challenged=d.get("last_challenged", _now_iso()),
            revision_history=[BeliefRevision.from_dict(r) for r in revisions],
            superseded_by=d.get("superseded_by"),
            supporting_engram_ids=supporting,
            tier=_validate_tier(d.get("tier")),
            needs_review=_bool_from_db(d.get("needs_review", 0), "needs_review", False),
            confidence_pending_review=_bool_from_db(
                d.get("confidence_pending_review", 0),
                "confidence_pending_review",
                False,
            ),
        )
