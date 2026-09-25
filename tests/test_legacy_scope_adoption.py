"""Memories the scope migration could not place must not vanish in silence.

Schema v6 (0.2.1) gave every engram a person and a project. A legacy engram
linked to exactly one continuity scope was backfilled into it; every other
legacy row was left without a scope and quarantined from scoped reads,
because guessing whose memory it is could show it to the wrong person. That
rule stays.

What was missing is everything around it. Nothing reported the quarantine:
on one real store `mnemos_health` read "186 active" over a file holding
about 7,000 engrams, 105 of them lessons distilled from experience, all
unreachable by recall and untouched by maintenance. And there was no way out
short of hand-written SQL. These tests pin both halves: health and doctor say
what is hidden, and `mnemos adopt-legacy` lets a human bring the lessons and
deliberately written memories back into a scope, dry run first, with a
verified backup. Transcript-indexer output stays hidden unless asked for by
name, because its volume is what buried continuity in the first place
(docs/vision.md, section II).

The legacy store is built the way a real one arrives: an engrams table from
before v6, with no person or project columns, opened by the current code so
the real migration runs. Every reader below uses the default scope, because
defaults are what an agent actually gets.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mnemos.backup import check_database
from mnemos.cli import main
from mnemos.simple_runtime import MnemosRuntime, format_health_card
from mnemos.store.sqlite_store import EngramStore

AGENT = "claude-code"

LESSON = "engram_legacy_lesson"
INDEXED = "engram_legacy_indexed_fact"
INDEXER_TYPED = "engram_legacy_indexer_typed_lesson"
DIRECT = "engram_legacy_direct"
ARCHIVED = "engram_legacy_archived_lesson"
FOREIGN = "engram_legacy_other_agent"

_V5_SCHEMA = """
CREATE TABLE engrams (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_at_encoding TEXT NOT NULL,
    impact TEXT NOT NULL DEFAULT '',
    impact_source TEXT NOT NULL DEFAULT '',
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
CREATE TABLE connections (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.5,
    formed_at TEXT NOT NULL,
    formed_by TEXT NOT NULL DEFAULT 'encoding',
    PRIMARY KEY (source_id, target_id, relation)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta (key, value) VALUES ('schema_version', '5');
"""

# id, owner, state, tags, source type, content
_ROWS = [
    # A lesson softening distilled from a fading indexer memory: it inherits
    # the indexer's tag, and must still count as a lesson.
    (LESSON, AGENT, "active", ["lesson", "distilled", "session-indexed"], "reflection",
     "Never push to main without an explicit instruction to push."),
    (INDEXED, AGENT, "active", ["session-indexed", "trace-type:fact"], "session",
     "The nightly backup cron fires at three in the morning."),
    # The indexer labels some of its own output "lesson". That is a type the
    # indexer guessed, not a lesson anything was distilled into.
    (INDEXER_TYPED, AGENT, "dormant", ["lesson", "session-indexed", "trace-type:lesson"],
     "session", "Always rebuild the sprite atlas after palette edits."),
    (DIRECT, AGENT, "active", [], "session",
     "Kathmandu Newar grammar marks how the speaker came to know a thing."),
    (ARCHIVED, AGENT, "archived", ["distilled"], "reflection",
     "The retired dashboard needed a manual refresh."),
    (FOREIGN, "someone-else", "active", ["distilled"], "reflection",
     "Another agent's lesson about quarterly planning."),
]


@pytest.fixture
def legacy_db(tmp_path) -> str:
    """A pre-v6 store, migrated by opening it with the current code."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V5_SCHEMA)
    stamp = "2026-07-01T00:00:00+00:00"
    for engram_id, owner, state, tags, source_type, content in _ROWS:
        conn.execute(
            "INSERT INTO engrams (id, content, content_at_encoding, kind, tags, source, "
            "owner_agent_id, state, created_at, last_accessed) "
            "VALUES (?, ?, ?, 'semantic', ?, ?, ?, ?, ?, ?)",
            (engram_id, content, content, json.dumps(tags),
             json.dumps({"type": source_type, "confidence": 0.85}),
             owner, state, stamp, stamp),
        )
        conn.execute("INSERT INTO engrams_fts (id, content) VALUES (?, ?)", (engram_id, content))
    conn.execute(
        "INSERT INTO connections (source_id, target_id, relation, strength, formed_at, formed_by) "
        "VALUES (?, ?, 'distilled_into', 0.9, ?, 'softening')",
        (INDEXED, LESSON, stamp),
    )
    conn.commit()
    conn.close()

    EngramStore(str(path)).close()  # the real v6+ migration
    scopes = _scopes(str(path))
    assert all(scopes[engram_id][1:] == (None, None) for engram_id, *_ in _ROWS), (
        "premise: the migration quarantines unlinked legacy rows"
    )
    return str(path)


def _scopes(db: str) -> dict[str, tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT id, owner_agent_id, person_id, project_scope FROM engrams"
            )
        }
    finally:
        conn.close()


def _recall(db: str, cue: str) -> str:
    runtime = MnemosRuntime(db_path=db, agent_id=AGENT)
    try:
        return runtime.recall(cue)
    finally:
        runtime.close()


def test_health_names_the_memories_the_scope_migration_hid(legacy_db):
    runtime = MnemosRuntime(db_path=legacy_db, agent_id=AGENT)
    try:
        data = runtime.health()
    finally:
        runtime.close()

    assert data["legacy"] == {
        "hidden": 4, "lessons": 1, "other": 1, "indexer": 2, "archived": 1,
    }
    card = format_health_card(data)
    assert "4 older memories from before scoping never reach recall" in card
    assert "(1 lesson, 1 other, 2 from the transcript indexer)" in card
    assert "mnemos adopt-legacy" in card


def test_doctor_points_at_the_hidden_memories(legacy_db, capsys):
    assert main(["doctor", "--db-path", legacy_db, "--agent-id", AGENT]) == 0
    out = capsys.readouterr().out

    assert "4 older memories from before scoping never reach recall" in out
    assert "mnemos adopt-legacy" in out


def test_adopt_legacy_is_a_dry_run_by_default(legacy_db, capsys):
    before = _scopes(legacy_db)

    assert main(["adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT]) == 0
    out = capsys.readouterr().out

    assert _scopes(legacy_db) == before, "a dry run wrote to the store"
    assert not list((Path(legacy_db).parent / "backups").glob("*.pre-adopt-legacy-*"))
    assert "Dry run" in out
    assert "claude-code / user / global" in out
    assert "Never push to main" in out, "the plan should show what would come back"
    assert "--write" in out


def test_adopt_legacy_brings_back_lessons_but_not_indexer_output(legacy_db, capsys):
    direct_cue = "Kathmandu Newar grammar"
    lesson_cue = "push to main without an explicit instruction"
    indexed_cue = "nightly backup cron"
    assert DIRECT not in _recall(legacy_db, direct_cue), "premise: quarantined before"
    assert LESSON not in _recall(legacy_db, lesson_cue), "premise: quarantined before"

    assert main(["adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT, "--write"]) == 0
    out = capsys.readouterr().out
    assert "Brought back 2 memories" in out

    # A fresh runtime, as the next session opens it, on the default scope.
    assert DIRECT in _recall(legacy_db, direct_cue)
    assert LESSON in _recall(legacy_db, lesson_cue)
    assert INDEXED not in _recall(legacy_db, indexed_cue)

    scopes = _scopes(legacy_db)
    assert scopes[DIRECT] == (AGENT, "user", "global")
    assert scopes[LESSON] == (AGENT, "user", "global")
    assert scopes[INDEXED] == (AGENT, None, None)
    assert scopes[INDEXER_TYPED] == (AGENT, None, None)
    assert scopes[ARCHIVED] == (AGENT, None, None)
    assert scopes[FOREIGN] == ("someone-else", None, None)

    backups = list((Path(legacy_db).parent / "backups").glob("*.pre-adopt-legacy-*.db"))
    assert len(backups) == 1
    assert check_database(backups[0])["integrity"] == "ok"
    assert _scopes(str(backups[0]))[DIRECT] == (AGENT, None, None), (
        "the backup must be the state before adoption"
    )


def test_indexer_output_left_hidden_is_counted_but_not_an_alarm(legacy_db, capsys):
    """Leaving indexer output out is the recommended state, not a fault.

    An ATTENTION that fires on the state you were told to choose teaches
    people to stop reading ATTENTION. The health card still tells the truth
    about what the file holds.
    """
    assert main(["adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT, "--write"]) == 0
    capsys.readouterr()

    assert main(["doctor", "--db-path", legacy_db, "--agent-id", AGENT]) == 0
    assert "never reach recall" not in capsys.readouterr().out

    runtime = MnemosRuntime(db_path=legacy_db, agent_id=AGENT)
    try:
        card = format_health_card(runtime.health())
    finally:
        runtime.close()
    assert (
        "2 older memories from before scoping never reach recall "
        "(2 from the transcript indexer)"
    ) in card


def test_indexer_output_comes_back_only_when_named(legacy_db, capsys):
    assert main([
        "adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT,
        "--include", "indexer", "--write",
    ]) == 0
    capsys.readouterr()

    scopes = _scopes(legacy_db)
    assert scopes[INDEXED] == (AGENT, "user", "global")
    assert scopes[INDEXER_TYPED] == (AGENT, "user", "global")
    assert scopes[LESSON] == (AGENT, None, None), "--include replaces the default"
    assert scopes[ARCHIVED] == (AGENT, None, None)


def test_adopt_legacy_will_not_choose_between_people(legacy_db, capsys):
    for person in ("alice", "bob"):
        runtime = MnemosRuntime(
            db_path=legacy_db, agent_id=AGENT, person_id=person, project_scope="global",
        )
        try:
            runtime.capture(f"{person.title()} keeps a private planning note.")
        finally:
            runtime.close()
    before = _scopes(legacy_db)

    assert main(["adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT, "--write"]) == 1
    out = capsys.readouterr().out
    assert _scopes(legacy_db) == before, "refused, yet something was written"
    assert "--person-id" in out and "--project-scope" in out

    assert main([
        "adopt-legacy", "--db-path", legacy_db, "--agent-id", AGENT,
        "--person-id", "alice", "--project-scope", "global", "--write",
    ]) == 0
    assert _scopes(legacy_db)[DIRECT] == (AGENT, "alice", "global")


def test_adopt_legacy_never_creates_a_store(tmp_path, capsys):
    missing = tmp_path / "typo" / "memory.db"

    assert main(["adopt-legacy", "--db-path", str(missing), "--agent-id", AGENT, "--write"]) == 0

    assert not missing.exists()
    assert "nothing to bring back" in capsys.readouterr().out.lower()
