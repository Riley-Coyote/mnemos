"""Tests for the simple continuity product surface."""

import re

from mnemos.simple_runtime import MnemosRuntime, format_health_card


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


def test_context_and_recall_hide_review_only_prose(tmp_path):
    from mnemos.core.engram import Engram
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "simple.db")
    seed = EngramStore(db_path)
    try:
        seed.save_engram(
            Engram(
                content="Review-only runtime engram phrase should stay hidden.",
                owner_agent_id="nova",
                read_visibility="review_only",
            )
        )
        seed.write_hypomnema_entry(
            "Review-only runtime continuity phrase should stay hidden.",
            agent_id="nova",
            person_id="riley",
            project_scope="demo",
            confidence=0.95,
            salience=0.9,
            foundational=True,
            read_visibility="review_only",
        )
    finally:
        seed.close()

    runtime = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        context = runtime.context("runtime phrase", max_results=6)
        recall = runtime.recall("runtime phrase", max_results=6)
    finally:
        runtime.close()

    assert "Review-only runtime engram phrase" not in context
    assert "Review-only runtime continuity phrase" not in context
    assert "Review-only runtime engram phrase" not in recall
    assert "Review-only runtime continuity phrase" not in recall


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


def test_identity_graph_snapshot_contains_svg_and_structured_data(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "simple.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.capture("Nova prefers clear memory visualizations.", importance=0.9)
        runtime.capture("Decision: the identity graph should be an optional artifact.", importance=0.85)
        graph = runtime.identity_graph(max_nodes=12)
    finally:
        runtime.close()

    assert graph["scope"] == {
        "agent_id": "nova",
        "person_id": "riley",
        "project_scope": "demo",
    }
    assert graph["stats"]["active_memories"] >= 2
    assert any(node["kind"] == "agent" for node in graph["nodes"])
    assert any(node["kind"] == "continuity" for node in graph["nodes"])
    assert graph["edges"]
    assert graph["timeline"]
    assert graph["svg"].startswith("<svg")
    assert "Mnemos Identity Graph" in graph["svg"]


def test_fresh_store_context_shows_onboarding_block(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "fresh.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        packet = runtime.context()
    finally:
        runtime.close()

    assert "ONBOARDING - first session" in packet
    assert "mnemos_introduce" in packet
    for step in ("1.", "2.", "3.", "4.", "5.", "6."):
        assert step in packet


def test_predating_store_is_grandfathered(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "legacy.db")
    seed = EngramStore(db_path)
    try:
        seed.write_hypomnema_entry(
            "Legacy continuity that predates the onboarding ritual.",
            agent_id="nova",
            person_id="riley",
            project_scope="demo",
        )
    finally:
        seed.close()

    runtime = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )
    try:
        packet = runtime.context()
    finally:
        runtime.close()

    assert "ONBOARDING" not in packet

    store = EngramStore(db_path)
    try:
        assert store.get_meta("simple:nova:riley:demo:onboarding_stage") == "complete"
        assert store.get_meta("simple:nova:riley:demo:verified_at") == "skipped"
    finally:
        store.close()


def test_onboarding_block_shortens_after_introduce(tmp_path):
    runtime = MnemosRuntime(
        db_path=str(tmp_path / "shorten.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.introduce("claude-opus-4-6")
        packet = runtime.context()
    finally:
        runtime.close()

    assert "ONBOARDING - almost done" in packet
    assert "ONBOARDING - first session" not in packet
    # The introduce bullet must be gone once the agent has introduced itself...
    assert "Call mnemos_introduce with agent_model" not in packet
    # ...while the capture bullet remains until something has been captured.
    assert "Ask the human for one small, true fact about themselves" in packet


def test_onboarding_completes_after_introduce_and_capture(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "complete.db")
    runtime = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        runtime.introduce("claude-opus-4-6")
        runtime.capture("My name is Sam")
        packet = runtime.context()
    finally:
        runtime.close()

    assert "ONBOARDING" not in packet

    store = EngramStore(db_path)
    try:
        assert store.get_meta("simple:nova:riley:demo:onboarding_stage") == "complete"
    finally:
        store.close()


def test_introduce_persists_meta_and_confirms(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "introduce.db")
    runtime = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        result = runtime.introduce("claude-opus-4-6", agent_name="Nova")
    finally:
        runtime.close()

    assert "Introduction recorded." in result
    assert "Agent model: claude-opus-4-6" in result
    assert "Agent name: Nova" in result

    store = EngramStore(db_path)
    try:
        assert store.get_meta("simple:nova:riley:demo:agent_model") == "claude-opus-4-6"
        assert store.get_meta("simple:nova:riley:demo:agent_name") == "Nova"
    finally:
        store.close()


def test_introduce_rejects_empty_model(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "reject.db")
    runtime = MnemosRuntime(
        db_path=db_path,
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )

    try:
        result = runtime.introduce("   ")
    finally:
        runtime.close()

    assert result == (
        "Introduction needs agent_model: your own model id "
        "(for example claude-sonnet-4-6)."
    )

    store = EngramStore(db_path)
    try:
        assert store.get_meta("simple:nova:riley:demo:agent_model") is None
    finally:
        store.close()


def test_introduce_hint_gates_dedicated_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("MNEMOS_MODEL", "anthropic/claude-sonnet-4-5")

    runtime = MnemosRuntime(
        db_path=str(tmp_path / "gate.db"),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=True,
    )

    try:
        # gpt agent vs claude substrate — family policy blocks the client.
        runtime.introduce("gpt-5")
        assert runtime.has_dedicated_model is False

        # claude agent vs claude substrate — kin, client comes through.
        runtime.introduce("claude-opus-4-6")
        assert runtime.has_dedicated_model is True
    finally:
        runtime.close()


def _runtime(tmp_path, name="memory.db"):
    return MnemosRuntime(
        db_path=str(tmp_path / name),
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        use_dedicated_model=False,
    )


def test_first_capture_records_meta_once(tmp_path):
    import json

    from mnemos.store.sqlite_store import EngramStore

    runtime = _runtime(tmp_path)
    try:
        first = runtime.capture("Riley keeps a garden gnome by the door")
        runtime.capture("A second fact that must not overwrite the first")
    finally:
        runtime.close()

    memory_id = re.search(r"Memory ID: (\S+)", first).group(1)
    note_id = re.search(r"Continuity note ID: (\S+)", first).group(1)

    store = EngramStore(str(tmp_path / "memory.db"))
    try:
        raw = store.get_meta("simple:nova:riley:demo:first_capture")
    finally:
        store.close()

    assert raw is not None
    payload = json.loads(raw)
    assert payload["engram_id"] == memory_id
    assert payload["note_id"] == note_id
    assert payload["session"] >= 1
    assert len(payload["excerpt"]) <= 160


def test_verification_fires_on_later_session_then_never_again(tmp_path):
    from datetime import datetime

    from mnemos.store.sqlite_store import EngramStore

    runtime1 = _runtime(tmp_path)
    try:
        runtime1.context()
        runtime1.introduce("claude-opus-4-6")
        runtime1.capture("The garden gnome is named Bartholomew")
        same_session = runtime1.context()
    finally:
        runtime1.close()

    assert "MEMORY VERIFIED" not in same_session

    runtime2 = _runtime(tmp_path)
    try:
        later_session = runtime2.context()
    finally:
        runtime2.close()

    assert "MEMORY VERIFIED" in later_session
    assert "Bartholomew" in later_session

    runtime3 = _runtime(tmp_path)
    try:
        after_verified = runtime3.context()
    finally:
        runtime3.close()

    assert "MEMORY VERIFIED" not in after_verified

    store = EngramStore(str(tmp_path / "memory.db"))
    try:
        verified_at = store.get_meta("simple:nova:riley:demo:verified_at")
    finally:
        store.close()

    assert verified_at is not None
    assert datetime.fromisoformat(verified_at) is not None


def test_verification_waits_for_completed_onboarding(tmp_path):
    runtime1 = _runtime(tmp_path)
    try:
        runtime1.capture("A fact captured before any introduction")
    finally:
        runtime1.close()

    runtime2 = _runtime(tmp_path)
    try:
        packet = runtime2.context()
    finally:
        runtime2.close()

    assert "MEMORY VERIFIED" not in packet
    assert "ONBOARDING - almost done" in packet


def test_grandfathered_scope_never_verifies(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "memory.db")
    seed = EngramStore(db_path)
    try:
        seed.write_hypomnema_entry(
            "Legacy continuity that predates verification.",
            agent_id="nova",
            person_id="riley",
            project_scope="demo",
        )
    finally:
        seed.close()

    runtime1 = _runtime(tmp_path)
    try:
        runtime1.context()
        runtime1.capture("something new")
    finally:
        runtime1.close()

    runtime2 = _runtime(tmp_path)
    try:
        packet = runtime2.context()
    finally:
        runtime2.close()

    assert "MEMORY VERIFIED" not in packet

    store = EngramStore(db_path)
    try:
        assert store.get_meta("simple:nova:riley:demo:first_capture") is None
    finally:
        store.close()


def test_session_counter_bumps_once_per_instance(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    runtime1 = _runtime(tmp_path)
    try:
        runtime1.context()
        runtime1.context()
    finally:
        runtime1.close()

    store = EngramStore(str(tmp_path / "memory.db"))
    try:
        assert store.get_meta("simple:nova:riley:demo:session_counter") == "1"
    finally:
        store.close()

    runtime2 = _runtime(tmp_path)
    try:
        runtime2.context()
    finally:
        runtime2.close()

    store = EngramStore(str(tmp_path / "memory.db"))
    try:
        assert store.get_meta("simple:nova:riley:demo:session_counter") == "2"
    finally:
        store.close()


def test_health_returns_structured_dict(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        runtime.capture("Riley prefers health cards that read like plain words")
        data = runtime.health()
    finally:
        runtime.close()

    assert set(data) == {
        "scope",
        "store",
        "counts",
        "last_cycle",
        "affinity",
        "identity",
        "onboarding",
        "verification",
        "dream",
    }
    assert data["counts"]["continuity_notes_active"] >= 1
    assert data["store"]["size_bytes"] > 0
    assert data["store"]["db_path"].endswith(".db")


def test_health_card_renders_expected_lines(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        runtime.capture("Riley keeps a garden gnome by the door")
        card = format_health_card(runtime.health())
    finally:
        runtime.close()

    for needle in (
        "Mnemos health card",
        "Scope:",
        "Affinity:",
        "Verification:",
        "Last dream:",
        "safe to relay",
    ):
        assert needle in card


def test_health_reports_last_cycle_after_maintain(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        runtime.maintain()
        data = runtime.health()
    finally:
        runtime.close()

    assert data["last_cycle"]["cycle_type"] == "shallow"
    assert isinstance(data["last_cycle"]["passes_run"], list)


def test_health_does_not_bump_session_or_write_stage(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    runtime = _runtime(tmp_path)
    try:
        # health() must come first: a truly read-only call leaves a fresh
        # store with no session counter and no onboarding stage behind.
        runtime.health()

        store = EngramStore(str(tmp_path / "memory.db"))
        try:
            assert store.get_meta("simple:nova:riley:demo:session_counter") is None
            assert store.get_meta("simple:nova:riley:demo:onboarding_stage") is None
        finally:
            store.close()

        packet = runtime.context()
    finally:
        runtime.close()

    assert "ONBOARDING - first session" in packet
