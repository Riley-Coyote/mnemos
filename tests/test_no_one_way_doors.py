"""No one-way doors: a memory that fades can come back.

A field report from another port of this memory found archived memories
invisible to recall, with recall the only way back: "complete and unreachable",
and every health check green. Mnemos had the same shape one step earlier:

  * decay read active memories only, so a dormant one was never touched again;
  * recall seeded active memories only, so a dormant one never came back;
  * ``archive.resharpen`` had no caller;
  * the dream report told the agent dormant memories were "ready to wake if
    needed".

Now recall seeds a dormant memory that matches the cue, at half the score, and
a returned one wakes. Dormant and archived memories take no part in resonance.
Decay keeps fading a dormant memory until it wakes or reaches the archive. A
memory that faded into the archive comes back by its id, or through
``mnemos_recall`` with ``include_archived``; one the agent forgot or replaced
never does. Health counts what an ordinary recall cannot reach, and names the
call that can. Code older than the store still reads all of it, and changes
none of it: it wakes, fades and restores nothing.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.consolidation.decay import run_decay_pass
from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.dream_journal import compose_dream_narrative
from mnemos.retrieval.reactive import ReactiveRetriever
from mnemos.simple_runtime import MnemosRuntime, format_health_card
from mnemos.store.archive import resharpen
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
STORE_SCOPE = {"owner_agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
FERRY = "Riley keeps the ferry timetable in the kitchen drawer."
HARBOUR = "The ferry leaves the harbour at seven on weekdays."
CUE = "ferry timetable kitchen drawer"
# Only the memory holds this phrase: a recall echoes its query, which does not.
FERRY_SHOWN = "timetable in the kitchen drawer"
# Memories that share no words with the cue, so every word it holds means
# something to the full-text index.
OTHERS = (
    "Marigolds bloom beside the greenhouse door every June.",
    "The lighthouse keeper logs the weather at dawn.",
    "Riley's bicycle has a squeaky rear brake.",
    "The choir rehearses on Tuesday evenings in the old hall.",
)
QUIET_LINE = "(it had gone quiet)"
UNREACHABLE = 'mnemos_recall("<its words>", include_archived=true)'
OLDER = "This session runs older Mnemos code than the store expects. Restart the session."
AHEAD = 999


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


def _row(db, engram_id: str, *fields: str) -> dict:
    fields = fields or ("state", "accessibility", "access_count")
    [values] = _all(db, f"SELECT {', '.join(fields)} FROM engrams WHERE id = ?", (engram_id,))
    return dict(zip(fields, values))


def _archive_rows(db, engram_id: str) -> list[tuple]:
    return _all(db, "SELECT archive_reason FROM archive WHERE id = ?", (engram_id,))


def _indexed(db, engram_id: str) -> bool:
    return bool(_all(db, "SELECT 1 FROM engrams_fts WHERE id = ?", (engram_id,)))


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _write(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _go_quiet(db, engram_id: str, accessibility: float = 0.03, hours_ago: float = 400) -> None:
    """Leave a memory as decay leaves one that went dormant: state, trace and
    last use, with its words still in the index."""
    _write(
        db,
        "UPDATE engrams SET state = 'dormant', accessibility = ?, last_accessed = ? "
        "WHERE id = ?",
        (accessibility, _ago(hours_ago), engram_id),
    )


def _fade(db, engram_id: str) -> None:
    """Archive a memory exactly as decay does when it fades below the archive
    threshold."""
    store = EngramStore(str(db))
    try:
        engram = store.get_engram(engram_id)
        engram.accessibility = 0.004
        store.archive_engram(engram, reason="decay_below_threshold")
    finally:
        store.close()


def _claim_for_newer_code(db, version: int = AHEAD) -> None:
    """What a newer server's startup does to a store: raise its minimum."""
    _write(
        db,
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
        (str(version),),
    )


