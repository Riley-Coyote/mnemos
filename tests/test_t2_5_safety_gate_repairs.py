"""T2.5 — inner-life safety-gate repairs (disposition 003d).

Regression tests for the four pre-existing findings surfaced by the T2
no-mistakes run:

1. inner-life-limited-scans-use-oldest-rows  (chokepoint sqlite_store.py)
2. scheduled-wander-dream-scope-lost
3. soak-cli-missing-llm-client
4. low-stakes-idempotency-not-atomic

Each test is written to go red without its fix (mutation-checked in report 004).
"""

from datetime import datetime, timedelta, timezone

from mnemos.store.sqlite_store import EngramStore


def _seed_event(
    store: EngramStore,
    key: str,
    *,
    created_at: str,
    process_name: str = "turn-finalizer",
    event_type: str = "tool_event",
    gate_decision: str = "ledger_only",
    rollout_tag: str = "u6.6-test",
    metadata: dict | None = None,
) -> None:
    """Insert an inner_life_events row with an explicit created_at (the API
    stamps _utc_now(), so timestamps are forced here for deterministic order)."""
    store.upsert_inner_life_event(
        idempotency_key=key,
        event_type=event_type,
        process_name=process_name,
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        gate_decision=gate_decision,
        rollout_tag=rollout_tag,
        metadata=metadata or {},
    )
    conn = store._get_conn()
    conn.execute(
        "UPDATE inner_life_events SET created_at = ? WHERE idempotency_key = ?",
        (created_at, key),
    )
    conn.commit()


# ── Finding 1: oldest-rows recency scan ─────────────────────────────────────


def test_get_inner_life_events_recent_returns_newest_ascending_default_oldest(tmp_path):
    """recent=True selects the newest `limit` rows and returns them ascending;
    the default remains the oldest `limit` rows (historical contract)."""
    store = EngramStore(tmp_path / "recency.db")
    try:
        base = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            _seed_event(
                store, f"e{i}", created_at=(base + timedelta(minutes=i)).isoformat()
            )

        oldest3 = store.get_inner_life_events(
            agent_id="oliver", person_id="david", project_scope="pai", limit=3
        )
        newest3 = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            limit=3,
            recent=True,
        )

        assert [r["idempotency_key"] for r in oldest3] == ["e0", "e1", "e2"]
        # newest three, still ascending so ASC-assuming callers stay correct
        assert [r["idempotency_key"] for r in newest3] == ["e2", "e3", "e4"]
    finally:
        store.close()


def test_cooldown_gate_sees_newest_run_beyond_scan_limit(tmp_path):
    """Disposition 003d's named case: grow the ledger past the scan limit and
    assert the cooldown gate still sees the newest run. Old oldest-N scan saw
    only filler and returned None (cooldown overrun)."""
    from mnemos.inner_life.activity_gate import _cooldown_until

    store = EngramStore(tmp_path / "cooldown.db")
    try:
        base = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        limit = 5
        # `limit` older tool_events the gate loop skips (not activity-gate runs)
        for i in range(limit):
            _seed_event(
                store,
                f"filler-{i}",
                created_at=(base + timedelta(minutes=i)).isoformat(),
            )
        # the real cooldown-run, newer than every filler -> excluded from oldest-N
        run_at = base + timedelta(hours=1)
        _seed_event(
            store,
            "recent-run",
            created_at=run_at.isoformat(),
            process_name="activity-gate",
            gate_decision="run",
            metadata={"target_process": "reflect"},
        )

        until = _cooldown_until(
            store,
            process_name="reflect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            cooldown_minutes=240,
            now=run_at + timedelta(minutes=1),
        )

        assert until == run_at + timedelta(minutes=240)
    finally:
        store.close()


# ── Finding 2: scheduled wander/dream scope threading ───────────────────────


def test_scheduled_wander_persists_under_requested_scope_not_defaults(tmp_path):
    """A scheduled wander must persist its low-stakes ledger row under the
    requested person/project/rollout triple — those rows are the rollback unit.
    The old path dropped the triple and wrote under user/global/u6.6 defaults."""
    from mnemos.core.engram import Engram
    from mnemos.inner_life.scheduler import _run_wander

    class _StubLLM:
        def structured_complete(self, system, user, temperature):
            return '{"thought": "a quiet grounded wandering", "origin": "authorized"}'

    store = EngramStore(tmp_path / "scope.db")
    try:
        store.save_engram(
            Engram(
                content="AUTHORIZED-WANDER-SEED", impact="src", owner_agent_id="oliver"
            )
        )
        result = _run_wander(
            store,
            agent_id="oliver",
            llm_client=_StubLLM(),
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )
        assert result["generated_memory_writes"] == 1

        under_requested = store.get_inner_life_events(
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )
        assert any(
            r.get("process_name") == "low-stakes-writer" for r in under_requested
        )

        # nothing leaked to the writer defaults
        under_defaults = store.get_inner_life_events(
            agent_id="oliver", person_id="user", project_scope="global"
        )
        assert under_defaults == []
    finally:
        store.close()


