"""`mnemos repair faded-words`: memories whose words a model-less softening cut.

Before soften_without_model defaulted to False, softening rewrote an old
memory's words to its first sentence and "... [details faded]" (or to "An
impression related to <first word>... [faded]") and kept the full words only
as a version. repair-softening restores from that version. Where the version
is gone, as on three live memories of the maintainer's store, the words
survive in content_at_encoding, which nothing rewrites.

This repair restores them from there, in R20's shape: a dry run by default,
--write with a verified backup first, nothing found on a second run, and
nothing changed from code older than the store. A memory is restored only
when it is live (active or quiet) and its words are exactly what that
softening makes of its words at encoding.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mnemos.cli import main
from mnemos.consolidation.softening import _rule_based_soften
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]

WORDS = {
    "details": (
        "The design direction for the observatory page is monochrome and near black. "
        "Riley dislikes warm brown tones, so the ferrous amber palette is out."
    ),
    "impression": (
        "Lighthouse logs record the weather at dawn and dusk. "
        "The keeper files them in the cobalt ledger on the second shelf."
    ),
    "quiet": (
        "The harbour office opens at eight on weekdays. "
        "Saturday hours changed to the tamarind schedule in spring."
    ),
    "archived": "The ferry leaves at nine. Its winter timetable sits in the teal folder.",
    "rewritten": "The tide tables live in the blue binder. The spare copy is in the attic.",
    "other_scope": "The pier lights come on at dusk. Their timer is in the saffron box.",
    "kept": "Riley keeps the release notes in docs/releases.",
}


def _runtime(db, **scope) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **{**SCOPE, **scope})


def _capture(db, words: str, **scope) -> str:
    runtime = _runtime(db, **scope)
    try:
        said = runtime.capture(words, impact="Kept for the faded-words test.")
    finally:
        runtime.close()
    return said.split("Memory ID: ")[1].split()[0]


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _cut(db, engram_id: str, words: str, *, state: str = "active") -> None:
    """Write what the model-less softening wrote, with no version kept."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE engrams SET content = ?, resolution = 0.4, state = ? WHERE id = ?",
            (words, state, engram_id),
        )
        conn.execute("DELETE FROM engrams_fts WHERE id = ?", (engram_id,))
        conn.execute("INSERT INTO engrams_fts (id, content) VALUES (?, ?)", (engram_id, words))
        conn.execute("DELETE FROM versions WHERE engram_id = ?", (engram_id,))
        conn.commit()
    finally:
        conn.close()


