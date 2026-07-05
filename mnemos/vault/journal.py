"""Canonical content hashing and hash-chain verification for the vault journal.

This module is the apply-side twin of the stdlib TCB ``scripts/mnemos-decide``.
Both must compute byte-identical content hashes and identical chain verdicts;
``tests/test_vault_journal_twin.py`` pins that with shared vectors. The
duplication is deliberate — the TCB stays self-contained stdlib so David can
audit the whole file, and the twin-lock test is how the copies stay honest.

Two hashes live here, do not confuse them:

- **content hash** (``canonical_content_sha256``): a fingerprint of *what a
  proposal would apply*. Bound into the journal line at decision time; recomputed
  at apply time and required to match byte-for-byte (closes TOCTOU).
- **line hash** (``line_hash``): a fingerprint of a whole journal line, used to
  chain lines (each line's ``prev_sha256`` equals the previous line's line hash)
  and as the ``decision_ref`` a target row stores.

Stdlib only (``hashlib``, ``json``) so the module imports clean under any Python.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

JOURNAL_SCHEMA_VERSION = 1

# Genesis anchor: the first journal line's ``prev_sha256`` must equal this. A
# fixed, versioned string — changing it is a hard fork of every journal.
_GENESIS_STRING = f"mnemos-vault-journal-genesis-v{JOURNAL_SCHEMA_VERSION}"

# The content-hash field set (Fable review 008 §3): every field the apply writes
# to the target row, plus every field the TCB renders to David at decision time.
# Order here is irrelevant — canonicalization sorts keys — but the set is
# permanent. Each field's admitting criterion is documented in the T4 report.
#   proposal_id      identity binding of the decision
#   agent_id/person_id/project_scope   applied (scope the durable row)
#   source_authority applied (provenance) + rendered
#   kind             applied (semantic type) + rendered
#   domain           applied (tier signal) + rendered
#   blast_radius     rendered (the tier approved) + gates surface acceptance
#   target_surface   applied (which table) + rendered
#   target_id        applied (which row)
#   transition       rendered (the change description)
#   payload          applied (the content + row fields)
#   provenance_ids   rendered (full provenance labels)
_CONTENT_FIELDS = (
    "proposal_id",
    "agent_id",
    "person_id",
    "project_scope",
    "source_authority",
    "kind",
    "domain",
    "blast_radius",
    "target_surface",
    "target_id",
    "transition",
    "payload",
    "provenance_ids",
)


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_content_sha256(proposal: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical content field set of a (hydrated) proposal row.

    Accepts either ``id`` or ``proposal_id`` for the identity field so the same
    function serves a raw DB row and an already-normalized dict. Missing scalar
    fields normalize to ``""``; missing ``payload`` to ``{}``; missing
    ``provenance_ids`` to ``[]`` — so a partially-specified row hashes
    deterministically rather than raising.
    """
    content: dict[str, Any] = {}
    for _f in _CONTENT_FIELDS:
        if _f == "proposal_id":
            value = proposal.get("proposal_id", proposal.get("id"))
        else:
            value = proposal.get(_f)
        if _f == "payload":
            content[_f] = value if isinstance(value, Mapping) else {}
        elif _f == "provenance_ids":
            content[_f] = list(value) if isinstance(value, (list, tuple)) else []
        else:
            content[_f] = "" if value is None else str(value)
    return _sha256_hex(_canonical_json(content))


# Legacy rows (the imported SOUL corpus) predate the proposal ledger, so they
# have no proposal to hash — they are witnessed against the row itself. Both the
# TCB (at witness time) and the agent (at stamp time) compute this identically.
def canonical_row_sha256(table: str, row: Mapping[str, Any]) -> str:
    """SHA-256 over the material identity fields of an existing row.

    ``foundational`` is normalized to a bool from either the hypomnema flag or a
    belief ``tier == 'foundational'``, so the hash binds the row's content and
    tier signal.

    008e-r4 #3: the hash also binds ``agent_id`` (both tables) and, for
    hypomnema, ``person_id`` + ``project_scope`` — extends the "hash binds
    every field apply touches" rule to legacy witness. Without this, a stamped
    hypomnema row could have its scope changed and reconcile would pass clean
    on matching content + tier + decision_ref.
    """
    foundational = bool(row.get("foundational")) or str(row.get("tier", "")) == (
        "foundational"
    )
    content: dict[str, Any] = {
        "table": str(table),
        "row_id": str(row.get("id", row.get("row_id", ""))),
        "content": str(row.get("content", "")),
        "domain": str(row.get("domain", "")),
        "foundational": foundational,
        "agent_id": str(row.get("agent_id", "")),
    }
    if table == "hypomnema_entries":
        content["person_id"] = str(row.get("person_id", ""))
        content["project_scope"] = str(row.get("project_scope", ""))
    return _sha256_hex(_canonical_json(content))


def genesis_prev_hash() -> str:
    """The ``prev_sha256`` the first journal line must carry."""
    return _sha256_hex(_GENESIS_STRING)


