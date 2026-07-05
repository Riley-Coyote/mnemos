"""U5 — inert DynamicModulation storage: the vessel and its inertness proof.

U5 ships the persistence layer for DynamicModulation values with NO READ PATH —
inertness by absence, not by flag (architecture review §5.9 / hidden-assumption
#9: "a flag is one diff from active; a missing read path is a visible,
reviewable addition"). These tests are the unit's heart:

1. BYTE-IDENTITY — every existing read surface (context packet, reactive
   retrieval/recall+salience, identity profile, engram/belief/hypomnema reads)
   produces byte-identical results against a DB WITH modulation rows vs the
   same DB WITHOUT them. If any read surface ever consumes the table, this
   goes red.
2. STRUCTURAL — no module in the package outside the two sanctioned store
   files (sqlite_store.py: write/backout methods + fresh-DB schema;
   migrations.py: the v10 migration) references the dynamic_modulations table
   at all. A read path added anywhere is caught at the source level even if
   its behavioral effect is subtle.
3. MIGRATION RE-RUN SAFETY (GAP-2 class) — re-running migrations on a
   populated DB (meta forced below 10) leaves existing modulation rows
   byte-identical. The v10 migration carries no backfill UPDATE by design.

Plus the write/persist contract: authority restricted to non-evidentiary
channels, magnitude capped, structural invariants pinned by CHECK constraints,
rollout-tag backout.
"""

import pathlib
import re
import sqlite3

import pytest

from mnemos.core.engram import EncodingContext, Engram
from mnemos.core.belief import Belief
from mnemos.core.identity import AgentIdentity
from mnemos.core.types import EngramKind
from mnemos.interface.context_packet import build_context_packet
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.store.migrations import get_current_version, run_migrations
from mnemos.store.sqlite_store import EngramStore


AGENT = "u5-agent"
PERSON = "u5-person"
SCOPE = "u5-scope"


def _populate(store: EngramStore) -> None:
    """A small but real memory graph: engrams, beliefs, hypomnema, identity."""
    for topic in ("harbors", "tides", "lighthouses", "storms"):
        e = Engram(
            content=f"{AGENT} learned something durable about {topic} today",
            content_at_encoding=f"{AGENT} learned something durable about {topic} today",
            kind=EngramKind.SEMANTIC,
            impact=f"insight about {topic}",
            owner_agent_id=AGENT,
            encoding_context=EncodingContext(session_id="u5-s1"),
        )
        store.save_engram(e)
    store.save_belief(
        Belief(
            agent_id=AGENT,
            content="harbors shelter what the storm would scatter",
            confidence=0.7,
            domain="general",
        )
    )
    store.write_hypomnema_entry(
        "tides return on their own schedule, not ours",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=SCOPE,
        domain="topical",
        confidence=0.5,
        salience=0.4,
    )
    identity = AgentIdentity()
    identity.memory_profile.agent_id = AGENT
    identity.invariants = {"values": ["width", "honesty"]}
    store.save_identity(identity)


def _insert_modulations(store: EngramStore) -> None:
    """Rows spanning targets, valences, topics, TTLs — a worst-plausible set.

    All TTLs positive per 016c U5-g2 (ttl_seconds > 0 is schema-pinned);
    the shortest (1s) and a long (86400s) bound the range.
    """
    specs = [
        ("salience", "harbors", -0.9, 1.0, 3600),
        ("activation", "tides", 0.8, -1.0, 1),
        ("retrieval_weight", "lighthouses", 0.0, 0.5, 86400),
        ("salience", "storms", -0.3, -0.25, 60),
    ]
    for i, (target, topic, valence, magnitude, ttl) in enumerate(specs):
        store.store_dynamic_modulation(
            target=target,
            magnitude=magnitude,
            agent_id=AGENT,
            person_id=PERSON,
            project_scope=SCOPE,
            target_topic=topic,
            valence=valence,
            ttl_seconds=ttl,
            decay_rate=0.05,
            rollout_tag="u5-inertness-proof",
            provenance_ids=[f"prov-{i}"],
            payload={"i": i},
            modulation_id=f"u5-mod-{i}",
            created_at=1_700_000_000 + i,
        )