def _settled_sha256(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backups(tmp_path) -> list[Path]:
    folder = tmp_path / "backups"
    return sorted(folder.glob("memory.pre-repair-faded-words-*.db")) if folder.exists() else []


def _faded_store(tmp_path) -> tuple[Path, dict[str, str]]:
    """Three restorable memories (active "details", active "impression", a
    quiet one) beside ones the repair must leave: an archived one, one whose
    cut words don't come from its words at encoding, one in another scope,
    and an ordinary memory."""
    db = tmp_path / "memory.db"
    ids = {
        name: _capture(db, words) for name, words in WORDS.items() if name != "other_scope"
    }
    ids["other_scope"] = _capture(db, WORDS["other_scope"], project_scope="elsewhere")

    _cut(db, ids["details"], _rule_based_soften(WORDS["details"], 0.4))
    _cut(db, ids["impression"], _rule_based_soften(WORDS["impression"], 0.2))
    _cut(db, ids["quiet"], _rule_based_soften(WORDS["quiet"], 0.4), state="dormant")
    _cut(db, ids["archived"], _rule_based_soften(WORDS["archived"], 0.4), state="archived")
    _cut(db, ids["rewritten"], "Something else was said here... [details faded]")
    _cut(db, ids["other_scope"], _rule_based_soften(WORDS["other_scope"], 0.4))
    return db, ids


def _words(db, engram_id: str) -> str:
    return _all(db, "SELECT content FROM engrams WHERE id = ?", (engram_id,))[0][0]


def test_a_dry_run_lists_them_and_changes_nothing(tmp_path, capsys):
    db, ids = _faded_store(tmp_path)
    before = _settled_sha256(db)

    code = main(["--db-path", str(db), *ARGS, "repair", "faded-words"])
    out = capsys.readouterr().out

    assert code == 0
    assert _settled_sha256(db) == before, "the dry run changed the store"
    assert _backups(tmp_path) == []
    assert "4  live memories whose words were cut when they faded" in out
    assert "3  that come back whole from their words at encoding" in out
    assert "1  whose words don't come from their words at encoding" in out
    for name in ("details", "impression", "quiet"):
        assert ids[name] in out
    for name in ("archived", "other_scope", "kept"):
        assert ids[name] not in out
    assert "Dry run: nothing changed." in out


def test_write_restores_the_words_they_were_encoded_with(tmp_path, capsys):
    db, ids = _faded_store(tmp_path)

    code = main(["--db-path", str(db), *ARGS, "repair", "faded-words", "--write"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Restored 3 memories" in out
    [backup] = _backups(tmp_path)
    assert str(backup) in out
    for name in ("details", "impression", "quiet"):
        assert _words(db, ids[name]) == WORDS[name]
        assert _all(db, "SELECT resolution FROM engrams WHERE id = ?", (ids[name],)) == [(1.0,)]
        # The cut words are kept as a version: history, never lost.
        assert _all(
            db, "SELECT change_reason FROM versions WHERE engram_id = ? "
            "ORDER BY version_num DESC LIMIT 1", (ids[name],),
        ) == [("repair_faded_words",)]
        # Searched by its words at once.
        assert _all(db, "SELECT content FROM engrams_fts WHERE id = ?", (ids[name],)) == [
            (WORDS[name],)
        ]
    # A quiet memory stays quiet: the repair brings words back, not the memory.
    assert _all(db, "SELECT state FROM engrams WHERE id = ?", (ids["quiet"],)) == [("dormant",)]
    # Left as they are.
    assert _words(db, ids["archived"]).endswith("... [details faded]")
    assert _words(db, ids["rewritten"]) == "Something else was said here... [details faded]"
    assert _words(db, ids["other_scope"]).endswith("... [details faded]")
    assert _words(db, ids["kept"]) == WORDS["kept"]

    # A second run finds nothing to do.
    assert main(["--db-path", str(db), *ARGS, "repair", "faded-words", "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert len(_backups(tmp_path)) == 1


def test_restored_words_are_recalled_in_another_process(tmp_path):
    db, ids = _faded_store(tmp_path)
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MNEMOS_") and key != "CLAUDE_CODE_SESSION_ID"
    }
    env["MNEMOS_DISABLE_DOTENV"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "--db-path", str(db), *ARGS,
         "repair", "faded-words", "--write"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr

    runtime = _runtime(db)
    try:
        # "cobalt ledger" was only in the part of the words that was cut.
        found = runtime.recall("cobalt ledger")
    finally:
        runtime.close()
    assert ids["impression"] in found


def test_code_older_than_the_store_changes_nothing(tmp_path, capsys):
    db, _ids = _faded_store(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')"
        )
        conn.commit()
    finally:
        conn.close()
    before = _settled_sha256(db)

    code = main(["--db-path", str(db), *ARGS, "repair", "faded-words", "--write"])
    out = capsys.readouterr().out

    assert code == 1
    assert "older than the store expects, so it changes nothing" in out
    assert _settled_sha256(db) == before
    assert _backups(tmp_path) == []


def test_no_store_is_created_by_asking(tmp_path, capsys):
    db = tmp_path / "absent.db"
    assert main(["--db-path", str(db), *ARGS, "repair", "faded-words"]) == 0
    assert "nothing to repair" in capsys.readouterr().out
    assert not db.exists()
