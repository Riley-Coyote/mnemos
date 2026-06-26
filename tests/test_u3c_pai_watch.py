import json
from dataclasses import replace

import pytest

from mnemos.importer import (
    ACTION_DEACTIVATE,
    ACTION_ERROR,
    ACTION_NOOP,
    ACTION_REPAIR,
    ACTION_REVIEW,
    ACTION_TOMBSTONE,
    PaiImportPreview,
    PaiImportSource,
    apply_pai_import,
    apply_pai_watch_update,
    preview_pai_import,
    preview_pai_watch_update,
)
from mnemos.store.sqlite_store import EngramStore


def _source(kind: str, text: str) -> PaiImportSource:
    return PaiImportSource(
        job_id="u3c-job",
        source_path=f"/pai/{kind}.md",
        source_kind=kind,
        source_text=text,
        original_substrate="claude-opus-4-6",
        original_timestamp=1710000000,
    )


def _row_with_action(preview, action: str):
    return next(row for row in preview.rows if row.action == action)


def test_u3c_removed_engram_section_tombstones_target_idempotently(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        removed_id = next(row for row in first.rows if row.source_anchor == "h:b:001").target_id

        removed = replace(source, source_text="# A\nalpha")
        preview = preview_pai_watch_update(store, [removed])

        assert preview.counts == {ACTION_NOOP: 1, ACTION_TOMBSTONE: 1}
        tombstone = _row_with_action(preview, ACTION_TOMBSTONE)
        assert tombstone.target_id == removed_id

        result = apply_pai_watch_update(store, preview)
        assert result.counts == {ACTION_NOOP: 1, ACTION_TOMBSTONE: 1}

        row = store._get_conn().execute(
            "SELECT state, softening_protected, decay_protected FROM engrams WHERE id = ?",
            (removed_id,),
        ).fetchone()
        assert row["state"] == "archived"
        assert bool(row["softening_protected"]) is True
        assert bool(row["decay_protected"]) is True
        archive = store._get_conn().execute(
            "SELECT archive_reason FROM archive WHERE id = ?",
            (removed_id,),
        ).fetchone()
        assert archive["archive_reason"] == "pai_import_tombstone:u3c-job"
        row_map = store._get_conn().execute(
            "SELECT tombstone_at FROM pai_import_row_map WHERE target_id = ?",
            (removed_id,),
        ).fetchone()
        assert row_map["tombstone_at"] is not None
        version = store._get_conn().execute(
            "SELECT content_snapshot, change_reason FROM versions WHERE engram_id = ?",
            (removed_id,),
        ).fetchone()
        assert version["content_snapshot"] == "# B\nbravo"
        assert version["change_reason"] == "pai_import_tombstone:u3c-job"
        tombstone_events = store._get_conn().execute(
            """
            SELECT COUNT(*) FROM pai_import_events
            WHERE target_id = ? AND action = ?
            """,
            (removed_id, ACTION_TOMBSTONE),
        ).fetchone()[0]
        assert tombstone_events == 1

        replay = preview_pai_watch_update(store, [removed])
        apply_pai_watch_update(store, replay)
        version_count = store._get_conn().execute(
            "SELECT COUNT(*) FROM versions WHERE engram_id = ?",
            (removed_id,),
        ).fetchone()[0]
        assert version_count == 1
        replayed_tombstone_events = store._get_conn().execute(
            """
            SELECT COUNT(*) FROM pai_import_events
            WHERE target_id = ? AND action = ?
            """,
            (removed_id, ACTION_TOMBSTONE),
        ).fetchone()[0]
        assert replayed_tombstone_events == 1
    finally:
        store.close()


def test_u3c_returned_pai_tombstoned_engram_reactivates(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        removed_id = next(row for row in first.rows if row.source_anchor == "h:b:001").target_id

        removed = replace(source, source_text="# A\nalpha")
        apply_pai_watch_update(store, preview_pai_watch_update(store, [removed]))

        returned = preview_pai_watch_update(store, [source])
        assert returned.counts == {ACTION_NOOP: 1, ACTION_REPAIR: 1}
        repair = _row_with_action(returned, ACTION_REPAIR)
        assert repair.target_id == removed_id
        assert "reactivating mapped engram" in repair.reason

        result = apply_pai_watch_update(store, returned)
        assert result.counts == {ACTION_NOOP: 1, ACTION_REPAIR: 1}
        row = store._get_conn().execute(
            "SELECT state FROM engrams WHERE id = ?",
            (removed_id,),
        ).fetchone()
        assert row["state"] == "active"
        row_map = store._get_conn().execute(
            "SELECT tombstone_at FROM pai_import_row_map WHERE target_id = ?",
            (removed_id,),
        ).fetchone()
        assert row_map["tombstone_at"] is None
        archive = store._get_conn().execute(
            "SELECT 1 FROM archive WHERE id = ?",
            (removed_id,),
        ).fetchone()
        assert archive is None
    finally:
        store.close()


def test_u3c_legacy_pai_tombstoned_engram_reactivates_without_row_map_tombstone(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        legacy_id = next(row for row in first.rows if row.source_anchor == "h:b:001").target_id
        conn = store._get_conn()
        engram = conn.execute("SELECT * FROM engrams WHERE id = ?", (legacy_id,)).fetchone()
        conn.execute("UPDATE engrams SET state = 'archived' WHERE id = ?", (legacy_id,))
        conn.execute(
            """
            INSERT INTO archive (
                id, content, content_at_encoding, kind, tags,
                archived_at, archive_reason, final_accessibility
            ) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?)
            """,
            (
                legacy_id,
                engram["content"],
                engram["content_at_encoding"],
                engram["kind"],
                engram["tags"],
                "pai_import_tombstone:u3c-job",
                engram["accessibility"],
            ),
        )
        conn.execute(
            "UPDATE pai_import_row_map SET tombstone_at = NULL WHERE target_id = ?",
            (legacy_id,),
        )
        conn.commit()

        returned = preview_pai_watch_update(store, [source])
        assert returned.counts == {ACTION_NOOP: 1, ACTION_REPAIR: 1}
        repair = _row_with_action(returned, ACTION_REPAIR)
        assert repair.target_id == legacy_id
        assert "reactivating mapped engram" in repair.reason

        result = apply_pai_watch_update(store, returned)
        assert result.counts == {ACTION_NOOP: 1, ACTION_REPAIR: 1}
        row = conn.execute("SELECT state FROM engrams WHERE id = ?", (legacy_id,)).fetchone()
        assert row["state"] == "active"
        archive = conn.execute("SELECT 1 FROM archive WHERE id = ?", (legacy_id,)).fetchone()
        assert archive is None
    finally:
        store.close()


def test_u3c_manually_archived_engram_still_refuses_reactivation(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        store._get_conn().execute(
            "UPDATE engrams SET state = 'archived' WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        preview = preview_pai_watch_update(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "refusing implicit PAI reactivation" in preview.rows[0].reason
    finally:
        store.close()


def test_u3c_empty_watched_source_tombstones_all_mapped_rows(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        imported_id = first.rows[0].target_id

        empty = replace(source, source_text="")
        preview = preview_pai_watch_update(store, [empty])

        assert preview.counts == {ACTION_TOMBSTONE: 1}
        tombstone = _row_with_action(preview, ACTION_TOMBSTONE)
        assert tombstone.target_id == imported_id

        apply_pai_watch_update(store, preview)
        row = store._get_conn().execute(
            "SELECT state FROM engrams WHERE id = ?",
            (imported_id,),
        ).fetchone()
        assert row["state"] == "archived"
    finally:
        store.close()


def test_u3c_empty_watched_source_without_prior_import_applies_as_noop(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        preview = preview_pai_watch_update(store, [_source("identity_kernel", "")])

        assert preview.rows == ()
        assert preview.counts == {}

        result = apply_pai_watch_update(store, preview)
        assert result.job_id == "u3c-job"
        assert result.rows == ()
        assert result.counts == {}
    finally:
        store.close()


def test_u3c_removed_hypomnema_section_deactivates_without_successor(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("hypomnema", "# Keep\nstill live\n\n# Drop\nretire this")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        removed_id = next(row for row in first.rows if row.source_anchor == "h:drop:001").target_id

        removed = replace(source, source_text="# Keep\nstill live")
        preview = preview_pai_watch_update(store, [removed])
        assert preview.counts == {ACTION_NOOP: 1, ACTION_DEACTIVATE: 1}

        apply_pai_watch_update(store, preview)
        entry = store.get_hypomnema_entry(
            removed_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert entry is not None
        assert entry["active"] is False
        assert entry["superseded_by"] is None
        assert entry["revision_count"] == 1
        assert entry["revisions"][0]["reason"] == "deactivated: pai_import_deactivate:u3c-job"

        replay = preview_pai_watch_update(store, [removed])
        apply_pai_watch_update(store, replay)
        replayed = store.get_hypomnema_entry(
            removed_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert replayed["revision_count"] == 1
    finally:
        store.close()


def test_u3c_removed_belief_section_flags_review_without_changing_content(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source(
            "beliefs",
            "David context is foundational.\n\nReports change family narratives.",
        )
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        removed_id = next(row for row in first.rows if row.source_anchor == "block:002").target_id

        before = store._get_conn().execute(
            "SELECT confidence FROM beliefs WHERE id = ?",
            (removed_id,),
        ).fetchone()
        store._get_conn().execute(
            "UPDATE beliefs SET needs_review = 0, confidence_pending_review = 0 WHERE id = ?",
            (removed_id,),
        )
        store._get_conn().commit()

        removed = replace(source, source_text="David context is foundational.")
        preview = preview_pai_watch_update(store, [removed])
        assert preview.counts == {ACTION_NOOP: 1, ACTION_REVIEW: 1}

        apply_pai_watch_update(store, preview)
        row = store._get_conn().execute(
            """
            SELECT content, confidence, needs_review, confidence_pending_review,
                   revision_history
            FROM beliefs
            WHERE id = ?
            """,
            (removed_id,),
        ).fetchone()
        assert row["content"] == "Reports change family narratives."
        assert row["confidence"] == before["confidence"]
        assert bool(row["needs_review"]) is True
        assert bool(row["confidence_pending_review"]) is True
        revisions = json.loads(row["revision_history"])
        assert revisions[-1]["reason"] == "pai_import_review:u3c-job"

        apply_pai_watch_update(store, preview_pai_watch_update(store, [removed]))
        replayed_revisions = store._get_conn().execute(
            "SELECT revision_history FROM beliefs WHERE id = ?",
            (removed_id,),
        ).fetchone()["revision_history"]
        assert len(json.loads(replayed_revisions)) == 1
    finally:
        store.close()


def test_u3c_watch_apply_rejects_stale_lifecycle_preview_after_target_drift(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        apply_pai_import(store, preview_pai_import(store, [source]))

        removed = replace(source, source_text="# A\nalpha")
        preview = preview_pai_watch_update(store, [removed])
        tombstone = _row_with_action(preview, ACTION_TOMBSTONE)
        store._get_conn().execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            ("operator edit after preview", tombstone.target_id),
        )
        store._get_conn().commit()

        with pytest.raises(ValueError, match="diverged from importer baseline"):
            apply_pai_watch_update(store, preview)

        row = store._get_conn().execute(
            "SELECT state, content FROM engrams WHERE id = ?",
            (tombstone.target_id,),
        ).fetchone()
        assert row["state"] == "active"
        assert row["content"] == "operator edit after preview"
    finally:
        store.close()


def test_u3c_lifecycle_apply_revalidates_profile_fields(tmp_path):
    store = EngramStore(tmp_path / "u3c.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        apply_pai_import(store, preview_pai_import(store, [source]))

        removed = replace(source, source_text="# A\nalpha")
        preview = preview_pai_watch_update(store, [removed])
        forged_rows = tuple(
            replace(row, tags=("wrong",)) if row.action == ACTION_TOMBSTONE else row
            for row in preview.rows
        )

        with pytest.raises(ValueError, match="tags do not match source profile"):
            apply_pai_watch_update(
                store,
                PaiImportPreview(job_id=preview.job_id, rows=forged_rows),
            )
    finally:
        store.close()
