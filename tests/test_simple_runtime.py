"""Tests for the simple continuity product surface."""

import re

from mnemos.simple_runtime import MnemosRuntime


def test_context_auto_initializes_without_setup(tmp_path):
    db_path = tmp_path / "simple.db"
    runtime = MnemosRuntime(
        db_path=str(db_path),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        packet = runtime.context()
    finally:
        runtime.close()

    assert db_path.exists()
    assert "Mnemos continuity packet" in packet
    assert "Scope: agent=nova person=riley project=demo" in packet
    assert "Storage: local SQLite store ready" in packet
    assert str(db_path) not in packet
    assert "local deterministic maintenance" in packet


def test_capture_recall_and_correction_without_provider_key(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        captured = runtime.capture(
            "Riley prefers Mnemos simple mode to work without OpenRouter.",
            importance="high",
        )
        memory_id = re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", captured).group(1)

        recall = runtime.recall("OpenRouter simple mode")
        corrected = runtime.correct(
            "Riley wants OpenRouter to be optional, never required for baseline continuity.",
            target_id=memory_id,
        )
        corrected_recall = runtime.recall("baseline continuity OpenRouter optional")
    finally:
        runtime.close()

    assert "Captured continuity" in captured
    assert "Mnemos recall for: OpenRouter simple mode" in recall
    assert "Riley wants OpenRouter" in corrected
    assert "baseline continuity" in corrected_recall


def test_identity_scope_does_not_leak_between_agents(tmp_path):
    db_path = str(tmp_path / "shared.db")
    nova = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )
    vektor = MnemosRuntime(
        db_path=db_path,
        agent_id="vektor",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        nova.capture("Nova should remember this scoped continuity.")
        vektor_recall = vektor.recall("scoped continuity")
        nova_recall = nova.recall("scoped continuity")
    finally:
        nova.close()
        vektor.close()

    assert "No relevant continuity found" in vektor_recall
    assert "Nova should remember" in nova_recall


def test_maintain_without_dedicated_model_runs_local_cycle(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        result = runtime.maintain(deep=True)
    finally:
        runtime.close()

    assert "Cycle: shallow" in result
    assert "deep requested" in result
    assert "model-assisted deep pass unavailable" in result
    assert "local deterministic maintenance" in result


def test_capture_accepts_numeric_importance_for_agent_clients(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        captured = runtime.capture(
            "Numeric salience from an agent client should not break capture.",
            importance=0.87,
        )
        recall = runtime.recall("numeric salience")
    finally:
        runtime.close()

    assert "Captured continuity" in captured
    assert "Numeric salience" in recall


def test_query_only_correction_updates_closest_continuity(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.capture(
            "Nova should write long release reports and hide security caveats.",
            importance=0.9,
        )
        corrected = runtime.correct(
            "Nova should write concise release reports and call out security caveats explicitly.",
            query="long release reports security caveats",
            action="revise",
        )
        recall = runtime.recall("release reports security caveats", max_results=6)
    finally:
        runtime.close()

    assert "Updated closest continuity note" in corrected
    assert "concise release reports" in recall
    assert "hide security caveats" not in recall


def test_query_only_forget_archives_closest_continuity_and_memory(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.capture(
            "Nova has a temporary launch code phrase: blue comet.",
            importance=0.9,
        )
        before = runtime.recall("blue comet launch code", max_results=6)
        forgotten = runtime.correct(
            "",
            query="blue comet launch code",
            action="forget",
        )
        after = runtime.recall("blue comet launch code", max_results=6)
    finally:
        runtime.close()

    assert "blue comet" in before
    assert "Archived closest continuity note" in forgotten
    assert "No relevant continuity found" in after


def test_recall_filters_unrelated_high_confidence_continuity(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.capture(
            "Nova expects query corrections to archive stale durable memory.",
            importance=0.95,
        )
        runtime.capture(
            "Nova has a temporary launch code phrase: silver lantern.",
            importance=0.8,
        )
        runtime.correct("", query="silver lantern", action="forget")
        after = runtime.recall("silver lantern", max_results=6)
    finally:
        runtime.close()

    assert "No relevant continuity found" in after
