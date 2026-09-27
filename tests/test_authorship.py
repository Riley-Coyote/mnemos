"""Every memory says who wrote it (WP-R05).

The memory could not tell the agent's words from a tool's. No memory recorded
its author; on one real store 103 of 450 memories were written by the
transcript indexer's model (54 it extracted, 49 lessons copied from them), and
lessons copied whatever impact their source had, so a model's words landed in
the most durable tier, and identity, themes and lessons were measured from them.

Now every memory records what kind of writer wrote it (``author_kind``:
agent, tool, system, import or unknown), which model and which harness
session, at write time, and never later. Only the agent's own words shape its
identity, its belief questions and its lessons. A store migrated to v12 labels
what it held from what each row carries and leaves the rest unknown. A tool's
memories can be moved into the legacy quarantine, and exactly those brought
back. The daemon no longer schedules the substrate tick. And every tool call
leaves one row saying what it showed and what it wrote.

Tests read rows with ``SELECT *`` and look fields up by name, so on code
without authorship they fail on what they find rather than on a missing
column.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

from mnemos.cli import main
from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import SCHEMA_VERSION, SQL_CREATE_TABLES, EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
OWNER = {"owner_agent_id": "nova", "person_id": "riley", "project_scope": "demo"}

SESSION = "sess-7a1c2e90-5b3d"
OTHER_SESSION = "sess-4f0b91aa-c2d8"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"


# ── Helpers ──


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _engram_row(db, engram_id: str) -> dict:
    [row] = _rows(db, "SELECT * FROM engrams WHERE id = ?", (engram_id,))
    return row


def _author(row: dict) -> tuple:
    return (row.get("author_kind"), row.get("author_model"), row.get("author_session"))


def _write(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _as_session(monkeypatch, tmp_path: Path, session: str) -> None:
    """Run as a Claude Code session with no transcript to read."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-transcripts"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)


def _in_session(monkeypatch, tmp_path: Path, session: str = SESSION, *models: str) -> None:
    """Run as a Claude Code session; its transcript names ``models`` in turn."""
    config = tmp_path / "claude-config"
    path = config / "projects" / "-Users-someone-harbour" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    lines += [
        json.dumps({"type": "assistant", "message": {
            "role": "assistant", "model": model, "content": [{"type": "text", "text": "…"}],
        }})
        for model in models
    ]
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)


def _captured_id(result: str) -> str:
    match = re.search(r"Memory ID: (engram_\w+)", result)
    assert match, result
    return match.group(1)


def _memory(store: EngramStore, content: str, author: str, *, tags=None, impact="",
            impact_source="", note: bool = True, **fields) -> Engram:
    """A memory in SCOPE, written by ``author``, and the note a capture leaves."""
    engram = Engram(content=content, impact=impact, impact_source=impact_source,
                    tags=list(tags or ["continuity"]), **OWNER, **fields)
    engram.author_kind = author  # an attribute older code simply ignores
    store.save_engram(engram)
    if note:
        store.write_hypomnema_entry(
            content, agent_id="nova", person_id="riley", project_scope="demo",
            authored_by="agent" if author == "agent" else "unknown",
            related_engram_id=engram.id,
        )
    return engram


def _fade(db, *ids: str) -> None:
    """Old and faint enough for softening to take what these taught."""
    conn = sqlite3.connect(str(db))
    try:
        for engram_id in ids:
            conn.execute(
                "UPDATE engrams SET accessibility = 0.02, resolution = 1.0, created_at = ? "
                "WHERE id = ?", ("2020-01-01T00:00:00+00:00", engram_id),
            )
        conn.commit()
    finally:
        conn.close()


def _lessons(db) -> list[dict]:
    return _rows(db, "SELECT * FROM engrams WHERE tags LIKE '%\"distilled\"%' ORDER BY created_at")


# ── Decision 1: authorship is recorded when a memory is written ──


def test_a_capture_records_who_wrote_it_on_the_memory_and_its_note(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path, SESSION, SONNET)
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        result = runtime.capture(
            "Riley keeps the harbour ledger in ledger/2026.csv.",
            impact="Ledgers live in files, not in my head.",
            signed_as=OPUS,
        )
    finally:
        runtime.close()

    engram_id = _captured_id(result)
    assert _author(_engram_row(db, engram_id)) == ("agent", OPUS, SESSION)
    [note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE related_engram_id = ?", (engram_id,))
    assert (note["authored_by"], note["author_model"], note.get("author_session")) == (
        "agent", OPUS, SESSION,
    )
    assert "Signed: Opus 5.5 (claude-opus-5-5)" in result


def test_the_signature_is_resolved_on_every_write(tmp_path, monkeypatch):
    """signed_as, then the operator's setting, then this session's own
    introduction, then the transcript; the human can switch models mid-session."""
    _in_session(monkeypatch, tmp_path, SESSION, SONNET)
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        by_transcript = _captured_id(runtime.capture("First, before any introduction."))
        runtime.introduce(FABLE)
        by_introduction = _captured_id(runtime.capture("After the introduction."))
        by_signature = _captured_id(runtime.capture("Signed on the write.", signed_as=OPUS))
        monkeypatch.setenv("MNEMOS_AGENT_MODEL", HAIKU)
        by_operator = _captured_id(runtime.capture("With the operator's setting."))
        signed_anyway = _captured_id(runtime.capture("Signed again.", signed_as=OPUS))
    finally:
        runtime.close()

    models = [
        _engram_row(db, engram_id).get("author_model")
        for engram_id in (by_transcript, by_introduction, by_signature, by_operator, signed_anyway)
    ]
    assert models == [SONNET, FABLE, OPUS, HAIKU, OPUS]


def test_only_this_sessions_introduction_signs_its_writes(tmp_path, monkeypatch):
    """Found live 2026-09-26: a Grok session introduced itself to the store,
    and the store kept one declared model for everyone. A session's
    introduction signs that session's writes, in any of its processes, and no
    other session's."""
    db = tmp_path / "m.db"
    _as_session(monkeypatch, tmp_path, OTHER_SESSION)
    grok = _runtime(db)
    try:
        grok.introduce("grok-4.5", "Grok")
    finally:
        grok.close()

    _as_session(monkeypatch, tmp_path, SESSION)
    first = _runtime(db)
    try:
        first.introduce(OPUS)
    finally:
        first.close()

    # Another process of the same session: a CLI capture, as a Bash tool runs it.
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "remember", "--db-path", str(db), *SCOPE_ARGS,
         "Written from another process of this session."],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
             "PYTHONPATH": ":".join(sys.path), "CLAUDE_CODE_SESSION_ID": SESSION},
    )
    assert proc.returncode == 0, proc.stderr
    from_cli = _captured_id(proc.stdout)

    _as_session(monkeypatch, tmp_path, "sess-0c9d77e2-aa10")
    stranger = _runtime(db)
    try:
        unintroduced = _captured_id(stranger.capture("A session that never introduced itself."))
    finally:
        stranger.close()

    assert _author(_engram_row(db, from_cli)) == ("agent", OPUS, SESSION)
    assert _engram_row(db, unintroduced).get("author_model") == "", (
        "another session's introduction signed this one's writes"
    )
    # Read back in this process, as the next session would.
    reader = _runtime(db)
    try:
        assert "Written from another process of this session." in reader.recall("another process")
    finally:
        reader.close()


