import json
import sqlite3
from dataclasses import replace

import pytest

from mnemos.core.engram import Engram
from mnemos.importer import (
    ACTION_DEACTIVATE,
    ACTION_ERROR,
    ACTION_INSERT,
    ACTION_NOOP,
    ACTION_PENDING,
    ACTION_REPAIR,
    ACTION_REVIEW,
    ACTION_TOMBSTONE,
    ACTION_UPDATE,
    PaiImportPreview,
    PaiImportSource,
    apply_pai_import,
    preview_pai_import,
    split_pai_source,
    split_beliefs,
    split_david_context,
    split_growth_substrate,
    split_hypomnema,
    split_identity_kernel,
)
from mnemos.store.migrations import upsert_pai_import_row
from mnemos.store.sqlite_store import EngramStore


def _source(kind: str, text: str) -> PaiImportSource:
    return PaiImportSource(
        job_id="u3b-job",
        source_path=f"/pai/{kind}.md",
        source_kind=kind,
        source_text=text,
        original_substrate="claude-opus-4-6",
        original_timestamp=1710000000,
    )


def test_u3b_five_splitters_emit_deterministic_contract_rows():
    text = "# Core\nI am Oliver.\n\n# Invariant\nRead before writing."
    identity = split_identity_kernel(_source("identity_kernel", text))
    david = split_david_context(_source("david_context", text))
    growth = split_growth_substrate(_source("growth_substrate", text))
    beliefs = split_beliefs(_source("beliefs", text))
    hypomnema = split_hypomnema(_source("hypomnema", text))

    assert [row.source_anchor for row in identity] == ["h:core:001", "h:invariant:001"]
    assert identity[0].target_table == "engrams"
    assert identity[0].tags == ("pai-import", "identity-kernel")
    assert identity[0].softening_protected is True
    assert identity[0].decay_protected is True
    assert identity[0].voice_exemplar_eligible is False
    assert identity[0].consolidation_authorized is False
    assert identity[0].original_substrate == "claude-opus-4-6"
    assert identity[0].original_timestamp == 1710000000

    assert david[0].tags == ("pai-import", "david-context")
    assert growth[0].tags == ("pai-import", "growth-substrate")
    assert beliefs[0].target_table == "beliefs"
    assert beliefs[0].tier == "operational"
    assert hypomnema[0].target_table == "hypomnema_entries"

    repeat = split_identity_kernel(_source("identity_kernel", text))
    assert [row.target_id for row in repeat] == [row.target_id for row in identity]
    assert [row.source_hash for row in repeat] == [row.source_hash for row in identity]


def test_u3b_heading_splitter_preserves_preamble_text():
    rows = split_identity_kernel(
        _source("identity_kernel", "Preamble survives.\n\n# Core\nI am Oliver.")
    )

    assert [row.source_anchor for row in rows] == ["preamble:001", "h:core:001"]
    assert rows[0].content == "Preamble survives."
    assert rows[1].content == "# Core\nI am Oliver."


def test_u3b_heading_anchors_do_not_renumber_unrelated_slugs():
    before = split_identity_kernel(_source("identity_kernel", "# Core\nI am Oliver."))
    after = split_identity_kernel(
        _source("identity_kernel", "# New\nnew material\n\n# Core\nI am Oliver.")
    )

    assert before[0].source_anchor == "h:core:001"
    assert after[1].source_anchor == "h:core:001"
    assert before[0].target_id == after[1].target_id


def test_u3b_duplicate_heading_slugs_use_ordinal_anchors():
    """U3b hardening B4-u3c-1: SOUL files routinely repeat H2s
    (Voce / Note / Stato). Raising on duplicate broke watcher imports and
    real source files. Ordinal numbering preserves stable per-file ordering
    while keeping anchor uniqueness."""
    rows = split_identity_kernel(
        _source("identity_kernel", "# Core\nA\n\n# Core\nB\n\n# Core\nC")
    )
    anchors = [row.source_anchor for row in rows]
    assert anchors == ["h:core:001", "h:core:002", "h:core:003"]
    # Anchors disambiguate the target_ids — three distinct rows, no clobber.
    target_ids = {row.target_id for row in rows}
    assert len(target_ids) == 3


def test_u3b_dispatcher_covers_all_source_kinds():
    for kind in (
        "identity_kernel",
        "david_context",
        "growth_substrate",
        "beliefs",
        "hypomnema",
    ):
        [row] = split_pai_source(_source(kind, "one block"))
        assert row.source_kind == kind


def test_u3b_named_splitters_reject_wrong_source_kind():
    with pytest.raises(ValueError, match="requires source_kind='beliefs'"):
        split_beliefs(_source("identity_kernel", "I am not a belief source."))


def test_u3b_preview_apply_and_rerun_noop(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_INSERT: 1}

        result = apply_pai_import(store, preview)
        assert result.counts == {ACTION_INSERT: 1}

        row = preview.rows[0]
        engram = store.get_engram(row.target_id)
        assert engram is not None
        assert engram.content == "# Core\nI am Oliver."
        assert engram.owner_agent_id == "oliver"
        assert engram.softening_protected is True
        assert engram.decay_protected is True
        assert engram.voice_exemplar_eligible is False
        assert engram.consolidation_authorized is False
        assert engram.original_substrate == "claude-opus-4-6"
        assert engram.original_timestamp == 1710000000

        second_preview = preview_pai_import(store, [source])
        assert second_preview.counts == {ACTION_NOOP: 1}
        second_result = apply_pai_import(store, second_preview)
        assert second_result.counts == {ACTION_NOOP: 1}
    finally:
        store.close()