# ── Finding 3: soak tick wires an LLM client for generative families ─────────

from copy import deepcopy  # noqa: E402
from mnemos.config.defaults import DEFAULT_CONFIG  # noqa: E402
from mnemos.inner_life.preflight import PROCESS_FAMILIES  # noqa: E402


def _enable_soak_tick_config(*, shallow: bool = False, inner_life: tuple = ()):
    config = deepcopy(DEFAULT_CONFIG)
    config["soak"]["tick"]["enabled"] = True
    config["soak"]["families"]["shallow_consolidation"]["enabled"] = shallow
    config["inner_life"]["schedules"]["enabled"] = bool(inner_life)
    for process in PROCESS_FAMILIES:
        config["inner_life"]["schedules"]["processes"][process]["enabled"] = (
            process in inner_life
        )
    return config


def test_soak_tick_wires_llm_client_for_generative_family(tmp_path, monkeypatch):
    """The scheduled soak tick must build an LLM client for a generative family
    when the caller injects none — otherwise wander/dream silently no-op and the
    soak never dreams through this path. create_client() itself stays gated
    (returns None without keys/affinity), so this adds capability, not activation."""
    import mnemos.llm as llm_mod
    from mnemos.soak.tick import run_scheduled_soak_tick

    calls = {"n": 0}

    class _StubLLM:
        def structured_complete(self, system, user, temperature):
            return '{"thought": "a quiet grounded wandering", "origin": "authorized"}'

    def _fake_create_client(*args, **kwargs):
        calls["n"] += 1
        return _StubLLM()

    monkeypatch.setattr(llm_mod, "create_client", _fake_create_client)

    store = EngramStore(tmp_path / "soak-wire.db")
    try:
        run_scheduled_soak_tick(
            store,
            config=_enable_soak_tick_config(inner_life=("wander",)),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="wire-once",
            llm_client=None,
        )
        assert calls["n"] == 1
    finally:
        store.close()


