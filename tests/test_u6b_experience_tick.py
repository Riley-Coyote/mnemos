"""U6b ExperienceTick — proposes modulations, never writes them.

The tick emits proposals into the ledger (target_surface='dynamic_modulations')
and must: honor the 016c/016d write-contract on the payload, refuse identity-tier
targeting (D7-#2 unanswered), respect a per-family kill-switch, write NO
modulation row (inert — U5 inertness by absence of a read path), and be
follow-the-loop stable (re-tick identical input updates the same pending row,
never multiplies — 018c / 013e).
"""

from mnemos.modulation import ExperienceTick, ProposedModulation
from mnemos.store.sqlite_store import EngramStore


def _store(tmp_path):
    return EngramStore(tmp_path / "u6b.db")


def _obs(**kw):
    base = dict(
        family="reflection",
        target="salience",
        magnitude=0.3,
        ttl_seconds=3600,
        target_topic="dyslexia",
        valence=0.2,
        provenance_ids=("eng-1",),
    )
    base.update(kw)
    return ProposedModulation(**base)


def _count_mod_proposals(store):
    return store._get_conn().execute(
        "SELECT COUNT(*) FROM proposal_ledger "
        "WHERE target_surface='dynamic_modulations'"
    ).fetchone()[0]


def _count_mod_rows(store):
    return store._get_conn().execute(
        "SELECT COUNT(*) FROM dynamic_modulations"
    ).fetchone()[0]


def test_tick_emits_modulation_proposal(tmp_path):
    store = _store(tmp_path)
    rows = ExperienceTick(store).tick([_obs()], rollout_tag="u6b-r1")
    assert len(rows) == 1
    row = rows[0]
    assert row["target_surface"] == "dynamic_modulations"
    assert row["kind"] == "prospective"
    assert row["source_authority"] == "generated"
    assert row["blast_radius"] == "low"
    assert row["domain"] == "situational"
    assert row["status"] == "pending_review"
    assert row["payload"]["target"] == "salience"
    assert row["payload"]["rollout_tag"] == "u6b-r1"
    assert row["payload"]["ttl_seconds"] == 3600


def test_tick_writes_no_modulation_row(tmp_path):
    """Inert by design: the tick proposes; it never writes the vessel."""
    store = _store(tmp_path)
    ExperienceTick(store).tick([_obs(), _obs(family="surprise")], rollout_tag="u6b")
    assert _count_mod_rows(store) == 0, "tick wrote a dynamic_modulations row"


def test_tick_rejects_empty_or_whitespace_rollout_tag(tmp_path):
    store = _store(tmp_path)
    tick = ExperienceTick(store)
    for bad in ("", "   ", "\t\n"):
        try:
            tick.tick([_obs()], rollout_tag=bad)
            assert False, f"empty/ws rollout_tag {bad!r} was accepted"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0, "a rejected tick still wrote a proposal"


def test_tick_rejects_nonpositive_ttl(tmp_path):
    store = _store(tmp_path)
    for bad in (0, -1):
        try:
            ExperienceTick(store).tick([_obs(ttl_seconds=bad)], rollout_tag="u6b")
            assert False, f"ttl_seconds={bad} was accepted"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0


def test_tick_rejects_invalid_target(tmp_path):
    store = _store(tmp_path)
    try:
        ExperienceTick(store).tick([_obs(target="mood")], rollout_tag="u6b")
        assert False, "invalid modulation target accepted"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0


def test_tick_refuses_identity_domain(tmp_path):
    """D7-#2 UNANSWERED: identity-tier modulation targeting is not built."""
    store = _store(tmp_path)
    for dom in ("identity", "foundational"):
        try:
            ExperienceTick(store).tick([_obs(domain=dom)], rollout_tag="u6b")
            assert False, f"domain={dom} accepted (D7-#2 must refuse)"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0


def test_tick_batch_is_atomic_on_validation_failure(tmp_path):
    """One invalid observation aborts the whole batch — no partial emit."""
    store = _store(tmp_path)
    good = _obs(target_topic="a")
    bad = _obs(target_topic="b", ttl_seconds=-5)
    try:
        ExperienceTick(store).tick([good, bad], rollout_tag="u6b")
        assert False, "batch with an invalid observation did not raise"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0, "partial emit leaked before the failure"


def test_kill_switch_disables_family(tmp_path):
    store = _store(tmp_path)
    tick = ExperienceTick(store, enabled_families={"reflection"})
    rows = tick.tick(
        [_obs(family="reflection"), _obs(family="wandering", target_topic="z")],
        rollout_tag="u6b",
    )
    assert len(rows) == 1, "disabled family was not skipped"
    assert rows[0]["payload"]["family"] == "reflection"
    assert _count_mod_proposals(store) == 1


def test_reticks_are_follow_the_loop_stable(tmp_path):
    """018c/013e: re-ticking identical input updates the SAME pending proposal,
    never multiplies. The next cycle (a second tick over the same observation)
    must not re-process into a growing set."""
    store = _store(tmp_path)
    tick = ExperienceTick(store)
    r1 = tick.tick([_obs()], rollout_tag="u6b-loop")
    r2 = tick.tick([_obs()], rollout_tag="u6b-loop")
    r3 = tick.tick([_obs()], rollout_tag="u6b-loop")
    assert r1[0]["id"] == r2[0]["id"] == r3[0]["id"], "re-tick minted a new id"
    assert _count_mod_proposals(store) == 1, (
        "re-ticking identical input multiplied proposals (propose→reprocess loop)"
    )


