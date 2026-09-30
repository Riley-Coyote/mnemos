"""The live clean-ups: four `mnemos repair` commands for what older code left.

- ``split-notes``: before one capture was one object, correcting a note
  rewrote it in place and left its memory saying the old words, and
  correcting a memory archived it and saved the correction as a new memory
  with no note, leaving the old note over the archived memory. These tests
  write both exactly as that code did, then repair them.
- ``archive-rows``: archive rows left behind by memories that are no longer
  archived.
- ``dead-embeddings``: stored vectors from models this Mnemos does not use.
- ``placeholder-impacts``: the meanings the server wrote itself.

Each is a dry run unless --write, takes a verified backup before it writes,
finds nothing on a second run, and changes nothing from code older than the
store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path

import pytest

from mnemos.cli import main
from mnemos.core.types import SourceType
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store import embedding_index as ei

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _write(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _settled_sha256(path: Path) -> str:
    """Hash the store with everything written so far folded into the file."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ids(said: str) -> tuple[str, str]:
    """(memory id, note id) from a capture's result."""
    return (
        said.split("Memory ID: ")[1].split()[0],
        said.split("Continuity note ID: ")[1].split()[0],
    )


def _backups(tmp_path, name: str) -> list[Path]:
    folder = tmp_path / "backups"
    return sorted(folder.glob(f"memory.pre-repair-{name}-*.db")) if folder.exists() else []


def _set_store_minimum(db, minimum: int) -> None:
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
           (str(minimum),))


# ── split-notes ──


def _split_store(tmp_path) -> dict:
    """A store with both kinds of split pair, written as the old code wrote
    them, beside pairs the repair must leave alone.

    - "ahead": a correction named the note, which was rewritten in place
      (reason "simple correction", signed by the corrector); its memory kept
      the old words;
    - "behind": a correction named the memory, which was archived
      ("simple_correction_update"); the correction was saved as a new memory
      with no note, and the old note stayed active over the archived memory;
    - "faded": a memory whose words softened; its note keeps the words it was
      encoded with;
    - "kept": an ordinary pair.
    """
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt._ensure_init()
    store = rt._store
    ids: dict[str, str] = {}
    try:
        for name, words in (
            ("ahead", "The ferry to the island leaves at nine from pier two."),
            ("behind", "The harbour office opens at eight on weekdays."),
            ("faded", "The lighthouse keeper logs the weather at dawn and dusk every day."),
            ("kept", "Riley keeps the tide tables in the blue binder."),
        ):
            ids[f"{name}_memory"], ids[f"{name}_note"] = _ids(
                rt.capture(words, impact=f"Kept for the {name} test.")
            )

        # The note path before R07: the note rewritten in place, the memory left.
        store.revise_hypomnema_entry(
            ids["ahead_note"],
            "The ferry to the island now leaves at ten from pier four.",
            reason="simple correction",
            **SCOPE,
            confidence=0.92,
            salience=0.75,
            author_model="model-b",
            revised_by="model-b",
            author_session="session-b",
        )

        # The memory-id path before R07: the memory archived, the correction
        # encoded as a new memory with no note, the note left active.
        old = store.get_engram(ids["behind_memory"])
        store.archive_engram(old, reason="simple_correction_update")
        replacement = rt._encoder.encode(
            content="The harbour office opens at seven on weekdays now.",
            impact="Correction to earlier continuity.",
            impact_source="template",
            kind="episodic",
            tags=["continuity", "correction"],
            source=SourceType.SESSION,
            **SCOPE,
            override_confidence=0.92,
            skip_surprise_detection=True,
            author_kind="agent",
            author_model="model-c",
            author_session="session-c",
        )
        ids["replacement"] = replacement.id
    finally:
        rt.close()

    _write(
        db,
        "UPDATE engrams SET content = ?, resolution = 0.4 WHERE id = ?",
        ("The lighthouse keeper logs the weather... [details faded]", ids["faded_memory"]),
    )
    return {"db": db, "ids": ids}


def test_split_notes_dry_run_finds_both_and_changes_nothing(tmp_path, capsys):
    case = _split_store(tmp_path)
    db, ids = case["db"], case["ids"]
    before = _settled_sha256(db)

    code = main(["repair", "split-notes", "--db-path", str(db), *ARGS])
    out = capsys.readouterr().out
    rt = _runtime(db)
    try:
        found = rt.repair_split_notes()["found"]
    finally:
        rt.close()

    assert code == 0
    assert _settled_sha256(db) == before, "the dry run changed the store"
    assert [item["memory_id"] for item in found["ahead"]] == [ids["ahead_memory"]]
    assert [(item["note_id"], item["replacement_id"]) for item in found["behind"]] == [
        (ids["behind_note"], ids["replacement"])
    ]
    # The faded pair and the ordinary one carry the same words.
    assert found["same"] == 2 and found["differ"] == [] and found["unclear"] == []
    assert "1  corrected, while the memory kept the old words" in out
    assert "1  over a replaced memory, while the new one has no note" in out
    assert "Dry run: nothing changed." in out
    assert _backups(tmp_path, "split-notes") == []


