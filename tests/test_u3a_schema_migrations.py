import sqlite3

import pytest

import mnemos.store.migrations as migrations
from mnemos.consolidation.connection_discovery import run_connection_discovery
from mnemos.consolidation.decay import run_decay_pass
from mnemos.consolidation.reflection import run_reflection_pass
from mnemos.consolidation.softening import run_softening_pass
from mnemos.core.emotional_state import EmotionalState
from mnemos.core.belief import Belief
from mnemos.core.engram import Connection, Engram
from mnemos.core.identity import AgentIdentity
from mnemos.core.types import ConnectionRelation
from mnemos.substrate.events import EventType, SubstrateEvent
from mnemos.store.migrations import (
    U3A_PHASE0_DECAY_FINDING,
    U3A_U3B_IMPORT_CONTRACT,
    apply_u3a_schema_migration,
    backup_sqlite_db,
    list_migrations,
    run_migrations,
    upsert_pai_import_row,
)
from mnemos.store.sqlite_store import EngramStore, SCHEMA_VERSION
from mnemos.substrate.config import SubstrateConfig
from mnemos.substrate.handlers import dreaming, initiation, wandering
from mnemos.substrate.modulators import ModulatorState, compute_modulators
from mnemos.substrate.tick import Substrate


OLD_TIMESTAMP = "2026-01-01T00:00:00+00:00"


LEGACY_V3_SCHEMA = """
CREATE TABLE engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
    resolution REAL NOT NULL DEFAULT 1.0,
    kind TEXT NOT NULL DEFAULT 'episodic',
    tags TEXT NOT NULL DEFAULT '[]',
    schema_refs TEXT NOT NULL DEFAULT '[]',
    strength REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.1,
    accessibility REAL NOT NULL DEFAULT 0.5,
    encoding_context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '{}',
    lineage TEXT NOT NULL DEFAULT '{}',
    owner_agent_id TEXT NOT NULL DEFAULT 'default',
    visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    reconsolidation_count INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE engrams_fts USING fts5(content, id UNINDEXED);
CREATE TABLE beliefs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.3,
    domain TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    last_revised TEXT NOT NULL,
    last_challenged TEXT NOT NULL,
    revision_history TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    supporting_engram_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE hypomnema_entries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'observed',
    density REAL NOT NULL DEFAULT 0.5,
    domain TEXT NOT NULL DEFAULT 'topical',
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    foundational INTEGER NOT NULL DEFAULT 0,
    revision_count INTEGER NOT NULL DEFAULT 0,
    revisions_json TEXT NOT NULL DEFAULT '[]',
    related_session_id TEXT,
    related_engram_id TEXT,
    graduated_to_engram_id TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    last_revised_at TEXT NOT NULL,
    last_challenged_at TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta(key, value) VALUES ('schema_version', '3');
"""


class StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response

    def structured_complete(self, system: str, user: str, temperature: float) -> str:
        self.prompts.append(user)
        return self.response


def _old_engram(content: str, **kwargs) -> Engram:
    accessibility = kwargs.pop("accessibility", 0.8)
    strength = kwargs.pop("strength", 0.8)
    stability = kwargs.pop("stability", 0.1)
    engram = Engram(
        content=content,
        content_at_encoding=content,
        owner_agent_id=kwargs.pop("owner_agent_id", "oliver"),
        impact=kwargs.pop("impact", "set"),
        **kwargs,
    )
    engram.last_accessed = OLD_TIMESTAMP
    engram.accessibility = accessibility
    engram.strength = strength
    engram.stability = stability
    return engram


def _create_legacy_v3_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_V3_SCHEMA)
        conn.execute(
            """
            INSERT INTO engrams (
                id, content, content_at_encoding, impact, created_at, last_accessed
            ) VALUES ('legacy_e', 'legacy content', 'legacy content', '', ?, ?)
            """,
            (OLD_TIMESTAMP, OLD_TIMESTAMP),
        )
        conn.execute(
            """
            INSERT INTO beliefs (
                id, agent_id, content, confidence, domain,
                created_at, last_revised, last_challenged
            ) VALUES (
                'legacy_b', 'oliver', 'legacy belief', 0.5, 'general', ?, ?, ?
            )
            """,
            (OLD_TIMESTAMP, OLD_TIMESTAMP, OLD_TIMESTAMP),
        )
        conn.commit()
    finally:
        conn.close()


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    assert row is not None
    return row["sql"] if isinstance(row, sqlite3.Row) else row[0]