def test_the_tools_take_signed_as_over_the_real_protocol(tmp_path):
    """The agent signs each write through the MCP tools it actually calls."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from mnemos.simple_mcp import SERVER_INSTRUCTIONS, simple_mcp

    assert "signed_as" in SERVER_INSTRUCTIONS
    assert "mnemos_introduce" in SERVER_INSTRUCTIONS and "again" in SERVER_INSTRUCTIONS
    import asyncio

    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(simple_mcp.list_tools())}
    for name in ("mnemos_capture", "mnemos_handoff", "mnemos_correct", "mnemos_reflect"):
        assert "signed_as" in schemas[name]["properties"], name

    db = tmp_path / "stdio.db"
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    written: dict[str, str] = {}

    async def run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemos.cli", "serve", "--mode", "simple", "--db-path", str(db),
                  *SCOPE_ARGS],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
                 "PYTHONPATH": ":".join(sys.path), "CLAUDE_CODE_SESSION_ID": SESSION},
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                captured = await session.call_tool("mnemos_capture", {
                    "content": "The ferry leaves at seven.", "signed_as": FABLE,
                })
                text = "\n".join(b.text for b in captured.content if b.type == "text")
                written["capture"] = _captured_id(text)
                await session.call_tool("mnemos_handoff", {
                    "text": "Where I stopped: the ferry timetable.", "signed_as": SONNET,
                })
                corrected = await session.call_tool("mnemos_correct", {
                    "target_id": written["capture"],
                    "correction": "The ferry leaves at eight.", "signed_as": OPUS,
                })
                text = "\n".join(b.text for b in corrected.content if b.type == "text")
                written["correction"] = re.search(r"captured correction (engram_\w+)", text).group(1)

    anyio.run(run)

    assert _author(_engram_row(db, written["capture"])) == ("agent", FABLE, SESSION)
    assert _author(_engram_row(db, written["correction"])) == ("agent", OPUS, SESSION)
    [handoff] = _rows(db, "SELECT * FROM hypomnema_entries WHERE entry_kind = 'handoff'")
    assert (handoff["author_model"], handoff["author_session"]) == (SONNET, SESSION)


def test_a_reflection_is_the_agents_and_so_is_the_lesson_it_becomes(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path, SESSION, SONNET)
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        # Captured with what it meant, so the lesson is the one question waiting.
        engram_id = _captured_id(runtime.capture(
            "Shipped the ferry fix without running it.", impact="Unrun code is a guess.",
        ))
        assert runtime._store is not None
        runtime._store.enqueue_reflection(
            "lesson", engram_id, "This is fading. What did it teach you?", **SCOPE,
        )
        result = runtime.reflect(
            engram_id, "Run the thing before calling it fixed.", signed_as=FABLE,
        )
    finally:
        runtime.close()

    assert "Lesson recorded." in result
    memory = _engram_row(db, engram_id)
    assert (memory["impact"], memory["impact_source"]) == (
        "Run the thing before calling it fixed.", "agent",
    )
    [lesson] = _lessons(db)
    assert lesson["content"] == "Run the thing before calling it fixed."
    assert _author(lesson) == ("agent", FABLE, SESSION)
    [note] = _rows(db, "SELECT * FROM hypomnema_entries WHERE related_engram_id = ?", (engram_id,))
    assert "What this changed: (Fable 5.1) Run the thing" in note["content"]


def test_authorship_is_written_once_and_no_later_save_restates_it(tmp_path):
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime.introduce(OPUS)
        engram_id = _captured_id(runtime.capture("Riley's studio is in Lisbon."))
        assert runtime._store is not None
        loaded = runtime._store.get_engram(engram_id)
        loaded.author_kind, loaded.author_model, loaded.author_session = "tool", "", ""
        loaded.strength = 0.9
        runtime._store.save_engram(loaded)
    finally:
        runtime.close()

    row = _engram_row(db, engram_id)
    assert row["strength"] == pytest.approx(0.9), "premise: the save landed"
    assert _author(row) == ("agent", OPUS, "")


# ── Decision 2: a store from before labels what it held, once ──


def _without_engram_authors(sql: str) -> str:
    """The schema script as a v11 store had it: engrams without the author
    columns (continuity notes had theirs already)."""
    head, rest = sql.split("CREATE TABLE IF NOT EXISTS engrams (", 1)
    body, tail = rest.split(");", 1)
    body = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith(("author_", "--"))
    )
    return f"{head}CREATE TABLE IF NOT EXISTS engrams ({body});{tail}"


_PRE_V12_SCHEMA = _without_engram_authors(SQL_CREATE_TABLES)


def _pre_v12_store(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A v11 store as a real one arrives: captures, indexer output, lessons."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_PRE_V12_SCHEMA)
    assert "author_kind" not in {r[1] for r in conn.execute("PRAGMA table_info(engrams)")}
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '11')")
    stamp = "2026-08-01T00:00:00+00:00"
    ids: dict[str, str] = {}

    def row(key, content, tags, source_type, impact="", impact_source="", person="riley"):
        engram_id = f"engram_{key}"
        ids[key] = engram_id
        conn.execute(
            "INSERT INTO engrams (id, content, content_at_encoding, impact, impact_source, "
            "tags, source, owner_agent_id, person_id, project_scope, created_at, last_accessed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'nova', ?, 'demo', ?, ?)",
            (engram_id, content, content, impact, impact_source, json.dumps(tags),
             json.dumps({"type": source_type}), person, stamp, stamp),
        )

    def distilled(source, lesson):
        conn.execute(
            "INSERT INTO connections (source_id, target_id, relation, strength, formed_at) "
            "VALUES (?, ?, 'distilled_into', 0.9, ?)", (ids[source], ids[lesson], stamp),
        )

    row("capture", "Shipped the ferry fix without running it.", ["continuity", "project"],
        "session", impact="Run the thing before calling it fixed.", impact_source="agent")
    row("lesson", "Run the thing before calling it fixed.", ["continuity", "lesson", "distilled"],
        "reflection", impact="Run the thing before calling it fixed.")
    distilled("capture", "lesson")
    # A lesson drawn from that lesson, as older softening did: still its words.
    row("relesson", "Run the thing before calling it fixed.", ["lesson", "distilled"],
        "reflection", impact="Run the thing before calling it fixed.")
    distilled("lesson", "relesson")
    row("correction", "The ferry leaves at eight.", ["continuity", "correction"], "session",
        impact="Correction to earlier continuity.", impact_source="template")
    row("indexed", "The nightly backup cron fires at three.", ["session-indexed", "trace-type:fact"],
        "session")
    row("indexer_lesson", "Rebuild the search index after every schema change.",
        ["lesson", "distilled", "session-indexed"], "reflection",
        impact="Rebuild the search index after every schema change.")
    distilled("indexed", "indexer_lesson")
    # An impact from before impact_source existed: nobody can say whose.
    row("older_capture", "Moved the atlas into its own repo.", ["continuity"], "session",
        impact="Keep the atlas apart from the site.")
    row("older_lesson", "Keep the atlas apart from the site.", ["lesson", "distilled"],
        "reflection", impact="Keep the atlas apart from the site.")
    distilled("older_capture", "older_lesson")
    row("template_lesson", "Current working context for continuity.", ["lesson", "distilled"],
        "reflection", impact="Current working context for continuity.")
    # The advanced console and the bridge tag nothing: whose words is not known.
    row("console", "Kathmandu Newar grammar marks how a thing is known.", [], "session")
    conn.commit()
    conn.close()
    return db, ids


def test_the_migration_labels_what_a_store_held_and_guesses_nothing(tmp_path):
    db, ids = _pre_v12_store(tmp_path)

    EngramStore(str(db)).close()

    kinds = {key: _engram_row(db, engram_id).get("author_kind") for key, engram_id in ids.items()}
    assert kinds == {
        "capture": "agent", "lesson": "agent", "relesson": "agent", "correction": "agent",
        "indexed": "tool", "indexer_lesson": "tool",
        "older_capture": "agent", "older_lesson": "unknown", "template_lesson": "unknown",
        "console": "unknown",
    }
    [(value,)] = [tuple(r.values()) for r in _rows(
        db, "SELECT value FROM meta WHERE key = 'engram_authors_labeled'")]
    assert json.loads(value)["counts"] == {"agent": 5, "tool": 2, "unknown": 3}
    assert [r["value"] for r in _rows(db, "SELECT value FROM meta WHERE key = 'schema_version'")] == [
        str(SCHEMA_VERSION)
    ]
    assert len(list((tmp_path / "backups").glob(f"old.pre-v{SCHEMA_VERSION}-*.db"))) == 1

    # Once: a row older code writes afterwards keeps the column's default.
    _write(db, "UPDATE engrams SET author_kind = 'unknown' WHERE id = ?", (ids["capture"],))
    EngramStore(str(db)).close()
    assert _engram_row(db, ids["capture"])["author_kind"] == "unknown"


# ── Decision 3: only the agent's own words make it who it is ──


def test_an_indexed_memory_is_a_tools_and_never_becomes_a_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".mnemos").mkdir(parents=True)
    from mnemos.consolidation.softening import run_softening_pass
    from mnemos.indexer.session_indexer import SessionIndexer

    db = tmp_path / "m.db"
    indexer = SessionIndexer(agent_id="nova", db_path=str(db), config={})
    assert indexer._encode_to_mnemos([
        # The indexer's "lesson" carries its own words as the impact.
        {"type": "lesson", "content": "Always rebuild the sprite atlas after palette edits.",
         "salience": 0.8},
        {"type": "fact", "content": "The nightly backup cron fires at three in the morning.",
         "salience": 0.6},
    ], "transcript-1") == 2

    indexed = _rows(db, "SELECT * FROM engrams")
    assert [row.get("author_kind") for row in indexed] == ["tool", "tool"]
    assert all("session-indexed" in json.loads(row["tags"]) for row in indexed)

    _fade(db, *(row["id"] for row in indexed))
    store = EngramStore(str(db))
    try:
        stats = run_softening_pass(store, {}, None, agent_id="nova")
    finally:
        store.close()
    assert [row["id"] for row in _lessons(db)] == [], "the indexer's words became a lesson"
    assert not stats.get("awaiting_impact"), "the agent was asked what a tool's memory taught"


def test_a_lesson_is_drawn_only_from_an_impact_the_agent_wrote(tmp_path):
    from mnemos.consolidation.softening import run_softening_pass

    db = tmp_path / "m.db"
    store = EngramStore(str(db))
    try:
        own = _memory(store, "Spent a morning on a guard clause.", "agent",
                      impact="Read the guard clauses first when a handler misbehaves.",
                      impact_source="agent", note=False)
        modelled = _memory(store, "Moved the atlas into its own repo.", "agent",
                           impact="Separate repositories keep releases independent.",
                           impact_source="model", note=False)
        tools = _memory(store, "The nightly backup cron fires at three.", "tool",
                        impact="Backups run while the harbour sleeps at three.",
                        impact_source="agent", note=False)
        _fade(db, own.id, modelled.id, tools.id)
        run_softening_pass(store, {}, None, agent_id="nova", person_id="riley",
                           project_scope="demo")
    finally:
        store.close()

    lessons = _lessons(db)
    assert [row["content"] for row in lessons] == [
        "Read the guard clauses first when a handler misbehaves."
    ]
    assert lessons[0].get("author_kind") == "agent"


def test_the_agents_evidence_never_strengthens_a_tools_lesson(tmp_path):
    from mnemos.consolidation.softening import run_softening_pass

    db = tmp_path / "m.db"
    store = EngramStore(str(db))
    try:
        words = "Check the live ferry page before calling a fix done."
        tools = _memory(store, words, "tool", tags=["lesson", "distilled", "session-indexed"],
                        impact=words, note=False, strength=0.5, stability=0.5)
        own = _memory(store, "Called the ferry fix done without looking.", "agent",
                      impact=words, impact_source="agent", note=False)
        _fade(db, own.id)
        run_softening_pass(store, {}, None, agent_id="nova", person_id="riley",
                           project_scope="demo")
    finally:
        store.close()

    assert _engram_row(db, tools.id)["strength"] == pytest.approx(0.5), (
        "the agent's evidence strengthened a tool's lesson"
    )
    mine = [row for row in _lessons(db) if row["id"] != tools.id]
    assert [(row["content"], row.get("author_kind")) for row in mine] == [(words, "agent")]


def test_themes_are_mined_only_from_what_the_agent_wrote(tmp_path):
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        store = runtime._store
        assert store is not None
        for text in (
            "The harbour ledger moved into a spreadsheet.",
            "Harbour cranes were repaired over the weekend.",
            "Tides at the harbour were charted again.",
            "The harbour office opens early on market days.",
        ):
            _memory(store, text, "agent")
        for text in (
            "The zeppelin hangar reopened in spring.",
            "Zeppelin tours sold out quickly.",
            "A zeppelin mooring mast was painted.",
            "Zeppelin pilots trained over the bay.",
            "The zeppelin museum added a wing.",
            "Zeppelin flights paused for storms.",
        ):
            _memory(store, text, "tool")
        for _ in range(3):  # one theme is asked per cycle
            runtime._enqueue_belief_reflections(limit=1)
        asked = [row["prompt"] for row in _rows(
            db, "SELECT prompt FROM reflection_queue WHERE kind = 'belief'")]
    finally:
        runtime.close()

    themes = {re.search(r"\[theme:([^\]]+)\]", prompt).group(1) for prompt in asked}
    assert themes == {"harbour"}, (
        f"the agent was asked whether a tool's words are its belief: {asked}"
    )


def test_identity_is_measured_only_from_what_the_agent_wrote(tmp_path):
    from mnemos.consolidation.reflection import run_identity_pass
    from mnemos.identity_diff import compute_graph_identity

    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        store = runtime._store
        assert store is not None
        for n in range(3):
            _memory(store, f"Harbour ledger entry {n}.", "agent", tags=["harbour"])
        for n in range(9):
            _memory(store, f"Zeppelin fact {n}.", "tool", tags=["zeppelin"])
        run_identity_pass(store, agent_id="nova", person_id="riley", project_scope="demo")
        summary = store.get_identity("nova").epoch_state.self_summary
        computed = compute_graph_identity(store, "nova")
        graph = runtime.identity_graph()
    finally:
        runtime.close()

    assert "harbour" in summary
    assert "zeppelin" not in summary, summary
    concerns = json.dumps(computed.to_dict() if hasattr(computed, "to_dict") else vars(computed),
                          default=str)
    assert "zeppelin" not in concerns
    labels = " ".join(node["label"] for node in graph["nodes"] if node["kind"] == "memory")
    assert "Harbour" in labels and "Zeppelin" not in labels


# ── The packet and the hook: signatures that say whose words ──


def test_the_packet_says_when_a_lesson_is_a_tools_words(tmp_path):
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime._ensure_init()
        store = runtime._store
        assert store is not None
        _memory(store, "Always rebuild the sprite atlas after palette edits.", "tool",
                tags=["lesson", "session-indexed"], note=False)
        _memory(store, "Check the live ferry page before calling a fix done.", "agent",
                tags=["lesson", "distilled"], note=False)
        packet = runtime.context()
    finally:
        runtime.close()

    carrying = packet.split("### What you're carrying", 1)[1].split("###", 1)[0]
    assert "from a tool, not yours: Always rebuild the sprite atlas" in carrying, carrying
    assert "lesson: Check the live ferry page" in carrying, carrying


def test_the_hook_knows_its_reader_from_the_sessions_own_introduction(tmp_path, monkeypatch):
    """With no model in its payload, the SessionStart hook reads the model its
    session last introduced itself as, and no other session's."""
    db = tmp_path / "m.db"
    _as_session(monkeypatch, tmp_path, OTHER_SESSION)
    writer = _runtime(db)
    try:
        writer.introduce(OPUS)
        writer.handoff("Where I stopped: the harbour ledger.")
    finally:
        writer.close()
    _as_session(monkeypatch, tmp_path, SESSION)
    reader = _runtime(db)
    try:
        reader.introduce(OPUS)
    finally:
        reader.close()

    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start", "--db-path", str(db),
         *SCOPE_ARGS],
        input=json.dumps({"session_id": SESSION}), capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
             "PYTHONPATH": ":".join(sys.path)},
    )
    assert proc.returncode == 0, proc.stderr
    packet = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Yours (Opus 5.5), from another session" in packet, packet