def _snapshot_read_surfaces(store: EngramStore) -> dict:
    """Every read surface named by the charter, captured as comparable data.

    Timestamps that move on every call (reconsolidation's last_accessed, the
    packet's generated_at) are normalized out — they change between two calls
    on an UNCHANGED db and would mask (or fake) a diff. Everything
    content-bearing stays.
    """
    packet_op = build_context_packet(
        store,
        "durable tides harbors",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=SCOPE,
        include_prompt=True,
        packet_mode="operational",
    )
    packet_review = build_context_packet(
        store,
        "durable tides harbors",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=SCOPE,
        include_prompt=True,
        packet_mode="review",
    )
    for p in (packet_op, packet_review):
        p.pop("generated_at", None)

    retriever = ReactiveRetriever(store)
    recall = [
        (
            r.engram.id,
            r.engram.content,
            round(r.score, 9),
            repr(sorted((k, round(v, 9)) for k, v in r.score_breakdown.items())),
            r.retrieval_path,
        )
        for r in retriever.retrieve("durable harbors storms", agent_id=AGENT)
    ]

    identity = store.get_identity(AGENT)
    identity_view = (
        None
        if identity is None
        else (
            identity.memory_profile.agent_id,
            repr(sorted(identity.invariants.items())),
        )
    )

    engrams = [
        (e.id, e.content, round(e.strength, 9), round(e.accessibility, 9), str(e.state))
        for e in store.get_active_engrams(agent_id=AGENT, limit=100)
    ]
    beliefs = [
        (b.id, b.content, round(b.confidence, 9), b.domain)
        for b in store.get_beliefs(AGENT, active_only=True)
    ]
    hypomnema = [
        (h["id"], h["content"], h["domain"], h["read_visibility"])
        for h in store.search_hypomnema(
            "tides",
            agent_id=AGENT,
            person_id=PERSON,
            project_scope=SCOPE,
            limit=20,
        )
    ]
    fts = [(e.id, e.content) for e in store.search_fts("harbors OR storms", limit=20)]
    return {
        "packet_operational": packet_op,
        "packet_review": packet_review,
        "recall": recall,
        "identity": identity_view,
        "engrams": engrams,
        "beliefs": beliefs,
        "hypomnema": hypomnema,
        "fts": fts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. THE INERTNESS PROOF — byte-identity across every read surface
# ─────────────────────────────────────────────────────────────────────────────


def test_u5_read_surfaces_byte_identical_with_and_without_modulations(tmp_path):
    """The heart of U5: modulation rows influence NOTHING that reads memory.

    Two identical populated stores; one receives modulation rows. Every read
    surface — operational + review context packets, reactive recall/salience,
    identity profile, engram/belief/hypomnema/FTS reads — must return
    byte-identical results. Read order: the WITH-modulations store is read
    FIRST so any ordering/reconsolidation artifact would show up as a diff,
    not hide as one.
    """
    store_plain = EngramStore(tmp_path / "plain.db")
    store_mod = EngramStore(tmp_path / "mod.db")
    _populate(store_plain)
    _populate(store_mod)

    # Deterministic populate check: same content in both stores (IDs differ —
    # ULIDs — so compare by content sets before snapshotting).
    plain_contents = {e.content for e in store_plain.get_active_engrams(agent_id=AGENT)}
    mod_contents = {e.content for e in store_mod.get_active_engrams(agent_id=AGENT)}
    assert plain_contents == mod_contents and len(plain_contents) == 4

    _insert_modulations(store_mod)
    assert store_mod.count_dynamic_modulations(rollout_tag="u5-inertness-proof") == 4
    assert store_plain.count_dynamic_modulations() == 0

    snap_mod = _snapshot_read_surfaces(store_mod)
    snap_plain = _snapshot_read_surfaces(store_plain)

    # IDs are per-store ULIDs; compare content-bearing projections per surface.
    def strip_ids(surface):
        return [tuple(x for x in row[1:]) for row in surface]

    assert strip_ids(snap_mod["recall"]) == strip_ids(snap_plain["recall"])
    assert strip_ids(snap_mod["engrams"]) == strip_ids(snap_plain["engrams"])
    assert strip_ids(snap_mod["beliefs"]) == strip_ids(snap_plain["beliefs"])
    assert strip_ids(snap_mod["hypomnema"]) == strip_ids(snap_plain["hypomnema"])
    assert strip_ids(snap_mod["fts"]) == strip_ids(snap_plain["fts"])
    assert snap_mod["identity"] == snap_plain["identity"]

    # Context packets: normalize the per-store IDs embedded in dict payloads
    # and drop per-store wall-clock timestamps (the two stores were populated
    # at different instants — that noise is not a modulation effect; the
    # single-store test below asserts RAW equality with timestamps included).
    _clock_keys = {
        "created_at",
        "updated_at",
        "last_revised",
        "last_revised_at",
        "last_challenged",
        "last_accessed",
        "first_created",
        "generated_at",
        "original_timestamp",
        "kernel_id",
    }

    def normalize(obj, id_map):
        if isinstance(obj, dict):
            return {
                k: normalize(v, id_map)
                for k, v in sorted(obj.items())
                if k not in _clock_keys
            }
        if isinstance(obj, list):
            return [normalize(v, id_map) for v in obj]
        if isinstance(obj, str):
            for old, new in id_map.items():
                obj = obj.replace(old, new)
            return obj
        return obj

    # Build an id map from content-matched engrams/beliefs/hypomnema.
    id_map = {}
    for surface in ("engrams", "beliefs", "hypomnema", "recall"):
        for row_mod, row_plain in zip(
            sorted(snap_mod[surface], key=lambda r: str(r[1])),
            sorted(snap_plain[surface], key=lambda r: str(r[1])),
        ):
            id_map[str(row_mod[0])] = str(row_plain[0])

    assert normalize(snap_mod["packet_operational"], id_map) == normalize(
        snap_plain["packet_operational"], {}
    )
    assert normalize(snap_mod["packet_review"], id_map) == normalize(
        snap_plain["packet_review"], {}
    )

    store_plain.close()
    store_mod.close()


def test_u5_same_store_before_after_modulation_insert_identical(tmp_path):
    """Single-store variant: snapshot → insert modulations → snapshot.

    No cross-store ID mapping needed — every ID is shared, so this variant
    asserts RAW equality including IDs on every surface. Reconsolidation makes
    retrieve() touch last_accessed, so the recall surface is snapshotted with
    rounding only (activation math must not move at all).
    """
    store = EngramStore(tmp_path / "one.db")
    _populate(store)

    # retrieve() reconsolidates (the testing effect: retrieval strengthens the
    # retrieved engram) — a pre-existing read-side mutation unrelated to U5.
    # Warm the store to reconsolidation's fixed point and PROVE stability with
    # two consecutive identical snapshots before inserting anything; only then
    # is a post-insert diff attributable to the modulation rows.
    stable_2 = None
    previous = _snapshot_read_surfaces(store)
    for _ in range(12):  # strengthening saturates at the strength cap
        current = _snapshot_read_surfaces(store)
        if current == previous:
            stable_2 = current
            break
        previous = current
    assert stable_2 is not None, (
        "harness self-check failed: snapshots never stabilized on an "
        "unchanged DB — reconsolidation did not reach its fixed point"
    )

    _insert_modulations(store)
    after = _snapshot_read_surfaces(store)

    assert after == stable_2

    store.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. STRUCTURAL — no module outside the sanctioned writers touches the table
# ─────────────────────────────────────────────────────────────────────────────

# The ONLY files permitted to reference the dynamic_modulations TABLE:
#  - store/sqlite_store.py: fresh-DB schema + the U5 write/persist/backout
#    methods (and the proposal target_surface enum, which predates U5)
#  - store/migrations.py: the v10 migration (and the same pre-existing enum)
_SANCTIONED = {"store/sqlite_store.py", "store/migrations.py"}

# SQL that READS the table (vs the enum string 'dynamic_modulations' inside
# the proposal target_surface whitelist, which is data, not a read).
_READ_SQL = re.compile(r"(FROM|JOIN)\s+dynamic_modulations", re.IGNORECASE)


def _package_root() -> pathlib.Path:
    import mnemos

    return pathlib.Path(mnemos.__file__).parent


def test_u5_no_module_reads_modulation_table_outside_sanctioned_files():
    """Import-graph/source-level inertness: the read path does not exist.

    Scans every .py in the package. Outside the two sanctioned store files, NO
    module may reference dynamic_modulations at all — not a SELECT, not a JOIN,
    not the name. (The proposal target_surface enum lives inside the
    sanctioned files.) This catches a read path at the source level anywhere:
    retrieval, salience, context packet, identity, consolidation, inner_life,
    substrate, advanced, interface, importer, MCP.
    """
    root = _package_root()
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root))
        if rel in _SANCTIONED:
            continue
        text = path.read_text(encoding="utf-8")
        if "dynamic_modulations" in text:
            offenders.append(rel)
    assert offenders == [], (
        f"dynamic_modulations referenced outside sanctioned store files: "
        f"{offenders} — U5 inertness is by ABSENCE of a read path; a new "
        "consumer requires the U6 activation ruling, not a quiet import."
    )


