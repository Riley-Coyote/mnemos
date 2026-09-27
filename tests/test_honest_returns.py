"""A return strengthens a memory only when it means something.

Retrieval reinforces a memory because it came back to the one reading it. On a
real store that had stopped meaning anything:

  * reconsolidation ran inside retrieval, before the runtime's filters, so
    results the reader was never shown were strengthened and linked;
  * nothing limited how often: on a copy of that store the most-returned
    memory had been reinforced 31,517 times;
  * maintenance counted its own reinforcing of a lesson as an access;
  * every return appended a full copy of the memory as a new version, and
    every save wrote the whole history again: 124,493 of the copy's 125,431
    version rows were such copies.

Now only what a result shows is reinforced, at most once per session; a return
writes no version; a save appends versions and never rewrites one; and
``mnemos repair-versions`` removes the copies when a human asks. Code older
than the store reinforces nothing.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
FERRY = "Riley keeps the ferry timetable in the kitchen drawer."
GARDEN = "Marigolds bloom beside the greenhouse door every June."
CUE = "ferry timetable"
# What a return changes on the memory's own row.
ACCESS = ("access_count", "last_accessed", "reconsolidation_count")
TRACE = ACCESS + ("strength", "stability", "accessibility")


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _memory_id(result: str) -> str:
    match = re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", result)
    assert match, result
    return match.group(1)


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _row(db, engram_id: str, fields: tuple[str, ...] = TRACE) -> dict:
    [values] = _all(db, f"SELECT {', '.join(fields)} FROM engrams WHERE id = ?", (engram_id,))
    return dict(zip(fields, values))


def _accesses(db, engram_id: str) -> int:
    return _row(db, engram_id, ("access_count",))["access_count"]


def _versions(db, engram_id: str | None = None) -> list[tuple]:
    if engram_id is None:
        return _all(db, "SELECT engram_id, version_num, content_snapshot FROM versions "
                        "ORDER BY engram_id, version_num")
    return _all(
        db,
        "SELECT version_num, content_snapshot, resolution_at_version, changed_at, change_reason "
        "FROM versions WHERE engram_id = ? ORDER BY version_num",
        (engram_id,),
    )


def _co_activated(db, a: str, b: str) -> list[tuple]:
    return _all(
        db,
        "SELECT source_id, target_id FROM connections WHERE relation = 'co_activated' "
        "AND ((source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?))",
        (a, b, b, a),
    )


def _set_store_minimum(db, version: int) -> None:
    """What a newer server's startup does to a store, or a human's reset."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(version),),
        )
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


def _ferry_and_garden(tmp_path) -> tuple[Path, str, str]:
    """Two memories that share no words, and a strong link from one to the
    other, so recall's resonance carries the ferry cue to the garden note."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        ferry = _memory_id(runtime.capture(FERRY))
        garden = _memory_id(runtime.capture(GARDEN))
        assert runtime._store is not None
        runtime._store.save_connection(ferry, Connection(
            target_id=garden, relation=ConnectionRelation.SUPPORTS, strength=1.0,
        ))
    finally:
        runtime.close()
    return db, ferry, garden


def _found(db, cue: str) -> list[str]:
    """What retrieval itself finds for ``cue``, before any runtime filter."""
    store = EngramStore(str(db))
    try:
        retriever = ReactiveRetriever(store, reconsolidation_enabled=False)
        return [r.engram.id for r in retriever.retrieve(cue, max_results=5, **SCOPE)]
    finally:
        store.close()


# A recall as a separate Mnemos process makes it.
_RECALL = """
import sys
from mnemos.simple_runtime import MnemosRuntime

runtime = MnemosRuntime(db_path=sys.argv[1], agent_id="nova", person_id="riley",
                        project_scope="demo", use_dedicated_model=False)
try:
    print(runtime.recall(sys.argv[2]))
finally:
    runtime.close()