def _stored(tmp_path, *texts: str) -> tuple[Path, list[str]]:
    """Durable memories in the runtime's scope, without the continuity note a
    capture also writes: whatever a recall then shows of one comes from the
    memory itself, never from a note that still holds its words."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    try:
        ids = [_engram(store, text) for text in texts]
    finally:
        store.close()
    return db, ids


# A recall as a separate Mnemos process makes it: the woken memory must be read
# back from the store by another process, not from this one's objects.
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


def _recall_in_another_process(db, cue: str, *, home: Path, session: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", _RECALL, str(db), cue],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
            "CLAUDE_CODE_SESSION_ID": session,
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _engram(store: EngramStore, content: str, **fields) -> str:
    engram = Engram(content=content, **STORE_SCOPE, **fields)
    store.save_engram(engram)
    return engram.id


def _link(store: EngramStore, source: str, target: str) -> None:
    store.save_connection(source, Connection(
        target_id=target, relation=ConnectionRelation.SUPPORTS, strength=1.0,
    ))


def _found(db, cue: str) -> dict[str, float]:
    """What retrieval itself finds for ``cue``, with scores, writing nothing."""
    store = EngramStore(str(db))
    try:
        retriever = ReactiveRetriever(store, reconsolidation_enabled=False)
        return {r.engram.id: r.score for r in retriever.retrieve(cue, max_results=10, **SCOPE)}
    finally:
        store.close()


# ── 1. Recall finds a dormant memory by a strong cue, and it wakes ──


def test_a_strong_cue_finds_a_dormant_memory_and_it_wakes(tmp_path):
    db, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR, *OTHERS)
    _go_quiet(db, ferry)
    home = tmp_path / "home"
    home.mkdir()

    shown = _recall_in_another_process(db, CUE, home=home, session="session-one")

    assert FERRY_SHOWN in shown, (
        f"a dormant memory the cue names outright was not recalled:\n{shown}"
    )
    assert QUIET_LINE in shown, shown
    woke = _row(db, ferry)
    assert woke["state"] == "active", "a dormant memory recall returned did not wake"
    assert woke["accessibility"] >= 0.8, (
        "a woken memory did not rise to what a returned memory gets"
    )
    assert woke["access_count"] == 1

    # Waking is a return, and a session returns a memory once.
    _recall_in_another_process(db, CUE, home=home, session="session-one")
    assert _row(db, ferry) == woke, "the same session woke or reinforced it twice"


def test_a_dormant_memory_starts_at_half_its_score(tmp_path):
    """Seeded when the cue matches it, at half the activation an active memory
    starts with, so an equal active match comes first."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    try:
        ferry = _engram(store, FERRY)
        harbour = _engram(store, HARBOUR)
        for text in OTHERS:
            _engram(store, text)
    finally:
        store.close()

    awake = _found(db, CUE)
    assert awake[ferry] == 1.0, "premise: the memory the cue names is its best match"
    _go_quiet(db, ferry)
    quiet = _found(db, CUE)

    assert ferry in quiet, "recall did not seed a dormant memory the cue matched"
    assert quiet[ferry] == pytest.approx(awake[ferry] / 2, abs=1e-4)
    assert quiet[harbour] == pytest.approx(awake[harbour], abs=1e-4), (
        "a dormant seed changed how an active memory scores"
    )


def test_recalling_a_dormant_memory_by_its_id_wakes_it(tmp_path):
    db, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR)
    _go_quiet(db, ferry)

    runtime = _runtime(db)
    try:
        shown = runtime.recall(ferry)
    finally:
        runtime.close()

    assert _row(db, ferry, "state")["state"] == "active", (
        "a dormant memory recalled by its own id stayed dormant"
    )
    assert FERRY in shown and "It had gone quiet." in shown, shown