def test_u5_sanctioned_files_contain_no_ranking_read_of_modulations():
    """Even inside the sanctioned files, no SELECT feeds ranking/retrieval.

    The permitted reads are exactly: get_dynamic_modulation (single row by
    primary key, lifecycle/inspection) and count_dynamic_modulations
    (COUNT(*), telemetry/backout). Assert every FROM/JOIN on the table in the
    sanctioned files is one of those two shapes — no ORDER BY relevance, no
    JOIN against engrams/beliefs/hypomnema, no LIMIT-N ranked scan.
    """
    root = _package_root()
    allowed_line_shapes = (
        "SELECT * FROM dynamic_modulations WHERE id = ?",
        "SELECT COUNT(*) FROM dynamic_modulations",
        "DELETE FROM dynamic_modulations WHERE rollout_tag = ?",
    )
    for rel in sorted(_SANCTIONED):
        text = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"[^\n]*(?:FROM|JOIN)\s+dynamic_modulations[^\n]*", text):
            line = " ".join(m.group(0).split())
            assert any(shape in line for shape in allowed_line_shapes), (
                f"{rel}: unsanctioned SQL against dynamic_modulations: {line!r}"
            )
        # No JOIN between the modulation table and any memory table, anywhere.
        assert not re.search(
            r"dynamic_modulations[^\n]*JOIN\s+(engrams|beliefs|hypomnema_entries)",
            text,
            re.IGNORECASE,
        )
        assert not re.search(
            r"(engrams|beliefs|hypomnema_entries)[^\n]*JOIN\s+dynamic_modulations",
            text,
            re.IGNORECASE,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. MIGRATION — v10 present, idempotent, re-run safe on a populated DB
# ─────────────────────────────────────────────────────────────────────────────


def test_u5_fresh_store_lands_v10_with_table_and_indexes(tmp_path):
    store = EngramStore(tmp_path / "fresh.db")
    conn = store._get_conn()
    assert get_current_version(conn) == 10
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dynamic_modulations'"
        ).fetchone()
        is not None
    )
    index_names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='dynamic_modulations'"
        )
    }
    assert "idx_dynamic_modulations_rollout" in index_names
    assert "idx_dynamic_modulations_scope" in index_names
    store.close()