def _column_map(conn, table: str):
    return {row["name"]: dict(row) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_u3a_schema_fields_roundtrip(tmp_path):
    store = EngramStore(tmp_path / "u3a.db")
    try:
        engram = _old_engram(
            "foundational memory should survive maintenance",
            voice_exemplar_eligible=False,
            softening_protected=True,
            original_substrate="claude-opus-4-6",
            original_timestamp=1710000000,
            consolidation_authorized=False,
            decay_protected=True,
        )
        store.save_engram(engram)
        loaded = store.get_engram(engram.id)

        assert loaded is not None
        assert loaded.voice_exemplar_eligible is False
        assert loaded.softening_protected is True
        assert loaded.original_substrate == "claude-opus-4-6"
        assert loaded.original_timestamp == 1710000000
        assert loaded.consolidation_authorized is False
        assert loaded.decay_protected is True

        belief = Belief(
            agent_id="oliver",
            content="David context is foundational",
            confidence=0.9,
            domain="social",
            tier="foundational",
            needs_review=True,
            confidence_pending_review=True,
        )
        store.save_belief(belief)
        [loaded_belief] = store.get_beliefs(
            agent_id="oliver",
            include_pending_review=True,
        )
        assert loaded_belief.tier == "foundational"
        assert loaded_belief.needs_review is True
        assert loaded_belief.confidence_pending_review is True

        entry_id = store.write_hypomnema_entry(
            "continuity entry from imported history",
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
            original_timestamp=1700000000,
        )
        entry = store.get_hypomnema_entry(
            entry_id,
            agent_id="oliver",
            person_id="david",
            project_scope="pai",
        )
        assert entry["original_timestamp"] == 1700000000
    finally:
        store.close()


def test_u3a_schema_shape_is_introspected(tmp_path):
    store = EngramStore(tmp_path / "schema-shape.db")
    conn = store._get_conn()
    try:
        engram_cols = _column_map(conn, "engrams")
        assert engram_cols["voice_exemplar_eligible"]["notnull"] == 1
        assert engram_cols["voice_exemplar_eligible"]["dflt_value"] == "1"
        assert engram_cols["softening_protected"]["notnull"] == 1
        assert engram_cols["softening_protected"]["dflt_value"] == "0"
        assert engram_cols["consolidation_authorized"]["notnull"] == 1
        assert engram_cols["consolidation_authorized"]["dflt_value"] == "1"
        assert engram_cols["decay_protected"]["notnull"] == 1
        assert engram_cols["decay_protected"]["dflt_value"] == "0"
        assert engram_cols["original_substrate"]["notnull"] == 0
        assert engram_cols["original_timestamp"]["notnull"] == 0

        belief_cols = _column_map(conn, "beliefs")
        assert belief_cols["tier"]["notnull"] == 0
        assert belief_cols["needs_review"]["notnull"] == 1
        assert belief_cols["needs_review"]["dflt_value"] == "0"
        assert belief_cols["confidence_pending_review"]["notnull"] == 1
        assert belief_cols["confidence_pending_review"]["dflt_value"] == "0"

        hypomnema_cols = _column_map(conn, "hypomnema_entries")
        assert hypomnema_cols["original_timestamp"]["notnull"] == 0

        engram_sql = _table_sql(conn, "engrams")
        for fragment in (
            "CHECK (voice_exemplar_eligible IN (0, 1))",
            "CHECK (softening_protected IN (0, 1))",
            "CHECK (consolidation_authorized IN (0, 1))",
            "CHECK (decay_protected IN (0, 1))",
        ):
            assert fragment in engram_sql

        belief_sql = _table_sql(conn, "beliefs")
        assert "tier IN ('foundational', 'operational', 'tactical')" in belief_sql
        assert "CHECK (needs_review IN (0, 1))" in belief_sql
        assert "CHECK (confidence_pending_review IN (0, 1))" in belief_sql

        row_map_sql = _table_sql(conn, "pai_import_row_map")
        assert "target_table IN ('engrams', 'beliefs', 'hypomnema_entries')" in row_map_sql

        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_engrams_decay_eligible" in indexes
        assert "idx_pai_import_row_map_target" in indexes
        assert "idx_pai_import_row_map_job" in indexes
    finally:
        store.close()


def test_decay_and_softening_protection_flags_have_distinct_semantics(tmp_path):
    assert "decay_protected guards accessibility" in U3A_U3B_IMPORT_CONTRACT
    assert "softening_protected guards content" in U3A_U3B_IMPORT_CONTRACT

    store = EngramStore(tmp_path / "flag-semantics.db")
    decay_only = _old_engram(
        "decay protected only row can still soften when content is fading",
        accessibility=0.5,
        decay_protected=True,
    )
    softening_only = _old_engram(
        "SOFTENING-PROTECTED-CONTRACT should never enter prompts",
        accessibility=0.5,
        softening_protected=True,
    )
    exemplar = _old_engram(
        "eligible contract voice exemplar with measured direct phrasing",
        accessibility=0.95,
    )
    normal = _old_engram(
        "ordinary contract row should be available to maintenance",
        accessibility=0.5,
    )
    for engram in (decay_only, softening_only, exemplar, normal):
        engram.resolution = 1.0
        store.save_engram(engram)

    try:
        before_decay_only = store.get_engram(decay_only.id)
        before_softening_only = store.get_engram(softening_only.id)

        run_decay_pass(store, {"decay_rate": 0.0001}, agent_id="oliver")

        after_decay_only_decay = store.get_engram(decay_only.id)
        after_softening_only_decay = store.get_engram(softening_only.id)
        assert after_decay_only_decay.accessibility == pytest.approx(
            before_decay_only.accessibility
        )
        assert after_decay_only_decay.strength == pytest.approx(before_decay_only.strength)
        assert after_softening_only_decay.accessibility < before_softening_only.accessibility

        stub = StubLLM("a blurred contract row")
        run_softening_pass(store, {"softening_threshold": 0.9}, stub, agent_id="oliver")
        prompt_text = "\n".join(stub.prompts)

        assert store.get_engram(decay_only.id).content == stub.response
        assert store.get_engram(softening_only.id).content == softening_only.content
        assert "measured direct phrasing" in prompt_text
        assert "SOFTENING-PROTECTED-CONTRACT" not in prompt_text
    finally:
        store.close()


def test_u3a_migration_and_row_map_are_idempotent(tmp_path):
    store = EngramStore(tmp_path / "row-map.db")
    conn = store._get_conn()
    try:
        assert any(m["version"] == 4 for m in list_migrations())
        # Re-run current SCHEMA_VERSION — should be no-op
        # since EngramStore.__init__ already migrated to latest.
        assert run_migrations(conn, target_version=SCHEMA_VERSION) == []
        apply_u3a_schema_migration(conn)
        apply_u3a_schema_migration(conn)

        first = upsert_pai_import_row(
            conn,
            job_id=" A ",
            source_path=" x.md ",
            source_anchor=" L1-L3 ",
            engram_id="engram_1",
            source_hash=" h1 ",
            timestamp=100,
        )
        same = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="x.md",
            source_anchor="L1-L3",
            target_id="engram_1",
            source_hash="h1",
            timestamp=200,
        )
        changed = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="x.md",
            source_anchor="L1-L3",
            target_id="engram_1",
            engram_id="engram_1",
            source_hash="h2",
            timestamp=300,
        )
        new_path = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="y.md",
            target_id="engram_2",
            engram_id="engram_2",
            source_hash="h1",
            timestamp=400,
        )
        belief_first = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="belief.md",
            target_table="beliefs",
            target_id="belief_1",
            engram_id="source_engram_1",
            source_hash="bh1",
            timestamp=500,
        )
        belief_same = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="belief.md",
            target_table="beliefs",
            target_id="belief_1",
            source_hash="bh1",
            timestamp=600,
        )
        belief_changed = upsert_pai_import_row(
            conn,
            job_id="A",
            source_path="belief.md",
            target_table="beliefs",
            target_id="belief_1",
            source_hash="bh2",
            timestamp=700,
        )

        assert first == {"inserted": True, "updated": False, "created_at": 100, "updated_at": 100}
        assert same == {"inserted": False, "updated": False, "created_at": 100, "updated_at": 100}
        assert changed == {"inserted": False, "updated": True, "created_at": 100, "updated_at": 300}
        assert new_path["inserted"] is True
        assert belief_first == {
            "inserted": True,
            "updated": False,
            "created_at": 500,
            "updated_at": 500,
        }
        assert belief_same == {
            "inserted": False,
            "updated": False,
            "created_at": 500,
            "updated_at": 500,
        }
        assert belief_changed == {
            "inserted": False,
            "updated": True,
            "created_at": 500,
            "updated_at": 700,
        }

        rows = conn.execute(
            "SELECT source_path, source_anchor, target_table, target_id, engram_id, "
            "source_hash, created_at, updated_at, imported_at "
            "FROM pai_import_row_map WHERE target_table = 'engrams' ORDER BY source_path"
        ).fetchall()
        assert len(rows) == 2
        assert dict(rows[0]) == {
            "source_path": "x.md",
            "source_anchor": "L1-L3",
            "target_table": "engrams",
            "target_id": "engram_1",
            "engram_id": "engram_1",
            "source_hash": "h2",
            "created_at": 100,
            "updated_at": 300,
            "imported_at": 100,
        }
        assert dict(rows[1]) == {
            "source_path": "y.md",
            "source_anchor": "",
            "target_table": "engrams",
            "target_id": "engram_2",
            "engram_id": "engram_2",
            "source_hash": "h1",
            "created_at": 400,
            "updated_at": 400,
            "imported_at": 400,
        }
        belief_row = conn.execute(
            "SELECT source_path, target_table, target_id, engram_id, source_hash, "
            "created_at, updated_at, imported_at "
            "FROM pai_import_row_map WHERE target_table = 'beliefs'"
        ).fetchone()
        assert dict(belief_row) == {
            "source_path": "belief.md",
            "target_table": "beliefs",
            "target_id": "belief_1",
            "engram_id": "source_engram_1",
            "source_hash": "bh2",
            "created_at": 500,
            "updated_at": 700,
            "imported_at": 500,
        }

        with pytest.raises(ValueError):
            upsert_pai_import_row(
                conn, job_id="A", source_path="z.md", target_id="   "
            )
        with pytest.raises(ValueError):
            upsert_pai_import_row(
                conn,
                job_id="A",
                source_path="z.md",
                target_table="beliefs",
                engram_id="source_engram_2",
            )
        with pytest.raises(ValueError):
            upsert_pai_import_row(
                conn,
                job_id="A",
                source_path="z.md",
                target_table="not_a_table",
                target_id="row_1",
            )
    finally:
        store.close()