def test_soak_tick_skips_client_build_when_injected_or_non_generative(
    tmp_path, monkeypatch
):
    """Injected client wins (no build); a non-generative-only tick builds none."""
    import mnemos.llm as llm_mod
    from mnemos.soak.tick import run_scheduled_soak_tick

    calls = {"n": 0}

    def _fake_create_client(*args, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(llm_mod, "create_client", _fake_create_client)

    class _Stub:
        def structured_complete(self, system, user, temperature):
            return "{}"

    store = EngramStore(tmp_path / "soak-noclient.db")
    try:
        # injected client → no build
        run_scheduled_soak_tick(
            store,
            config=_enable_soak_tick_config(inner_life=("wander",)),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="injected",
            llm_client=_Stub(),
        )
        assert calls["n"] == 0
        # non-generative only (shallow consolidation) → no build
        run_scheduled_soak_tick(
            store,
            config=_enable_soak_tick_config(shallow=True),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="shallow",
            llm_client=None,
        )
        assert calls["n"] == 0
    finally:
        store.close()


# ── Finding 4: low-stakes idempotency-guard atomicity ───────────────────────


def test_low_stakes_write_is_atomic_no_orphan_or_duplicate_on_crash(
    tmp_path, monkeypatch
):
    """The engram and its idempotency-guard ledger row are one transaction: a
    crash during the ledger write rolls the engram back (no orphan), so the retry
    writes exactly one engram (no duplicate). Old path committed the engram first,
    leaving an orphan that the retry re-minted."""
    import pytest

    from mnemos.inner_life.low_stakes import write_low_stakes_record

    store = EngramStore(tmp_path / "atomic.db")
    try:
        gate_result = {
            "allowed": True,
            "content": "a private wandering to persist",
            "source_ids": ["seed-1"],
        }
        real_upsert = store.upsert_inner_life_event

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-write")

        # crash inside the ledger write (after the engram insert, before commit)
        monkeypatch.setattr(store, "upsert_inner_life_event", _boom)
        with pytest.raises(RuntimeError):
            write_low_stakes_record(
                store,
                gate_result=gate_result,
                candidate_kind="wandering",
                agent_id="oliver",
                person_id="david",
                project_scope="pai",
                rollout_tag="u6.6-test",
            )
        # atomic: the engram rolled back with the failed ledger write — no orphan
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == 0

        # retry (recovered): writes exactly one engram, no duplicate
        monkeypatch.setattr(store, "upsert_inner_life_event", real_upsert)
        write_low_stakes_record(
            store,
            gate_result=gate_result,
            candidate_kind="wandering",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u6.6-test",
        )
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == 1
    finally:
        store.close()


# ── Review findings (004 pipeline): preflight-dry-run + idempotency-race ─────


def test_soak_tick_build_llm_client_false_suppresses_build(tmp_path, monkeypatch):
    """build_llm_client=False (the dry-run preflight path) suppresses the
    auto-build even for a generative family — a copy-DB dry run must never reach
    a real model (review finding preflight-dry-run-can-use-real-llm)."""
    import mnemos.llm as llm_mod
    from mnemos.soak.tick import run_scheduled_soak_tick

    calls = {"n": 0}

    def _fake_create_client(*args, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(llm_mod, "create_client", _fake_create_client)

    store = EngramStore(tmp_path / "soak-dry.db")
    try:
        run_scheduled_soak_tick(
            store,
            config=_enable_soak_tick_config(inner_life=("wander",)),
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            run_id="dry",
            llm_client=None,
            build_llm_client=False,
        )
        assert calls["n"] == 0
    finally:
        store.close()


def test_soak_preflight_dry_run_does_not_reach_real_llm(tmp_path, monkeypatch):
    """The activation preflight runs the tick on a DB copy; it must never build a
    real LLM client (review finding preflight-dry-run-can-use-real-llm)."""
    import mnemos.llm as llm_mod
    from mnemos.soak.preflight import build_soak_activation_preflight

    calls = {"n": 0}

    def _fake_create_client(*args, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(llm_mod, "create_client", _fake_create_client)

    db = tmp_path / "preflight.db"
    EngramStore(db).close()
    build_soak_activation_preflight(
        config=_enable_soak_tick_config(inner_life=("wander",)),
        db_path=str(db),
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        rollout_tag="u7-test",
        run_tick_dry_run=True,
    )
    assert calls["n"] == 0


def test_atomic_write_detects_idempotency_race_and_rolls_back(tmp_path):
    """If the idempotency key was inserted concurrently (between the caller's
    pre-check and the transaction), the staged engram is rolled back — no
    duplicate engram (review finding low-stakes-idempotency-race)."""
    from mnemos.core.engram import Engram

    store = EngramStore(tmp_path / "race.db")
    try:
        # a concurrent writer already reserved this idempotency key
        store.upsert_inner_life_event(
            idempotency_key="race-key",
            event_type="tool_event",
            process_name="low-stakes-writer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        before = store.count_engrams(agent_id="oliver", read_visibility=None)
        outcome = store.save_engram_with_inner_life_event(
            Engram(content="would-be-duplicate", impact="", owner_agent_id="oliver"),
            idempotency_key="race-key",
            event_type="tool_event",
            process_name="low-stakes-writer",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert outcome["duplicate"] is True
        assert store.count_engrams(agent_id="oliver", read_visibility=None) == before
    finally:
        store.close()


# ── Review findings (004 re-run): filter-after-limit on the recency scans ───


def test_cooldown_survives_newer_unrelated_tool_event_noise(tmp_path):
    """Findings C+E (review, error): neither unrelated tool_events NOR newer
    activity-gate *runs for other processes* may push this process's last run out
    of the scan and lapse its cooldown. The full predicate (activity-gate run for
    THIS target_process, via json_extract) is resolved in SQL with LIMIT 1."""
    from mnemos.inner_life.activity_gate import _cooldown_until

    store = EngramStore(tmp_path / "cooldown-noise.db")
    try:
        base = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        run_at = base
        _seed_event(
            store,
            "target-run",
            created_at=run_at.isoformat(),
            process_name="activity-gate",
            gate_decision="run",
            metadata={"target_process": "reflect"},
        )
        # 30 NEWER activity-gate runs for OTHER processes (finding E) interleaved
        # with unrelated tool_events (finding C) — all newer than the target run
        others = ["wander", "dream", "affect", "observe", "challenge"]
        for i in range(30):
            _seed_event(
                store,
                f"other-run-{i}",
                created_at=(base + timedelta(minutes=i + 1)).isoformat(),
                process_name="activity-gate",
                gate_decision="run",
                metadata={"target_process": others[i % len(others)]},
            )
            _seed_event(
                store,
                f"noise-{i}",
                created_at=(base + timedelta(minutes=i + 1, seconds=30)).isoformat(),
                process_name="turn-finalizer",
                gate_decision="ledger_only",
            )
        until = _cooldown_until(
            store,
            process_name="reflect",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            cooldown_minutes=240,
            now=base + timedelta(minutes=60),
        )
        assert until == run_at + timedelta(minutes=240)
    finally:
        store.close()


def test_signal_scan_survives_newer_activity_gate_telemetry(tmp_path):
    """Finding D (review, warning): activity-gate telemetry newer than a real
    signal must not push that signal out of the newest-N scan — telemetry is now
    excluded in SQL before the limit."""
    from mnemos.inner_life.activity_gate import _collect_activity_signals

    store = EngramStore(tmp_path / "signal-noise.db")
    try:
        base = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        event_limit = 5
        _seed_event(
            store,
            "real-signal",
            created_at=base.isoformat(),
            process_name="turn-finalizer",
            event_type="turn_finalized",
        )
        # >event_limit NEWER activity-gate telemetry rows (the noise)
        for i in range(event_limit * 3):
            _seed_event(
                store,
                f"gate-telemetry-{i}",
                created_at=(base + timedelta(seconds=i + 1)).isoformat(),
                process_name="activity-gate",
                event_type="tool_event",
                gate_decision="skipped:no_recent_activity",
            )
        signals = _collect_activity_signals(
            store,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            since=base - timedelta(minutes=1),
            now=base + timedelta(minutes=1),
            event_limit=event_limit,
            consolidation_limit=0,
            consolidation_passes=[],
        )
        assert any(s.source_type == "inner_life:turn_finalized" for s in signals)
    finally:
        store.close()


def test_family_cadence_survives_newer_other_family_rows(tmp_path):
    """Finding F (review, warning): other families' rows (and this family's own
    not-due skips) must not evict the family's last real attempt from the cadence
    scan and make _family_due fire early. The last attempt is now resolved in SQL
    (process_name + exclude skip) with LIMIT 1."""
    from mnemos.soak.tick import _family_due

    store = EngramStore(tmp_path / "cadence.db")
    try:
        base = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        # THIS family's real attempt, oldest
        _seed_event(
            store,
            "wander-attempt",
            created_at=base.isoformat(),
            process_name="wander",
            gate_decision="run",
            rollout_tag="u7-test",
        )
        # >500 NEWER rows (other-family runs + this family's not-due skips) so a
        # limit-500 scan would evict the real attempt (the old bug)
        for i in range(505):
            _seed_event(
                store,
                f"dream-{i}",
                created_at=(base + timedelta(seconds=i + 1)).isoformat(),
                process_name="dream",
                gate_decision="run",
                rollout_tag="u7-test",
            )
        # cadence 240 min; only 10 min since the real attempt -> NOT due
        due = _family_due(
            store,
            family="wander",
            cadence_minutes=240,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            rollout_tag="u7-test",
            now=base + timedelta(minutes=10),
        )
        assert due is False
    finally:
        store.close()


# ── Ruling 004 tooth 2 → RM-7: affect recency residual closed ────────────────


def test_affect_recency_residual_closed(tmp_path):
    """Ruling 004 tooth 2 held affect out of full-scheduled activation while
    `emotional-driver-filter-after-limit` was open. RM-7's paging primitive
    (`_recent_signal_events`) closed the eviction, and per ruling 004b the
    KNOWN_ACTIVATION_BLOCKERS entry and the fix live and die together: a
    schedule-enabled affect must now report no known open issues and no
    known_open_issue blocker. The closure is self-verifying — re-adding an
    affect entry without a fix turns this test red."""
    from mnemos.config.defaults import DEFAULT_CONFIG
    from mnemos.inner_life.preflight import build_inner_life_preflight

    db = tmp_path / "preflight.db"
    EngramStore(db).close()
    config = deepcopy(DEFAULT_CONFIG)
    config["inner_life"]["schedules"]["enabled"] = True
    config["inner_life"]["schedules"]["processes"]["affect"]["enabled"] = True

    pf = build_inner_life_preflight(config=config, db_path=str(db))

    assert pf["processes"]["affect"]["known_open_issues"] == []
    assert not any(
        blocker.startswith("known_open_issue:affect:") for blocker in pf["blockers"]
    )
