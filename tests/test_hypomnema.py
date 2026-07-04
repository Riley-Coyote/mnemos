"""Tests for the scoped hypomnema continuity layer."""

import pytest


class TestHypomnemaStore:
    def test_init_creates_hypomnema_table(self, store):
        conn = store._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in rows}

        assert "hypomnema_entries" in table_names

    def test_write_and_search_hypomnema(self, store):
        entry_id = store.write_hypomnema_entry(
            "Riley and Vektor keep functional memory, hypomnema, and Mnemos distinct.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            domain="topical",
            tags="memory,continuity",
            confidence=0.7,
            salience=0.6,
            read_visibility="operational_context",
        )

        results = store.search_hypomnema(
            "functional memory mnemos",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        assert results[0]["id"] == entry_id
        assert results[0]["tags"] == ["memory", "continuity"]
        assert results[0]["foundational"] is False

    def test_revise_hypomnema_keeps_revision_history(self, store):
        entry_id = store.write_hypomnema_entry(
            "Hypomnema is just a note.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        store.revise_hypomnema_entry(
            entry_id,
            "Hypomnema is scoped continuity that can revise before promotion.",
            reason="sharpen definition",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.85,
            salience=0.7,
        )

        # Revise reclassifies this row to review_only (revision_count>=1
        # crosses the promotion threshold), so inspect it via admin opt-in
        # (R5/D8-A); the assertions are unchanged.
        entry = store.get_hypomnema_entry(
            entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )

        assert entry["revision_count"] == 1
        assert entry["confidence"] == pytest.approx(0.85)
        assert entry["revisions"][0]["prior_content"] == "Hypomnema is just a note."

    def test_supersede_hypomnema_hides_original_from_active_search(self, store):
        entry_id = store.write_hypomnema_entry(
            "Old continuity claim",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        replacement_id = store.supersede_hypomnema_entry(
            entry_id,
            "New continuity claim",
            reason="better evidence",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        active = store.search_hypomnema(
            "",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )
        inactive = store.get_hypomnema_entry(
            entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        assert [entry["id"] for entry in active] == [replacement_id]
        assert inactive["active"] is False
        assert inactive["superseded_by"] == replacement_id

    def test_supersede_hypomnema_preserves_read_visibility(self, store):
        entry_id = store.write_hypomnema_entry(
            "Review-only continuity claim",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility="review_only",
        )

        replacement_id = store.supersede_hypomnema_entry(
            entry_id,
            "Replacement review-only continuity claim",
            reason="better evidence",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        # Admin inspection of the review_only replacement's stored tier (R5/D8-A).
        replacement = store.get_hypomnema_entry(
            replacement_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )
        operational = store.search_hypomnema(
            "",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )
        review = store.search_hypomnema(
            "",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility="review_only",
        )

        assert replacement["read_visibility"] == "review_only"
        assert replacement_id not in [entry["id"] for entry in operational]
        assert [entry["id"] for entry in review] == [replacement_id]

    def test_hypomnema_upsert_preserves_existing_visibility_when_omitted(self, store):
        entry_id = store.write_hypomnema_entry(
            "Audit-only continuity claim",
            entry_id="audit-continuity",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility="audit_only",
        )

        store.write_hypomnema_entry(
            "Updated audit-only continuity claim",
            entry_id=entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )

        # Admin inspection of the audit_only row's preserved tier (R5/D8-A).
        updated = store.get_hypomnema_entry(
            entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )

        assert updated["read_visibility"] == "audit_only"
        assert updated["revision_count"] == 1

    def test_hypomnema_upsert_reclassifies_operational_row_crossing_promotion_threshold(
        self, store
    ):
        entry_id = store.write_hypomnema_entry(
            "Ordinary continuity claim",
            entry_id="ordinary-continuity",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.5,
            salience=0.4,
        )

        store.write_hypomnema_entry(
            "Ordinary continuity claim revised into promotion territory",
            entry_id=entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.9,
            salience=0.8,
        )

        # Admin inspection of the row reclassified to review_only after it
        # crossed the promotion threshold (R5/D8-A).
        updated = store.get_hypomnema_entry(
            entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )

        assert updated["revision_count"] == 1
        assert updated["read_visibility"] == "review_only"

    def test_review_candidate_queue_includes_stable_and_high_blast_rows(self, store):
        low_id = store.write_hypomnema_entry(
            "Interesting but weak continuity",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.7,
            salience=0.6,
            foundational=True,
        )
        high_id = store.write_hypomnema_entry(
            "Repeated topical continuity ready for review.",
            entry_id="review-candidate",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.9,
            salience=0.8,
        )
        store.revise_hypomnema_entry(
            high_id,
            "Repeated topical continuity ready for review after revision.",
            reason="stabilized",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.9,
            salience=0.8,
        )

        candidates = store.get_hypomnema_promotion_candidates(
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility="review_only",
        )

        assert {entry["id"] for entry in candidates} == {high_id, low_id}

    def test_live_write_classifies_stable_hypomnema_as_review_only(self, store):
        entry_id = store.write_hypomnema_entry(
            "Fresh foundational continuity should wait for review.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.9,
            salience=0.8,
            foundational=True,
        )

        # Admin inspection of the review_only tier assigned at write; the
        # operational-exclusion assertions below stay on the default filter
        # (R5/D8-A).
        entry = store.get_hypomnema_entry(
            entry_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )
        operational_search = store.search_hypomnema(
            "foundational continuity",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )
        operational_candidates = store.get_hypomnema_promotion_candidates(
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
        )
        review_candidates = store.get_hypomnema_promotion_candidates(
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility="review_only",
        )

        assert entry["read_visibility"] == "review_only"
        assert entry_id not in [item["id"] for item in operational_search]
        assert entry_id not in [item["id"] for item in operational_candidates]
        assert [item["id"] for item in review_candidates] == [entry_id]

    def test_identity_or_foundational_hypomnema_defaults_review_only_even_below_promotion_threshold(
        self, store
    ):
        identity_id = store.write_hypomnema_entry(
            "Identity-domain continuity should wait for review even when low salience.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            domain="identity",
            confidence=0.55,
            salience=0.4,
        )
        foundational_id = store.write_hypomnema_entry(
            "Foundational flag should wait for review even when low confidence.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.5,
            salience=0.45,
            foundational=True,
        )

        # Admin inspection of the review_only tiers assigned by domain/flag
        # even below the promotion threshold (R5/D8-A).
        identity = store.get_hypomnema_entry(
            identity_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )
        foundational = store.get_hypomnema_entry(
            foundational_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )

        assert identity["read_visibility"] == "review_only"
        assert foundational["read_visibility"] == "review_only"

    def test_low_salience_identity_review_rows_are_listed_and_counted(self, store):
        identity_id = store.write_hypomnema_entry(
            "Identity-domain continuity should remain review-visible even below thresholds.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            domain="identity",
            confidence=0.45,
            salience=0.35,
        )
        store.write_hypomnema_entry(
            "Ordinary review-only continuity should not count as review-needed.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.45,
            salience=0.35,
            read_visibility="review_only",
        )
        store.write_hypomnema_entry(
            "Ordinary operational continuity should not count as review-needed.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.45,
            salience=0.35,
            read_visibility="operational_context",
        )
        store.write_hypomnema_entry(
            "Audit-only identity continuity should require explicit audit reads.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            domain="identity",
            confidence=0.45,
            salience=0.35,
            read_visibility="audit_only",
        )

        candidates = store.get_hypomnema_promotion_candidates(
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=("operational_context", "review_only"),
        )
        stats = store.get_hypomnema_stats(
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=("operational_context", "review_only"),
        )

        assert [entry["id"] for entry in candidates] == [identity_id]
        assert stats["hypomnema_promotion_candidates"] == 1

    def test_hypomnema_schema_default_is_operational_with_store_classifier(self, store):
        conn = store._get_conn()
        read_visibility_column = next(
            column
            for column in conn.execute("PRAGMA table_info(hypomnema_entries)")
            if column["name"] == "read_visibility"
        )

        assert read_visibility_column["dflt_value"] == "'operational_context'"

    def test_explicit_operational_visibility_for_review_worthy_hypomnema_is_rejected(
        self, store
    ):
        with pytest.raises(ValueError, match="requires review visibility"):
            store.write_hypomnema_entry(
                "Explicit operational identity continuity should fail closed.",
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
                domain="identity",
                confidence=0.6,
                salience=0.4,
                read_visibility="operational_context",
            )

    def test_explicit_upsert_cannot_downgrade_existing_review_or_audit_visibility(
        self, store
    ):
        review_id = store.write_hypomnema_entry(
            "Existing review-only continuity must not downgrade.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.45,
            salience=0.45,
            read_visibility="review_only",
        )
        audit_id = store.write_hypomnema_entry(
            "Existing audit-only continuity must not downgrade.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.45,
            salience=0.45,
            read_visibility="audit_only",
        )

        with pytest.raises(ValueError, match="requires review visibility"):
            store.write_hypomnema_entry(
                "Existing review-only continuity must not downgrade.",
                entry_id=review_id,
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
                confidence=0.45,
                salience=0.45,
                read_visibility="operational_context",
            )
        with pytest.raises(ValueError, match="requires review visibility"):
            store.write_hypomnema_entry(
                "Existing audit-only continuity must not downgrade.",
                entry_id=audit_id,
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
                confidence=0.45,
                salience=0.45,
                read_visibility="review_only",
            )

        review = store.get_hypomnema_entry(
            review_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )
        audit = store.get_hypomnema_entry(
            audit_id,
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            read_visibility=None,
        )

        assert review["read_visibility"] == "review_only"
        assert audit["read_visibility"] == "audit_only"

    def test_promotion_candidates_filter_by_visibility(self, store):
        review_id = store.write_hypomnema_entry(
            "Review-only candidate requires explicit review visibility.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.95,
            salience=0.9,
            foundational=True,
            read_visibility="review_only",
        )
        audit_id = store.write_hypomnema_entry(
            "Audit-only candidate requires explicit all-visibility opt-in.",
            agent_id="vektor",
            person_id="riley",
            project_scope="codex-test",
            confidence=0.99,
            salience=0.95,
            foundational=True,
            read_visibility="audit_only",
        )

        default_ids = {
            entry["id"]
            for entry in store.get_hypomnema_promotion_candidates(
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
            )
        }
        review_ids = {
            entry["id"]
            for entry in store.get_hypomnema_promotion_candidates(
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
                read_visibility="review_only",
            )
        }
        explicit_all_ids = {
            entry["id"]
            for entry in store.get_hypomnema_promotion_candidates(
                agent_id="vektor",
                person_id="riley",
                project_scope="codex-test",
                read_visibility=None,
            )
        }

        assert default_ids == set()
        assert review_ids == {review_id}
        assert audit_id in explicit_all_ids