def test_u3b_import_row_map_reruns_preserve_target_identity(tmp_path):
    assert "must not silently remap" in U3A_U3B_IMPORT_CONTRACT

    store = EngramStore(tmp_path / "row-map-contract.db")
    conn = store._get_conn()
    try:
        first = upsert_pai_import_row(
            conn,
            job_id="U3B",
            source_path="source.md",
            source_anchor="paragraph-1",
            target_table="engrams",
            target_id="engram_1",
            source_hash="h1",
            timestamp=100,
        )
        same = upsert_pai_import_row(
            conn,
            job_id=" U3B ",
            source_path=" source.md ",
            source_anchor=" paragraph-1 ",
            target_table=" engrams ",
            target_id=" engram_1 ",
            source_hash=" h1 ",
            timestamp=200,
        )
        changed_source = upsert_pai_import_row(
            conn,
            job_id="U3B",
            source_path="source.md",
            source_anchor="paragraph-1",
            target_table="engrams",
            target_id="engram_1",
            source_hash="h2",
            timestamp=300,
        )

        assert first == {"inserted": True, "updated": False, "created_at": 100, "updated_at": 100}
        assert same == {"inserted": False, "updated": False, "created_at": 100, "updated_at": 100}
        assert changed_source == {
            "inserted": False,
            "updated": True,
            "created_at": 100,
            "updated_at": 300,
        }

        with pytest.raises(ValueError, match="refusing to remap"):
            upsert_pai_import_row(
                conn,
                job_id="U3B",
                source_path="source.md",
                source_anchor="paragraph-1",
                target_table="engrams",
                target_id="engram_2",
                source_hash="h3",
                timestamp=400,
            )

        new_anchor = upsert_pai_import_row(
            conn,
            job_id="U3B",
            source_path="source.md",
            source_anchor="paragraph-2",
            target_table="engrams",
            target_id="engram_2",
            source_hash="h3",
            timestamp=500,
        )
        different_table = upsert_pai_import_row(
            conn,
            job_id="U3B",
            source_path="source.md",
            source_anchor="paragraph-1",
            target_table="beliefs",
            target_id="belief_1",
            engram_id="engram_1",
            source_hash="bh1",
            timestamp=600,
        )
        assert new_anchor["inserted"] is True
        assert different_table["inserted"] is True

        rows = conn.execute(
            """
            SELECT source_anchor, target_table, target_id, engram_id, source_hash,
                   created_at, updated_at, imported_at
            FROM pai_import_row_map
            WHERE job_id = 'U3B' AND source_path = 'source.md'
            ORDER BY target_table, source_anchor
            """
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "source_anchor": "paragraph-1",
                "target_table": "beliefs",
                "target_id": "belief_1",
                "engram_id": "engram_1",
                "source_hash": "bh1",
                "created_at": 600,
                "updated_at": 600,
                "imported_at": 600,
            },
            {
                "source_anchor": "paragraph-1",
                "target_table": "engrams",
                "target_id": "engram_1",
                "engram_id": "engram_1",
                "source_hash": "h2",
                "created_at": 100,
                "updated_at": 300,
                "imported_at": 100,
            },
            {
                "source_anchor": "paragraph-2",
                "target_table": "engrams",
                "target_id": "engram_2",
                "engram_id": "engram_2",
                "source_hash": "h3",
                "created_at": 500,
                "updated_at": 500,
                "imported_at": 500,
            },
        ]
    finally:
        store.close()


def test_legacy_v3_db_opens_and_migrates_through_engram_store(tmp_path):
    db_path = tmp_path / "legacy-v3.db"
    _create_legacy_v3_db(db_path)

    store = EngramStore(db_path)
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        legacy = store.get_engram("legacy_e")
        assert legacy is not None
        assert legacy.voice_exemplar_eligible is True
        assert legacy.softening_protected is False
        assert legacy.consolidation_authorized is True
        assert legacy.decay_protected is False
        [belief] = store.get_beliefs(agent_id="oliver")
        assert belief.tier is None
        assert belief.needs_review is False
    finally:
        store.close()