def test_u5_migration_rerun_on_populated_db_preserves_rows_byte_identical(tmp_path):
    """GAP-2 class: a meta-rewind re-run must not clobber populated rows.

    The hazard class (reports/014 Phase 0): v6's unconditional backfill UPDATE
    rewrote witnessed rows on re-run. The v10 migration carries NO UPDATE by
    design; this test pins that property against regression — populate, force
    meta below 10, re-run to head, assert rows byte-identical.
    """
    store = EngramStore(tmp_path / "rerun.db")
    _insert_modulations(store)
    conn = store._get_conn()
    before = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM dynamic_modulations ORDER BY id"
        ).fetchall()
    ]
    assert len(before) == 4

    conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    conn.commit()
    applied = run_migrations(conn)
    assert applied == [10]

    after = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM dynamic_modulations ORDER BY id"
        ).fetchall()
    ]
    assert after == before  # byte-identical: no clobber, no backfill
    assert get_current_version(conn) == 10
    store.close()


def test_u5_migration_upgrades_a_pre_v10_db(tmp_path):
    """A real pre-v10 DB (table dropped, meta at 9) gains the table on migrate."""
    store = EngramStore(tmp_path / "prev10.db")
    conn = store._get_conn()
    conn.execute("DROP TABLE dynamic_modulations")
    conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    conn.commit()
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dynamic_modulations'"
        ).fetchone()
        is None
    )

    applied = run_migrations(conn)
    assert applied == [10]
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dynamic_modulations'"
        ).fetchone()
        is not None
    )
    store.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. THE WRITE/PERSIST CONTRACT
# ─────────────────────────────────────────────────────────────────────────────