def test_split_notes_write_joins_them_and_keeps_both_words(tmp_path, capsys):
    case = _split_store(tmp_path)
    db, ids = case["db"], case["ids"]
    untouched = _all(
        db, "SELECT id, content, content_at_encoding, author_model FROM engrams WHERE id IN (?, ?)",
        (ids["faded_memory"], ids["kept_memory"]),
    )

    code = main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Joined 2 pairs: 1 memory took the note's words, 1 note moved" in out
    # The way to make the new words searchable by meaning at once, for this store.
    assert f"mnemos --db-path {db} embeddings index" in out
    # The memory takes the note's newer words; a version keeps the old ones,
    # signed by whoever wrote the new.
    corrected = "The ferry to the island now leaves at ten from pier four."
    assert _all(
        db, "SELECT content, content_at_encoding, author_model, author_session FROM engrams "
        "WHERE id = ?", (ids["ahead_memory"],),
    ) == [(corrected, corrected, "model-b", "session-b")]
    versions = _all(
        db, "SELECT content_snapshot, change_reason, author_model FROM versions "
        "WHERE engram_id = ? ORDER BY version_num", (ids["ahead_memory"],),
    )
    assert versions[-1] == (
        "The ferry to the island leaves at nine from pier two.", "repair_split_notes", "model-b",
    )
    assert _all(db, "SELECT content FROM engrams_fts WHERE id = ?", (ids["ahead_memory"],)) == [
        (corrected,)
    ]
    # The note moves to the replacement and takes its words; its revision
    # trail keeps what it said, and the correction's lineage is recorded.
    [(note_words, paired, related, signer, revisions)] = _all(
        db, "SELECT content, graduated_to_engram_id, related_engram_id, author_model, "
        "revisions_json FROM hypomnema_entries WHERE id = ?", (ids["behind_note"],),
    )
    assert note_words == "The harbour office opens at seven on weekdays now."
    assert paired == related == ids["replacement"]
    assert signer == "model-c"
    last = json.loads(revisions)[-1]
    assert last["prior_content"] == "The harbour office opens at eight on weekdays."
    assert last["reason"].startswith("repair split-notes:")
    [(old_lineage,)] = _all(db, "SELECT lineage FROM engrams WHERE id = ?", (ids["behind_memory"],))
    [(new_lineage,)] = _all(db, "SELECT lineage FROM engrams WHERE id = ?", (ids["replacement"],))
    assert json.loads(old_lineage)["superseded_by"] == ids["replacement"]
    assert json.loads(new_lineage)["supersedes"] == [ids["behind_memory"]]
    assert _all(
        db, "SELECT relation FROM connections WHERE source_id = ? AND target_id = ?",
        (ids["replacement"], ids["behind_memory"]),
    ) == [("supersedes",)]
    # Nothing else moved.
    assert _all(
        db, "SELECT id, content, content_at_encoding, author_model FROM engrams WHERE id IN (?, ?)",
        (ids["faded_memory"], ids["kept_memory"]),
    ) == untouched
    [backup] = _backups(tmp_path, "split-notes")
    assert f"Backup: {backup}" in out
    assert _all(backup, "PRAGMA integrity_check") == [("ok",)]
    assert _all(backup, "SELECT content FROM engrams WHERE id = ?", (ids["ahead_memory"],)) == [
        ("The ferry to the island leaves at nine from pier two.",)
    ]

    after_first = _settled_sha256(db)
    assert main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert _settled_sha256(db) == after_first, "a second run changed the store"
    assert len(_backups(tmp_path, "split-notes")) == 1


def test_after_split_notes_a_correction_by_the_old_id_reaches_the_pair_in_use(tmp_path):
    case = _split_store(tmp_path)
    db, ids = case["db"], case["ids"]
    assert main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"]) == 0

    rt = _runtime(db)
    try:
        rt.correct("The harbour office opens at six on weekdays.", target_id=ids["behind_memory"])
    finally:
        rt.close()

    live = _all(
        db, "SELECT h.content FROM hypomnema_entries h JOIN engrams m "
        "ON m.id = h.graduated_to_engram_id WHERE h.active = 1 AND m.state = 'active' "
        "AND h.content LIKE 'The harbour office%'",
    )
    assert live == [("The harbour office opens at six on weekdays.",)], (
        "the correction left a second pair live"
    )
    assert _all(db, "SELECT state FROM engrams WHERE id = ?", (ids["replacement"],)) == [
        ("archived",)
    ]