def test_distinct_observations_get_distinct_proposals(tmp_path):
    """Different (family/target/topic/tag) → distinct rows, so the loop-stability
    is real deduplication, not a collapse that drops genuine proposals."""
    store = _store(tmp_path)
    tick = ExperienceTick(store)
    tick.tick(
        [
            _obs(target_topic="dyslexia"),
            _obs(target_topic="adhd"),
            _obs(family="surprise", target="activation", target_topic="dyslexia"),
        ],
        rollout_tag="u6b-multi",
    )
    assert _count_mod_proposals(store) == 3


# ── 018e gate-finding fixes ───────────────────────────────────────────────────


def test_tick_proposals_are_review_visible(tmp_path):
    """018e #1: the disposal gate reviews via the ordinary review surface, so a
    proposal must be review_only — not the audit-only store default that would
    hide it from the gate and break 'propose→dispose' by construction."""
    store = _store(tmp_path)
    ExperienceTick(store).tick([_obs()], rollout_tag="u6b-rev")
    on_review_surface = store.list_proposals()  # defaults to review-visibility
    assert any(
        p["target_surface"] == "dynamic_modulations" for p in on_review_surface
    ), "emitted modulation proposal is not on the review surface (audit-only?)"
    # And it is genuinely review_only, not operational/audit.
    assert on_review_surface, "no proposal visible to the disposal gate"
    assert all(
        p["read_visibility"] == "review_only" for p in on_review_surface
    )


def test_tick_refuses_whitespace_padded_identity_domain(tmp_path):
    """018e #2 (fourth whitespace-bypass): a domain that only differs from
    'identity' by edge whitespace must still be refused — the guard decides on
    the normalized value, the same normalization write_proposal applies."""
    store = _store(tmp_path)
    for dom in (" identity ", "\tidentity", "identity\n", " foundational"):
        try:
            ExperienceTick(store).tick([_obs(domain=dom)], rollout_tag="u6b")
            assert False, f"whitespace-padded identity domain {dom!r} accepted"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0, "a padded-identity proposal leaked"


def test_tick_rejects_bad_valence(tmp_path):
    """018e #3: valence must be numeric, validated pre-loop."""
    store = _store(tmp_path)
    try:
        ExperienceTick(store).tick([_obs(valence="high")], rollout_tag="u6b")
        assert False, "non-numeric valence accepted"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0


def test_tick_rejects_negative_decay_rate(tmp_path):
    """018e #3: decay_rate must be a non-negative number (matches U5)."""
    store = _store(tmp_path)
    try:
        ExperienceTick(store).tick([_obs(decay_rate=-0.1)], rollout_tag="u6b")
        assert False, "negative decay_rate accepted"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0


def test_batch_atomic_on_bad_valence(tmp_path):
    """018e #3 all-or-nothing: a bad later observation commits NOTHING — the good
    earlier one must not be left behind."""
    store = _store(tmp_path)
    good = _obs(target_topic="a")
    bad = _obs(target_topic="b", valence="nope")
    try:
        ExperienceTick(store).tick([good, bad], rollout_tag="u6b")
        assert False, "batch with a bad-valence observation did not raise"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0, "partial emit before the bad valence"


# ── 018h round-2 gate-finding fixes ───────────────────────────────────────────


def test_batch_atomic_on_store_level_failure(tmp_path):
    """018h #1: a STORE-level raise on a later proposal (an unsupported non-
    identity domain passes _validate's D7-#2 check but fails write_proposal's
    domain enum) rolls back the WHOLE batch — the earlier good proposal, written
    with commit=False, must not survive."""
    store = _store(tmp_path)
    good = _obs(target_topic="a")
    # 'bogus' is not identity/foundational (passes the tick guard) but is not a
    # valid proposal domain (write_proposal raises at the store layer).
    bad = _obs(target_topic="b", domain="bogus")
    try:
        ExperienceTick(store).tick([good, bad], rollout_tag="u6b")
        assert False, "store-level domain rejection did not raise"
    except ValueError:
        pass
    assert _count_mod_proposals(store) == 0, (
        "partial batch persisted — the good proposal committed before the "
        "store-level failure on the bad one (transaction wrap missing)"
    )


def test_tick_rejects_nonfinite_valence(tmp_path):
    """018h #2: NaN/Inf valence must be rejected (isfinite), not serialized."""
    store = _store(tmp_path)
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            ExperienceTick(store).tick([_obs(valence=bad)], rollout_tag="u6b")
            assert False, f"non-finite valence {bad} accepted"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0


def test_tick_rejects_nonfinite_decay(tmp_path):
    """018h #2: NaN/Inf decay_rate must be rejected — Inf passes the (x<0) check
    but is not a valid decay parameter."""
    store = _store(tmp_path)
    for bad in (float("inf"), float("nan")):
        try:
            ExperienceTick(store).tick([_obs(decay_rate=bad)], rollout_tag="u6b")
            assert False, f"non-finite decay_rate {bad} accepted"
        except ValueError:
            pass
    assert _count_mod_proposals(store) == 0
