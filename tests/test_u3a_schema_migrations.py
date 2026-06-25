import sqlite3

import pytest

from mnemos.consolidation.connection_discovery import run_connection_discovery
from mnemos.consolidation.decay import run_decay_pass
from mnemos.consolidation.softening import run_softening_pass
from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.substrate.events import EventType
from mnemos.store.migrations import (
    U3A_PHASE0_DECAY_FINDING,
    apply_u3a_schema_migration,
    backup_sqlite_db,
    list_migrations,
    run_migrations,
    upsert_pai_import_row,
)
from mnemos.store.sqlite_store import EngramStore
from mnemos.substrate.config import SubstrateConfig
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
        [loaded_belief] = store.get_beliefs(agent_id="oliver")
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


def test_u3a_migration_and_row_map_are_idempotent(tmp_path):
    store = EngramStore(tmp_path / "row-map.db")
    conn = store._get_conn()
    try:
        assert any(m["version"] == 4 for m in list_migrations())
        assert run_migrations(conn, target_version=4) == []
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


def test_legacy_v3_db_opens_and_migrates_through_engram_store(tmp_path):
    db_path = tmp_path / "legacy-v3.db"
    _create_legacy_v3_db(db_path)

    store = EngramStore(db_path)
    try:
        assert store.get_meta("schema_version") == "4"
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


def test_migration_version_guards(tmp_path):
    empty = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert run_migrations(empty, target_version=4) == []
        assert empty.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
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