def test_split_notes_leaves_a_replacement_it_cannot_tell_alone(tmp_path, capsys):
    case = _split_store(tmp_path)
    db, ids = case["db"], case["ids"]
    # A second correction saved in the same moment: which one replaced the
    # memory can no longer be told.
    rt = _runtime(db)
    try:
        rt._ensure_init()
        second = rt._encoder.encode(
            content="The harbour office closes at noon on Fridays.",
            tags=["continuity", "correction"], source=SourceType.SESSION, **SCOPE,
            skip_surprise_detection=True, author_kind="agent",
        )
    finally:
        rt.close()
    [(archived_at,)] = _all(db, "SELECT archived_at FROM archive WHERE id = ?", (ids["behind_memory"],))
    _write(db, "UPDATE engrams SET created_at = ? WHERE id = ?", (archived_at, second.id))
    note_before = _all(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (ids["behind_note"],))

    assert main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"]) == 0
    out = capsys.readouterr().out

    assert "1  that can't be joined without a guess" in out
    assert f"left alone: note {ids['behind_note']}" in out
    assert _all(db, "SELECT * FROM hypomnema_entries WHERE id = ?", (ids["behind_note"],)) == note_before
    # The other split is still joined.
    assert _all(db, "SELECT content FROM engrams WHERE id = ?", (ids["ahead_memory"],)) == [
        ("The ferry to the island now leaves at ten from pier four.",)
    ]


def test_split_notes_leaves_pairs_it_cannot_settle_alone(tmp_path, capsys):
    case = _split_store(tmp_path)
    db, ids = case["db"], case["ids"]
    rt = _runtime(db)
    try:
        rt._ensure_init()
        # Rewritten in place, but not by a correction: nothing says whose
        # words are newer.
        rt._store.revise_hypomnema_entry(
            ids["kept_note"], "Riley keeps the tide tables on the shelf.",
            reason="edited through the advanced tools", **SCOPE,
        )
        # A second note over the corrected memory, corrected too: the memory
        # can't take both notes' words.
        second = rt._store.write_hypomnema_entry(
            "The ferry to the island leaves at nine from pier two.", **SCOPE,
            entry_kind="continuity", authored_by="agent",
        )
        rt._store.revise_hypomnema_entry(
            second, "The ferry to the island leaves at eleven.",
            reason="simple correction", **SCOPE,
        )
    finally:
        rt.close()
    _write(db, "UPDATE hypomnema_entries SET graduated_to_engram_id = ? WHERE id = ?",
           (ids["ahead_memory"], second))
    memories = _all(db, "SELECT id, content FROM engrams ORDER BY id")

    assert main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"]) == 0
    out = capsys.readouterr().out

    assert "1  that differ from their memory for another reason" in out
    assert "2  that can't be joined without a guess" in out
    for note_id in (ids["kept_note"], ids["ahead_note"], second):
        assert f"left alone: note {note_id}" in out
    assert _all(db, "SELECT id, content FROM engrams ORDER BY id") == memories, (
        "a memory took words the repair had to guess at"
    )
    # The replaced memory's note still moves.
    assert _all(db, "SELECT graduated_to_engram_id FROM hypomnema_entries WHERE id = ?",
                (ids["behind_note"],)) == [(ids["replacement"],)]