# ── Decision 4: the daemon no longer schedules the substrate ──


def test_the_daemon_install_preview_lists_no_substrate_job(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("mnemos.setup.scheduler.detect_backend", lambda system=None: "launchd")

    assert main(["daemon", "install", "--agent-id", "nova"]) == 0
    out = capsys.readouterr().out
    assert "maintain" in out, "premise: the preview lists the jobs"
    assert "substrate" not in out, out


def test_installing_removes_the_substrate_job_an_earlier_install_left(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("mnemos.setup.scheduler.detect_backend", lambda system=None: "launchd")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    old = agents / "com.mnemos.nova.substrate-tick.plist"
    old.write_bytes(b"<plist/>")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "mnemos.cli._launchctl",
        lambda action, plist, allow_missing=False: calls.append((action, Path(plist).name)),
    )

    assert main(["daemon", "install", "--agent-id", "nova"]) == 0
    preview = capsys.readouterr().out
    assert old.exists(), "a preview removed something"
    assert "substrate-tick" in preview and "removed" in preview

    assert main(["daemon", "install", "--agent-id", "nova", "--write"]) == 0
    assert not old.exists(), "the substrate job an earlier install left is still scheduled"
    assert ("bootout", old.name) in calls
    assert ("bootstrap", old.name) not in calls
    assert {path.name for path in agents.iterdir()} == {
        "com.mnemos.nova.maintain.plist", "com.mnemos.nova.maintain-deep.plist",
    }


@pytest.mark.parametrize("command", ["index", "substrate-tick"])
def test_running_a_model_writer_by_hand_says_so(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("HOME", str(tmp_path))

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return {}

        def tick(self):
            return {}

    monkeypatch.setattr("mnemos.indexer.session_indexer.SessionIndexer", _Stub)
    monkeypatch.setattr("mnemos.substrate.tick.Substrate", _Stub)

    assert main(["--db-path", str(tmp_path / "m.db"), "--agent-id", "nova", command]) == 0
    out = capsys.readouterr().out
    assert "in a model's words, not the agent's" in out, out


# ── Decision 5: moving what a tool wrote out of the way, and back ──


def _mixed_store(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """The agent's capture, the indexer's output in scope, and older indexer
    output the v6 migration already hid."""
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    ids: dict[str, str] = {}
    try:
        ids["own"] = _captured_id(runtime.capture("Riley's harbour ledger lives in ledger/2026.csv."))
        store = runtime._store
        assert store is not None
        ids["fact"] = _memory(store, "The nightly backup cron fires at three.", "tool",
                              tags=["session-indexed"], note=False).id
        ids["lesson"] = _memory(store, "Always rebuild the sprite atlas after palette edits.",
                                "tool", tags=["lesson", "distilled", "session-indexed"],
                                note=False).id
        ids["dormant"] = _memory(store, "The zeppelin hangar reopened in spring.", "tool",
                                 tags=["session-indexed"], note=False, state="dormant").id
        ids["archived"] = _memory(store, "The harbour cafe closed.", "tool",
                                  tags=["session-indexed"], note=False, state="archived").id
        ids["linked"] = _memory(store, "Deploys go to staging first.", "tool",
                                tags=["session-indexed"], note=True).id
        ids["legacy"] = _memory(store, "Old indexer output from before scoping.", "tool",
                                tags=["session-indexed"], note=False).id
    finally:
        runtime.close()
    _write(db, "UPDATE engrams SET person_id = NULL, project_scope = NULL WHERE id = ?",
           (ids["legacy"],))
    return db, ids


def _scopes(db) -> dict[str, tuple]:
    return {
        row["id"]: (row["person_id"], row["project_scope"])
        for row in _rows(db, "SELECT id, person_id, project_scope FROM engrams")
    }


def _digest(db) -> str:
    return hashlib.sha256(Path(db).read_bytes()).hexdigest()


def test_quarantine_is_a_dry_run_first_and_undo_brings_back_exactly_what_it_moved(
    tmp_path, capsys,
):
    db, ids = _mixed_store(tmp_path)
    here = ("riley", "demo")
    cli = ["repair", "quarantine-tool-written", "--db-path", str(db), *SCOPE_ARGS]

    before = _digest(db)
    assert main(cli) == 0
    out = capsys.readouterr().out
    assert _digest(db) == before, "the dry run changed the store"
    assert re.search(r"\b5\s+written by a tool", out), out
    assert re.search(r"\b3\s+in recall and the packet\s+move to the quarantine", out), out
    assert "Dry run" in out

    assert main([*cli, "--write"]) == 0
    out = capsys.readouterr().out
    assert "Moved 3 memories into the quarantine" in out
    scopes = _scopes(db)
    assert {key: scopes[ids[key]] for key in ("fact", "lesson", "dormant")} == {
        "fact": (None, None), "lesson": (None, None), "dormant": (None, None),
    }
    assert scopes[ids["own"]] == here
    assert scopes[ids["archived"]] == here, "an archived memory moved"
    assert scopes[ids["linked"]] == here, "a memory a note points at moved"
    backups = list((tmp_path / "backups").glob("m.pre-quarantine-tool-written-*.db"))
    assert len(backups) == 1
    from mnemos.backup import check_database

    assert check_database(backups[0])["integrity"] == "ok"

    # Opening the store again does not undo it, and recall no longer finds them.
    runtime = _runtime(db)
    try:
        assert "sprite atlas" not in runtime.recall("sprite atlas palette")
        assert "harbour ledger" in runtime.recall("harbour ledger")
    finally:
        runtime.close()
    assert _scopes(db)[ids["fact"]] == (None, None)

    # A default adoption leaves them; undo returns exactly these, and no older output.
    assert main(["adopt-legacy", "--db-path", str(db), *SCOPE_ARGS, "--write"]) == 0
    capsys.readouterr()
    assert _scopes(db)[ids["lesson"]] == (None, None), "a default adoption brought a tool's lesson back"
    assert main([*cli, "--undo", "--write"]) == 0
    assert "Brought back 3 memories" in capsys.readouterr().out
    scopes = _scopes(db)
    assert {scopes[ids[key]] for key in ("fact", "lesson", "dormant")} == {here}
    assert scopes[ids["legacy"]] == (None, None), "undo brought back what it never moved"
    assert main(cli) == 0
    assert re.search(r"\b3\s+in recall and the packet", capsys.readouterr().out)


def test_code_older_than_the_store_quarantines_nothing(tmp_path, capsys):
    db, ids = _mixed_store(tmp_path)
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")

    assert main(["repair", "quarantine-tool-written", "--db-path", str(db), *SCOPE_ARGS,
                 "--write"]) == 1
    assert "older than the store" in capsys.readouterr().out
    assert _scopes(db)[ids["fact"]] == ("riley", "demo")
    assert not (tmp_path / "backups").exists() or not list(
        (tmp_path / "backups").glob("*quarantine*")
    )


# ── Decision 6: one trace row per tool call ──


def test_every_tool_call_leaves_one_trace_row(tmp_path, monkeypatch):
    _in_session(monkeypatch, tmp_path, SESSION, SONNET)
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime.capture("")  # returns before it opens the store: no row
        runtime.introduce(OPUS)
        engram_id = _captured_id(runtime.capture("Riley keeps the harbour ledger.",
                                                 signed_as=FABLE))
        recalled = runtime.recall("harbour ledger")
        handoff_id = re.search(r"Handoff ID: (\S+)", runtime.handoff("Where I stopped.")).group(1)
        runtime.context()
        runtime.health()  # read-only by contract: no row
        assert runtime._store is not None
        runtime._store.enqueue_reflection("impact", engram_id, "What did this change?", **SCOPE)
        runtime.reflect(engram_id, "Ledgers live in files.")
        runtime.correct("Riley keeps the harbour ledger in ledger/2026.csv.", target_id=engram_id)
        runtime.maintain()
    finally:
        runtime.close()

    trace = _rows(db, "SELECT * FROM memory_trace ORDER BY id")
    assert [row["tool"] for row in trace] == [
        "introduce", "capture", "recall", "handoff", "context", "reflect", "correct", "maintain",
    ]
    by_tool = {row["tool"]: row for row in trace}
    assert {row["session"] for row in trace} == {SESSION}
    assert by_tool["capture"]["author_model"] == FABLE
    assert by_tool["handoff"]["author_model"] == OPUS
    assert engram_id in json.loads(by_tool["capture"]["written_ids"])
    assert engram_id in json.loads(by_tool["recall"]["read_ids"]) and "harbour" in recalled
    assert handoff_id in json.loads(by_tool["context"]["read_ids"])
    assert handoff_id in json.loads(by_tool["handoff"]["written_ids"])
    assert json.loads(by_tool["reflect"]["read_ids"]) == [engram_id]
    corrected = json.loads(by_tool["correct"]["written_ids"])
    assert engram_id in corrected and len(corrected) == 2, corrected
    for row in trace:
        assert "harbour" not in row["read_ids"] + row["written_ids"], "a trace holds text"


def test_code_older_than_the_store_writes_no_trace(tmp_path):
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the harbour ledger.")
        assert len(_rows(db, "SELECT * FROM memory_trace")) == 1, "premise: current code traces"
        _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")
        result = runtime.capture("The ferry leaves at seven.")
    finally:
        runtime.close()

    assert "Captured continuity." in result, "the agent's words must still land"
    assert len(_rows(db, "SELECT * FROM memory_trace")) == 1


def test_the_trace_keeps_ninety_days(tmp_path):
    from mnemos.store.sqlite_store import TRACE_KEEP_DAYS

    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the harbour ledger.")
        old = (datetime.now(timezone.utc) - timedelta(days=TRACE_KEEP_DAYS + 1)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=TRACE_KEEP_DAYS - 1)).isoformat()
        for at in (old, recent):
            _write(db, "INSERT INTO memory_trace (at, tool) VALUES (?, 'recall')", (at,))
        runtime.recall("harbour")
    finally:
        runtime.close()

    kept = [row["at"] for row in _rows(db, "SELECT at FROM memory_trace")]
    assert TRACE_KEEP_DAYS == 90
    assert old not in kept and recent in kept and len(kept) == 3


# ── Round 2: every writer says so, and health names this session ──


def test_the_advanced_tools_record_the_agents_own_words(tmp_path, monkeypatch):
    """mnemos_remember and mnemos_ingest are the agent writing through its own
    tools: its words, signed by the order the simple tools use."""
    import mnemos.mcp_server as server
    import mnemos.simple_mcp as simple

    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    (home / ".mnemos" / "config.json").write_text(json.dumps({"setup_complete": True}))
    monkeypatch.setenv("HOME", str(home))
    _as_session(monkeypatch, tmp_path, SESSION)
    for name in ("_store", "_encoder", "_retriever", "_llm_client", "_embedding_index",
                 "_shared_pool", "_config"):
        monkeypatch.setattr(server, name, None, raising=False)
    monkeypatch.setattr(server, "_default_agent_id", "nova")
    monkeypatch.setattr(simple, "_runtime", None)
    monkeypatch.setattr(simple, "_runtime_kwargs", {})
    db = tmp_path / "advanced.db"
    simple.configure_runtime(db_path=str(db), **SCOPE)
    try:
        # The session introduces itself through the simple tools the advanced
        # server also serves.
        simple._get_runtime().introduce(FABLE)
        server._init_store(str(db))

        said = server.mnemos_remember("Riley keeps the harbour ledger in ledger/2026.csv.",
                                      agent_id="nova")
        remembered = re.search(r"Remembered: (engram_\w+)", said).group(1)
        assert _author(_engram_row(db, remembered)) == ("agent", FABLE, SESSION)

        said = server.mnemos_ingest("The October tide table for the harbour.",
                                    agent_id="nova", signed_as=OPUS)
        ingested = re.search(r"Ingested: (engram_\w+)", said).group(1)
        assert _author(_engram_row(db, ingested)) == ("agent", OPUS, SESSION)

        # A client that names no session: this process's own introduction signs.
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        said = server.mnemos_remember("The harbour office opens early.", agent_id="nova")
        unsessioned = re.search(r"Remembered: (engram_\w+)", said).group(1)
        assert _author(_engram_row(db, unsessioned)) == ("agent", FABLE, "")
    finally:
        if server._store is not None:
            server._store.close()
        if simple._runtime is not None:
            simple._runtime.close()


def test_what_the_substrate_writes_is_a_tools(tmp_path, monkeypatch):
    """Each substrate writer's memories are a tool's, so the quarantine can
    move them as it moves the indexer's."""
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    from mnemos.substrate.config import SubstrateConfig
    from mnemos.substrate.events import EventType, SubstrateEvent
    from mnemos.substrate.handlers import dreaming, initiation, insight, surprise, wandering
    from mnemos.substrate.introspection_pass import _encode_audit
    from mnemos.substrate.modulators import ModulatorState

    class _Model:
        """Answers every handler's question at once."""

        def structured_complete(self, system="", user="", temperature=0.7, **_):
            return json.dumps({
                "insight": "The ledger and the ferry keep the same clock.",
                "pattern": "Everything here runs on the harbour's timetable.",
                "dream": "A ledger floating out on the morning ferry.",
                "reflection": "The ferry schedule moved and the ledger did not.",
                "expectation_violated": "that timetables stay put",
                "thought": "What else keeps harbour time?",
                "origin": "the ledger",
                "significance": "Schedules tie the work together.",
            })

    db = tmp_path / "substrate.db"
    store = EngramStore(str(db))
    try:
        vivid = Engram(content="Riley keeps the harbour ledger in ledger/2026.csv.",
                       accessibility=0.95, strength=0.95)
        fading = Engram(content="The ferry left at seven that spring.",
                        accessibility=0.05, strength=0.2)
        for engram in (vivid, fading):
            engram.author_kind = "agent"
            store.save_engram(engram)
        before = {row["id"] for row in _rows(db, "SELECT id FROM engrams")}
        config = SubstrateConfig(agent_id="default", db_path=str(db))
        model, calm = _Model(), ModulatorState()
        insight.handle(SubstrateEvent(EventType.CONNECTION_DISCOVERED, {
            "from_engram_id": vivid.id, "to_engram_id": fading.id}), config, calm, store, model)
        initiation.handle(SubstrateEvent(EventType.SALIENCE_ACCUMULATED),
                          config, calm, store, model)
        dreaming.handle(SubstrateEvent(EventType.MEMORY_SOFTENED, {"engram_id": fading.id}),
                        config, calm, store, model)
        surprise.handle(SubstrateEvent(EventType.SURPRISE_DETECTED, {
            "engram_id": vivid.id, "surprise_score": 0.7}), config, calm, store, model)
        wandering.handle(SubstrateEvent(EventType.SILENCE_EXTENDED), config, calm, store, model)
        _encode_audit({"mode": "heuristic", "pattern_score": 0.2, "reaching_score": 0.8,
                       "assessment": "Reaching."}, config, store)
    finally:
        store.close()

    written = [row for row in _rows(db, "SELECT * FROM engrams") if row["id"] not in before]
    kinds = {row["content"].split("]", 1)[0].lstrip("["): row.get("author_kind") for row in written}
    assert kinds == {
        "insight": "tool", "initiation": "tool", "dream": "tool",
        "surprise": "tool", "wandering": "tool", "introspection": "tool",
    }


def test_the_openclaw_schedules_leave_the_substrate_tick_out(tmp_path):
    from mnemos.openclaw_cron import generate_cron_jobs, install_cron_jobs
    from mnemos.setup.cron_installer import generate_install_commands, get_job_definitions

    jobs = generate_cron_jobs(agent_id="nova")
    assert jobs, "premise: the generator schedules maintenance"
    assert "substrate" not in json.dumps(jobs).lower()
    assert "substrate" not in json.dumps(
        get_job_definitions(agent_name="Nova", workspace="~/nova")
    ).lower()
    assert "substrate" not in generate_install_commands(
        agent_name="Nova", agent_id="nova", workspace="~/nova"
    ).lower()

    # Reinstalling retires the tick an earlier install added; others' jobs stay.
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([
        {"name": "mnemos-substrate-tick", "payload": {"message": "mnemos substrate-tick"}},
        {"name": "someone-elses-job"},
    ]))
    assert install_cron_jobs(jobs, jobs_file=str(jobs_file))["success"]
    names = [job["name"] for job in json.loads(jobs_file.read_text())]
    assert "mnemos-substrate-tick" not in names
    assert "someone-elses-job" in names


def test_health_and_doctor_name_this_sessions_own_introduction(tmp_path, monkeypatch, capsys):
    """Found live 2026-09-26: health named the scope's last introduction, a
    Grok session's, whoever asked."""
    from mnemos.simple_runtime import format_health_card

    db = tmp_path / "m.db"
    _as_session(monkeypatch, tmp_path, OTHER_SESSION)
    grok = _runtime(db)
    try:
        grok.introduce("grok-4.5", "Grok")
    finally:
        grok.close()

    _as_session(monkeypatch, tmp_path, SESSION)
    runtime = _runtime(db)
    try:
        before = runtime.health()
        runtime.introduce(OPUS, "Nova")
        after = runtime.health()
    finally:
        runtime.close()

    assert "grok" not in json.dumps(before["identity"]).lower(), before["identity"]
    assert "Identity:      none this session" in format_health_card(before)
    assert after["identity"] == {"session": SESSION, "model": OPUS, "name": "Nova"}
    said = "Opus 5.5 (claude-opus-5-5), named Nova (introduced this session)"
    assert f"Identity:      {said}" in format_health_card(after)

    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    assert f"Identity:     {said}" in capsys.readouterr().out
    _as_session(monkeypatch, tmp_path, "sess-0c9d77e2-aa10")
    assert main(["doctor", "--db-path", str(db), *SCOPE_ARGS]) == 0
    out = capsys.readouterr().out
    assert "Identity:     none this session" in out
    assert "grok" not in out.lower()


def test_a_memory_restored_from_the_archive_is_traced_as_written(tmp_path):
    """Recall with include_archived (WP-R06) brings a faded memory back: the
    trace records it as shown and as written, since restoring changes it."""
    db = tmp_path / "m.db"
    runtime = _runtime(db)
    try:
        engram_id = _captured_id(runtime.capture("The harbour crane was repainted blue in May."))
        assert runtime._store is not None
        runtime._store.archive_engram(
            runtime._store.get_engram(engram_id), reason="decay_below_threshold",
        )
        said = runtime.recall("harbour crane repainted", include_archived=True)
    finally:
        runtime.close()

    assert "From the archive:" in said, said
    [row] = _rows(db, "SELECT * FROM memory_trace WHERE tool = 'recall'")
    assert engram_id in json.loads(row["read_ids"])
    assert engram_id in json.loads(row["written_ids"])
    assert _engram_row(db, engram_id)["state"] == "active"


def test_identity_diff_reads_only_what_the_agent_wrote(tmp_path):
    """Review of PR #86: `mnemos identity diff` loaded its own list, unfiltered,
    so a tool's memory that had been returned often, or held a contradiction,
    read as the agent's preoccupation and tension, and was counted."""
    from mnemos.identity_diff import compute_graph_identity, diff_identity, parse_soul_file

    db = tmp_path / "m.db"
    soul = tmp_path / "SOUL.md"
    soul.write_text(
        "# Soul\n\n## Essence\n\n"
        "- I keep the harbour ledger in one spreadsheet.\n"
        "- Riley's harbour work comes first.\n"
    )
    store = EngramStore(str(db))
    try:
        anchor = _memory(store, "Riley decided the harbour ledger stays in one csv file.",
                         "agent", tags=["harbour"], note=False)
        twins = {}
        for author, words, tag in (
            ("agent", "The harbour ledger moved into a spreadsheet after all.", "spreadsheet"),
            ("tool", "The zeppelin hangar log moved into a spreadsheet as well.", "zeppelin"),
        ):
            twin = Engram(content=words, tags=[tag], access_count=12,
                          reconsolidation_count=9, **OWNER)
            twin.author_kind = author
            twin.add_connection(anchor.id, "contradicts", 0.7)
            twin.add_connection(anchor.id, "co_activated", 0.5)
            store.save_engram(twin)
            twins[author] = twin
        computed = compute_graph_identity(store, "nova")
        report = diff_identity(parse_soul_file(soul), computed, llm_client=None)
    finally:
        store.close()

    shown = json.dumps({
        "items": [(item.facet, item.text) for item in computed.items],
        "contradictions": computed.contradiction_edges,
        "report": report.to_dict(),
    })
    assert "zeppelin" not in shown.lower(), shown
    agent_twin = twins["agent"].content
    assert [item.text for item in computed.facet("preoccupation")] == [agent_twin]
    assert agent_twin in [item.text for item in computed.facet("hub")]
    assert computed.contradiction_edges == [(agent_twin, anchor.content)]
    assert computed.engram_count == 2
    assert report.graph_stats["engram_count"] == 2
