"""T4 vault — identity-tier decision journal and reconciliation.

The vault is the second half of the authority story. T3 made authority physics
at ingest (a dream cannot sign itself as David's words). T4 makes *application*
of identity-tier change require a witnessed decision: nothing identity-tier
becomes operational without a hash-chained journal line David wrote with his own
password, verified byte-exact at apply time.

This package holds the agent-side, in-repo half:

- ``journal`` — canonical content hashing + hash-chain verification (the twin of
  the stdlib TCB ``scripts/mnemos-decide``; a cross-consistency test pins them).
- ``reconcile`` — journal↔table reconciliation that re-quarantines any
  identity-tier row whose columns no longer match a witnessed decision.

The journal *writer* (``mnemos-decide``) and its OS enforcement (a vault-owned,
append-only file the agent cannot write) are installed by David, by hand. The
agent can build and maintain everything here in the open because none of it is
load-bearing for the boundary — the journal remains ground truth even if this
code is tampered with, so divergence is always provable.
"""

from .journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalRead,
    canonical_content_sha256,
    canonical_row_sha256,
    find_decision,
    genesis_prev_hash,
    line_hash,
    read_journal,
    read_journal_classified,
    verify_chain,
)

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "JournalRead",
    "canonical_content_sha256",
    "canonical_row_sha256",
    "find_decision",
    "genesis_prev_hash",
    "line_hash",
    "read_journal",
    "read_journal_classified",
    "verify_chain",
]
