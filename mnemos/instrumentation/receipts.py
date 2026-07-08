"""Validation and failure accounting for Step 1 runtime receipts."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .receipt_kinds import is_registered_receipt_kind

ORIGIN_USER_WITNESSED = "user-witnessed"
ORIGIN_INFERENCE = "inference"
ORIGIN_RETRIEVAL = "retrieval"
ORIGIN_IMPORT = "import"
ORIGIN_STAMPS = frozenset(
    {ORIGIN_USER_WITNESSED, ORIGIN_INFERENCE, ORIGIN_RETRIEVAL, ORIGIN_IMPORT}
)
IMMEDIACY_LIVE = "live"
IMMEDIACY_REMEMBERED = "remembered"
IMMEDIACY_OPERATIONAL = "n-a-operational"
IMMEDIACY_VALUES = frozenset(
    {IMMEDIACY_LIVE, IMMEDIACY_REMEMBERED, IMMEDIACY_OPERATIONAL}
)

class ReceiptValidationError(ValueError):
    """Receipt envelope failed before durable write."""


def validate_origin_stamp(value: str | None, *, required: bool = False) -> str | None:
    """Normalize and validate the Step 1 origin-stamp axis."""

    if value is None:
        if required:
            raise ValueError("origin_stamp is required")
        return None
    stamp = str(value).strip()
    if not stamp:
        if required:
            raise ValueError("origin_stamp is required")
        return None
    if stamp not in ORIGIN_STAMPS:
        raise ValueError(f"Unsupported origin_stamp: {stamp!r}")
    return stamp


def validate_receipt_envelope(
    *,
    kind: str,
    actor: str,
    runtime: str,
    session_id: str,
    engram_refs: list[str] | tuple[str, ...] | None,
    immediacy: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate the full runtime receipt envelope before insertion."""

    clean_kind = _required_nonempty_string(kind, "kind")
    if not is_registered_receipt_kind(clean_kind):
        raise ReceiptValidationError(f"Unregistered receipt kind: {clean_kind}")

    clean_actor = _required_nonempty_string(actor, "actor")
    clean_runtime = _required_nonempty_string(runtime, "runtime")
    if session_id is None:
        raise ReceiptValidationError("session_id is required")
    clean_session_id = str(session_id)
    clean_immediacy = _required_nonempty_string(immediacy, "immediacy")
    if clean_immediacy not in IMMEDIACY_VALUES:
        raise ReceiptValidationError(f"Unsupported immediacy: {clean_immediacy}")
    if engram_refs is None:
        clean_refs: list[str] = []
    elif isinstance(engram_refs, (list, tuple)):
        clean_refs = [str(ref) for ref in engram_refs]
    else:
        raise ReceiptValidationError("engram_refs must be a list")
    if not isinstance(payload, dict):
        raise ReceiptValidationError("payload must be a JSON object")
    try:
        json.dumps(payload, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError(f"payload is not JSON-serializable: {exc}") from exc

    return {
        "kind": clean_kind,
        "actor": clean_actor,
        "runtime": clean_runtime,
        "session_id": clean_session_id,
        "engram_refs": clean_refs,
        "immediacy": clean_immediacy,
        "payload": deepcopy(payload),
    }


def validate_producer_name(producer: str) -> str:
    """Normalize a Step 1 instrumentation producer name."""

    return _required_nonempty_string(producer, "producer")


def _required_nonempty_string(value: str, field_name: str) -> str:
    if value is None:
        raise ReceiptValidationError(f"{field_name} is required")
    cleaned = str(value).strip()
    if not cleaned:
        raise ReceiptValidationError(f"{field_name} is required")
    return cleaned
