from dataclasses import replace

import pytest

from mnemos.importer import (
    ACTION_ERROR,
    ACTION_INSERT,
    ACTION_NOOP,
    ACTION_PENDING,
    ACTION_REPAIR,
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


def test_u3b_missing_target_repairs_from_row_map(tmp_path):
    store = EngramStore(tmp_path / "u3b.db")
    try:
        source = _source("identity_kernel", "# Core\nI am Oliver.")
        first = preview_pai_import(store, [source])
        apply_pai_import(store, first)
        target_id = first.rows[0].target_id

        store.delete_engram(target_id)

        repair_preview = preview_pai_import(store, [source])
        assert repair_preview.counts == {ACTION_REPAIR: 1}

        apply_pai_import(store, repair_preview)
        repaired = store.get_engram(target_id)
        assert repaired is not None
        assert repaired.content == "# Core\nI am Oliver."
    finally:
        store.close()


def test_u3b_stale_noop_preview_rejects_later_target_drift(tmp_path):
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

        with pytest.raises(ValueError, match="preview is stale"):
            apply_pai_import(store, noop_preview)
        assert store.get_engram(target_id).content == "corrupted after preview"
    finally:
        store.close()


def test_u3b_target_content_drift_repairs_even_when_source_hash_matches(tmp_path):
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

        repair_preview = preview_pai_import(store, [source])
        assert repair_preview.counts == {ACTION_REPAIR: 1}
        assert "target projection drifted" in repair_preview.rows[0].reason

        apply_pai_import(store, repair_preview)
        repaired = store.get_engram(target_id)
        assert repaired is not None
        assert repaired.content == "# Core\nI am Oliver."
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

        [belief] = store.get_beliefs(agent_id="oliver", domain="identity")
        assert belief.id == target_id
        assert belief.content == "David context is foundational."
        assert belief.tier == "operational"
        assert preview_pai_import(store, [source]).counts == {ACTION_NOOP: 1}

        changed = replace(source, source_text="David context remains foundational.")
        changed_preview = preview_pai_import(store, [changed])
        assert changed_preview.counts == {ACTION_UPDATE: 1}
        apply_pai_import(store, changed_preview)
        [updated] = store.get_beliefs(agent_id="oliver", domain="identity")
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