def test_split_notes_changes_nothing_from_older_code(tmp_path, capsys):
    case = _split_store(tmp_path)
    db = case["db"]
    _set_store_minimum(db, 999)
    before = _settled_sha256(db)

    code = main(["repair", "split-notes", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 1
    assert "older than the store expects, so it changes nothing" in out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path, "split-notes") == []


# ── archive-rows ──


def _archive_store(tmp_path) -> dict:
    """Archive rows of every kind: one repeating a memory that is quiet again
    (as an older repair left 1,392 on the live store), one whose words differ
    from the memory's now, one for a memory still archived, and one for a
    memory no longer in the store."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt._ensure_init()
    store = rt._store
    ids: dict[str, str] = {}
    try:
        for name, words in (
            ("repeat", "The night ferry was cancelled for the storm."),
            ("history", "The old pier was rebuilt in stone."),
            ("archived", "The fog horn was tested on Monday."),
            ("gone", "The kiosk sold maps of the bay."),
        ):
            ids[name], _ = _ids(rt.capture(words))
            store.archive_engram(store.get_engram(ids[name]), reason="decay_below_threshold")
    finally:
        rt.close()
    _write(db, "UPDATE engrams SET state = 'dormant' WHERE id IN (?, ?)", (ids["repeat"], ids["history"]))
    _write(db, "UPDATE engrams SET content = 'The old pier was rebuilt in stone and oak.' "
           "WHERE id = ?", (ids["history"],))
    _write(db, "DELETE FROM engrams WHERE id = ?", (ids["gone"],))
    return {"db": db, "ids": ids}


def test_archive_rows_drops_only_rows_that_repeat_a_memory_no_longer_archived(tmp_path, capsys):
    case = _archive_store(tmp_path)
    db, ids = case["db"], case["ids"]
    before = _settled_sha256(db)

    assert main(["repair", "archive-rows", "--db-path", str(db), *ARGS]) == 0
    out = capsys.readouterr().out
    assert _settled_sha256(db) == before, "the dry run changed the store"
    assert "1  repeating a memory no longer archived, word for word" in out
    assert "(1 quiet)" in out
    assert "1  for a memory no longer archived, in other words" in out
    assert "Dry run: nothing changed." in out
    assert _backups(tmp_path, "archive-rows") == []

    assert main(["repair", "archive-rows", "--db-path", str(db), *ARGS, "--write"]) == 0
    out = capsys.readouterr().out
    assert "Dropped 1 row; 3 remain." in out
    assert sorted(row[0] for row in _all(db, "SELECT id FROM archive")) == sorted(
        [ids["history"], ids["archived"], ids["gone"]]
    )
    [backup] = _backups(tmp_path, "archive-rows")
    assert _all(backup, "PRAGMA integrity_check") == [("ok",)]
    assert _all(backup, "SELECT COUNT(*) FROM archive") == [(4,)]

    after_first = _settled_sha256(db)
    assert main(["repair", "archive-rows", "--db-path", str(db), *ARGS, "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert _settled_sha256(db) == after_first, "a second run changed the store"


def test_archive_rows_changes_nothing_from_older_code(tmp_path, capsys):
    db = _archive_store(tmp_path)["db"]
    _set_store_minimum(db, 999)
    before = _settled_sha256(db)

    assert main(["repair", "archive-rows", "--db-path", str(db), *ARGS, "--write"]) == 1
    assert "older than the store expects" in capsys.readouterr().out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path, "archive-rows") == []


# ── dead-embeddings ──


@pytest.fixture
def local_model(monkeypatch):
    """The local model set up in this process, as on the maintainer's
    machine, without loading it."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)


def _vector_store(tmp_path) -> Path:
    """Whole-text vectors and passages from three models."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt._ensure_init()
    finally:
        rt.close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (engram_id TEXT PRIMARY KEY, "
            "embedding BLOB NOT NULL, model_name TEXT NOT NULL, dims INTEGER NOT NULL)"
        )
        conn.execute(ei.PASSAGE_TABLE_SQL)
        blob = struct.pack("2f", 0.6, 0.8)
        rows = [
            ("all-MiniLM-L6-v2", 3), ("gemini-embedding-2", 2), ("gemini-embedding-2-preview", 1),
        ]
        for model, count in rows:
            for i in range(count):
                conn.execute(
                    "INSERT INTO embeddings VALUES (?, ?, ?, 2)", (f"engram_{model}_{i}", blob, model)
                )
                conn.execute(
                    "INSERT INTO passage_vectors (item_id, model_name, part, text_hash, dims, "
                    "embedding, scheme) VALUES (?, ?, 0, 'h', 2, ?, 3)",
                    (f"engram_{model}_{i}", model, blob),
                )
        conn.commit()
    finally:
        conn.close()
    return db


def test_dead_embeddings_prunes_only_other_models(tmp_path, capsys, local_model):
    db = _vector_store(tmp_path)
    before = _settled_sha256(db)

    assert main(["repair", "dead-embeddings", "--db-path", str(db), *ARGS]) == 0
    out = capsys.readouterr().out
    assert _settled_sha256(db) == before, "the dry run changed the store"
    assert "This Mnemos embeds with: local model all-MiniLM-L6-v2" in out
    assert "2  gemini-embedding-2" in out and "1  gemini-embedding-2-preview" in out
    assert "Dry run: nothing changed." in out

    assert main(["repair", "dead-embeddings", "--db-path", str(db), *ARGS, "--write"]) == 0
    out = capsys.readouterr().out
    assert "Pruned 3 whole-text vectors and 3 passages." in out
    for table in ("embeddings", "passage_vectors"):
        assert _all(db, f"SELECT model_name, COUNT(*) FROM {table} GROUP BY model_name") == [
            ("all-MiniLM-L6-v2", 3)
        ]
    [backup] = _backups(tmp_path, "dead-embeddings")
    assert _all(backup, "SELECT COUNT(*) FROM embeddings") == [(6,)]

    after_first = _settled_sha256(db)
    assert main(["repair", "dead-embeddings", "--db-path", str(db), *ARGS, "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert _settled_sha256(db) == after_first, "a second run changed the store"


def test_dead_embeddings_changes_nothing_from_older_code(tmp_path, capsys, local_model):
    db = _vector_store(tmp_path)
    _set_store_minimum(db, 999)
    before = _settled_sha256(db)

    assert main(["repair", "dead-embeddings", "--db-path", str(db), *ARGS, "--write"]) == 1
    assert "older than the store expects" in capsys.readouterr().out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path, "dead-embeddings") == []


def test_dead_embeddings_prunes_nothing_without_a_model(tmp_path, capsys, no_model):
    db = _vector_store(tmp_path)
    before = _settled_sha256(db)

    assert main(["repair", "dead-embeddings", "--db-path", str(db), *ARGS, "--write"]) == 1
    out = capsys.readouterr().out
    assert "a dead vector can't be told from a live one, so nothing is pruned" in out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path, "dead-embeddings") == []


# ── placeholder-impacts ──


def test_placeholder_impacts_empties_only_the_servers_phrases(tmp_path, capsys):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    ids: dict[str, str] = {}
    try:
        for name in ("correction", "corrected", "promoted", "older", "agent"):
            ids[name], _ = _ids(rt.capture(f"A note for the {name} case, about the harbour."))
    finally:
        rt.close()
    for name, impact, source in (
        ("correction", "Correction to earlier continuity.", "template"),
        ("corrected", "Corrected continuity for future interactions.\n", "template"),
        ("promoted", "Stable continuity promoted during simple maintenance.", "template"),
        ("older", "Durable continuity captured from the session.", ""),
        ("agent", "Check the tide before booking the ferry.", "agent"),
    ):
        _write(db, "UPDATE engrams SET impact = ?, impact_source = ? WHERE id = ?",
               (impact, source, ids[name]))
    contents = _all(db, "SELECT id, content FROM engrams ORDER BY id")
    before = _settled_sha256(db)

    assert main(["repair", "placeholder-impacts", "--db-path", str(db), *ARGS]) == 0
    out = capsys.readouterr().out
    assert _settled_sha256(db) == before, "the dry run changed the store"
    for text in (
        "Correction to earlier continuity.",
        "Corrected continuity for future interactions.",
        "Stable continuity promoted during simple maintenance.",
        "Durable continuity captured from the session.",
    ):
        assert f'1  "{text}"' in out
    assert "other phrases, from the older capture path" not in out

    assert main(["repair", "placeholder-impacts", "--db-path", str(db), *ARGS, "--write"]) == 0
    out = capsys.readouterr().out
    assert "Emptied 4 meanings" in out
    impacts = dict(
        (row[0], (row[1], row[2])) for row in _all(db, "SELECT id, impact, impact_source FROM engrams")
    )
    for name in ("correction", "corrected", "promoted", "older"):
        assert impacts[ids[name]] == ("", ""), name
    assert impacts[ids["agent"]] == ("Check the tide before booking the ferry.", "agent")
    assert _all(db, "SELECT id, content FROM engrams ORDER BY id") == contents
    [backup] = _backups(tmp_path, "placeholder-impacts")
    assert _all(backup, "SELECT impact FROM engrams WHERE id = ?", (ids["promoted"],)) == [
        ("Stable continuity promoted during simple maintenance.",)
    ]

    after_first = _settled_sha256(db)
    assert main(["repair", "placeholder-impacts", "--db-path", str(db), *ARGS, "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert _settled_sha256(db) == after_first, "a second run changed the store"


def test_placeholder_impacts_changes_nothing_from_older_code(tmp_path, capsys):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        memory_id, _ = _ids(rt.capture("A note about the harbour lights."))
    finally:
        rt.close()
    _write(db, "UPDATE engrams SET impact = 'Correction to earlier continuity.' WHERE id = ?",
           (memory_id,))
    _set_store_minimum(db, 999)
    before = _settled_sha256(db)

    assert main(["repair", "placeholder-impacts", "--db-path", str(db), *ARGS, "--write"]) == 1
    assert "older than the store expects" in capsys.readouterr().out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path, "placeholder-impacts") == []
