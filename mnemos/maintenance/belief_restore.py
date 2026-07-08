"""Receipted restoration for false encoder contradiction revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..importer.operator import _db_path_requires_live_override
from ..instrumentation.receipts import IMMEDIACY_OPERATIONAL, ORIGIN_INFERENCE
from ..store.migration_runner import MigrationError, MigrationRunner
from ..store.migrations import list_migrations
from ..store.sqlite_store import SCHEMA_VERSION, EngramStore


FALSE_CONTRADICTION_PREFIX = "Contradicted by new evidence: "
BELIEF_CONFIDENCE_RESTORE_KIND = "belief_confidence_restore"
RESTORE_REASON = (
    "Restored false encoder contradiction per render-with-dissent-beliefs "
    "section 1a and report 047."
)


@dataclass(frozen=True)
class BeliefRestoreRow:
    belief_id: str
    current_confidence: float
    restored_confidence: float
    false_event_timestamps: list[str]
    trigger_engram_ids: list[str]

    @property
    def false_event_count(self) -> int:
        return len(self.false_event_timestamps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "belief_id": self.belief_id,
            "current_confidence": self.current_confidence,
            "restored_confidence": self.restored_confidence,
            "false_event_count": self.false_event_count,
            "false_event_timestamps": self.false_event_timestamps,
            "trigger_engram_ids": self.trigger_engram_ids,
        }


def restore_false_contradictions(
    db_path: str | Path,
    *,
    apply: bool = False,
    allow_live_db: bool = False,
    actor: str = "mnemos-maintain",
    runtime: str = "mnemos-cli",
    session_id: str = "belief-erosion-restore",
) -> dict[str, Any]:
    """Plan or apply restoration of known false encoder contradiction events.

    Dry-run reads the target through SQLite read-only mode. Apply first
    preflights schema shape read-only, then appends annulling restore events and
    runtime receipts without deleting the false history.
    """

    db = Path(db_path).expanduser()
    if _db_path_requires_live_override(db) and not allow_live_db:
        raise ValueError(
            "mnemos maintain refuses live Mnemos databases without "
            "--allow-live-db and explicit David authorization"
        )

    if apply:
        _preflight_apply_db(db)
        store = EngramStore(db, assume_initialized=True)
        try:
            conn = store._get_conn()
            rows = _plan_rows(conn)
            receipts: list[dict[str, Any]] = []
            for row in rows:
                receipt = _apply_row(
                    store,
                    row,
                    actor=actor,
                    runtime=runtime,
                    session_id=session_id,
                )
                if receipt is not None:
                    receipts.append(receipt)
            return _result(db, "apply", rows, receipts)
        finally:
            store.close()

    read_only_uri = db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(read_only_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = _plan_rows(conn)
    return _result(db, "dry-run", rows, [])


def _preflight_apply_db(db: Path) -> None:
    if not db.exists():
        raise ValueError(f"mnemos maintain refuses missing DB path: {db}")

    read_only_uri = db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(read_only_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required_tables = {"beliefs", "runtime_receipts", "meta"}
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise ValueError(
                "mnemos maintain refuses DB without required tables: "
                + ", ".join(missing_tables)
            )

        schema_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema_row is None or str(schema_row["value"]) != str(SCHEMA_VERSION):
            found = None if schema_row is None else schema_row["value"]
            raise ValueError(
                "mnemos maintain refuses DB with unsupported schema_version "
                f"{found!r}; expected {SCHEMA_VERSION}"
            )

        try:
            _migration_runner_for_db(db).check_not_ahead(conn)
        except MigrationError as exc:
            raise ValueError(
                f"mnemos maintain refuses DB with unsupported schema_migrations: {exc}"
            ) from exc

        required_columns = {
            "beliefs": {"id", "confidence", "last_revised", "revision_history"},
            "runtime_receipts": {
                "receipt_id",
                "ts",
                "actor",
                "runtime",
                "session_id",
                "engram_refs_json",
                "immediacy",
                "kind",
                "payload_json",
            },
        }
        for table, columns in required_columns.items():
            existing = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing_columns = sorted(columns - existing)
            if missing_columns:
                raise ValueError(
                    f"mnemos maintain refuses DB with incomplete {table} table: "
                    + ", ".join(missing_columns)
                )


def _migration_runner_for_db(db: Path) -> MigrationRunner:
    python_versions = [int(m["version"]) for m in list_migrations()]
    if 1 not in python_versions:
        python_versions.append(1)
    return MigrationRunner(db, known_python_versions=python_versions)


def _plan_rows(conn: sqlite3.Connection) -> list[BeliefRestoreRow]:
    rows = conn.execute(
        "SELECT id, confidence, revision_history FROM beliefs ORDER BY id"
    ).fetchall()
    restore_rows: list[BeliefRestoreRow] = []
    for row in rows:
        restore_row = _plan_belief_restore_row(
            belief_id=str(row["id"]),
            current_confidence=float(row["confidence"]),
            revision_history_raw=row["revision_history"],
        )
        if restore_row is not None:
            restore_rows.append(restore_row)
    return restore_rows


def _plan_belief_restore_row(
    *,
    belief_id: str,
    current_confidence: float,
    revision_history_raw: str | None,
) -> BeliefRestoreRow | None:
    history = _decode_history(revision_history_raw)
    if not history:
        return None
    annulled = _annulled_timestamps(history)
    false_events = [
        event
        for event in history
        if _is_false_contradiction_event(event)
        and str(event.get("timestamp")) not in annulled
    ]
    if not false_events:
        return None
    restored = _restored_confidence_from_false_deltas(current_confidence, false_events)
    if restored <= current_confidence:
        raise ValueError(
            "refusing belief confidence restore for "
            f"{belief_id}: restored confidence {restored:.6f} "
            f"must strictly raise current confidence {current_confidence:.6f}"
        )
    trigger_ids = sorted(
        {
            str(event.get("trigger_engram_id"))
            for event in false_events
            if event.get("trigger_engram_id")
        }
    )
    return BeliefRestoreRow(
        belief_id=belief_id,
        current_confidence=current_confidence,
        restored_confidence=restored,
        false_event_timestamps=[str(event.get("timestamp")) for event in false_events],
        trigger_engram_ids=trigger_ids,
    )


def _apply_row(
    store: EngramStore,
    row: BeliefRestoreRow,
    *,
    actor: str,
    runtime: str,
    session_id: str,
) -> dict[str, Any] | None:
    conn = store._get_conn()
    db_row = conn.execute(
        "SELECT confidence, revision_history FROM beliefs WHERE id = ?",
        (row.belief_id,),
    ).fetchone()
    if db_row is None:
        raise ValueError(f"belief not found: {row.belief_id}")

    history_raw = db_row["revision_history"]
    fresh_row = _plan_belief_restore_row(
        belief_id=row.belief_id,
        current_confidence=float(db_row["confidence"]),
        revision_history_raw=history_raw,
    )
    if fresh_row is None:
        return None

    history = _decode_history(history_raw)
    now = datetime.now(timezone.utc).isoformat()
    restore_event = {
        "timestamp": now,
        "old_confidence": fresh_row.current_confidence,
        "new_confidence": fresh_row.restored_confidence,
        "reason": RESTORE_REASON,
        "trigger_engram_id": None,
        "annuls": fresh_row.false_event_timestamps,
    }
    history.append(restore_event)
    updated_history = json.dumps(history, ensure_ascii=True, sort_keys=True)
    try:
        cursor = conn.execute(
            """
            UPDATE beliefs
            SET confidence = ?, last_revised = ?, revision_history = ?
            WHERE id = ? AND confidence = ? AND revision_history = ?
            """,
            (
                fresh_row.restored_confidence,
                now,
                updated_history,
                fresh_row.belief_id,
                fresh_row.current_confidence,
                history_raw,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "belief confidence restore lost compare-and-swap race for "
                f"{fresh_row.belief_id}; retry the maintenance command"
            )
        receipt = store.append_receipt(
            kind=BELIEF_CONFIDENCE_RESTORE_KIND,
            actor=actor,
            runtime=runtime,
            session_id=session_id,
            engram_refs=fresh_row.trigger_engram_ids,
            immediacy=IMMEDIACY_OPERATIONAL,
            payload={
                "origin_stamp": ORIGIN_INFERENCE,
                "belief_id": fresh_row.belief_id,
                "old_confidence": fresh_row.current_confidence,
                "new_confidence": fresh_row.restored_confidence,
                "annuls": fresh_row.false_event_timestamps,
                "reason": RESTORE_REASON,
                "source_spec": "specs/render-with-dissent-beliefs.md section 1a",
                "source_report": (
                    "reports/047-dissent-selfrelevance-review-and-belief-erosion.md"
                ),
            },
        )
    except Exception:
        conn.rollback()
        raise
    return receipt


def _result(
    db: Path,
    mode: str,
    rows: list[BeliefRestoreRow],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "mode": mode,
        "db_path": str(db),
        "beliefs_to_restore": len(rows),
        "false_events_to_annul": sum(row.false_event_count for row in rows),
        "rows": [row.to_dict() for row in rows],
        "receipts": receipts,
    }


def _decode_history(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _annulled_timestamps(history: list[dict[str, Any]]) -> set[str]:
    annulled: set[str] = set()
    for event in history:
        annuls = event.get("annuls")
        if isinstance(annuls, list):
            annulled.update(str(timestamp) for timestamp in annuls)
    return annulled


def _is_false_contradiction_event(event: dict[str, Any]) -> bool:
    reason = str(event.get("reason") or "")
    return reason.startswith(FALSE_CONTRADICTION_PREFIX)


def _restored_confidence_from_false_deltas(
    current_confidence: float,
    false_events: list[dict[str, Any]],
) -> float:
    delta = 0.0
    for event in false_events:
        old_conf = _confidence_value(event, "old_confidence")
        new_conf = _confidence_value(event, "new_confidence")
        reversal = old_conf - new_conf
        if reversal <= 0:
            raise ValueError(
                "refusing belief confidence restore: false contradiction event "
                f"{event.get('timestamp')} is not a downward revision"
            )
        delta += reversal
    return _clamp_belief_confidence(current_confidence + delta)


def _confidence_value(event: dict[str, Any], key: str) -> float:
    try:
        return float(event[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "refusing belief confidence restore: false contradiction event "
            f"{event.get('timestamp')} lacks numeric {key}"
        ) from exc


def _clamp_belief_confidence(value: float) -> float:
    return min(0.99, max(0.0, value))