def test_every_recall_path_wakes_what_it_returns_and_older_code_none(tmp_path):
    """The advanced server, the bridge and `mnemos search` retrieve and
    reinforce in one call. A dormant memory they return wakes too, and a store
    newer code has opened keeps its dormant memories as they are."""
    def retrieve(db) -> list[str]:
        store = EngramStore(str(db))
        try:
            return [r.engram.id for r in ReactiveRetriever(store).retrieve(CUE, **SCOPE)]
        finally:
            store.close()

    current, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR)
    _go_quiet(current, ferry)
    assert ferry in retrieve(current)
    assert _row(current, ferry, "state")["state"] == "active"

    (tmp_path / "older").mkdir()
    older, (quiet, *_) = _stored(tmp_path / "older", FERRY, HARBOUR)
    _go_quiet(older, quiet)
    _claim_for_newer_code(older)
    before = _row(older, quiet)
    assert quiet in retrieve(older), "premise: older code still reads a dormant memory"
    assert _row(older, quiet) == before, "older code woke a dormant memory"


# ── 2. Dormant and archived memories never relay ──


@pytest.mark.parametrize("quiet_state", ["dormant", "archived"])
def test_quiet_memories_never_relay(tmp_path, quiet_state):
    """A → Q → B: the cue matches A alone. Through an active Q, resonance
    reaches B; through a quiet Q it must not, and Q gets nothing either."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    try:
        seed = _engram(store, FERRY)
        middle = _engram(store, "The lighthouse keeper logs the weather at dawn.")
        far = _engram(store, "Marigolds bloom beside the greenhouse door every June.")
        seeded_quiet = _engram(store, "The ferry timetable changes in the kitchen at noon.")
        beyond = _engram(store, "The choir rehearses on Tuesday evenings in the old hall.")
        _engram(store, "Riley's bicycle has a squeaky rear brake.")
        _link(store, seed, middle)
        _link(store, middle, far)
        _link(store, seeded_quiet, beyond)
    finally:
        store.close()

    found = _found(db, CUE)
    assert middle in found and far in found, "premise: the path carries activation while active"

    if quiet_state == "dormant":
        _go_quiet(db, middle)
    else:
        _fade(db, middle)
    _go_quiet(db, seeded_quiet)
    found = _found(db, CUE)

    assert far not in found, f"a {quiet_state} memory passed activation on"
    assert middle not in found, f"a {quiet_state} memory was reached through a link"
    assert seeded_quiet in found, "premise: the cue itself matches the dormant seed"
    assert beyond not in found, "a dormant seed passed activation on"


# ── 3. Decay keeps running over dormant memories ──


def _decay_store(tmp_path, name: str = "memory.db") -> tuple[Path, dict[str, str]]:
    db = tmp_path / name
    store = EngramStore(str(db))
    try:
        ids = {
            # Quiet for a month: its next pass takes it to the archive.
            "old": _engram(store, "The old ferry pier was rebuilt in stone.",
                           state="dormant", accessibility=0.03, stability=0.0,
                           last_accessed=_ago(720)),
            # Quiet, but touched ten hours ago: the recency floor that holds an
            # active memory at 0.4 must not lift it.
            "recent": _engram(store, "The harbour master prefers written requests.",
                              state="dormant", accessibility=0.04, stability=0.0,
                              last_accessed=_ago(10)),
            "active": _engram(store, "Riley keeps the ferry timetable in the kitchen drawer.",
                              accessibility=0.5, last_accessed=_ago(1)),
        }
    finally:
        store.close()
    return db, ids


def _decay(db) -> dict:
    store = EngramStore(str(db))
    try:
        return run_decay_pass(store, {}, **SCOPE)
    finally:
        store.close()


def test_decay_keeps_fading_a_dormant_memory_to_the_archive(tmp_path):
    db, ids = _decay_store(tmp_path)

    stats = _decay(db)

    assert _row(db, ids["old"], "state")["state"] == "archived", (
        "decay never touched a dormant memory, so it could not finish fading"
    )
    assert _archive_rows(db, ids["old"]) == [("decay_below_threshold",)]
    recent = _row(db, ids["recent"], "state", "accessibility")
    assert recent["state"] == "dormant"
    assert recent["accessibility"] < 0.04, "a dormant memory did not keep fading"
    assert stats["engrams_archived"] == 1
    # Already quiet is not newly quiet: the report must not say it every cycle.
    assert stats["engrams_dormant"] == 0
    assert stats["dormant_processed"] == 2


def test_older_code_leaves_dormant_memories_to_the_newer_rules(tmp_path):
    """Fading a dormant memory is this version's rule: code older than the
    store applies it through no path, the runtime's maintenance or the pass
    called directly (as the bridge and the advanced server call it)."""
    current, current_ids = _decay_store(tmp_path, "current.db")
    runtime = _runtime(current)
    try:
        runtime.maintain()
    finally:
        runtime.close()
    assert _row(current, current_ids["old"], "state")["state"] == "archived", (
        "premise: current code fades a dormant memory"
    )

    older, older_ids = _decay_store(tmp_path, "older.db")
    _claim_for_newer_code(older)
    quiet_ids = (older_ids["old"], older_ids["recent"])
    before = {engram_id: _row(older, engram_id) for engram_id in quiet_ids}
    runtime = _runtime(older)
    try:
        runtime.maintain()
    finally:
        runtime.close()
    _decay(older)

    assert {engram_id: _row(older, engram_id) for engram_id in quiet_ids} == before


# ── 4. A memory that faded comes back; one forgotten or replaced does not ──


def test_a_faded_memory_comes_back_by_its_id(tmp_path):
    db, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR, *OTHERS)
    _fade(db, ferry)

    runtime = _runtime(db)
    try:
        ordinary = runtime.recall(CUE)
        by_id = runtime.recall(ferry)
    finally:
        runtime.close()

    assert FERRY_SHOWN not in ordinary, "premise: ordinary recall leaves the archive alone"
    assert FERRY in by_id, f"a faded memory did not come back by its id:\n{by_id}"
    assert "It had faded into the archive; recalling it by its id brought it back." in by_id
    restored = _row(db, ferry)
    assert restored["state"] == "active"
    assert restored["accessibility"] >= 0.8 and restored["access_count"] == 1, (
        "a restored memory was not treated as returned"
    )
    assert _archive_rows(db, ferry) == [] and _indexed(db, ferry)

    runtime = _runtime(db)
    try:
        assert FERRY_SHOWN in runtime.recall(CUE), "a restored memory stayed out of recall"
    finally:
        runtime.close()


def test_include_archived_reaches_what_faded_and_nothing_forgotten(tmp_path):
    faded_text = "The harbour ledger lives in the blue binder on the shelf."
    forgotten_text = "The harbour ledger password is written on a sticky note."
    replaced_text = "The harbour ledger closes on Fridays at noon."
    db, (faded, forgotten, replaced) = _stored(
        tmp_path, faded_text, forgotten_text, replaced_text,
    )
    _fade(db, faded)
    runtime = _runtime(db)
    try:
        runtime.correct("", target_id=forgotten, action="forget")
        corrected = runtime.correct(
            "The harbour ledger closes on Thursdays at noon.", target_id=replaced,
        )
        ordinary = runtime.recall("harbour ledger")
        archived = runtime.recall("harbour ledger", include_archived=True)
        forgotten_by_id = runtime.recall(forgotten)
        replaced_by_id = runtime.recall(replaced)
    finally:
        runtime.close()

    assert "blue binder" not in ordinary, "premise: ordinary recall leaves the archive alone"
    [replacement] = re.findall(r"captured correction (engram_[A-Za-z0-9]+)", corrected)
    assert replacement in ordinary, "premise: the correction is an ordinary memory"
    section = archived.split("From the archive:", 1)
    assert len(section) == 2, f"nothing came back from the archive:\n{archived}"
    assert "blue binder" in section[1]
    assert "It had faded out of ordinary recall; recalling it brought it back." in section[1]
    assert _row(db, faded, "state")["state"] == "active"

    for text, engram_id, by_id in (
        (forgotten_text, forgotten, forgotten_by_id),
        (replaced_text, replaced, replaced_by_id),
    ):
        assert text not in archived and text not in by_id, (
            f"a memory the agent closed on purpose came back:\n{archived}\n{by_id}"
        )
        assert _row(db, engram_id, "state")["state"] == "archived"


def test_include_archived_matches_words_whatever_their_case(tmp_path):
    """SQLite's LIKE folds the case of ASCII letters only, so the archive is
    matched word by word, as recall's own filters match."""
    db, (emile, *_) = _stored(tmp_path, "Émile keeps the harbour ledger in the attic.")
    _fade(db, emile)

    runtime = _runtime(db)
    try:
        shown = runtime.recall("émile", include_archived=True)
    finally:
        runtime.close()

    assert "in the attic" in shown.split("From the archive:", 1)[-1], shown
    assert _row(db, emile, "state")["state"] == "active"