def test_u3b_apply_requires_explicit_preview(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        with pytest.raises(TypeError, match="requires a PaiImportPreview"):
            apply_pai_import(store, [_source("identity_kernel", "# Core\nI am Oliver.")])
    finally:
        store.close()


def test_u3b_apply_revalidates_preview_rows_against_profiles(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        row = preview_pai_import(
            store, [_source("identity_kernel", "# Core\nI am Oliver.")]
        ).rows[0]
        forged = replace(
            row,
            agent_id="riley",
            person_id="riley",
            project_scope="not-pai",
            softening_protected=False,
            decay_protected=False,
            consolidation_authorized=True,
        )

        with pytest.raises(ValueError, match="restricted to oliver/david/pai"):
            apply_pai_import(store, PaiImportPreview(job_id="u3b-job", rows=(forged,)))
        assert store.get_engram(row.target_id) is None
    finally:
        store.close()


def test_u3b_source_identity_is_canonicalized_before_hashing_and_mapping(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        padded = PaiImportSource(
            job_id=" u3b-job ",
            source_path=" /pai/identity_kernel.md ",
            source_kind=" identity_kernel ",
            source_text="# Core\nI am Oliver.",
            agent_id=" oliver ",
            person_id=" david ",
            project_scope=" pai ",
            original_substrate=" claude-opus-4-6 ",
            original_timestamp=1710000000,
        )
        clean = _source("identity_kernel", "# Core\nI am Oliver.")

        padded_preview = preview_pai_import(store, [padded])
        clean_preview = preview_pai_import(store, [clean])
        assert padded_preview.rows[0].target_id == clean_preview.rows[0].target_id

        apply_pai_import(store, padded_preview)
        assert preview_pai_import(store, [clean]).counts == {ACTION_NOOP: 1}
    finally:
        store.close()


def test_u3b_rejects_non_pai_scope(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = replace(_source("identity_kernel", "scope"), person_id="riley")
        with pytest.raises(ValueError, match="restricted to oliver/david/pai"):
            preview_pai_import(store, [source])
    finally:
        store.close()


def test_u3b_changed_source_updates_same_target(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        first_hash = first.rows[0].source_hash

        changed = replace(source, source_text="# Core\nI am Oliver, David's agent.")
        changed_preview = preview_pai_import(store, [changed])

        assert changed_preview.counts == {ACTION_UPDATE: 1}
        assert changed_preview.rows[0].target_id == target_id
        assert changed_preview.rows[0].source_hash != first_hash

        apply_pai_import(store, changed_preview)
        engram = store.get_engram(target_id)
        assert engram is not None
        assert engram.content == "# Core\nI am Oliver, David's agent."
        assert engram.content_at_encoding == "# Core\nI am Oliver."

        row_map = store._get_conn().execute(
            """
            SELECT target_id, source_hash FROM pai_import_row_map
            WHERE job_id = ? AND source_path = ? AND source_anchor = ?
            """,
            ("u3b-job", "/pai/identity_kernel.md", "h:core:001"),
        ).fetchone()
        assert row_map["target_id"] == target_id
        assert row_map["source_hash"] == changed_preview.rows[0].source_hash
    finally:
        store.close()


def test_u3b_metadata_only_source_change_updates_same_target(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)

        changed = replace(source, original_timestamp=1710000001)
        changed_preview = preview_pai_import(store, [changed])
        assert changed_preview.counts == {ACTION_UPDATE: 1}
        assert changed_preview.rows[0].target_id == first.rows[0].target_id

        apply_pai_import(store, changed_preview)
        engram = store.get_engram(first.rows[0].target_id)
        assert engram.original_timestamp == 1710000001
    finally:
        store.close()


def test_u3b_stale_update_preview_cannot_overwrite_newer_import(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nA")
        apply_pai_import(store, preview_pai_import(store, [source]))
        update_b = preview_pai_import(store, [replace(source, source_text="# Core\nB")])
        update_c = preview_pai_import(store, [replace(source, source_text="# Core\nC")])
        apply_pai_import(store, update_c)

        with pytest.raises(ValueError, match="preview is stale"):
            apply_pai_import(store, update_b)

        loaded = store.get_engram(update_c.rows[0].target_id)
        assert loaded is not None
        assert loaded.content == "# Core\nC"
    finally:
        store.close()


def test_u3b_stale_update_preview_rejects_external_target_drift(tmp_path):
    """The drift check at apply time catches operator hand-edits that landed
    between preview and apply. Prior assertion matched the generic
    'preview is stale' message; v5 adds content-baseline drift detection that
    produces a more specific error message naming the failure mode.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nA")
        apply_pai_import(store, preview_pai_import(store, [source]))
        update_preview = preview_pai_import(
            store, [replace(source, source_text="# Core\nB")]
        )
        target_id = update_preview.rows[0].target_id

        store._get_conn().execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            ("external drift after preview", target_id),
        )
        store._get_conn().commit()

        with pytest.raises(ValueError, match="diverged from importer baseline"):
            apply_pai_import(store, update_preview)
        edited = store.get_engram(target_id)
        assert edited is not None
        assert edited.content == "external drift after preview"
    finally:
        store.close()


def test_u3b_insert_preview_rejects_target_that_appears_after_preview(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        preview = preview_pai_import(
            store, [_source("identity_kernel", "# Core\nI am Oliver.")]
        )
        target_id = preview.rows[0].target_id
        store.save_engram(Engram(id=target_id, content="preexisting row"))

        with pytest.raises(ValueError, match="without a PAI row map"):
            apply_pai_import(store, preview)
        assert store.get_engram(target_id).content == "preexisting row"
    finally:
        store.close()


def test_u3b_removed_source_sections_are_preview_errors(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# A\nalpha\n\n# B\nbravo")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)

        removed = replace(source, source_text="# A\nalpha")
        preview = preview_pai_import(store, [removed])
        assert preview.counts == {ACTION_NOOP: 1, ACTION_ERROR: 1}
        assert "absent from current PAI import batch" in preview.rows[-1].reason
        with pytest.raises(ValueError, match="absent from current PAI import batch"):
            apply_pai_import(store, preview)
    finally:
        store.close()


def test_u3b_deleted_target_refuses_implicit_resurrection(tmp_path):
    """U3b hardening B1-state-3: hard-DELETE on a mapped target sets
    tombstone_at via the AFTER DELETE trigger (v5). Next preview must classify
    as ACTION_ERROR — silent resurrection on re-import was the prior bug. To
    re-import, the operator must clear tombstone_at explicitly."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store.delete_engram(target_id)

        # tombstone_at trigger should have fired
        tombstone = store._get_conn().execute(
            "SELECT tombstone_at FROM pai_import_row_map WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        assert tombstone is not None
        assert tombstone[0] is not None, "AFTER DELETE trigger must set tombstone_at"

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "tombstone" in preview.rows[0].reason
        with pytest.raises(ValueError, match="tombstone"):
            apply_pai_import(store, preview)
        # Target stays deleted — no implicit resurrection
        assert store.get_engram(target_id) is None
    finally:
        store.close()


def test_u3b_missing_target_without_tombstone_repairs(tmp_path):
    """REPAIR-from-truly-missing still works when the row was never tracked
    by a tombstone (pre-v5 row-map entries, or an operator who explicitly
    cleared the tombstone after reconciliation).
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        # Simulate the legitimate missing-without-tombstone case: delete the
        # target row, then clear the tombstone (operator-level reconciliation).
        store.delete_engram(target_id)
        store._get_conn().execute(
            "UPDATE pai_import_row_map SET tombstone_at = NULL WHERE target_id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        repair_preview = preview_pai_import(store, [source])
        assert repair_preview.counts == {ACTION_REPAIR: 1}

        apply_pai_import(store, repair_preview)
        repaired = store.get_engram(target_id)
        assert repaired is not None
        assert repaired.content == "# Core\nI am Oliver."
    finally:
        store.close()


def test_u3b_archived_mapped_engram_refuses_implicit_reactivation(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nA")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        store._get_conn().execute(
            "UPDATE engrams SET state = 'archived' WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        changed = replace(source, source_text="# Core\nB")
        preview = preview_pai_import(store, [changed])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "refusing implicit PAI reactivation" in preview.rows[0].reason
        with pytest.raises(ValueError, match="implicit PAI reactivation"):
            apply_pai_import(store, preview)
        state = store._get_conn().execute(
            "SELECT state FROM engrams WHERE id = ?",
            (target_id,),
        ).fetchone()[0]
        assert state == "archived"
    finally:
        store.close()


def test_u3b_stale_noop_preview_rejects_later_target_drift(tmp_path):
    """NOOP previews must also fail at apply time if the target drifts. The
    v5 drift check produces a content-baseline-divergence error rather than
    the generic stale message.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        noop_preview = preview_pai_import(store, [source])
        target_id = noop_preview.rows[0].target_id

        store._get_conn().execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            ("corrupted after preview", target_id),
        )
        store._get_conn().commit()

        with pytest.raises(ValueError, match="diverged from importer baseline"):
            apply_pai_import(store, noop_preview)
        edited = store.get_engram(target_id)
        assert edited is not None
        assert edited.content == "corrupted after preview"
    finally:
        store.close()


def test_u3b_corrupt_target_json_classifies_repair_not_crash(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store._get_conn().execute(
            "UPDATE engrams SET tags = ? WHERE id = ?",
            ("not-json", target_id),
        )
        store._get_conn().commit()

        repair_preview = preview_pai_import(store, [source])
        assert repair_preview.counts == {ACTION_REPAIR: 1}
        apply_pai_import(store, repair_preview)
        assert store.get_engram(target_id).tags == ["pai-import", "identity-kernel"]
    finally:
        store.close()


def test_u3b_target_content_drift_refuses_clobber_on_operator_edit(tmp_path):
    """U3b hardening B1-state-2/4 + B5-audit-13: external edits to imported
    content are detected against pai_import_row_map.content_at_last_import
    and refused. Prior behavior was to silently REPAIR/UPDATE, clobbering the
    operator's edit. The operator must reconcile manually before re-import.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store._get_conn().execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            ("corrupted outside importer", target_id),
        )
        store._get_conn().commit()

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "diverged from importer baseline" in preview.rows[0].reason
        assert "operator hand-edit detected" in preview.rows[0].reason
        with pytest.raises(ValueError, match="diverged from importer baseline"):
            apply_pai_import(store, preview)
        # Hand-edit preserved — no silent clobber
        edited = store.get_engram(target_id)
        assert edited is not None
        assert edited.content == "corrupted outside importer"
    finally:
        store.close()


def test_u3b_belief_and_hypomnema_projection_drift_repairs(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        belief_source = _source("beliefs", "David context is foundational.")
        belief_preview = preview_pai_import(store, [belief_source])
        apply_pai_import(store, belief_preview)
        belief_id = belief_preview.rows[0].target_id
        store._get_conn().execute(
            "UPDATE beliefs SET tier = ? WHERE id = ?",
            ("tactical", belief_id),
        )
        store._get_conn().commit()
        repair_belief = preview_pai_import(store, [belief_source])
        assert repair_belief.counts == {ACTION_REPAIR: 1}
        apply_pai_import(store, repair_belief)
        [belief] = store.get_beliefs(
            agent_id="oliver",
            domain="identity",
            include_pending_review=True,
        )
        assert belief.tier == "operational"

        hypo_source = replace(
            _source("hypomnema", "Continuity belongs in scoped memory."),
            job_id="u3b-hypomnema-job",
        )
        hypo_preview = preview_pai_import(store, [hypo_source])
        apply_pai_import(store, hypo_preview)
        hypo_id = hypo_preview.rows[0].target_id
        store._get_conn().execute(
            "UPDATE hypomnema_entries SET tags_json = ? WHERE id = ?",
            (json.dumps(["wrong"]), hypo_id),
        )
        store._get_conn().commit()
        repair_hypo = preview_pai_import(store, [hypo_source])
        assert repair_hypo.counts == {ACTION_REPAIR: 1}
        apply_pai_import(store, repair_hypo)
        repaired = store.get_hypomnema_entry(
            hypo_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert repaired["tags"] == ["pai-import", "hypomnema"]
    finally:
        store.close()


def test_u3b_target_projection_drift_repairs_protection_flags(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store._get_conn().execute(
            """
            UPDATE engrams
            SET softening_protected = 0, decay_protected = 0
            WHERE id = ?
            """,
            (target_id,),
        )
        store._get_conn().commit()

        repair_preview = preview_pai_import(store, [source])
        assert repair_preview.counts == {ACTION_REPAIR: 1}
        apply_pai_import(store, repair_preview)
        repaired = store.get_engram(target_id)
        assert repaired.softening_protected is True
        assert repaired.decay_protected is True
    finally:
        store.close()


def test_u3b_engram_updates_preserve_non_importer_state_and_add_version(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nA")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        store._get_conn().execute(
            """
            UPDATE engrams
            SET access_count = 7,
                reconsolidation_count = 3,
                created_at = 'original-created',
                last_accessed = 'original-accessed'
            WHERE id = ?
            """,
            (target_id,),
        )
        store._get_conn().commit()

        changed = preview_pai_import(
            store, [replace(source, source_text="# Core\nB")]
        )
        apply_pai_import(store, changed)
        row = store._get_conn().execute(
            """
            SELECT content, access_count, reconsolidation_count, created_at,
                   last_accessed
            FROM engrams
            WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        assert row["content"] == "# Core\nB"
        assert row["access_count"] == 7
        assert row["reconsolidation_count"] == 3
        assert row["created_at"] == "original-created"
        assert row["last_accessed"] == "original-accessed"
        version = store._get_conn().execute(
            "SELECT content_snapshot FROM versions WHERE engram_id = ?",
            (target_id,),
        ).fetchone()
        assert version["content_snapshot"] == "# Core\nA"
    finally:
        store.close()


def test_u3b_belief_updates_preserve_audit_state(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David context is foundational.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id
        store._get_conn().execute(
            """
            UPDATE beliefs
            SET created_at = 'original-created',
                last_challenged = 'original-challenged',
                revision_history = ?,
                superseded_by = 'belief_next',
                supporting_engram_ids = ?
            WHERE id = ?
            """,
            (
                json.dumps([{"reason": "existing"}]),
                json.dumps(["engram_support"]),
                target_id,
            ),
        )
        store._get_conn().commit()

        changed = replace(source, source_text="David context remains foundational.")
        preview = preview_pai_import(store, [changed])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "superseded" in preview.rows[0].reason

        store._get_conn().execute(
            "UPDATE beliefs SET superseded_by = NULL WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()
        preview = preview_pai_import(store, [changed])
        apply_pai_import(store, preview)
        row = store._get_conn().execute(
            """
            SELECT content, created_at, last_challenged, revision_history,
                   supporting_engram_ids, superseded_by
            FROM beliefs
            WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        assert row["content"] == "David context remains foundational."
        assert row["created_at"] == "original-created"
        assert row["last_challenged"] == "original-challenged"
        assert json.loads(row["supporting_engram_ids"]) == ["engram_support"]
        assert row["superseded_by"] is None
        revisions = json.loads(row["revision_history"])
        assert revisions[0]["reason"] == "existing"
        assert revisions[1]["reason"] == "pai_import_update"
    finally:
        store.close()


def test_u3b_preexisting_remap_is_preview_error_and_apply_refuses(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        row = preview_pai_import(store, [source]).rows[0]

        upsert_pai_import_row(
            store._get_conn(),
            job_id=row.job_id,
            source_path=row.source_path,
            source_anchor=row.source_anchor,
            target_table=row.target_table,
            target_id="engram_elsewhere",
            source_hash=row.source_hash,
        )
        store._get_conn().commit()

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "refusing remap" in preview.rows[0].reason

        with pytest.raises(ValueError, match="refusing remap"):
            apply_pai_import(store, preview)
    finally:
        store.close()


def test_u3b_preview_does_not_create_missing_schema_objects(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        conn = store._get_conn()
        conn.execute("DROP TABLE pai_import_row_map")
        conn.commit()

        with pytest.raises(sqlite3.OperationalError, match="pai_import_row_map"):
            preview_pai_import(
                store, [_source("identity_kernel", "# Core\nI am Oliver.")]
            )
        missing = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'pai_import_row_map'
            """
        ).fetchone()
        assert missing is None
    finally:
        store.close()


def test_u3b_apply_preflights_stale_preview_before_target_write(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        preview = preview_pai_import(store, [source])
        row = preview.rows[0]

        upsert_pai_import_row(
            store._get_conn(),
            job_id=row.job_id,
            source_path=row.source_path,
            source_anchor=row.source_anchor,
            target_table=row.target_table,
            target_id="engram_elsewhere",
            source_hash=row.source_hash,
        )
        store._get_conn().commit()

        with pytest.raises(ValueError, match="refusing remap"):
            apply_pai_import(store, preview)
        assert store.get_engram(row.target_id) is None
    finally:
        store.close()


def test_u3b_apply_rolls_back_entire_mixed_target_batch_when_late_row_map_fails(
    tmp_path, monkeypatch
):
    import mnemos.importer.pai as pai

    store = EngramStore(tmp_path / "u3b.db")
    try:
        sources = [
            _source("identity_kernel", "identity"),
            replace(_source("beliefs", "belief"), source_path="/pai/beliefs.md"),
            replace(_source("hypomnema", "hypo"), source_path="/pai/hypo.md"),
        ]
        preview = preview_pai_import(store, sources)
        calls = {"count": 0}
        real_upsert = pai.upsert_pai_import_row

        def fail_second(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("forced second row-map failure")
            return real_upsert(*args, **kwargs)

        monkeypatch.setattr(pai, "upsert_pai_import_row", fail_second)
        with pytest.raises(RuntimeError, match="forced second row-map failure"):
            apply_pai_import(store, preview)

        conn = store._get_conn()
        for table in ("engrams", "beliefs", "hypomnema_entries", "pai_import_row_map"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()


def test_u3b_apply_rolls_back_target_when_row_map_write_fails(tmp_path, monkeypatch):
    import mnemos.importer.pai as pai

    store = EngramStore(tmp_path / "u3b.db")
    try:
        preview = preview_pai_import(
            store, [_source("identity_kernel", "# Core\nI am Oliver.")]
        )
        target_id = preview.rows[0].target_id

        def fail_upsert(*args, **kwargs):
            raise RuntimeError("forced row-map failure")

        monkeypatch.setattr(pai, "upsert_pai_import_row", fail_upsert)
        with pytest.raises(RuntimeError, match="forced row-map failure"):
            apply_pai_import(store, preview)

        assert store.get_engram(target_id) is None
        count = store._get_conn().execute(
            "SELECT COUNT(*) FROM pai_import_row_map"
        ).fetchone()[0]
        assert count == 0
    finally:
        store.close()


def test_u3b_apply_rejects_unpreviewed_pending_rows(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        row = preview_pai_import(store, [source]).rows[0]
        pending = PaiImportPreview(
            job_id="u3b-job",
            rows=(replace(row, action=ACTION_PENDING),),
        )

        with pytest.raises(ValueError, match="requires previewed rows"):
            apply_pai_import(store, pending)
    finally:
        store.close()


def test_u3b_belief_preview_apply_noop_and_update(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David context is foundational.")
        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_INSERT: 1}
        apply_pai_import(store, preview)
        target_id = preview.rows[0].target_id

        [belief] = store.get_beliefs(
            agent_id="oliver",
            domain="identity",
            include_pending_review=True,
        )
        assert belief.id == target_id
        assert belief.content == "David context is foundational."
        assert belief.tier == "operational"
        assert preview_pai_import(store, [source]).counts == {ACTION_NOOP: 1}

        changed = replace(source, source_text="David context remains foundational.")
        changed_preview = preview_pai_import(store, [changed])
        assert changed_preview.counts == {ACTION_UPDATE: 1}
        apply_pai_import(store, changed_preview)
        [updated] = store.get_beliefs(
            agent_id="oliver",
            domain="identity",
            include_pending_review=True,
        )
        assert updated.id == target_id
        assert updated.content == "David context remains foundational."
    finally:
        store.close()


def test_u3b_hypomnema_preview_apply_updates_explicit_target(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("hypomnema", "Continuity belongs in scoped memory.")
        first = preview_pai_import(store, [source])
        assert first.counts == {ACTION_INSERT: 1}
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        entry = store.get_hypomnema_entry(
            target_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert entry is not None
        assert entry["content"] == "Continuity belongs in scoped memory."
        assert entry["original_timestamp"] == 1710000000

        changed = replace(source, source_text="Continuity remains scoped and durable.")
        changed_preview = preview_pai_import(store, [changed])
        assert changed_preview.counts == {ACTION_UPDATE: 1}
        assert changed_preview.rows[0].target_id == target_id
        apply_pai_import(store, changed_preview)

        updated = store.get_hypomnema_entry(
            target_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert updated is not None
        assert updated["content"] == "Continuity remains scoped and durable."
        assert updated["revision_count"] == 1
        assert updated["revisions"][0]["content"] == "Continuity belongs in scoped memory."
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad_source,match",
    [
        (
            PaiImportSource(
                job_id=" ",
                source_path="/pai/identity_kernel.md",
                source_kind="identity_kernel",
                source_text="x",
            ),
            "job_id is required",
        ),
        (
            PaiImportSource(
                job_id="u3b-job",
                source_path=" ",
                source_kind="identity_kernel",
                source_text="x",
            ),
            "source_path is required",
        ),
        (
            PaiImportSource(
                job_id="u3b-job",
                source_path="/pai/identity_kernel.md",
                source_kind="unknown",
                source_text="x",
            ),
            "Unsupported PAI source_kind",
        ),
        (
            PaiImportSource(
                job_id="u3b-job",
                source_path="/pai/identity_kernel.md",
                source_kind="identity_kernel",
                source_text=" ",
            ),
            "No importable content",
        ),
    ],
)
def test_u3b_rejects_malformed_sources(tmp_path, bad_source, match):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        with pytest.raises(ValueError, match=match):
            preview_pai_import(store, [bad_source])
    finally:
        store.close()


def test_u3b_rejects_duplicate_row_keys(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "same block")
        with pytest.raises(ValueError, match="Duplicate PAI import row key"):
            preview_pai_import(store, [source, source])
    finally:
        store.close()


def test_u3b_rejects_mixed_job_batches(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source_a = _source("identity_kernel", "A")
        source_b = replace(source_a, job_id="other-job", source_path="/pai/other.md")
        with pytest.raises(ValueError, match="one job_id"):
            preview_pai_import(store, [source_a, source_b])
    finally:
        store.close()


# ── Hardening pass II: substrate-cast verification (creative-boris CB1 + CB9) ──


def test_u3b_imported_pai_tags_excluded_from_persistent_concerns(tmp_path):
    """Hardening CB1: PAI import-routing tags must not flood IdentityProfile.

    Without the reflection.py exclusion-set extension, every imported engram
    carries `("pai-import", "<kind-slug>")` and `tag_counts.most_common(10)`
    surfaces those routing markers as Oliver's persistent concerns. The first
    IdentityProfile computed after a PAI import would literally report
    "Oliver's persistent concern is being imported." Substrate-cast bug;
    locks once row-map is written.
    """
    from mnemos.consolidation.reflection import compute_identity_profile
    from mnemos.core.identity import AgentIdentity

    store = EngramStore(tmp_path / "u3b-identity.db")
    try:
        sources = [
            _source(
                "identity_kernel",
                "# Core\nI am Oliver.\n\n# Voice\nDirect, dense, present.",
            ),
            replace(
                _source(
                    "david_context",
                    "# David\nBSD school psych.\n\n# Norman\nHusband since 2009.",
                ),
                source_path="/pai/david.md",
            ),
            replace(
                _source(
                    "growth_substrate",
                    "# Heavy context\nDistrust first response.\n\n# Position\nMove before rewriting.",
                ),
                source_path="/pai/growth.md",
            ),
        ]
        preview = preview_pai_import(store, sources)
        apply_pai_import(store, preview)

        identity = AgentIdentity()
        identity.memory_profile.agent_id = "oliver"
        store.save_identity(identity)

        engrams = store.get_active_engrams(agent_id="oliver", limit=100)
        profile = compute_identity_profile(store, engrams, identity)

        pai_marker_tags = {
            "pai-import",
            "identity-kernel",
            "david-context",
            "growth-substrate",
            "belief",
            "hypomnema",
        }
        leaked = {tag for tag, _ in profile.persistent_concerns} & pai_marker_tags
        assert not leaked, (
            f"PAI import-routing tags leaked into persistent_concerns: {leaked}. "
            "Substrate is reporting import metadata as identity. Extend the "
            "exclusion set in mnemos/consolidation/reflection.py."
        )
    finally:
        store.close()


def test_u3b_identity_profile_from_imported_soul_is_semantic(tmp_path):
    """Hardening CB9: end-to-end IdentityProfile contract test.

    The engineering view tests `assert preview.counts == {"insert": N}` then
    `assert result.counts == {"insert": N}`. That can pass while the resulting
    IdentityProfile is structurally broken (top concerns = PAI routing tags,
    zero core_beliefs because beliefs auto-confirm with no surfacing). This
    test runs a SOUL-mirroring fixture import end-to-end and asserts
    substrate-shaped properties of the resulting profile.
    """
    from mnemos.consolidation.reflection import compute_identity_profile
    from mnemos.core.identity import AgentIdentity

    store = EngramStore(tmp_path / "u3b-soul.db")
    try:
        sources = [
            _source(
                "identity_kernel",
                "# Nome\nI am Oliver. David's agent.\n\n"
                "# Voce\nFirst person, present tense, direct verbs.",
            ),
            replace(
                _source(
                    "david_context",
                    "# David\nBoard-certified school neuropsych, BSD VT.\n\n"
                    "# Mission\nHelp ~300 kids get seen well.",
                ),
                source_path="/pai/david.md",
            ),
            replace(
                _source(
                    "growth_substrate",
                    "# Plan Mode\nFor uncertainty, not willpower.\n\n"
                    "# Verify\nClaimed done is not actually done.",
                ),
                source_path="/pai/growth.md",
            ),
            replace(
                _source(
                    "beliefs",
                    "David grinds his own coffee.\n\n"
                    "Reports change how families see their child.",
                ),
                source_path="/pai/facts.md",
            ),
        ]
        preview = preview_pai_import(store, sources)
        apply_pai_import(store, preview)

        identity = AgentIdentity()
        identity.memory_profile.agent_id = "oliver"
        store.save_identity(identity)

        engrams = store.get_active_engrams(agent_id="oliver", limit=100)
        engram_content_joined = "\n".join(engram.content for engram in engrams)
        engram_tags_flat = [tag for engram in engrams for tag in engram.tags]

        assert engram_tags_flat.count("identity-kernel") >= 1, (
            "identity_kernel engrams missing from store after import"
        )
        assert engram_tags_flat.count("david-context") >= 1, (
            "david_context engrams missing from store after import"
        )
        assert engram_tags_flat.count("growth-substrate") >= 1, (
            "growth_substrate engrams missing from store after import"
        )
        assert "Oliver" in engram_content_joined, (
            "identity_kernel content not propagated to engrams"
        )
        assert "BSD" in engram_content_joined or "Burlington" in engram_content_joined, (
            "david_context content not propagated to engrams"
        )
        assert "Plan Mode" in engram_content_joined or "Verify" in engram_content_joined, (
            "growth_substrate content not propagated to engrams"
        )

        profile = compute_identity_profile(store, engrams, identity)

        # 1. persistent_concerns is non-empty AND non-leaked
        # (covered by CB1 test above; reasserted here as a smoke check)
        pai_marker_tags = {
            "pai-import",
            "identity-kernel",
            "david-context",
            "growth-substrate",
            "belief",
            "hypomnema",
        }
        leaked = {tag for tag, _ in profile.persistent_concerns} & pai_marker_tags
        assert not leaked, f"PAI tags leaked: {leaked}"

        # 2. pending-confidence imports stay out of substrate identity until review.
        assert profile.core_beliefs == []
        store._get_conn().execute(
            """
            UPDATE beliefs
            SET needs_review = 0, confidence_pending_review = 0
            WHERE agent_id = ?
            """,
            ("oliver",),
        )
        store._get_conn().commit()

        reviewed_profile = compute_identity_profile(store, engrams, identity)
        assert len(reviewed_profile.core_beliefs) >= 2, (
            "Reviewed imported beliefs not surfacing in IdentityProfile.core_beliefs. "
            f"Got {len(reviewed_profile.core_beliefs)}; expected >=2 from the 2 fact blocks."
        )
        belief_text = " ".join(content for content, _ in reviewed_profile.core_beliefs)
        assert "coffee" in belief_text.lower() or "report" in belief_text.lower(), (
            "Belief content from fixture not present in core_beliefs. "
            f"Got: {reviewed_profile.core_beliefs}"
        )

        # 3. to_summary() emits a non-trivial string anchored in imported content
        summary = reviewed_profile.to_summary()
        assert summary, "IdentityProfile.to_summary returned empty"
    finally:
        store.close()


# ── Hardening pass II: HIGH cluster (schema v5 + code path) ──


def test_u3b_apply_writes_pai_import_events(tmp_path):
    """U3b hardening B5-audit-6 + B4-u3c-5: apply must produce an append-only
    event row per row-touched so the operator can reconstruct "what did job X
    do" even after a later job touches the same targets."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.\n\n# Voice\nDirect.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        first_events = store._get_conn().execute(
            "SELECT action, source_path FROM pai_import_events ORDER BY event_id"
        ).fetchall()
        assert len(first_events) == 2
        assert all(e["action"] == "insert" for e in first_events)

        changed = replace(source, source_text="# Core\nI am Oliver, agent.\n\n# Voice\nDirect.")
        update_preview = preview_pai_import(store, [changed])
        apply_pai_import(store, update_preview)
        all_events = store._get_conn().execute(
            "SELECT action, source_anchor, source_hash_before, source_hash_after, change_reason "
            "FROM pai_import_events ORDER BY event_id"
        ).fetchall()
        # 2 inserts from first apply + 1 update + 1 noop from second apply
        assert len(all_events) == 4
        actions = [e["action"] for e in all_events]
        assert actions == ["insert", "insert", "update", "noop"]
        update_event = all_events[2]
        assert update_event["source_hash_before"] != update_event["source_hash_after"]
        assert update_event["change_reason"]
    finally:
        store.close()


def test_u3b_imported_belief_arrives_needs_review_true(tmp_path):
    """U3b hardening CB3: PAI imports are canonical AT THE MOMENT of import
    but the substrate's downstream review work shouldn't be silently erased.
    Imported beliefs arrive needs_review=True; substrate clears it after
    review."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        preview = preview_pai_import(store, [source])
        apply_pai_import(store, preview)
        target_id = preview.rows[0].target_id

        row = store._get_conn().execute(
            "SELECT needs_review, confidence_pending_review, original_substrate, "
            "original_timestamp FROM beliefs WHERE id = ?",
            (target_id,),
        ).fetchone()
        assert bool(row["needs_review"]) is True
        assert bool(row["confidence_pending_review"]) is True
        assert row["original_substrate"] == "claude-opus-4-6"
        assert row["original_timestamp"] == 1710000000

        # Substrate-side review concludes; flips needs_review off.
        store._get_conn().execute(
            "UPDATE beliefs SET needs_review = 0, confidence_pending_review = 0 WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        # Re-running the same source must NOOP (needs_review is workflow state,
        # not part of equality). The substrate's review work is preserved.
        reimport_preview = preview_pai_import(store, [source])
        assert reimport_preview.counts == {ACTION_NOOP: 1}
    finally:
        store.close()


def test_u3b_reviewed_belief_confidence_is_same_source_workflow_state(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store._get_conn().execute(
            """
            UPDATE beliefs
            SET confidence = ?, needs_review = 0, confidence_pending_review = 0
            WHERE id = ?
            """,
            (0.91, target_id),
        )
        store._get_conn().commit()

        reimport_preview = preview_pai_import(store, [source])
        assert reimport_preview.counts == {ACTION_NOOP: 1}
        reimport_result = apply_pai_import(store, reimport_preview)
        assert reimport_result.counts == {ACTION_NOOP: 1}

        row = store._get_conn().execute(
            """
            SELECT confidence, needs_review, confidence_pending_review
            FROM beliefs
            WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        assert row["confidence"] == pytest.approx(0.91)
        assert bool(row["needs_review"]) is False
        assert bool(row["confidence_pending_review"]) is False
    finally:
        store.close()


def test_u3b_imported_belief_re_import_flips_needs_review_back_on_change(tmp_path):
    """When David edits a belief in SOUL/FACTS.md, the re-import flips
    needs_review back to True — substrate's prior review must be reconfirmed
    against the new content. revision_history preserves the trail with job_id.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        # Substrate review concludes
        store._get_conn().execute(
            "UPDATE beliefs SET needs_review = 0, confidence_pending_review = 0 WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        # David edits the fact
        changed = replace(
            source,
            source_text="David grinds his own coffee every morning.",
        )
        apply_pai_import(store, preview_pai_import(store, [changed]))

        row = store._get_conn().execute(
            "SELECT needs_review, confidence_pending_review, revision_history FROM beliefs WHERE id = ?",
            (target_id,),
        ).fetchone()
        assert bool(row["needs_review"]) is True
        assert bool(row["confidence_pending_review"]) is True
        revisions = json.loads(row["revision_history"])
        assert revisions
        # Hardening MEDIUM 15: revision entries carry job_id
        assert revisions[-1].get("job_id") == "u3b-job"
    finally:
        store.close()


def test_u3b_imported_belief_revision_metadata_survives_later_save(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("beliefs", "David grinds his own coffee.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        changed = replace(source, source_text="David grinds his own coffee every morning.")
        apply_pai_import(store, preview_pai_import(store, [changed]))

        loaded = next(
            b
            for b in store.get_beliefs(
                agent_id=first.rows[0].agent_id,
                include_pending_review=True,
            )
            if b.id == target_id
        )
        loaded.challenge()
        store.save_belief(loaded)

        row = store._get_conn().execute(
            "SELECT revision_history FROM beliefs WHERE id = ?",
            (target_id,),
        ).fetchone()
        revisions = json.loads(row["revision_history"])
        assert revisions[-1]["job_id"] == "u3b-job"
        assert revisions[-1]["old_content"] == "David grinds his own coffee."
        assert (
            revisions[-1]["new_content"]
            == "David grinds his own coffee every morning."
        )
    finally:
        store.close()


def test_u3b_hypomnema_inactive_reactivates_on_reimport(tmp_path):
    """U3b hardening CB8: hypomnema deactivation (active=False, no successor)
    is a retire signal that PAI re-import is the canonical reactivation
    channel for. Engrams keep refuse-on-archived; beliefs keep refuse-on-
    superseded. Hypomnema specifically reactivates."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source(
            "hypomnema", "# Today\nSubstrate warm. Lo Stelo intact."
        )
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        # Substrate-side deactivation (retired-not-superseded)
        store._get_conn().execute(
            "UPDATE hypomnema_entries SET active = 0 WHERE id = ?",
            (target_id,),
        )
        store._get_conn().commit()

        # Re-import same content — should produce REPAIR (target projection
        # mismatch on active flag) and write path reactivates.
        preview = preview_pai_import(store, [source])
        assert ACTION_REPAIR in preview.counts or ACTION_NOOP in preview.counts
        apply_pai_import(store, preview)

        active = store._get_conn().execute(
            "SELECT active FROM hypomnema_entries WHERE id = ?",
            (target_id,),
        ).fetchone()
        assert bool(active["active"]) is True
    finally:
        store.close()


def test_u3b_hypomnema_superseded_still_refuses_reactivation(tmp_path):
    """Superseded hypomnema (active=False AND superseded_by IS NOT NULL) means
    a successor exists. Reactivating would create two active entries in the
    supersede chain. Must refuse."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source(
            "hypomnema", "# Today\nSubstrate warm. Lo Stelo intact."
        )
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        # Create a real successor hypomnema row so the foreign key constraint
        # on superseded_by is satisfiable.
        successor_id = store.write_hypomnema_entry(
            "# Tomorrow\nNew substrate state.",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            source="observed",
            domain="identity",
        )
        store._get_conn().execute(
            "UPDATE hypomnema_entries SET active = 0, superseded_by = ? WHERE id = ?",
            (successor_id, target_id),
        )
        store._get_conn().commit()

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        assert "superseded" in preview.rows[0].reason
    finally:
        store.close()


def test_u3b_infer_source_kind_raises_for_ambiguous_stale_engram(tmp_path):
    """U3b hardening CB6: when a stale row-map entry references an engram
    whose marker tags were hand-edited away AND no source_kind is recorded
    in the row-map, source_kind cannot be safely inferred. The prior silent
    default to identity_kernel produced profile-incoherent stale-row
    construction. Preview now emits a row-level ACTION_ERROR instead of
    crashing the batch."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("david_context", "# David\nBSD school psych.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        # Simulate hand-edit: remove marker tags from engram AND clear
        # source_kind from row-map (the v5 column protects against this case
        # by default — we manually clear to test the no-row-map-source-kind
        # fallback).
        conn = store._get_conn()
        conn.execute(
            "UPDATE engrams SET tags = ? WHERE id = ?",
            (json.dumps([]), target_id),
        )
        conn.execute(
            "UPDATE pai_import_row_map SET source_kind = NULL WHERE target_id = ?",
            (target_id,),
        )
        conn.commit()

        # Now run an import that excludes the original source — _stale_mapped_rows
        # will try to infer source_kind for this target.
        other = replace(source, source_path="/pai/other.md", source_text="# Other\nelse")
        preview = preview_pai_import(store, [other])
        assert preview.counts == {ACTION_INSERT: 1, ACTION_ERROR: 1}
        ambiguous = [row for row in preview.rows if "ambiguous stale row" in row.reason]
        assert len(ambiguous) == 1
        assert "Cannot infer source_kind" in ambiguous[0].reason
    finally:
        store.close()


def test_u3b_ambiguous_stale_row_does_not_suppress_other_stale_rows(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        current = _source("identity_kernel", "# Current\nkeep")
        ambiguous_source = replace(
            _source("david_context", "# David\nBSD school psych."),
            source_path="/pai/david.md",
        )
        clean_stale_source = replace(
            _source("growth_substrate", "# Verify\nClaimed done is not done."),
            source_path="/pai/growth.md",
        )
        apply_pai_import(
            store,
            preview_pai_import(
                store,
                [current, ambiguous_source, clean_stale_source],
            ),
        )
        ambiguous_target_id = preview_pai_import(store, [ambiguous_source]).rows[0].target_id

        conn = store._get_conn()
        conn.execute(
            "UPDATE engrams SET tags = ? WHERE id = ?",
            (json.dumps([]), ambiguous_target_id),
        )
        conn.execute(
            "UPDATE pai_import_row_map SET source_kind = NULL WHERE target_id = ?",
            (ambiguous_target_id,),
        )
        conn.commit()

        preview = preview_pai_import(store, [current])
        assert preview.counts == {ACTION_NOOP: 1, ACTION_ERROR: 2}
        reasons = [row.reason for row in preview.rows if row.action == ACTION_ERROR]
        assert any("ambiguous stale row" in reason for reason in reasons)
        assert any("absent from current PAI import batch" in reason for reason in reasons)
    finally:
        store.close()


# ── Hardening pass II: MEDIUM cleanups + U3c-reserve ──


def test_u3b_engram_version_change_reason_carries_job_id(tmp_path):
    """U3b hardening B5-audit-4: versions.change_reason includes job_id so
    audit reconstruction doesn't require timestamp-correlating to
    pai_import_events."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        changed = replace(source, source_text="# Core\nI am Oliver, agent.")
        apply_pai_import(store, preview_pai_import(store, [changed]))

        reason = store._get_conn().execute(
            "SELECT change_reason FROM versions WHERE engram_id = ?",
            (target_id,),
        ).fetchone()
        assert reason is not None
        assert "u3b-job" in reason["change_reason"]
        assert "pai_import_update" in reason["change_reason"]
    finally:
        store.close()


def test_u3b_u3c_reserved_actions_raise_not_implemented(tmp_path):
    """U3b hardening CB7: ACTION_TOMBSTONE / ACTION_DEACTIVATE / ACTION_REVIEW
    are reserved constants. apply_pai_import must raise NotImplementedError
    rather than silently proceeding — locks U3c contract surface."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        preview = preview_pai_import(store, [source])
        # Forge a preview row carrying a reserved action.
        forged = replace(preview.rows[0], action=ACTION_TOMBSTONE)
        with pytest.raises(NotImplementedError, match="reserved for U3c"):
            apply_pai_import(
                store, PaiImportPreview(job_id=preview.job_id, rows=(forged,))
            )
        for reserved_action in (ACTION_DEACTIVATE, ACTION_REVIEW):
            forged_other = replace(preview.rows[0], action=reserved_action)
            with pytest.raises(NotImplementedError, match="reserved for U3c"):
                apply_pai_import(
                    store,
                    PaiImportPreview(job_id=preview.job_id, rows=(forged_other,)),
                )
    finally:
        store.close()


def test_u3b_reserved_action_constants_are_distinct_strings():
    """The reserved constants must be distinct from the active set; U3c
    cannot reshape these strings without re-opening U3b."""
    active = {ACTION_INSERT, ACTION_REPAIR, ACTION_UPDATE, ACTION_NOOP, ACTION_ERROR, ACTION_PENDING}
    reserved = {ACTION_TOMBSTONE, ACTION_DEACTIVATE, ACTION_REVIEW}
    assert not (active & reserved)
    # Each reserved action is a non-empty string with a clear name
    for action in reserved:
        assert isinstance(action, str)
        assert action


def test_u3b_row_map_populates_extended_columns_on_insert(tmp_path):
    """B4-u3c-4: row-map gains agent_id / project_scope / source_kind /
    original_timestamp / content_at_last_import so U3c watcher doesn't need
    to re-join target tables to reconcile."""
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("growth_substrate", "# Voice\nDense over sparse.")
        apply_pai_import(store, preview_pai_import(store, [source]))

        row = store._get_conn().execute(
            "SELECT content_at_last_import, agent_id, project_scope, "
            "source_kind, original_timestamp, tombstone_at FROM pai_import_row_map"
        ).fetchone()
        assert row["content_at_last_import"]
        assert "Dense over sparse" in row["content_at_last_import"]
        assert row["agent_id"] == "oliver"
        assert row["project_scope"] == "pai"
        assert row["source_kind"] == "growth_substrate"
        assert row["original_timestamp"] == 1710000000
        assert row["tombstone_at"] is None
    finally:
        store.close()


def test_u3b_noop_apply_backfills_v5_row_map_baseline(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("growth_substrate", "# Voice\nDense over sparse.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        conn = store._get_conn()
        conn.execute(
            """
            UPDATE pai_import_row_map
            SET source_kind = NULL,
                agent_id = NULL,
                project_scope = NULL,
                original_timestamp = NULL
            WHERE target_id = ?
            """,
            (target_id,),
        )
        conn.commit()

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_NOOP: 1}
        apply_pai_import(store, preview)

        row = conn.execute(
            """
            SELECT content_at_last_import, source_kind, agent_id, project_scope,
                   original_timestamp
            FROM pai_import_row_map
            WHERE target_id = ?
            """,
            (target_id,),
        ).fetchone()
        assert row["content_at_last_import"] == "# Voice\nDense over sparse."
        assert row["source_kind"] == "growth_substrate"
        assert row["agent_id"] == "oliver"
        assert row["project_scope"] == "pai"
        assert row["original_timestamp"] == 1710000000
    finally:
        store.close()


# ── Hardening pass III: ce-debug — recovery-doc trap documentation ──


def test_u3b_drift_error_message_documents_recovery_options_honestly(tmp_path):
    """ce-debug finding: the prior 'Reconcile manually or clear
    content_at_last_import' message was a trap — an operator who followed it
    naively (clearing baseline to NULL OR setting baseline to current target)
    had their hand-edit silently clobbered on next import.

    The fix is in the message TEXT: name the ONLY non-destructive path
    (edit the source file) AND explicitly warn that EITHER baseline
    manipulation (NULL or current-target) clobbers. The asymmetry is real:
    there's no row-map-only recovery that preserves a hand-edit.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        # Operator hand-edit
        store._get_conn().execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            ("# Core\nI am Oliver. (operator edit)", target_id),
        )
        store._get_conn().commit()

        preview = preview_pai_import(store, [source])
        assert preview.counts == {ACTION_ERROR: 1}
        reason = preview.rows[0].reason

        # Headline still names the failure mode (existing test contract)
        assert "diverged from importer baseline" in reason
        assert "operator hand-edit detected" in reason

        # Non-destructive recovery is named
        assert "edit the source file" in reason, (
            "Must name the source-side fix as the only non-destructive option"
        )

        # Destructive paths are explicitly labeled DESTRUCTIVE
        assert "DESTRUCTIVE" in reason and "NULL" in reason, (
            "NULL-clear must be labeled DESTRUCTIVE"
        )

        # The trap-shaped intent (set baseline to current target) is
        # explicitly disclaimed — operators must not believe this preserves
        # their edit
        assert "does NOT preserve" in reason or "still clobbers" in reason, (
            "Must warn that setting baseline to current-target does NOT "
            "preserve the hand-edit"
        )
    finally:
        store.close()


def test_u3b_recovery_doc_NULL_clobbers_hand_edit_as_documented(tmp_path):
    """Operator follows the NULL path: per the new message, this is
    DESTRUCTIVE. Test asserts the clobber actually happens — so the
    documentation matches the behavior.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        canonical = "# Core\ncanonical source content"
        source = _source("identity_kernel", canonical)
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        hand_edited = "# Core\nhand-edited substance the operator added"
        conn = store._get_conn()
        conn.execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            (hand_edited, target_id),
        )
        # Operator follows the NULL path
        conn.execute(
            "UPDATE pai_import_row_map SET content_at_last_import = NULL WHERE target_id = ?",
            (target_id,),
        )
        conn.commit()

        preview = preview_pai_import(store, [source])
        assert ACTION_REPAIR in preview.counts
        apply_pai_import(store, preview)

        final = conn.execute(
            "SELECT content FROM engrams WHERE id = ?", (target_id,)
        ).fetchone()["content"]
        assert final == canonical, (
            f"NULL path per message: discards edit, restores source. "
            f"Expected {canonical!r}, got {final!r}."
        )
    finally:
        store.close()


def test_u3b_recovery_doc_baseline_to_current_target_ALSO_clobbers(tmp_path):
    """The trap path: operator follows what FEELS like a 'soft' recovery —
    'my edit IS the baseline now' — by setting content_at_last_import to
    their current target content. The message now explicitly disclaims this:
    it does NOT preserve the edit. The next REPAIR still clobbers because
    source still diverges from the (now-aligned) target.

    This test pins the behavior so the message stays honest.
    """
    store = EngramStore(tmp_path / "u3b.db")
    try:
        canonical = "# Core\ncanonical"
        source = _source("identity_kernel", canonical)
        apply_pai_import(store, preview_pai_import(store, [source]))
        target_id = preview_pai_import(store, [source]).rows[0].target_id

        hand_edited = "# Core\ncanonical plus operator note"
        conn = store._get_conn()
        conn.execute(
            "UPDATE engrams SET content = ? WHERE id = ?",
            (hand_edited, target_id),
        )
        # Operator's well-intentioned attempt: "my edit is new baseline"
        conn.execute(
            "UPDATE pai_import_row_map SET content_at_last_import = ? WHERE target_id = ?",
            (hand_edited, target_id),
        )
        conn.commit()

        preview = preview_pai_import(store, [source])
        # baseline==target now → divergence check passes; source_hash unchanged
        # → REPAIR fires because target_matches_row sees engram.content != row.content
        assert ACTION_REPAIR in preview.counts
        apply_pai_import(store, preview)

        final = conn.execute(
            "SELECT content FROM engrams WHERE id = ?", (target_id,)
        ).fetchone()["content"]
        assert final == canonical, (
            "Setting baseline to current target does NOT preserve hand-edit; "
            f"REPAIR clobbers. Expected {canonical!r}, got {final!r}."
        )
    finally:
        store.close()
