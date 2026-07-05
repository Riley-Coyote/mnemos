"""ExperienceTick (U6b) — proposes DynamicModulations; never writes them.

The tick observes experience signals and emits modulation PROPOSALS into the
proposal ledger, targeting the U5 modulation surface via
``MODULATION_PROPOSAL_SURFACE`` (the store constant — the literal table name
stays in the sanctioned store layer so the U5 source-level inertness grep remains
a pure read-path check). It is the propose half of "the tick proposes, the gate
disposes" (five-day plan Day 3). It writes NOTHING to any memory table and reads
NOTHING from the U5 modulation vessel — U5's inertness (inert by *absence of a
read path*, architecture-review §5.9 #9) is preserved because this unit adds no
read path and no direct write. A future U6
activation unit, under its own ruling, builds the disposal/read path that would
turn an approved proposal into a stored, applied modulation.

Invariants enforced here (each has a test in test_u6b_experience_tick.py):

- **Write-contract (016c/016d, 018 §16):** every emitted proposal's payload
  carries a non-empty ``rollout_tag`` trimmed on ``MODULATION_TAG_EDGE_WS`` (the
  single-sourced charset — never bare ``.strip()``, which strips unicode wider
  than the schema CHECK and reopens the whitespace-bypass class g3 closes) and a
  positive ``ttl_seconds``. Validated at emission; fail-closed.
- **D7-#2 (identity-domain targeting UNANSWERED):** the tick refuses to target
  identity-tier items — ``domain`` in {'identity','foundational'} is rejected.
  Identity modulation is left structurally unaddressed per the open decision; it
  is not built here.
- **Kill-switch per family:** a disabled modulation family emits nothing.
- **Inert:** proposals are ``pending_review`` review artifacts; nothing applies
  them while U6-active is OFF (015b). No modulation row is written by a tick.
- **Follow-the-loop stability (018c / 013e):** re-ticking the same observation
  under the same rollout_tag updates the SAME pending proposal (deterministic
  proposal id) rather than multiplying — no propose→reprocess loop.
- **Manual invocation only:** no scheduling here (launchd/cron is operator-plan /
  [DAVID] territory). ``ExperienceTick.tick()`` is called explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from mnemos.store.sqlite_store import (
    MODULATION_MAGNITUDE_CAP,
    MODULATION_PROPOSAL_SURFACE,
    MODULATION_TAG_EDGE_WS,
    READ_VISIBILITY_REVIEW,
    VALID_MODULATION_TARGETS,
)

# Identity-tier domains the tick must never target (D7-#2 unanswered).
_IDENTITY_DOMAINS = frozenset({"identity", "foundational"})
# Default non-identity domain for a modulation proposal (a modulation is
# situational, non-evidentiary residue).
_DEFAULT_MODULATION_DOMAIN = "situational"
# Deterministic proposal-id prefix, so re-ticks are visibly this unit's rows.
_PROPOSAL_ID_PREFIX = "u6b-mod-"


def _normalize_domain(raw: str) -> str:
    """Normalize a domain exactly as ``write_proposal`` will (``_clean_choice``
    does ``.strip()``). The identity guard and the stored value both go through
    this so they can never diverge — the whitespace-bypass class (018e #2)."""
    return (raw or "").strip()


@dataclass(frozen=True)
class ProposedModulation:
    """One modulation the tick intends to propose (NOT a stored row).

    ``family`` is the kill-switch unit. The spec fields
    (``target``/``target_topic``/``valence``/``magnitude``/``ttl_seconds``/
    ``decay_rate``) are what the future disposal path (``store_dynamic_modulation``)
    would validate in full; this unit validates the write-contract subset (tag +
    ttl) required of every modulation proposal, plus the target/magnitude sanity
    that would otherwise make an emitted proposal un-disposable.
    """

    family: str
    target: str
    magnitude: float
    ttl_seconds: int
    target_topic: str = ""
    valence: float = 0.0
    decay_rate: float = 0.0
    domain: str = _DEFAULT_MODULATION_DOMAIN
    provenance_ids: tuple[str, ...] = ()


class ExperienceTick:
    """Propose modulations from observed experience. Never writes them."""

    def __init__(
        self, store: Any, *, enabled_families: set[str] | None = None
    ) -> None:
        self._store = store
        # None = all families enabled; a set = ONLY those families are enabled.
        # The kill-switch is deny-by-omission once a set is supplied.
        self._enabled_families = enabled_families

    def is_family_enabled(self, family: str) -> bool:
        return self._enabled_families is None or family in self._enabled_families

    def tick(
        self,
        observations: list[ProposedModulation],
        *,
        rollout_tag: str,
        agent_id: str = "default",
        person_id: str = "user",
        project_scope: str = "global",
    ) -> list[dict[str, Any]]:
        """Emit one modulation proposal per enabled observation; return the rows.

        Fail-closed: every enabled observation is validated BEFORE any proposal is
        written, so a contract/D7-#2 violation aborts the whole batch rather than
        leaving a partial emit behind. Re-ticking identical inputs updates the
        same pending rows (deterministic ids) — follow-the-loop stable.
        """
        tag = (rollout_tag or "").strip(MODULATION_TAG_EDGE_WS)
        if not tag:
            raise ValueError(
                "ExperienceTick rollout_tag is required and must be non-empty "
                "after trimming MODULATION_TAG_EDGE_WS (016c U5-g1 / 016d g3): "
                "every proposed modulation must be reachable by the tag-scoped "
                "backout."
            )

        to_emit = [o for o in observations if self.is_family_enabled(o.family)]
        for obs in to_emit:
            self._validate(obs)

        # 018h finding 1: the batch is all-or-nothing at the STORE level, not just
        # payload validation. write_proposal commits per row, so a store-level
        # raise on the Nth proposal (unsupported domain, terminal id collision)
        # after prevalidation would leave a partial batch committed. Emit every
        # proposal with commit=False so the INSERTs join one deferred transaction;
        # commit once at the end, and roll the whole batch back on any raise —
        # zero proposals persist on any failure.
        conn = self._store._get_conn()
        emitted: list[dict[str, Any]] = []
        try:
            for obs in to_emit:
                payload = {
                    "target": obs.target,
                    "magnitude": float(obs.magnitude),
                    "valence": float(obs.valence),
                    "ttl_seconds": int(obs.ttl_seconds),
                    "decay_rate": float(obs.decay_rate),
                    "target_topic": obs.target_topic,
                    "rollout_tag": tag,
                    "source_authority": "generated",
                    "family": obs.family,
                }
                row = self._store.write_proposal(
                    source_authority="generated",
                    kind="prospective",  # modulation → prospective (PROPOSAL_KIND_ALIASES)
                    target_surface=MODULATION_PROPOSAL_SURFACE,
                    transition=(
                        f"ExperienceTick proposes a {obs.target} modulation "
                        f"(family={obs.family}, topic={obs.target_topic!r})"
                    ),
                    # 018e finding 2: write the NORMALIZED domain (the same
                    # normalization the guard decided on) so guard and stored
                    # value can never diverge.
                    domain=_normalize_domain(obs.domain),
                    # 018e finding 1: proposals are review_only, not the audit-only
                    # store default — the disposal gate must SEE what it disposes.
                    # Inert per 015b: the proposal ledger's review axis, not a read
                    # path into the modulation table.
                    read_visibility=READ_VISIBILITY_REVIEW,
                    blast_radius="low",
                    agent_id=agent_id,
                    person_id=person_id,
                    project_scope=project_scope,
                    provenance_ids=list(obs.provenance_ids),
                    payload=payload,
                    reason=f"u6b-experience-tick:{obs.family}",
                    proposal_id=_proposal_id(
                        agent_id, person_id, project_scope, obs, tag
                    ),
                    commit=False,
                )
                emitted.append(row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return emitted

    def _validate(self, obs: ProposedModulation) -> None:
        # D7-#2: identity-tier targeting is UNANSWERED — refuse it structurally.
        # 018e finding 2 (the fourth whitespace-bypass): decide on the NORMALIZED
        # domain, at the SAME normalization write_proposal applies (_clean_choice
        # does .strip()). Checking the raw obs.domain let ' identity ' slip past
        # this guard and then store as identity-domain. The permanent rule: any
        # guard that decides on a field decides on the normalized field.
        domain = _normalize_domain(obs.domain)
        if domain in _IDENTITY_DOMAINS:
            raise ValueError(
                f"ExperienceTick refuses domain {domain!r}: identity-tier "
                "modulation targeting is D7-#2-UNANSWERED and deliberately not "
                "built. Propose only non-identity modulations."
            )
        # target must be a real modulation target (else disposal would fail).
        if obs.target not in VALID_MODULATION_TARGETS:
            raise ValueError(
                f"invalid modulation target {obs.target!r}; must be one of "
                f"{sorted(VALID_MODULATION_TARGETS)}"
            )
        # Write-contract: positive ttl (016c U5-g2).
        try:
            ttl = int(obs.ttl_seconds)
        except (TypeError, ValueError):
            raise ValueError("modulation ttl_seconds must be an integer")
        if ttl <= 0:
            raise ValueError(
                f"modulation ttl_seconds must be positive (016c U5-g2); got {ttl}"
            )
        # magnitude cap (mirrors store_dynamic_modulation; disposal re-checks).
        try:
            mag = float(obs.magnitude)
        except (TypeError, ValueError):
            raise ValueError("modulation magnitude must be a number")
        if not (-MODULATION_MAGNITUDE_CAP <= mag <= MODULATION_MAGNITUDE_CAP):
            raise ValueError(
                f"modulation magnitude {mag} exceeds cap ±{MODULATION_MAGNITUDE_CAP}"
            )
        # 018e finding 3 + 018h finding 2: valence and decay_rate must be FINITE
        # numbers, validated pre-loop. float('nan')/float('inf') pass a bare
        # float() and the (nan/inf < 0) comparison is False, so they would
        # serialize into the payload — reject with math.isfinite (same numeric-
        # hygiene class as U5's write contract). All-or-nothing: a bad later
        # observation leaves no earlier proposal committed.
        try:
            valence = float(obs.valence)
        except (TypeError, ValueError):
            raise ValueError("modulation valence must be a number")
        if not math.isfinite(valence):
            raise ValueError(
                f"modulation valence must be finite; got {obs.valence!r}"
            )
        try:
            decay = float(obs.decay_rate)
        except (TypeError, ValueError):
            raise ValueError("modulation decay_rate must be a number")
        if not math.isfinite(decay):
            raise ValueError(
                f"modulation decay_rate must be finite; got {obs.decay_rate!r}"
            )
        if decay < 0:
            raise ValueError(
                f"modulation decay_rate must be non-negative; got {decay}"
            )


def _proposal_id(
    agent_id: str,
    person_id: str,
    project_scope: str,
    obs: ProposedModulation,
    rollout_tag: str,
) -> str:
    """Deterministic id for (scope, family, target, topic, tag).

    Re-ticking the same observation under the same tag resolves to the same
    proposal_id, so ``write_proposal`` updates the existing ``pending_review`` row
    instead of creating a duplicate. This is the follow-the-loop guard: a second
    tick over identical input does not multiply proposals (018c / 013e).
    """
    key = json.dumps(
        {
            "agent_id": agent_id,
            "person_id": person_id,
            "project_scope": project_scope,
            "family": obs.family,
            "target": obs.target,
            "target_topic": obs.target_topic,
            "rollout_tag": rollout_tag,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _PROPOSAL_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