def test_mnemos_recall_takes_include_archived_over_the_protocol(tmp_path):
    """The call the health card names works through the real MCP server, a
    separate process from the one that reads the store back."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = tmp_path / "surface.db"

    def text(result) -> str:
        return "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )

    async def session_calls(steps):
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "mnemos.cli", "serve", "--mode", "simple", "--db-path", str(db),
                "--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo",
            ],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await steps(session)

    async def capture(session):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert "include_archived" in tools["mnemos_recall"].inputSchema["properties"]
        return _memory_id(text(await session.call_tool("mnemos_capture", {"content": FERRY})))

    async def recall(session):
        return text(await session.call_tool(
            "mnemos_recall", {"query": "ferry timetable", "include_archived": True},
        ))

    ferry = anyio.run(session_calls, capture)
    _fade(db, ferry)
    shown = anyio.run(session_calls, recall)

    assert FERRY_SHOWN in shown.split("From the archive:", 1)[-1], shown
    assert _row(db, ferry, "state")["state"] == "active"


def test_older_code_shows_what_faded_but_leaves_it_in_the_archive(tmp_path):
    db, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR)
    _fade(db, ferry)
    _claim_for_newer_code(db)

    runtime = _runtime(db)
    try:
        by_id = runtime.recall(ferry)
        assert FERRY in by_id, f"older code could not even read a faded memory by its id:\n{by_id}"
        by_words = runtime.recall("ferry timetable", include_archived=True)
    finally:
        runtime.close()

    assert "It has faded into the archive, and this session's code leaves it there." in by_id
    assert FERRY_SHOWN in by_words.split("From the archive:", 1)[-1]
    assert "this session's code leaves it in the archive" in by_words
    assert by_id.endswith(OLDER) and by_words.endswith(OLDER)
    assert _row(db, ferry, "state")["state"] == "archived", "older code restored a memory"
    assert _archive_rows(db, ferry) == [("decay_below_threshold",)]
    assert not _indexed(db, ferry)


def test_older_code_shows_a_dormant_memory_but_does_not_wake_it(tmp_path):
    db, (ferry, *_) = _stored(tmp_path, FERRY, HARBOUR, *OTHERS)
    _go_quiet(db, ferry)
    _claim_for_newer_code(db)
    before = _row(db, ferry, "state", "accessibility", "access_count", "last_accessed")

    runtime = _runtime(db)
    try:
        by_words = runtime.recall(CUE)
        by_id = runtime.recall(ferry)
    finally:
        runtime.close()

    assert FERRY_SHOWN in by_words and QUIET_LINE in by_words, by_words
    assert "It had gone quiet." in by_id, by_id
    assert _row(db, ferry, "state", "accessibility", "access_count", "last_accessed") == before, (
        "older code woke a dormant memory"
    )


# ── 5. resharpen restores ──


def test_resharpen_restores_worn_words_at_full_resolution(tmp_path):
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    try:
        worn = Engram(
            content="ferry drawer", content_at_encoding=FERRY, resolution=0.4,
            accessibility=0.004, **STORE_SCOPE,
        )
        store.save_engram(worn)
        store.archive_engram(worn, reason="decay_below_threshold")
        restored = resharpen(store, worn.id)
    finally:
        store.close()

    assert restored is not None and restored.state == "active"
    assert restored.content == FERRY
    assert restored.resolution == 1.0, "the original words came back marked as worn down"
    assert [v.content_snapshot for v in restored.versions if v.change_reason == "resharpen"] == [
        "ferry drawer"
    ], "the worn wording was not kept as a version"
    assert _archive_rows(db, worn.id) == [] and _indexed(db, worn.id)


def test_resharpen_leaves_a_memory_that_is_not_archived(tmp_path):
    """Real stores hold archive rows for memories that are active or dormant
    again (1,392 of them on a copy of one). Such a row is not an archived
    memory, and restoring from it would overwrite what the memory holds now."""
    db = tmp_path / "memory.db"
    store = EngramStore(str(db))
    try:
        engram = Engram(content=FERRY, **STORE_SCOPE)
        store.save_engram(engram)
        store.archive_engram(engram, reason="decay_below_threshold")
    finally:
        store.close()
    _write(db, "UPDATE engrams SET state = 'dormant', content = ? WHERE id = ?",
           ("The ferry timetable moved to the hall.", engram.id))
    before = _row(db, engram.id, "state", "content", "accessibility")

    store = EngramStore(str(db))
    try:
        assert resharpen(store, engram.id) is None
    finally:
        store.close()
    assert _row(db, engram.id, "state", "content", "accessibility") == before


# ── 6. Health counts what an ordinary recall cannot reach ──


def test_health_counts_memories_out_of_ordinary_recall(tmp_path):
    db, (faded, forgotten, quiet, _) = _stored(
        tmp_path,
        "The harbour ledger lives in the blue binder on the shelf.",
        "The harbour ledger password is written on a sticky note.",
        FERRY,
        HARBOUR,
    )
    _fade(db, faded)
    _go_quiet(db, quiet)
    runtime = _runtime(db)
    try:
        runtime.correct("", target_id=forgotten, action="forget")
        data = runtime.health()
    finally:
        runtime.close()

    # The faded one only: a strong match brings the dormant one back, and the
    # forgotten one stays gone by the agent's own choice.
    assert data["unreachable"] == {"count": 1, "command": UNREACHABLE}
    assert data["counts"]["memories_dormant"] == 1
    card = format_health_card(data)
    assert (
        "Unreachable:   1 faded memory is stored in the archive, out of ordinary recall; "
        f"{UNREACHABLE} reaches it"
    ) in card, card
    assert "Memories:      1 active, 1 dormant, 2 archived" in card, card


# ── 7. The dream report says what is true ──


def test_the_dream_report_says_what_recall_does_with_quiet_memories():
    many = compose_dream_narrative({"cycle_type": "deep", "decay": {"engrams_dormant": 3}})
    one = compose_dream_narrative({"cycle_type": "deep", "decay": {"engrams_dormant": 1}})
    both = compose_dream_narrative({"decay": {"engrams_archived": 2, "engrams_dormant": 3}})

    assert "3 memories went quiet. A strong match brings them back." in many, many
    assert "1 memory went quiet. A strong match brings it back." in one, one
    assert "ready to wake" not in many
    assert "Mnemos moved 2 faded memories into the archive." in both, both
    assert "3 memories went quiet." in both, "a cycle that did both reported one"


def test_this_is_code_version_four_or_later():
    """Waking, fading dormant memories and restoring from the archive are
    rules this version introduced; servers still running version 3 must stand
    down once it opens a store."""
    assert MAINTENANCE_CODE_VERSION >= 4
