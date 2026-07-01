"""Tests for the dream journal: consolidation rendered as first-person narrative."""

from datetime import datetime, timedelta, timezone

import pytest

from mnemos.core.belief import Belief, BeliefRevision
from mnemos.dream_journal import (
    DREAM_JOURNAL_TAG,
    MAX_NARRATIVE_CHARS,
    collect_belief_deltas,
    compose_dream_narrative,
    fetch_active_dream_entry,
    write_dream_entry,
)
from mnemos.simple_runtime import MnemosRuntime
from mnemos.simple_scope import MnemosScope


def _runtime(tmp_path) -> MnemosRuntime:
    return MnemosRuntime(
        db_path=str(tmp_path / "dream.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )


def _seed_promotion_candidate(runtime: MnemosRuntime) -> None:
    """Plant a legacy operational candidate that _promote_candidates can graduate."""

    runtime._ensure_init()
    assert runtime._store is not None
    entry_id = runtime._store.write_hypomnema_entry(
        "Riley uses the dream journal verification tests.",
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
    )
    runtime._store._get_conn().execute(
        """
        UPDATE hypomnema_entries
        SET confidence = 0.9,
            salience = 0.8,
            foundational = 1,
            read_visibility = 'operational_context'
        WHERE id = ?
        """,
        (entry_id,),
    )
    runtime._store._get_conn().commit()


def test_compose_returns_none_when_nothing_noteworthy():
    stats = {
        "cycle_type": "shallow",
        "passes_run": ["connection_discovery", "decay"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "connection_discovery": {"connections_created": 0, "connections_reclassified": 0},
        "decay": {"engrams_decayed": 0, "engrams_dormant": 0, "engrams_archived": 0},
    }
    assert compose_dream_narrative(stats, [], promoted=0) is None


def test_compose_deep_cycle_writes_fallback():
    narrative = compose_dream_narrative({"cycle_type": "deep"}, [], promoted=0)
    assert narrative == "I went through everything and it all still holds."


def test_compose_includes_counts_and_belief_deltas():
    stats = {
        "cycle_type": "deep",
        "connection_discovery": {"connections_created": 2},
        "decay": {"engrams_decayed": 0, "engrams_dormant": 0, "engrams_archived": 3},
        "softening": {"engrams_softened": 1, "lessons_created": 1, "lessons_reinforced": 0},
        "reflection": {"thoughts_generated": 2, "persistent_concerns": []},
    }
    deltas = [
        {
            "belief_id": "belief_x",
            "content": "tests matter",
            "old_confidence": 0.72,
            "new_confidence": 0.81,
            "reason": "supported",
        }
    ]

    narrative = compose_dream_narrative(stats, deltas, promoted=2)

    assert narrative is not None
    assert "Deep consolidation ran while you were away." in narrative
    assert "I connected 2 memories" in narrative
    assert "I softened 1 stale detail and kept their lessons." in narrative
    assert "3 faded memories" in narrative
    assert '"tests matter" strengthened' in narrative
    assert "0.72 -> 0.81" in narrative
    assert "promoted 2 continuity notes" in narrative
    assert "2 new thoughts surfaced" in narrative
    assert len(narrative) <= MAX_NARRATIVE_CHARS


def test_collect_belief_deltas_filters_and_caps(store):
    now = datetime.now(timezone.utc)
    # Naive since timestamp exercises the assume-UTC coercion path.
    since_iso = (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    old_ts = (now - timedelta(hours=2)).isoformat()

    big = Belief(agent_id="nova", content="tests matter", confidence=0.5)
    big.revision_history.append(
        BeliefRevision(timestamp=old_ts, old_confidence=0.4, new_confidence=0.5, reason="old support")
    )
    big.revise(0.9, reason="strongly supported")
    store.save_belief(big)

    medium = Belief(agent_id="nova", content="docs help", confidence=0.5)
    medium.revise(0.7, reason="supported")
    store.save_belief(medium)

    small = Belief(agent_id="nova", content="naps are optional", confidence=0.5)
    small.revise(0.55, reason="weak support")
    store.save_belief(small)

    stale = Belief(agent_id="nova", content="stale belief", confidence=0.6)
    stale.revision_history.append(
        BeliefRevision(timestamp=old_ts, old_confidence=0.5, new_confidence=0.6, reason="pre-window")
    )
    store.save_belief(stale)

    other_agent = Belief(agent_id="vektor", content="not my belief", confidence=0.5)
    other_agent.revise(0.95, reason="someone else's evidence")
    store.save_belief(other_agent)

    deltas = collect_belief_deltas(store, "nova", since_iso, limit=2)

    # Sorted by magnitude (0.4 then 0.2), capped at limit; pre-window and
    # other-agent revisions never appear.
    assert [d["content"] for d in deltas] == ["tests matter", "docs help"]
    assert deltas[0]["old_confidence"] == pytest.approx(0.5)
    assert deltas[0]["new_confidence"] == pytest.approx(0.9)
    assert deltas[0]["reason"] == "strongly supported"

    # Unparseable since timestamps never raise.
    assert collect_belief_deltas(store, "nova", "not-a-timestamp") == []


def test_write_dream_entry_supersedes_prior(store, tmp_db):
    scope = MnemosScope(agent_id="nova", person_id="riley", project_scope="demo", db_path=tmp_db)

    first_id = write_dream_entry(store, scope, "I connected 2 memories that belong together.")
    second_id = write_dream_entry(store, scope, "I let 1 faded memory rest in the archive.")
    assert first_id != second_id

    entries = store.search_hypomnema(
        "",
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        limit=50,
        include_inactive=True,
    )
    dream_entries = [e for e in entries if DREAM_JOURNAL_TAG in e["tags"]]
    active = [e for e in dream_entries if e["active"]]
    assert len(active) == 1
    assert active[0]["id"] == second_id
    assert active[0]["content"] == "I let 1 faded memory rest in the archive."

    prior = next(e for e in dream_entries if e["id"] == first_id)
    assert prior["active"] is False
    assert prior["superseded_by"] == second_id

    fetched = fetch_active_dream_entry(store, scope)
    assert fetched is not None
    assert fetched["id"] == second_id


def test_maintain_writes_dream_and_context_renders_section(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        _seed_promotion_candidate(runtime)

        result = runtime.maintain()
        assert "Dream journal: updated" in result
        assert runtime.last_dream_note_id is not None
        narrative = runtime.last_dream_narrative
        assert narrative is not None
        assert "I promoted 1 continuity note into durable memory." in narrative
        assert runtime._get_meta("dream_last_written_at") is not None

        packet = runtime.context()
        assert "While you were away:" in packet
        # The narrative renders in its own section only — never duplicated
        # into the Continuity notes list.
        assert packet.count(narrative) == 1
    finally:
        runtime.close()


def test_dream_failure_never_breaks_maintain(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    try:
        _seed_promotion_candidate(runtime)

        def _boom(*args, **kwargs):
            raise RuntimeError("dream store offline")

        # maintain() imports write_dream_entry from the module at call time,
        # so patching the source module is sufficient.
        monkeypatch.setattr("mnemos.dream_journal.write_dream_entry", _boom)

        result = runtime.maintain()
        assert "Dream journal: skipped (write failed)" in result
        assert runtime.last_dream_note_id is None
        assert runtime.last_dream_narrative is None
    finally:
        runtime.close()


def test_polish_dream_revises_entry_and_rejects_bad_input(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        _seed_promotion_candidate(runtime)
        runtime.maintain()
        note_id = runtime.last_dream_note_id
        assert note_id is not None

        assert runtime.polish_dream(note_id, "a warm short polish") is True
        entry = fetch_active_dream_entry(runtime._store, runtime.scope)
        assert entry is not None
        assert entry["content"] == "a warm short polish"

        assert runtime.polish_dream(note_id, "") is False
        assert runtime.polish_dream(note_id, "x" * 901) is False
        entry = fetch_active_dream_entry(runtime._store, runtime.scope)
        assert entry["content"] == "a warm short polish"
    finally:
        runtime.close()
