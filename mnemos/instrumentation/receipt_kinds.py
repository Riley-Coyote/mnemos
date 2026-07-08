"""Closed receipt-kind manifest for Step 1 runtime receipts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiptKindSpec:
    """Reviewable manifest entry for a durable runtime receipt kind."""

    kind: str
    owner: str
    payload_schema: str
    active: bool = True


RECEIPT_KINDS: dict[str, ReceiptKindSpec] = {
    "affect-state-update": ReceiptKindSpec(
        "affect-state-update", "future-affect", "schemas/receipts/affect-state-update"
    ),
    "retrieval-why": ReceiptKindSpec(
        "retrieval-why", "retrieval", "schemas/receipts/retrieval-why"
    ),
    "recall/stability": ReceiptKindSpec(
        "recall/stability", "future-recall", "schemas/receipts/recall-stability"
    ),
    "schema-mint": ReceiptKindSpec(
        "schema-mint", "future-schema", "schemas/receipts/schema-mint"
    ),
    "reconsolidation-attribution": ReceiptKindSpec(
        "reconsolidation-attribution",
        "future-reconsolidation",
        "schemas/receipts/reconsolidation-attribution",
    ),
    "consolidation-op": ReceiptKindSpec(
        "consolidation-op", "future-consolidation", "schemas/receipts/consolidation-op"
    ),
    "bond-update": ReceiptKindSpec(
        "bond-update", "future-bonds", "schemas/receipts/bond-update"
    ),
    "goal-status": ReceiptKindSpec(
        "goal-status", "future-goals", "schemas/receipts/goal-status"
    ),
    "play-episode": ReceiptKindSpec(
        "play-episode", "future-play", "schemas/receipts/play-episode"
    ),
    "appraisal-verdict": ReceiptKindSpec(
        "appraisal-verdict", "future-appraisal", "schemas/receipts/appraisal-verdict"
    ),
    "stamp-translation": ReceiptKindSpec(
        "stamp-translation", "future-stamps", "schemas/receipts/stamp-translation"
    ),
}


def is_registered_receipt_kind(kind: str) -> bool:
    """Return True only for receipt kinds shipped in the manifest."""

    return kind in RECEIPT_KINDS