"""


def _recall_in_another_process(db, cue: str, *, home: Path, session: str | None = None) -> str:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "MNEMOS_DISABLE_DOTENV": "1",
        "PYTHONPATH": ":".join(sys.path),
    }
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = session
    done = subprocess.run(
        [sys.executable, "-c", _RECALL, str(db), cue],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# ── Only what a result shows is reinforced ──


@pytest.mark.parametrize("call", ["recall", "context"])
def test_only_the_memories_a_result_shows_are_reinforced(tmp_path, call):
    db, ferry, garden = _ferry_and_garden(tmp_path)
    assert garden in _found(db, CUE), "premise: retrieval reaches the garden note"
    garden_before = _row(db, garden, ACCESS)

    runtime = _runtime(db)
    try:
        shown = getattr(runtime, call)(CUE)
    finally:
        runtime.close()

    assert "ferry timetable in the kitchen drawer" in shown
    # context() shows the scope's notes before its results for the query, the
    # garden note among them; what it returned for the query comes after.
    returned = shown.split(f'### For "{CUE}"', 1)[-1]
    assert "Marigolds" not in returned, "premise: the runtime's filter drops the garden note"
    assert _accesses(db, ferry) == 1, "the memory shown was not reinforced"
    assert _row(db, garden, ACCESS) == garden_before, (
        "a memory the reader was never shown was reinforced"
    )
    assert _co_activated(db, ferry, garden) == [], (
        "a memory shown was linked to one the reader never saw"
    )


# ── At most once per session ──


def test_a_session_reinforces_a_memory_once(tmp_path, monkeypatch):
    db, ferry, _ = _ferry_and_garden(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")

    runtime = _runtime(db)
    try:
        runtime.recall(CUE)
        once = _row(db, ferry)
        runtime.recall(CUE)
        runtime.recall(CUE)
        again = _row(db, ferry)
        runtime.context(CUE)
    finally:
        runtime.close()
    runtime = _runtime(db)  # the same session, a new server
    try:
        runtime.recall(CUE)
    finally:
        runtime.close()

    assert once["access_count"] == 1
    assert again == once, "a second return in the same session changed the memory"
    assert _row(db, ferry, ACCESS) == {k: once[k] for k in ACCESS}

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-two")
    runtime = _runtime(db)
    try:
        runtime.recall(CUE)
    finally:
        runtime.close()
    assert _accesses(db, ferry) == 2, "a new session did not reinforce the memory"


def test_one_session_reinforces_once_across_its_processes(tmp_path):
    db, ferry, _ = _ferry_and_garden(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    for _ in range(2):
        shown = _recall_in_another_process(db, CUE, home=home, session="session-one")
        assert "ferry timetable in the kitchen drawer" in shown
    assert _accesses(db, ferry) == 1, (
        "a second process of the same session reinforced the memory again"
    )

    _recall_in_another_process(db, CUE, home=home, session="session-two")
    assert _accesses(db, ferry) == 2


def test_without_a_session_each_process_reinforces_once(tmp_path):
    db, ferry, _ = _ferry_and_garden(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    for _ in range(2):  # two servers in this process
        runtime = _runtime(db)
        try:
            runtime.recall(CUE)
        finally:
            runtime.close()
    assert _accesses(db, ferry) == 1, "this process reinforced the memory twice"

    _recall_in_another_process(db, CUE, home=home)
    assert _accesses(db, ferry) == 2, "another process did not reinforce the memory"


# ── A return writes no version ──


def test_a_return_writes_no_version(tmp_path, monkeypatch):
    db, ferry, garden = _ferry_and_garden(tmp_path)

    for session in ("session-one", "session-two"):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        runtime = _runtime(db)
        try:
            runtime.recall(CUE)
        finally:
            runtime.close()
    # The other surfaces reconsolidate inside retrieval itself.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-three")
    store = EngramStore(str(db))
    try:
        results = ReactiveRetriever(store).retrieve(CUE, max_results=5, **SCOPE)
    finally:
        store.close()

    assert {r.engram.id for r in results} >= {ferry, garden}
    assert _accesses(db, ferry) == 3, "premise: every return reinforced the memory"
    assert _versions(db) == [], "a return wrote a version"


# ── A save appends versions and never rewrites one ──


def _reworded(store: EngramStore, engram_id: str, wording: str) -> Engram:
    engram = store.get_engram(engram_id)
    assert engram is not None
    engram.add_version(reason="softening")
    engram.content = wording
    store.save_engram(engram)
    return engram


def test_a_save_appends_versions_and_never_rewrites_one(tmp_path):
    db = tmp_path / "store.db"
    store = EngramStore(str(db))
    try:
        memory = Engram(content="The first wording of the lighthouse note.")
        store.save_engram(memory)
        _reworded(store, memory.id, "The second wording of the lighthouse note.")
        [(num, snapshot, *_)] = _versions(db, memory.id)
        assert (num, snapshot) == (1, "The first wording of the lighthouse note.")

        # An in-memory copy that no longer matches what was written.
        stale = store.get_engram(memory.id)
        stale.versions[0].content_snapshot = "rewritten"
        stale.add_version(reason="softening")
        stale.content = "The third wording of the lighthouse note."
        store.save_engram(stale)

        # A partial engram, as a text search returns it: no versions loaded,
        # so its new version is numbered 1 in memory.
        [partial] = store.search_fts('"third"', agent_id="default")
        assert partial.versions == []
        partial.add_version(reason="softening")
        partial.content = "The fourth wording of the lighthouse note."
        store.save_engram(partial)

        # A save that rolled back leaves its version to the next save, which
        # writes it once, even when the memory changes again before the
        # transaction commits.
        fifth = store.get_engram(memory.id)
        fifth.add_version(reason="softening")
        fifth.content = "The fifth wording of the lighthouse note."
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.save_engram(fifth)
                raise RuntimeError("the rest of this change failed")
        with store.transaction():
            store.save_engram(fifth)
            fifth.add_version(reason="softening")
            fifth.content = "The sixth wording of the lighthouse note."
            store.save_engram(fifth)
    finally:
        store.close()

    assert [(num, snapshot) for num, snapshot, *_ in _versions(db, memory.id)] == [
        (1, "The first wording of the lighthouse note."),
        (2, "The second wording of the lighthouse note."),
        (3, "The third wording of the lighthouse note."),
        (4, "The fourth wording of the lighthouse note."),
        (5, "The fifth wording of the lighthouse note."),
    ]


def test_saving_an_unchanged_memory_writes_no_version_row(tmp_path):
    db = tmp_path / "store.db"
    store = EngramStore(str(db))
    try:
        memory = Engram(content="Wording one.")
        store.save_engram(memory)
        for wording in ("Wording two.", "Wording three.", "Wording four."):
            _reworded(store, memory.id, wording)
        loaded = store.get_engram(memory.id)
        assert len(loaded.versions) == 3

        conn = store._get_conn()
        conn.execute("CREATE TEMP TABLE version_writes (n INTEGER NOT NULL)")
        conn.execute("INSERT INTO version_writes VALUES (0)")
        conn.execute(
            "CREATE TEMP TRIGGER count_version_writes AFTER INSERT ON main.versions "
            "BEGIN UPDATE version_writes SET n = n + 1; END"
        )
        conn.commit()
        # What decay does to every memory, every cycle.
        loaded.strength, loaded.accessibility = 0.42, 0.31
        store.save_engram(loaded)
        writes = conn.execute("SELECT n FROM version_writes").fetchone()[0]
    finally:
        store.close()

    assert writes == 0, f"saving an unchanged memory wrote {writes} version rows"
    assert len(_versions(db, memory.id)) == 3


def test_a_version_is_written_only_when_content_impact_or_resolution_changes(tmp_path):
    db = tmp_path / "store.db"
    store = EngramStore(str(db))

    def snapshot_then(change) -> int:
        engram = store.get_engram(memory.id)
        engram.add_version(reason="reconsolidation")
        change(engram)
        store.save_engram(engram)
        return len(_versions(db, memory.id))

    try:
        memory = Engram(content="The ferry leaves at seven.")
        store.save_engram(memory)

        unchanged = snapshot_then(lambda e: setattr(e, "strength", 0.9))
        impact = snapshot_then(lambda e: setattr(e, "impact", "Trips bend to the ferry."))
        resolution = snapshot_then(lambda e: setattr(e, "resolution", 0.7))
        content = snapshot_then(lambda e: setattr(e, "content", "The ferry leaves at eight."))
        in_memory = len(store.get_engram(memory.id).versions)
    finally:
        store.close()

    assert unchanged == 0, "a snapshot of an unchanged memory was written as a version"
    assert (impact, resolution, content) == (1, 2, 3)
    assert in_memory == 3


# ── Maintenance reads without recording an access ──


def test_maintenance_records_no_access(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    db = tmp_path / "l.db"
    runtime = MnemosRuntime(db_path=str(db), agent_id="demo", use_dedicated_model=False)
    try:
        runtime.capture("Checked the header in the inspector and it looked right",
                        impact="Verify the rendered result, not the declaration.")
        assert runtime._store is not None
        store = runtime._store
        said = "Verify the rendered result, not the declaration: computed styles prove nothing."
        lesson = Engram(
            content=said, impact=said, kind="procedural", tags=["lesson", "distilled"],
            strength=0.5, stability=0.5, owner_agent_id="demo",
            person_id=runtime.scope.person_id, project_scope=runtime.scope.project_scope,
            last_accessed="2026-01-01T00:00:00+00:00", author_kind="agent",
        )
        store.save_engram(lesson)
        # The captured memory has faded, and long enough ago to be softened.
        [fading] = [e for e in store.get_active_engrams(agent_id="demo", limit=10)
                    if "lesson" not in e.tags]
        fading.accessibility, fading.resolution = 0.02, 1.0
        store.save_engram(fading)
        conn = store._get_conn()
        conn.execute("UPDATE engrams SET created_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00+00:00", fading.id))
        conn.commit()
        before = dict((engram_id, rest) for engram_id, *rest in _all(
            db, "SELECT id, access_count, last_accessed FROM engrams"))

        runtime.maintain()
        after = dict((engram_id, rest) for engram_id, *rest in _all(
            db, "SELECT id, access_count, last_accessed FROM engrams"))
        reinforced = store.get_engram(lesson.id)
    finally:
        runtime.close()

    assert reinforced.strength == pytest.approx(0.6, abs=0.01), (
        "premise: maintenance reinforced the lesson"
    )
    # Maintenance may add memories (a promoted note); none it had may change.
    assert {engram_id: after[engram_id] for engram_id in before} == before, (
        "maintenance recorded an access"
    )


# ── Code older than the store reinforces nothing ──


def test_older_code_reinforces_nothing_through_the_retriever(tmp_path):
    db, ferry, garden = _ferry_and_garden(tmp_path)
    _set_store_minimum(db, 999)
    before = {engram_id: _row(db, engram_id) for engram_id in (ferry, garden)}

    store = EngramStore(str(db))
    try:
        results = ReactiveRetriever(store).retrieve(CUE, max_results=5, **SCOPE)
    finally:
        store.close()

    assert {r.engram.id for r in results} >= {ferry, garden}, "premise: both came back"
    assert {engram_id: _row(db, engram_id) for engram_id in (ferry, garden)} == before
    assert _co_activated(db, ferry, garden) == []
    assert _versions(db) == []


def test_older_code_asks_for_no_reinforcement(tmp_path, monkeypatch):
    """The runtime's own check, apart from the retriever's: current code asks
    to reinforce exactly what it shows, and older code asks for nothing."""
    from mnemos.retrieval import reactive

    db, ferry, _ = _ferry_and_garden(tmp_path)
    asked: list[str] = []
    real = reactive.reconsolidate

    def reconsolidate(**kwargs):
        asked.append(kwargs["engram"].id)
        return real(**kwargs)

    monkeypatch.setattr(reactive, "reconsolidate", reconsolidate)
    # Take the retriever's own check away, so only the runtime's is left.
    monkeypatch.setattr(reactive, "_code_older_than", lambda store: False, raising=False)

    runtime = _runtime(db)
    try:
        runtime.recall(CUE)
    finally:
        runtime.close()
    assert asked == [ferry], "current code asked to reinforce what it did not show"

    asked.clear()
    _set_store_minimum(db, 999)
    runtime = _runtime(db)
    try:
        assert "ferry timetable in the kitchen drawer" in runtime.recall(CUE)
        runtime.context(CUE)
    finally:
        runtime.close()
    assert asked == [], "older code asked to reinforce a memory"


@pytest.mark.parametrize("session", ["session-one", None])
def test_older_code_spends_no_reinforcement(tmp_path, monkeypatch, session):
    """Older code records no return at all, so the session's one
    reinforcement is still there for current code, and is spent once."""
    db, ferry, _ = _ferry_and_garden(tmp_path)
    if session:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    _set_store_minimum(db, 999)
    before = _row(db, ferry)

    runtime = _runtime(db)
    try:
        assert "ferry timetable in the kitchen drawer" in runtime.recall(CUE)
    finally:
        runtime.close()
    assert _row(db, ferry) == before, "older code reinforced a memory"

    _set_store_minimum(db, MAINTENANCE_CODE_VERSION)
    for _ in range(2):
        runtime = _runtime(db)
        try:
            runtime.recall(CUE)
        finally:
            runtime.close()
    # Had older code recorded the return, current code would reinforce nothing.
    assert _accesses(db, ferry) == 1
    claims = _all(db, "SELECT session_id, engram_id FROM session_reinforcements")
    assert claims == ([(session, ferry)] if session else [])


# ── mnemos repair-versions ──


def _store_with_copied_versions(tmp_path) -> tuple[Path, str, str]:
    """What returns used to leave behind: runs of copies after real versions."""
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        many = _memory_id(runtime.capture("The harbour log is kept in the blue binder."))
        single = _memory_id(runtime.capture("The pier lights are checked every Tuesday."))
    finally:
        runtime.close()
    rows = [
        (many, 1, "first", 1.0, "reconsolidation"),
        (many, 2, "first", 1.0, "reconsolidation"),   # copy
        (many, 3, "first", 1.0, "reconsolidation"),   # copy
        (many, 4, "first", 1.0, "softening"),         # another reason: stays
        (many, 5, "first", 1.0, "reconsolidation"),   # copy
        (many, 6, "second", 0.7, "reconsolidation"),  # a new state: stays
        (many, 7, "second", 0.7, "reconsolidation"),  # copy
        (many, 8, "second", 0.4, "reconsolidation"),  # a new resolution: stays
        (single, 1, "only", 1.0, "reconsolidation"),
    ]
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO versions (engram_id, version_num, content_snapshot, "
            "resolution_at_version, changed_at, change_reason) "
            "VALUES (?, ?, ?, ?, '2026-07-01T00:00:00+00:00', ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db, many, single


def test_repair_versions_is_a_dry_run_by_default(tmp_path, capsys):
    db, many, _ = _store_with_copied_versions(tmp_path)
    before = _settled_sha256(db)

    code = main(["repair-versions", "--db-path", str(db), *ARGS])
    out = capsys.readouterr().out

    assert code == 0
    assert _settled_sha256(db) == before, "the dry run changed the store"
    assert "9  version rows, for 2 memories" in out
    assert "4  copies of the version before them" in out
    assert f"{many}: 4 copies" in out
    assert "Dry run: nothing changed." in out
    assert not (tmp_path / "backups").exists() or not list(
        (tmp_path / "backups").glob("*repair-versions*")
    )


def test_repair_versions_removes_only_the_copies(tmp_path, capsys):
    db, many, single = _store_with_copied_versions(tmp_path)
    everything_else = _all(db, "SELECT id, content, access_count FROM engrams ORDER BY id")

    code = main(["repair-versions", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Removed 4 rows; 5 remain." in out
    assert [(num, snapshot, resolution, reason)
            for num, snapshot, resolution, _, reason in _versions(db, many)] == [
        (1, "first", 1.0, "reconsolidation"),
        (4, "first", 1.0, "softening"),
        (6, "second", 0.7, "reconsolidation"),
        (8, "second", 0.4, "reconsolidation"),
    ]
    assert [num for num, *_ in _versions(db, single)] == [1]
    assert _all(db, "SELECT id, content, access_count FROM engrams ORDER BY id") == everything_else
    [backup] = (tmp_path / "backups").glob("memory.pre-repair-versions-*.db")
    assert _all(backup, "PRAGMA integrity_check") == [("ok",)]
    assert _all(backup, "SELECT COUNT(*) FROM versions") == [(9,)]

    after_first = _settled_sha256(db)
    assert main(["repair-versions", "--db-path", str(db), *ARGS, "--write"]) == 0
    assert "Nothing to repair." in capsys.readouterr().out
    assert _settled_sha256(db) == after_first, "a second run changed the store"
    assert len(list((tmp_path / "backups").glob("memory.pre-repair-versions-*.db"))) == 1


def test_repair_versions_refuses_to_write_from_older_code(tmp_path, capsys):
    db, _, _ = _store_with_copied_versions(tmp_path)
    _set_store_minimum(db, 999)
    before = _settled_sha256(db)

    code = main(["repair-versions", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 1
    assert "older than the store expects, so it changes nothing" in out
    assert _settled_sha256(db) == before