def test_u5_store_and_load_roundtrip(tmp_path):
    store = EngramStore(tmp_path / "rt.db")
    rec = store.store_dynamic_modulation(
        target="retrieval_weight",
        magnitude=-0.75,
        source_authority="observed",
        agent_id=AGENT,
        person_id=PERSON,
        project_scope=SCOPE,
        target_topic="storms",
        valence=-0.4,
        ttl_seconds=7200,
        decay_rate=0.2,
        rollout_tag="u5-rt",
        provenance_ids=["p1", " p2 ", ""],
        payload={"origin": "test"},
        created_at=1_700_000_500,
    )
    assert rec["target"] == "retrieval_weight"
    assert rec["magnitude"] == -0.75
    assert rec["source_authority"] == "observed"
    assert rec["target_topic"] == "storms"
    assert rec["valence"] == -0.4
    assert rec["ttl_seconds"] == 7200
    assert rec["expires_at"] == 1_700_000_500 + 7200
    assert rec["provenance_ids"] == ["p1", "p2"]  # trimmed, empties dropped
    assert rec["payload"] == {"origin": "test"}
    # Structural invariants pinned in the row itself:
    assert rec["evidentiary"] == 0
    assert rec["recurrence_promote"] == 0
    assert rec["identity_authority"] == 0

    loaded = store.get_dynamic_modulation(rec["id"])
    assert loaded == rec
    assert store.get_dynamic_modulation("missing") is None
    store.close()


def test_u5_g1_requires_non_empty_rollout_tag(tmp_path):
    """016c U5-g1: every row must be reachable by the tag-scoped backout.

    No defaults, all rows, both authorities — an untagged row cannot be
    written through the method layer.
    """
    store = EngramStore(tmp_path / "g1.db")
    for authority in ("generated", "observed"):
        for bad_tag in ("", "   ", None):
            with pytest.raises(ValueError, match="rollout_tag"):
                store.store_dynamic_modulation(
                    target="salience",
                    magnitude=0.1,
                    rollout_tag=bad_tag,
                    ttl_seconds=60,
                    source_authority=authority,
                )
    # And the parameter is required, not defaulted:
    with pytest.raises(TypeError):
        store.store_dynamic_modulation(target="salience", magnitude=0.1, ttl_seconds=60)
    assert store.count_dynamic_modulations() == 0
    store.close()


def test_u5_g2_requires_positive_ttl(tmp_path):
    """016c U5-g2: a modulation is decaying residue — no permanent rows.

    ttl_seconds must be a positive integer; 0 (permanent) and negatives are
    refused for both authorities, and the parameter itself is required.
    """
    store = EngramStore(tmp_path / "g2.db")
    for authority in ("generated", "observed"):
        for bad_ttl in (0, -5, "soon"):
            with pytest.raises(ValueError, match="ttl_seconds"):
                store.store_dynamic_modulation(
                    target="salience",
                    magnitude=0.1,
                    rollout_tag="g2-tag",
                    ttl_seconds=bad_ttl,
                    source_authority=authority,
                )
    with pytest.raises(TypeError):
        store.store_dynamic_modulation(
            target="salience", magnitude=0.1, rollout_tag="g2-tag"
        )
    # Consequence: every stored row expires.
    rec = store.store_dynamic_modulation(
        target="salience",
        magnitude=0.1,
        rollout_tag="g2-tag",
        ttl_seconds=1,
        created_at=1_700_000_000,
    )
    assert rec["expires_at"] == 1_700_000_001
    assert store.count_dynamic_modulations() == 1
    store.close()


def test_u5_rejects_evidentiary_authorities(tmp_path):
    """evidentiary is FALSE always → user_stated/imported can never author one."""
    store = EngramStore(tmp_path / "auth.db")
    for authority in ("user_stated", "imported", "harness", ""):
        with pytest.raises(ValueError):
            store.store_dynamic_modulation(
                target="salience",
                magnitude=0.1,
                rollout_tag="auth-tag",
                ttl_seconds=60,
                source_authority=authority,
            )
    store.close()


def test_u5_rejects_bad_target_magnitude_ttl_decay(tmp_path):
    store = EngramStore(tmp_path / "val.db")
    kw = {"rollout_tag": "val-tag", "ttl_seconds": 60}
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(target="identity", magnitude=0.1, **kw)
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(target="salience", magnitude=1.01, **kw)
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(target="salience", magnitude=-1.01, **kw)
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(target="salience", magnitude="wide", **kw)
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(
            target="salience",
            magnitude=0.1,
            rollout_tag="val-tag",
            ttl_seconds=-5,
        )
    with pytest.raises(ValueError):
        store.store_dynamic_modulation(
            target="salience", magnitude=0.1, decay_rate=-0.1, **kw
        )
    store.close()


