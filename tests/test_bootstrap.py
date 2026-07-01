"""Tests for turnkey bootstrap setup."""

from mnemos.setup.bootstrap import bootstrap
from mnemos.store.sqlite_store import EngramStore


def test_bootstrap_seeds_foundational_hypomnema_for_review(tmp_path):
    db_path = tmp_path / "memory.db"

    bootstrap(
        agent_name="Nova",
        workspace=str(tmp_path / "workspace"),
        user_name="Riley",
        db_path=str(db_path),
        agent_id="nova",
    )

    store = EngramStore(db_path)
    try:
        operational = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova",
            person_id="riley",
            project_scope="global",
            read_visibility="operational_context",
        )
        review_only = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova",
            person_id="riley",
            project_scope="global",
            read_visibility="review_only",
        )
    finally:
        store.close()

    assert operational == []
    assert review_only