def line_hash(line: Mapping[str, Any]) -> str:
    """SHA-256 over a whole journal line, as stored (including its ``prev_sha256``).

    This is the value the next line's ``prev_sha256`` must equal, and the value a
    witnessed target row stores as ``decision_ref``.
    """
    return _sha256_hex(_canonical_json(dict(line)))


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL journal file into a list of line dicts.

    Missing file → ``[]`` (the caller treats an absent journal as fail-closed:
    every identity row degrades to review_only). Blank lines are skipped.
    Mid-file malformation raises ``ValueError`` — a corrupt journal must never
    be silently read as a shorter valid one.

    008i — **torn-tail tolerance.** A malformed FINAL line (and only the last
    one) is a torn append: power loss mid-write is the overwhelmingly likely
    real-world cause, no attacker in the honest threat model can author it,
    and no row can reference a line whose hash never existed. That case
    returns the prefix of good lines and does NOT raise — the classifier
    ``read_journal_classified`` surfaces the torn-tail signal to the caller
    for alerting, without triggering full corruption quarantine. Malformed
    content **anywhere before the tail** stays fatal.
    """
    result = read_journal_classified(path)
    if result.error == "corrupt":
        raise ValueError(result.detail)
    return result.lines


@dataclass
class JournalRead:
    """Classified result of a journal read (008i)."""

    lines: list[dict[str, Any]] = field(default_factory=list)
    error: str = "healthy"  # 'healthy' | 'torn_tail' | 'corrupt' | 'missing'
    detail: str = ""


def read_journal_classified(path: str | Path) -> "JournalRead":
    """Parse a journal, returning a classification distinguishing torn-tail
    from mid-file corruption (008i)."""
    p = Path(path)
    if not p.exists():
        return JournalRead(lines=[], error="missing", detail=f"journal not present: {p}")
    lines: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        # 008k-r13 #1: an unreadable journal (permission denied, I/O error,
        # decode failure) is UNTRUSTWORTHY, not absent — classify it as
        # corrupt so the reconciler fails closed and quarantines every
        # witnessed operational row, exactly as for mid-file malformation.
        # (Absent is a distinct, benign state handled above.)
        return JournalRead(
            lines=[],
            error="corrupt",
            detail=f"journal unreadable: {type(exc).__name__}: {exc}",
        )
    except UnicodeDecodeError as exc:
        return JournalRead(
            lines=[],
            error="corrupt",
            detail=f"journal not valid UTF-8: {exc}",
        )
    # Strip blank lines from the tail-classification (a blank final line is
    # a normal artifact of appending, not a torn write).
    last_content_idx = -1
    for i, raw in enumerate(raw_lines):
        if raw.strip():
            last_content_idx = i
    for lineno, raw in enumerate(raw_lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            # 008i — torn tail: malformed FINAL content line only.
            if lineno - 1 == last_content_idx:
                return JournalRead(
                    lines=lines,
                    error="torn_tail",
                    detail=f"malformed final journal line {lineno}: {exc}",
                )
            return JournalRead(
                lines=lines,
                error="corrupt",
                detail=f"Malformed journal line {lineno}: {exc}",
            )
        if not isinstance(obj, dict):
            if lineno - 1 == last_content_idx:
                return JournalRead(
                    lines=lines,
                    error="torn_tail",
                    detail=f"final journal line {lineno} is not a JSON object",
                )
            return JournalRead(
                lines=lines,
                error="corrupt",
                detail=f"Journal line {lineno} is not a JSON object",
            )
        lines.append(obj)
    return JournalRead(lines=lines, error="healthy")


def verify_chain(lines: Sequence[Mapping[str, Any]]) -> tuple[bool, int]:
    """Verify the ``prev_sha256`` back-links from genesis forward.

    Returns ``(ok, break_index)``. ``break_index`` is the 0-based index of the
    first line whose ``prev_sha256`` does not match (genesis for line 0, the
    previous line's ``line_hash`` otherwise), or ``-1`` when the whole chain is
    intact. An empty journal is a valid (trivially intact) chain.
    """
    expected_prev = genesis_prev_hash()
    for index, line in enumerate(lines):
        if str(line.get("prev_sha256", "")) != expected_prev:
            return (False, index)
        expected_prev = line_hash(line)
    return (True, -1)


def find_decision(
    lines: Sequence[Mapping[str, Any]], proposal_id: str
) -> dict[str, Any] | None:
    """Return the most recent journal line for ``proposal_id``, or ``None``.

    Append-only means a proposal may have several lines (e.g. deferred then
    approved); the last one is the operative decision. The caller still verifies
    the chain up to this line before trusting it.
    """
    match: dict[str, Any] | None = None
    match_index = -1
    for index, line in enumerate(lines):
        if str(line.get("proposal_id", "")) == proposal_id:
            match = dict(line)
            match_index = index
    if match is not None:
        match["_line_index"] = match_index
    return match