def test_u5_check_constraints_hold_against_raw_sql(tmp_path):
    """The invariants survive a writer that bypasses the store method.

    The base INSERT carries a valid tag + ttl + expiry so each case isolates
    exactly the CHECK under test (016c/016d: rollout_tag/ttl_seconds/
    expires_at have no DEFAULT, so they must be supplied for the row to reach
    the mutated column's CHECK).
    """
    store = EngramStore(tmp_path / "raw.db")
    conn = store._get_conn()
    base = (
        "INSERT INTO dynamic_modulations "
        "(id, source_authority, target, magnitude, created_at, "
        "rollout_tag, ttl_seconds, expires_at, {col}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for col, bad in (
        ("evidentiary", 1),
        ("recurrence_promote", 1),
        ("identity_authority", 1),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                base.format(col=col),
                (f"raw-{col}", "generated", "salience", 0.5, 1, "raw-tag", 60, 61, bad),
            )
    valid_prefix = (
        "INSERT INTO dynamic_modulations "
        "(id, source_authority, target, magnitude, created_at, "
        "rollout_tag, ttl_seconds, expires_at) VALUES "
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            valid_prefix
            + "('raw-mag', 'generated', 'salience', 2.0, 1, 'raw-tag', 60, 61)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            valid_prefix
            + "('raw-auth', 'user_stated', 'salience', 0.5, 1, 'raw-tag', 60, 61)"
        )
    store.close()


def test_u5_g1_check_pins_non_empty_tag_against_raw_sql(tmp_path):
    """016c U5-g1 schema layer: a raw-SQL writer cannot mint an untagged row.

    CHECK (rollout_tag <> '' AND ...) catches the empty string; omitting the
    column entirely hits NOT NULL (no DEFAULT exists to sneak past either).
    """
    store = EngramStore(tmp_path / "g1raw.db")
    conn = store._get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dynamic_modulations "
            "(id, source_authority, target, magnitude, created_at, "
            "rollout_tag, ttl_seconds, expires_at) "
            "VALUES ('g1-empty', 'generated', 'salience', 0.5, 1, '', 60, 61)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dynamic_modulations "
            "(id, source_authority, target, magnitude, created_at, "
            "ttl_seconds, expires_at) "
            "VALUES ('g1-omitted', 'generated', 'salience', 0.5, 1, 60, 61)"
        )
    store.close()


def test_u5_g3_check_pins_normalized_tag_against_raw_sql(tmp_path):
    """016d U5-g3 schema layer: the whitespace-bypass class is closed.

    A padded or whitespace-edged tag would pass a bare non-empty CHECK yet be
    unreachable by the normalizing backout delete — the T3 D4
    whitespace-bypass class at a new surface. The CHECK's trim charset is the
    FULL ASCII whitespace set: SQLite's bare trim() strips only 0x20 and
    would let tab/newline-edged tags through (proven live during this round —
    'u5\\t' and '\\nu5' passed a bare-trim CHECK while remaining
    backout-unreachable).
    """
    store = EngramStore(tmp_path / "g3raw.db")
    conn = store._get_conn()
    bad_tags = ("   ", " u5 ", "u5\t", "\nu5", "u5\x0b", "u5\x0c", "\ru5")
    for i, bad_tag in enumerate(bad_tags):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dynamic_modulations "
                "(id, source_authority, target, magnitude, created_at, "
                "rollout_tag, ttl_seconds, expires_at) "
                f"VALUES ('g3-{i}', 'generated', 'salience', 0.5, 1, ?, 60, 61)",
                (bad_tag,),
            )
    store.close()


