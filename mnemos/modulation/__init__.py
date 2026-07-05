"""Modulation proposing (U6b). The U5 modulation vessel lives in the store and
stays inert by absence of a read path; this package only *proposes* modulations
into the proposal ledger — the tick proposes, the gate disposes."""

from .experience_tick import ExperienceTick, ProposedModulation

__all__ = ["ExperienceTick", "ProposedModulation"]