def test_backed_up_legacy_db_rehearsal_preserves_u3a_sentinels(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    source_db = tmp_path / "legacy-source.db"
    backup_db = tmp_path / "rehearsal" / "legacy-copy.db"
    _create_legacy_v3_db(source_db)

    backup_sqlite_db(source_db, backup_db)

    source_conn = sqlite3.connect(source_db)
    try:
        assert source_conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "3"
        legacy_columns = {
            row[1] for row in source_conn.execute("PRAGMA table_info(engrams)")
        }
        assert "decay_protected" not in legacy_columns
    finally:
        source_conn.close()

    store = EngramStore(backup_db)
    sentinel_ids: set[str] = set()
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert store._get_conn().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.get_engram("legacy_e") is not None

        exemplar = _old_engram(
            "eligible rehearsal voice exemplar with plain exact register",
            accessibility=0.95,
        )
        foundational = _old_engram(
            "REHEARSAL-FOUNDATIONAL-SENTINEL shared u3a token",
            accessibility=0.2,
            strength=0.5,
            decay_protected=True,
            softening_protected=True,
            consolidation_authorized=False,
            voice_exemplar_eligible=False,
        )
        read_only = _old_engram(
            "REHEARSAL-READONLY-SENTINEL shared u3a token",
            accessibility=0.2,
            strength=0.5,
            consolidation_authorized=False,
        )
        other_agent = _old_engram(
            "REHEARSAL-OTHER-AGENT-SENTINEL shared u3a token",
            accessibility=0.2,
            strength=0.5,
            owner_agent_id="claude",
        )
        normal = _old_engram(
            "normal rehearsal candidate shared u3a token",
            accessibility=0.7,
            strength=0.8,
        )
        for engram in (exemplar, foundational, read_only, other_agent, normal):
            engram.resolution = 1.0
            store.save_engram(engram)

        sentinel_ids = {foundational.id, read_only.id, other_agent.id}
        before = {
            eid: (
                store.get_engram(eid).content,
                store.get_engram(eid).accessibility,
                store.get_engram(eid).strength,
                len(store.get_connections(eid)),
            )
            for eid in sentinel_ids
        }

        decay_stats = run_decay_pass(store, {"decay_rate": 0.0001}, agent_id="oliver")
        assert decay_stats["engrams_processed"] >= 1

        stub = StubLLM("a blurred rehearsal candidate")
        run_softening_pass(store, {"softening_threshold": 0.9}, stub, agent_id="oliver")
        prompt_text = "\n".join(stub.prompts)
        assert "plain exact register" in prompt_text
        assert "REHEARSAL-FOUNDATIONAL-SENTINEL" not in prompt_text
        assert "REHEARSAL-READONLY-SENTINEL" not in prompt_text
        assert "REHEARSAL-OTHER-AGENT-SENTINEL" not in prompt_text

        run_connection_discovery(
            store,
            embedding_index=None,
            config={"max_engrams_per_discovery_pass": 20},
            llm_client=None,
            agent_id="oliver",
        )

        for eid, expected in before.items():
            loaded = store.get_engram(eid)
            assert (
                loaded.content,
                loaded.accessibility,
                loaded.strength,
                len(store.get_connections(eid)),
            ) == expected
    finally:
        store.close()

    substrate = Substrate(
        SubstrateConfig(
            agent_id="oliver",
            db_path=str(backup_db),
            log_dir=str(tmp_path / "logs"),
            decay_rate=0.2,
            silence_threshold_hours=999999,
        )
    )
    try:
        events = substrate._consolidate({})
        event_ids = {event.payload.get("engram_id") for event in events}
        assert not (event_ids & sentinel_ids)
    finally:
        substrate.store.close()

    store = EngramStore(backup_db)
    try:
        for eid in sentinel_ids:
            loaded = store.get_engram(eid)
            assert loaded is not None
            expected = before[eid]
            assert (
                loaded.content,
                loaded.accessibility,
                loaded.strength,
                len(store.get_connections(eid)),
            ) == expected
        assert store._get_conn().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_migration_version_guards(tmp_path):
    empty = sqlite3.connect(tmp_path / "empty.db")
    empty.row_factory = sqlite3.Row
    try:
        # Bootstrap empty DB to the current latest schema (v8 after the
        # PR4 × inner-life merge: v6 membrane, v7 U2.5, v8 inner-life ledger).
        assert run_migrations(empty, target_version=SCHEMA_VERSION) == [4, 5, 6, 7, 8]
        assert empty.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"] == str(SCHEMA_VERSION)
        row_map_cols = _column_map(empty, "pai_import_row_map")
        assert "content_at_last_import" in row_map_cols
        assert "tombstone_at" in row_map_cols
        assert empty.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pai_import_events'"
        ).fetchone()
        assert empty.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inner_life_events'"
        ).fetchone()
        assert empty.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'trigger' AND name IN (
                'pai_import_engrams_tombstone',
                'pai_import_beliefs_tombstone',
                'pai_import_hypomnema_entries_tombstone'
            )
            """
        ).fetchone()[0] == 3
    finally:
        empty.close()

    historical_empty = sqlite3.connect(tmp_path / "historical-empty.db")
    try:
        with pytest.raises(RuntimeError, match="historical schema"):
            run_migrations(historical_empty, target_version=3)
        assert not historical_empty.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'engrams'"
        ).fetchone()
    finally:
        historical_empty.close()

    future = sqlite3.connect(tmp_path / "future.db")
    try:
        future.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        future.execute("INSERT INTO meta VALUES ('schema_version', '999')")
        future.commit()
        with pytest.raises(RuntimeError, match="newer than supported"):
            run_migrations(future, target_version=4)
    finally:
        future.close()

    malformed = sqlite3.connect(tmp_path / "malformed.db")
    try:
        malformed.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        malformed.execute("INSERT INTO meta VALUES ('schema_version', 'bad')")
        malformed.commit()
        with pytest.raises(RuntimeError, match="Malformed schema_version"):
            run_migrations(malformed, target_version=4)
    finally:
        malformed.close()


def test_failed_migration_rolls_back_and_preserves_schema_version(tmp_path):
    """v8 is real now (v6 membrane, v7 U2.5, v8 inner-life ledger); use slot 9
    to test failure isolation.

    The DB rolls back to v8 (current latest) when a hypothetical v9 migration
    fails partway through.
    """
    db_path = tmp_path / "rollback.db"
    store = EngramStore(db_path)
    store.close()

    previous = migrations._MIGRATIONS.get(9)

    def fail_after_writes(conn):
        conn.execute("CREATE TABLE u3a_failure_probe (id TEXT PRIMARY KEY)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('u3a_failure_marker', 'dirty')"
        )
        raise ValueError("synthetic migration failure")

    migrations._MIGRATIONS[9] = ("synthetic failing migration", fail_after_writes)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(RuntimeError, match="Migration 9 failed"):
            run_migrations(conn, target_version=9)
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'u3a_failure_probe'"
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM meta WHERE key = 'u3a_failure_marker'"
        ).fetchone()
    finally:
        conn.close()
        if previous is None:
            del migrations._MIGRATIONS[9]
        else:
            migrations._MIGRATIONS[9] = previous


def test_future_schema_store_open_fails_before_mutating_schema(tmp_path):
    db_path = tmp_path / "future-store.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        EngramStore(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pai_import_row_map'"
        ).fetchone()
    finally:
        conn.close()


def test_u3a_constraints_reject_invalid_states(tmp_path):
    store = EngramStore(tmp_path / "constraints.db")
    try:
        engram = Engram(content="constraint row")
        store.save_engram(engram)
        with pytest.raises(sqlite3.IntegrityError):
            store._get_conn().execute(
                "UPDATE engrams SET decay_protected = 2 WHERE id = ?", (engram.id,)
            )
        with pytest.raises(ValueError):
            Engram.from_dict({"id": "bad", "voice_exemplar_eligible": 2})

        belief = Belief(agent_id="oliver", content="constraint belief")
        store.save_belief(belief)
        with pytest.raises(sqlite3.IntegrityError):
            store._get_conn().execute(
                "UPDATE beliefs SET tier = 'nonsense' WHERE id = ?", (belief.id,)
            )
        with pytest.raises(ValueError):
            Belief.from_dict({"id": "bad", "tier": "nonsense"})

        with pytest.raises(sqlite3.IntegrityError):
            store._get_conn().execute(
                """
                INSERT INTO pai_import_row_map (
                    job_id, source_path, source_anchor, target_table, target_id,
                    source_hash, created_at, updated_at, imported_at
                ) VALUES ('job', 'source.md', '', 'not_a_table', 'row', '', 1, 1, 1)
                """
            )
    finally:
        store.close()


def test_sqlite_backup_helper_uses_single_integrity_checked_db(tmp_path):
    source = tmp_path / "source.db"
    dest = tmp_path / "snapshots" / "backup.db"
    store = EngramStore(source)
    try:
        store.save_engram(Engram(content="backup source row"))
    finally:
        store.close()

    backup_sqlite_db(source, dest)

    assert dest.exists()
    assert not dest.with_name("backup.db-wal").exists()
    assert not dest.with_name("backup.db-shm").exists()
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 1
    finally:
        conn.close()

    with pytest.raises(FileNotFoundError):
        backup_sqlite_db(tmp_path / "missing.db", tmp_path / "missing-backup.db")

    with pytest.raises(ValueError):
        backup_sqlite_db(source, source)

    invalid = tmp_path / "invalid.db"
    invalid.write_text("not a sqlite database")
    broken_dest = tmp_path / "snapshots" / "broken.db"
    with pytest.raises(sqlite3.DatabaseError):
        backup_sqlite_db(invalid, broken_dest)
    assert not broken_dest.exists()
    assert not broken_dest.with_name("broken.db.tmp").exists()


def test_decay_protected_is_excluded_from_consolidation_decay(tmp_path):
    assert "load-bearing" in U3A_PHASE0_DECAY_FINDING
    store = EngramStore(tmp_path / "decay.db")
    protected = _old_engram("protected identity kernel", decay_protected=True)
    read_only = _old_engram("unauthorized import row", consolidation_authorized=False)
    normal = _old_engram("ordinary fading memory")
    for engram in (protected, read_only, normal):
        store.save_engram(engram)

    try:
        stats = run_decay_pass(store, {"decay_rate": 0.0001}, agent_id="oliver")
        assert stats["engrams_processed"] == 1
        assert store.get_engram(protected.id).accessibility == pytest.approx(0.8)
        assert store.get_engram(read_only.id).accessibility == pytest.approx(0.8)
        assert store.get_engram(normal.id).accessibility < 0.8
    finally:
        store.close()


def test_decay_protected_is_excluded_from_substrate_tick_decay(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    db_path = tmp_path / "tick.db"
    store = EngramStore(db_path)
    protected = _old_engram("protected substrate tick row", decay_protected=True)
    normal = _old_engram("ordinary substrate tick row")
    other_agent = _old_engram(
        "other agent substrate tick row",
        owner_agent_id="claude",
    )
    for engram in (protected, normal, other_agent):
        store.save_engram(engram)
    store.close()

    substrate = Substrate(
        SubstrateConfig(
            agent_id="oliver",
            db_path=str(db_path),
            log_dir=str(tmp_path / "logs"),
            decay_rate=0.2,
            silence_threshold_hours=999999,
        )
    )
    try:
        summary: dict = {}
        substrate._consolidate(summary)
        store = EngramStore(db_path)
        try:
            assert summary["engrams_decayed"] == 1
            assert store.get_engram(protected.id).accessibility == pytest.approx(0.8)
            assert store.get_engram(normal.id).accessibility == pytest.approx(0.6)
            assert store.get_engram(other_agent.id).accessibility == pytest.approx(0.8)
        finally:
            store.close()
    finally:
        substrate.store.close()


def test_substrate_tick_softening_events_exclude_protected_and_unauthorized(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    db_path = tmp_path / "tick-softening.db"
    store = EngramStore(db_path)
    protected = _old_engram(
        "protected low vividness row",
        accessibility=0.2,
        strength=0.5,
        softening_protected=True,
    )
    read_only = _old_engram(
        "unauthorized low vividness row",
        accessibility=0.2,
        strength=0.5,
        consolidation_authorized=False,
    )
    other_agent = _old_engram(
        "other agent low vividness row",
        accessibility=0.2,
        strength=0.5,
        owner_agent_id="claude",
    )
    for engram in (protected, read_only, other_agent):
        store.save_engram(engram)
    store.close()

    substrate = Substrate(
        SubstrateConfig(
            agent_id="oliver",
            db_path=str(db_path),
            log_dir=str(tmp_path / "logs"),
            decay_rate=0.02,
            silence_threshold_hours=999999,
        )
    )
    try:
        events = substrate._consolidate({})
        assert not any(e.event_type == EventType.MEMORY_SOFTENED for e in events)
    finally:
        substrate.store.close()


def test_softening_uses_u3a_protection_and_voice_exemplar_filter(tmp_path):
    store = EngramStore(tmp_path / "softening.db")
    eligible = _old_engram(
        "eligible same-agent voice exemplar with quiet exact register",
        accessibility=0.95,
    )
    protected_vivid = _old_engram(
        "PROTECTED-DISTINCTIVE-PHRASE should never enter the exemplar prompt",
        accessibility=0.95,
        softening_protected=True,
    )
    ineligible = _old_engram(
        "INELIGIBLE-DISTINCTIVE-PHRASE should never enter the exemplar prompt",
        accessibility=0.95,
        voice_exemplar_eligible=False,
    )
    unauthorized_vivid = _old_engram(
        "UNAUTHORIZED-DISTINCTIVE-PHRASE should never enter the exemplar prompt",
        accessibility=0.95,
        consolidation_authorized=False,
    )
    protected = _old_engram(
        "protected softening candidate must not be rewritten",
        accessibility=0.45,
        softening_protected=True,
    )
    unauthorized = _old_engram(
        "unauthorized softening candidate must not be rewritten",
        accessibility=0.45,
        consolidation_authorized=False,
    )
    fading = _old_engram(
        "On June 3rd I debugged a threshold with a profiler trace",
        accessibility=0.45,
    )
    for engram in (
        eligible,
        protected_vivid,
        ineligible,
        unauthorized_vivid,
        protected,
        unauthorized,
        fading,
    ):
        engram.resolution = 1.0
        store.save_engram(engram)

    stub = StubLLM("a fading sense of debugging the threshold")
    try:
        run_softening_pass(store, {}, stub, agent_id="oliver")
        prompt = stub.prompts[0]
        assert "quiet exact register" in prompt
        assert "PROTECTED-DISTINCTIVE-PHRASE" not in prompt
        assert "INELIGIBLE-DISTINCTIVE-PHRASE" not in prompt
        assert "UNAUTHORIZED-DISTINCTIVE-PHRASE" not in prompt
        assert store.get_engram(protected.id).content == protected.content
        assert store.get_engram(unauthorized.id).content == unauthorized.content
        assert store.get_engram(fading.id).content == stub.response
    finally:
        store.close()


def test_connection_discovery_skips_unauthorized_sources_and_targets(tmp_path):
    store = EngramStore(tmp_path / "connections.db")
    source = _old_engram(
        "shared alpha beta phrase for connection discovery",
        owner_agent_id="oliver",
    )
    unauthorized_target = _old_engram(
        "shared alpha beta phrase for connection discovery target",
        owner_agent_id="oliver",
        consolidation_authorized=False,
    )
    unauthorized_source = _old_engram(
        "shared alpha beta phrase for unauthorized source",
        owner_agent_id="oliver",
        consolidation_authorized=False,
    )
    other_agent_target = _old_engram(
        "shared alpha beta phrase for connection discovery other agent",
        owner_agent_id="claude",
    )
    for engram in (source, unauthorized_target, unauthorized_source, other_agent_target):
        store.save_engram(engram)

    try:
        stats = run_connection_discovery(
            store,
            embedding_index=None,
            config={"max_engrams_per_discovery_pass": 10},
            llm_client=None,
            agent_id="oliver",
        )
        assert stats["engrams_processed"] == 1
        assert store.get_connections(source.id) == []
        assert store.get_connections(unauthorized_source.id) == []
    finally:
        store.close()


def test_connection_discovery_excludes_review_only_embedding_candidates(tmp_path):
    class StubEmbeddingIndex:
        available = True

        def __init__(self, hits):
            self.hits = hits

        def search(self, _query, k=10, exclude_ids=None):
            excluded = exclude_ids or set()
            return [(eid, score) for eid, score in self.hits if eid not in excluded][:k]

    store = EngramStore(tmp_path / "connections-visibility.db")
    source = _old_engram(
        "operational source for embedding discovery",
        owner_agent_id="oliver",
        accessibility=0.95,
    )
    review_candidate = _old_engram(
        "review only embedding candidate hidden from producers",
        owner_agent_id="oliver",
        read_visibility="review_only",
    )
    operational_candidate = _old_engram(
        "operational embedding candidate can connect",
        owner_agent_id="oliver",
        read_visibility="operational_context",
    )
    for engram in (source, review_candidate, operational_candidate):
        store.save_engram(engram)

    try:
        stats = run_connection_discovery(
            store,
            embedding_index=StubEmbeddingIndex([
                (review_candidate.id, 0.95),
                (operational_candidate.id, 0.95),
            ]),
            config={"max_engrams_per_discovery_pass": 1},
            llm_client=None,
            agent_id="oliver",
        )
        targets = {connection.target_id for connection in store.get_connections(source.id)}
        assert stats["embedding_candidates"] == 1
        assert operational_candidate.id in targets
        assert review_candidate.id not in targets
    finally:
        store.close()


def test_connection_discovery_counts_only_operational_existing_edges(tmp_path):
    class StubEmbeddingIndex:
        available = True

        def __init__(self, hits):
            self.hits = hits

        def search(self, _query, k=10, exclude_ids=None):
            excluded = exclude_ids or set()
            return [(eid, score) for eid, score in self.hits if eid not in excluded][:k]

    store = EngramStore(tmp_path / "connections-hidden-edge-count.db")
    source = _old_engram(
        "operational source with hidden existing edges",
        owner_agent_id="oliver",
        accessibility=0.99,
    )
    review_target = _old_engram(
        "review only existing target should not satisfy connectivity",
        owner_agent_id="oliver",
        read_visibility="review_only",
        accessibility=0.4,
    )
    audit_target = _old_engram(
        "audit only existing target should not satisfy connectivity",
        owner_agent_id="oliver",
        read_visibility="audit_only",
        accessibility=0.4,
    )
    operational_target = _old_engram(
        "operational target should be discovered despite hidden edges",
        owner_agent_id="oliver",
        accessibility=0.3,
    )
    for engram in (source, review_target, audit_target, operational_target):
        store.save_engram(engram)
    for target in (review_target, audit_target):
        store.save_connection(
            source.id,
            Connection(
                target_id=target.id,
                relation=ConnectionRelation.SUPPORTS,
                strength=0.7,
            ),
        )

    try:
        stats = run_connection_discovery(
            store,
            embedding_index=StubEmbeddingIndex([(operational_target.id, 0.95)]),
            config={
                "max_engrams_per_discovery_pass": 1,
                "max_connections_per_engram": 2,
            },
            llm_client=None,
            agent_id="oliver",
        )
        operational_targets = {
            connection.target_id
            for connection in store.get_connections(
                source.id,
                read_visibility="operational_context",
            )
        }
        all_targets = {connection.target_id for connection in store.get_connections(source.id)}

        assert stats["engrams_processed"] == 1
        assert stats["embedding_candidates"] == 1
        assert operational_targets == {operational_target.id}
        assert all_targets == {review_target.id, audit_target.id, operational_target.id}
    finally:
        store.close()


def test_reflection_skips_unauthorized_imports_for_prompts_and_identity(tmp_path):
    store = EngramStore(tmp_path / "reflection.db")
    authorized = [
        _old_engram("authorized reflection memory one", tags=["authorized-theme"]),
        _old_engram("authorized reflection memory two", tags=["authorized-theme"]),
        _old_engram("authorized reflection memory three", tags=["authorized-theme"]),
    ]
    unauthorized = _old_engram(
        "UNAUTHORIZED-PROMPT-LEAK",
        impact="UNAUTHORIZED-LIVING-QUESTION",
        tags=["unresolved", "unauthorized-theme"],
        consolidation_authorized=False,
    )
    for engram in (*authorized, unauthorized):
        store.save_engram(engram)

    identity = AgentIdentity()
    identity.memory_profile.agent_id = "oliver"
    stub = StubLLM("authorized synthesized thought")
    try:
        stats = run_reflection_pass(
            store,
            identity,
            EmotionalState(),
            stub,
            {"reflection_lookback_hours": 999999},
        )
        assert stats["engrams_reviewed"] == 3
        assert stub.prompts
        assert "UNAUTHORIZED-PROMPT-LEAK" not in stub.prompts[0]
        assert "UNAUTHORIZED-LIVING-QUESTION" not in identity.epoch_state.self_summary
        assert "unauthorized-theme" not in identity.epoch_state.self_summary
    finally:
        store.close()


def test_substrate_dreaming_prompt_excludes_unauthorized_and_other_agent(tmp_path):
    db_path = tmp_path / "dreaming-handler.db"
    store = EngramStore(db_path)
    softened = _old_engram(
        "authorized fading dream seed",
        accessibility=0.2,
        strength=0.5,
    )
    unauthorized_vivid = _old_engram(
        "UNAUTHORIZED-DREAM-PROMPT-LEAK",
        accessibility=1.0,
        strength=1.0,
        consolidation_authorized=False,
    )
    other_agent_vivid = _old_engram(
        "OTHER-AGENT-DREAM-PROMPT-LEAK",
        accessibility=0.99,
        strength=1.0,
        owner_agent_id="claude",
    )
    authorized_vivid = _old_engram(
        "AUTHORIZED-DREAM-PROMPT-SOURCE",
        accessibility=0.9,
        strength=0.9,
    )
    for engram in (softened, unauthorized_vivid, other_agent_vivid, authorized_vivid):
        store.save_engram(engram)

    stub = StubLLM('{"dream": "authorized synthesis", "significance": "set"}')
    try:
        dreaming.handle(
            SubstrateEvent(
                event_type=EventType.MEMORY_SOFTENED,
                payload={"engram_id": softened.id},
                source="test",
            ),
            SubstrateConfig(
                agent_id="oliver",
                db_path=str(db_path),
                log_dir=str(tmp_path / "logs"),
                dreaming_collision_threshold=0.1,
            ),
            ModulatorState(),
            store,
            stub,
        )
        prompt = stub.prompts[0]
        assert "AUTHORIZED-DREAM-PROMPT-SOURCE" in prompt
        assert "UNAUTHORIZED-DREAM-PROMPT-LEAK" not in prompt
        assert "OTHER-AGENT-DREAM-PROMPT-LEAK" not in prompt
        outputs = [
            e.content
            for e in store.get_active_engrams(agent_id="oliver", limit=20, read_visibility=None)
        ]
        assert "[dream] authorized synthesis" in outputs
    finally:
        store.close()


def test_substrate_wandering_prompt_excludes_unauthorized_and_other_agent(tmp_path):
    db_path = tmp_path / "wandering-handler.db"
    store = EngramStore(db_path)
    unauthorized = _old_engram(
        "UNAUTHORIZED-WANDERING-PROMPT-LEAK",
        consolidation_authorized=False,
    )
    unauthorized.created_at = "2999-01-01T00:00:00+00:00"
    other_agent = _old_engram(
        "OTHER-AGENT-WANDERING-PROMPT-LEAK",
        owner_agent_id="claude",
    )
    other_agent.created_at = "2999-01-02T00:00:00+00:00"
    authorized = _old_engram("AUTHORIZED-WANDERING-PROMPT-SOURCE")
    authorized.created_at = "2026-01-01T00:00:00+00:00"
    for engram in (unauthorized, other_agent, authorized):
        store.save_engram(engram)

    stub = StubLLM('{"thought": "authorized wandering", "origin": "authorized"}')
    try:
        wandering.handle(
            SubstrateEvent(
                event_type=EventType.SILENCE_EXTENDED,
                payload={"silence_hours": 12},
                source="test",
            ),
            SubstrateConfig(
                agent_id="oliver",
                db_path=str(db_path),
                log_dir=str(tmp_path / "logs"),
            ),
            ModulatorState(),
            store,
            stub,
        )
        prompt = stub.prompts[0]
        assert "AUTHORIZED-WANDERING-PROMPT-SOURCE" in prompt
        assert "UNAUTHORIZED-WANDERING-PROMPT-LEAK" not in prompt
        assert "OTHER-AGENT-WANDERING-PROMPT-LEAK" not in prompt
        outputs = [
            e.content
            for e in store.get_active_engrams(agent_id="oliver", limit=20, read_visibility=None)
        ]
        assert "[wandering] authorized wandering" in outputs
    finally:
        store.close()


def test_substrate_initiation_prompt_excludes_unauthorized_and_other_agent(tmp_path):
    db_path = tmp_path / "initiation-handler.db"
    store = EngramStore(db_path)
    unauthorized = _old_engram(
        "UNAUTHORIZED-INITIATION-PROMPT-LEAK",
        accessibility=1.0,
        strength=1.0,
        consolidation_authorized=False,
    )
    other_agent = _old_engram(
        "OTHER-AGENT-INITIATION-PROMPT-LEAK",
        accessibility=0.99,
        strength=1.0,
        owner_agent_id="claude",
    )
    authorized_one = _old_engram(
        "AUTHORIZED-INITIATION-PROMPT-SOURCE-ONE",
        accessibility=0.8,
        strength=0.8,
    )
    authorized_two = _old_engram(
        "AUTHORIZED-INITIATION-PROMPT-SOURCE-TWO",
        accessibility=0.7,
        strength=0.8,
    )
    for engram in (unauthorized, other_agent, authorized_one, authorized_two):
        store.save_engram(engram)

    stub = StubLLM('{"pattern": "authorized pattern", "significance": "set"}')
    try:
        initiation.handle(
            SubstrateEvent(
                event_type=EventType.SALIENCE_ACCUMULATED,
                payload={},
                source="test",
            ),
            SubstrateConfig(
                agent_id="oliver",
                db_path=str(db_path),
                log_dir=str(tmp_path / "logs"),
            ),
            ModulatorState(),
            store,
            stub,
        )
        prompt = stub.prompts[0]
        assert "AUTHORIZED-INITIATION-PROMPT-SOURCE-ONE" in prompt
        assert "AUTHORIZED-INITIATION-PROMPT-SOURCE-TWO" in prompt
        assert "UNAUTHORIZED-INITIATION-PROMPT-LEAK" not in prompt
        assert "OTHER-AGENT-INITIATION-PROMPT-LEAK" not in prompt
        outputs = [
            e.content
            for e in store.get_active_engrams(agent_id="oliver", limit=20, read_visibility=None)
        ]
        assert "[initiation] authorized pattern" in outputs
    finally:
        store.close()


def test_substrate_modulators_scope_to_authorized_agent_rows(tmp_path):
    db_path = tmp_path / "modulators.db"
    store = EngramStore(db_path)
    authorized = _old_engram(
        "authorized low vividness modulator row",
        accessibility=0.2,
        strength=0.2,
    )
    unauthorized = _old_engram(
        "unauthorized high vividness modulator row",
        accessibility=1.0,
        strength=1.0,
        consolidation_authorized=False,
    )
    other_agent = _old_engram(
        "other agent high vividness modulator row",
        accessibility=1.0,
        strength=1.0,
        owner_agent_id="claude",
    )
    for engram in (authorized, unauthorized, other_agent):
        store.save_engram(engram)
    store.close()

    modulators = compute_modulators(
        str(db_path),
        agent_id="oliver",
        require_consolidation_authorized=True,
    )

    assert modulators.resolution == pytest.approx(0.2)


def test_substrate_modulator_connection_density_scopes_to_authorized_agent_edges(tmp_path):
    db_path = tmp_path / "modulator_connections.db"
    store = EngramStore(db_path)
    authorized_source = _old_engram("authorized source")
    authorized_target = _old_engram("authorized target")
    unauthorized_source = _old_engram(
        "unauthorized source",
        consolidation_authorized=False,
    )
    unauthorized_target = _old_engram(
        "unauthorized target",
        consolidation_authorized=False,
    )
    other_source = _old_engram("other source", owner_agent_id="claude")
    other_target = _old_engram("other target", owner_agent_id="claude")
    for engram in (
        authorized_source,
        authorized_target,
        unauthorized_source,
        unauthorized_target,
        other_source,
        other_target,
    ):
        store.save_engram(engram)

    store.save_connection(
        authorized_source.id,
        Connection(target_id=authorized_target.id, relation="supports"),
    )
    store.save_connection(
        unauthorized_source.id,
        Connection(target_id=unauthorized_target.id, relation="supports"),
    )
    store.save_connection(
        authorized_source.id,
        Connection(target_id=unauthorized_target.id, relation="contradicts"),
    )
    store.save_connection(
        unauthorized_source.id,
        Connection(target_id=authorized_target.id, relation="extends"),
    )
    store.save_connection(
        other_source.id,
        Connection(target_id=other_target.id, relation="supports"),
    )
    store.save_connection(
        other_source.id,
        Connection(target_id=authorized_target.id, relation="grounds"),
    )
    store.close()

    modulators = compute_modulators(
        str(db_path),
        agent_id="oliver",
        require_consolidation_authorized=True,
    )

    assert modulators.openness == pytest.approx(0.75)
    assert modulators.temperature == pytest.approx(0.85)


def test_substrate_modulators_ignore_pending_confidence_beliefs(tmp_path):
    db_path = tmp_path / "modulator_pending_beliefs.db"
    store = EngramStore(db_path)
    store.save_engram(_old_engram("authorized modulator anchor"))
    for index in range(10):
        store.save_belief(
            Belief(
                agent_id="oliver",
                content=f"pending imported belief {index}",
                confidence=0.9,
                confidence_pending_review=True,
            )
        )
    store.close()

    modulators = compute_modulators(str(db_path), agent_id="oliver")

    assert modulators.openness == pytest.approx(0.8)


def test_substrate_temporal_event_ignores_unauthorized_and_other_agent_rows(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    db_path = tmp_path / "temporal.db"
    store = EngramStore(db_path)
    authorized = _old_engram("authorized old temporal row")
    authorized.created_at = "2026-01-01T00:00:00+00:00"
    unauthorized = _old_engram(
        "unauthorized future temporal row",
        consolidation_authorized=False,
    )
    unauthorized.created_at = "2999-01-01T00:00:00+00:00"
    other_agent = _old_engram(
        "other agent future temporal row",
        owner_agent_id="claude",
    )
    other_agent.created_at = "2999-01-02T00:00:00+00:00"
    for engram in (authorized, unauthorized, other_agent):
        store.save_engram(engram)
    store.close()

    substrate = Substrate(
        SubstrateConfig(
            agent_id="oliver",
            db_path=str(db_path),
            log_dir=str(tmp_path / "logs"),
            silence_threshold_hours=1,
        )
    )
    try:
        events = substrate._check_temporal({})
        assert [event.event_type for event in events] == [EventType.SILENCE_EXTENDED]
    finally:
        substrate.store.close()