def test_u5_g3_backout_reaches_every_schema_legal_tag(tmp_path):
    """016d U5-g3 closure property: schema-legal ⟹ backout-reachable.

    The schema CHECK's trim charset and the backout delete's strip charset
    are the SAME set (MODULATION_TAG_EDGE_WS), so any row that can exist can
    be deleted by its own tag. Proven over raw-SQL-inserted rows (the
    adversarial writer), including interior whitespace (legal — trimming is
    edge-only) and a unicode-whitespace-EDGED tag: '\\xa0u5' is schema-legal
    (NBSP is not in the ASCII charset) and stays reachable precisely because
    the delete's strip uses the same charset and preserves it — the case a
    bare Python .strip() would have missed.
    """
    store = EngramStore(tmp_path / "g3close.db")
    conn = store._get_conn()
    legal_tags = ["u5", "u5 mid space", "u5-mixed_1", "étiquette", "\xa0u5"]
    for i, tag in enumerate(legal_tags):
        conn.execute(
            "INSERT INTO dynamic_modulations "
            "(id, source_authority, target, magnitude, created_at, "
            "rollout_tag, ttl_seconds, expires_at) "
            f"VALUES ('g3c-{i}', 'generated', 'salience', 0.5, 1, ?, 60, 61)",
            (tag,),
        )
    conn.commit()
    assert store.count_dynamic_modulations() == len(legal_tags)
    for tag in legal_tags:
        assert store.delete_dynamic_modulations_by_rollout_tag(tag) == 1, (
            f"schema-legal tag {tag!r} was not reachable by the backout delete"
        )
    assert store.count_dynamic_modulations() == 0
    store.close()


def test_u5_g3_method_normalization_matches_schema_charset(tmp_path):
    """016d g3 method layer: stored tags are normalized with the SAME charset.

    ASCII-whitespace edges are stripped before storage; a unicode-whitespace
    edge is preserved (schema-legal, reachable). The two normalizations
    agreeing IS the invariant — this pins the Python half.
    """
    store = EngramStore(tmp_path / "g3method.db")
    rec = store.store_dynamic_modulation(
        target="salience",
        magnitude=0.1,
        rollout_tag="\t padded-tag \n",
        ttl_seconds=60,
    )
    assert rec["rollout_tag"] == "padded-tag"
    rec2 = store.store_dynamic_modulation(
        target="salience",
        magnitude=0.1,
        rollout_tag="\xa0nbsp-tag",
        ttl_seconds=60,
    )
    assert rec2["rollout_tag"] == "\xa0nbsp-tag"  # preserved, and reachable:
    assert store.delete_dynamic_modulations_by_rollout_tag("\xa0nbsp-tag") == 1
    store.close()


def test_u5_g4_check_pins_expiry_against_raw_sql(tmp_path):
    """016d U5-g4 schema layer: the OPERATIVE column cannot express permanence.

    016c pinned the input (ttl_seconds > 0); raw SQL could still mint ttl>0
    with NULL expires_at — permanent anyway. NOT NULL closes the NULL escape;
    CHECK (expires_at > created_at) closes the backdated/equal escape.
    """
    store = EngramStore(tmp_path / "g4raw.db")
    conn = store._get_conn()
    prefix = (
        "INSERT INTO dynamic_modulations "
        "(id, source_authority, target, magnitude, created_at, "
        "rollout_tag, ttl_seconds, expires_at) VALUES "
    )
    # NULL expiry (explicit and by omission) → IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            prefix + "('g4-null', 'generated', 'salience', 0.5, 100, "
            "'g4-tag', 60, NULL)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dynamic_modulations "
            "(id, source_authority, target, magnitude, created_at, "
            "rollout_tag, ttl_seconds) "
            "VALUES ('g4-omitted', 'generated', 'salience', 0.5, 100, "
            "'g4-tag', 60)"
        )
    # expires_at == created_at and expires_at < created_at → IntegrityError
    for i, bad_expiry in enumerate((100, 99)):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                prefix + f"('g4-rel-{i}', 'generated', 'salience', 0.5, 100, "
                f"'g4-tag', 60, {bad_expiry})"
            )
    # Sanity: a consistent row is accepted.
    conn.execute(
        prefix + "('g4-ok', 'generated', 'salience', 0.5, 100, 'g4-tag', 60, 160)"
    )
    conn.commit()
    assert store.count_dynamic_modulations() == 1
    store.close()


def test_u5_g2_check_pins_positive_ttl_against_raw_sql(tmp_path):
    """016c U5-g2 schema layer: a raw-SQL writer cannot mint a permanent row.

    CHECK (ttl_seconds > 0) catches 0 and negatives; omitting the column
    hits NOT NULL (no DEFAULT).
    """
    store = EngramStore(tmp_path / "g2raw.db")
    conn = store._get_conn()
    for bad_ttl in (0, -1):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dynamic_modulations "
                "(id, source_authority, target, magnitude, created_at, "
                "rollout_tag, ttl_seconds) "
                f"VALUES ('g2-{bad_ttl}', 'generated', 'salience', 0.5, 1, "
                f"'g2-tag', {bad_ttl})"
            )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dynamic_modulations "
            "(id, source_authority, target, magnitude, created_at, rollout_tag) "
            "VALUES ('g2-omitted', 'generated', 'salience', 0.5, 1, 'g2-tag')"
        )
    store.close()


def test_u5_schema_and_migration_create_byte_identical():
    """016c ruling: the two CREATE bodies stay byte-identical.

    Extracts the dynamic_modulations CREATE from SQL_CREATE_TABLES and from
    apply_dynamic_modulation_storage_migration's source, normalizes leading
    whitespace per line, and requires exact equality — schema drift between
    the fresh-DB path and the migration path fails here.
    """
    import inspect

    from mnemos.store import migrations as migrations_module
    from mnemos.store import sqlite_store as store_module

    def extract_create(text: str) -> list[str]:
        """Line-based capture: from the CREATE line to the standalone ')'.

        A paren-matching regex truncates at the first ')' inside a CHECK's
        IN (...) list and silently compares only the table head — proven by
        mutation (the g1 CHECK-drop left a regex-based version green).
        """
        lines = text.splitlines()
        start = next(
            i
            for i, line in enumerate(lines)
            if "CREATE TABLE IF NOT EXISTS dynamic_modulations" in line
        )
        body: list[str] = []
        for line in lines[start + 1 :]:
            stripped = line.strip()
            if stripped in (")", ");"):
                break
            if stripped:
                body.append(stripped)
        assert len(body) >= 15, f"suspiciously short CREATE body: {len(body)} lines"
        return body

    schema_body = extract_create(store_module.SQL_CREATE_TABLES)
    migration_body = extract_create(
        inspect.getsource(migrations_module.apply_dynamic_modulation_storage_migration)
    )
    assert schema_body == migration_body


def test_u5_backout_by_rollout_tag(tmp_path):
    """Every generated row is deletable by tag; a blank tag is refused."""
    store = EngramStore(tmp_path / "backout.db")
    _insert_modulations(store)
    store.store_dynamic_modulation(
        target="salience",
        magnitude=0.2,
        rollout_tag="other-tag",
        ttl_seconds=60,
        modulation_id="keep-me",
    )
    assert store.count_dynamic_modulations() == 5

    removed = store.delete_dynamic_modulations_by_rollout_tag("u5-inertness-proof")
    assert removed == 4
    assert store.count_dynamic_modulations() == 1
    assert store.get_dynamic_modulation("keep-me") is not None

    with pytest.raises(ValueError):
        store.delete_dynamic_modulations_by_rollout_tag("")
    with pytest.raises(ValueError):
        store.delete_dynamic_modulations_by_rollout_tag("   ")
    assert store.count_dynamic_modulations() == 1
    store.close()


def test_u5_count_filters(tmp_path):
    store = EngramStore(tmp_path / "count.db")
    _insert_modulations(store)
    assert store.count_dynamic_modulations() == 4
    assert store.count_dynamic_modulations(agent_id=AGENT) == 4
    assert store.count_dynamic_modulations(agent_id="someone-else") == 0
    assert store.count_dynamic_modulations(rollout_tag="u5-inertness-proof") == 4
    assert store.count_dynamic_modulations(rollout_tag="absent") == 0
    store.close()


def test_u5_count_normalizes_rollout_tag_filter(tmp_path):
    """016d rule, last surface of the class: count's tag filter normalizes.

    Writes and the backout delete normalize with MODULATION_TAG_EDGE_WS; a
    count comparing the RAW caller input would report a false 0 for a padded
    operator tag during backout verification. Padded-tag count must equal
    normalized-tag count — across every charset member, and for the NBSP
    edge the charset deliberately preserves.
    """
    store = EngramStore(tmp_path / "countnorm.db")
    _insert_modulations(store)  # 4 rows, tag "u5-inertness-proof"
    clean = store.count_dynamic_modulations(rollout_tag="u5-inertness-proof")
    assert clean == 4
    for padded in (
        " u5-inertness-proof ",
        "\tu5-inertness-proof",
        "u5-inertness-proof\n",
        "\r u5-inertness-proof \x0b\x0c",
    ):
        assert store.count_dynamic_modulations(rollout_tag=padded) == clean, (
            f"padded operator tag {padded!r} reported a different count"
        )
    # NBSP is NOT in the charset (schema-legal edge) — a distinct tag, not
    # normalized away: counting it must NOT alias onto the plain tag.
    assert store.count_dynamic_modulations(rollout_tag="\xa0u5-inertness-proof") == 0
    store.close()
