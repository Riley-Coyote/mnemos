"""A correction keeps what the memory meant.

Every memory carries an impact: what it changed in how the agent understands
things, in the agent's own words. It is the part meant to survive when the
details fade, and softening turns it into a lesson. ``correct()`` retired the
memory and wrote its replacement with a placeholder impact, and
``mnemos_correct`` had no way to pass one, so the meaning went with the memory
it replaced, and no lesson could ever come from the replacement.

Seen through MnemosRuntime: "...six weeks, starting in March." captured with
"Spring changes shape: the draft has to be whole before she leaves.", then
corrected to five weeks. The replacement meant "Corrected continuity for
future interactions."

A correction usually fixes a detail, not the meaning. So the replacement keeps
the meaning unless the agent gives a new one, and the result says what was
kept, so the agent can notice a meaning that no longer holds.
"""

from __future__ import annotations

import re
import sqlite3
import sys

import anyio
import pytest

from mnemos.simple_runtime import MnemosRuntime

ORIGINAL = "She got a writing residency in Lisbon: six weeks, starting in March."
CORRECTED = "She got a writing residency in Lisbon: five weeks, starting in March."
MEANING = "Spring changes shape: the draft has to be whole before she leaves."
NEW_MEANING = "Spring is still hers: the draft only has to be whole by April."
QUERY = "Lisbon residency"

PLACEHOLDER = {
    "by-memory-id": "Correction to earlier continuity.",
    "by-query": "Corrected continuity for future interactions.",
}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "correct.db")


def _runtime(db):
    return MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")


def _live(db, words):
    """(impact, impact_source) of the one live memory containing ``words``.

    Read on a fresh connection, not through the runtime that wrote it.
    """
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT impact, impact_source FROM engrams "
            "WHERE state != 'archived' AND content LIKE ?",
            (f"%{words}%",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    return rows[0]


def _correct(rt, path, captured, correction, **impact):
    """Correct the captured memory the way an agent would, by id or by query.

    ``impact`` is passed only when a test gives one, so the default is what
    gets exercised: an agent correcting a detail passes no impact at all.
    """
    if path == "by-memory-id":
        memory_id = re.search(r"Memory ID: (\S+)", captured).group(1)
        return rt.correct(correction=correction, target_id=memory_id, **impact)
    return rt.correct(correction=correction, query=QUERY, **impact)


@pytest.mark.parametrize("path", ["by-memory-id", "by-query"])
def test_a_correction_keeps_what_the_memory_meant(db, path):
    rt = _runtime(db)
    try:
        captured = rt.capture(content=ORIGINAL, impact=MEANING)
        result = _correct(rt, path, captured, CORRECTED)
    finally:
        rt.close()

    impact, source = _live(db, "five weeks")
    assert impact == MEANING, (
        "the replacement lost what the memory meant; it carries a "
        f"placeholder instead: {impact!r}"
    )
    assert source == "agent", source
    # The agent is told what was kept, so it can see a meaning that no
    # longer holds.
    assert f'Kept what it meant: "{MEANING}"' in result, result


@pytest.mark.parametrize("path", ["by-memory-id", "by-query", "fresh"])
def test_a_new_impact_is_what_the_correction_means(db, path):
    rt = _runtime(db)
    try:
        captured = rt.capture(content=ORIGINAL, impact=MEANING)
        if path == "fresh":
            # No target and no query: the correction is captured as new.
            result = rt.correct(correction=CORRECTED, impact=NEW_MEANING)
        else:
            result = _correct(rt, path, captured, CORRECTED, impact=NEW_MEANING)
    finally:
        rt.close()

    impact, source = _live(db, "five weeks")
    assert impact == NEW_MEANING, impact
    # Labelled exactly as capture labels an impact the agent wrote.
    assert source == "agent", source
    assert "Kept what it meant" not in result, result


def test_the_newest_meaning_is_the_one_kept(db):
    """A note corrected twice keeps what its latest memory meant.

    The note still points at the memory first captured, as well as at the
    latest replacement. Carrying from the first would quietly undo a meaning
    the agent gave in between.
    """
    rt = _runtime(db)
    try:
        rt.capture(content=ORIGINAL, impact=MEANING)
        rt.correct(correction=CORRECTED, query=QUERY, impact=NEW_MEANING)
        result = rt.correct(
            correction="She got a writing residency in Lisbon: four weeks, starting in March.",
            query=QUERY,
        )
    finally:
        rt.close()

    impact, source = _live(db, "four weeks")
    assert impact == NEW_MEANING, impact
    assert source == "agent", source
    assert f'Kept what it meant: "{NEW_MEANING}"' in result, result


@pytest.mark.parametrize(
    ("impact", "impact_source"),
    [
        ("Durable continuity captured from the session.", "template"),
        # A row from before provenance was recorded: the phrase is the test.
        ("Durable continuity captured from the session.", ""),
        ("", ""),
    ],
    ids=["template", "legacy-phrase", "none"],
)
@pytest.mark.parametrize("path", ["by-memory-id", "by-query"])
def test_a_placeholder_is_never_carried_as_meaning(db, path, impact, impact_source):
    rt = _runtime(db)
    try:
        captured = rt.capture(content=ORIGINAL, impact=impact, impact_source=impact_source)
        result = _correct(rt, path, captured, CORRECTED)
    finally:
        rt.close()

    kept, source = _live(db, "five weeks")
    # Nothing true to carry, so the replacement gets its placeholder, as
    # before, labelled as one.
    assert kept == PLACEHOLDER[path], kept
    assert source == "template", source
    assert "Kept what it meant" not in result, result


def test_an_impact_for_a_continuity_note_is_not_dropped_silently(db):
    """A note is revised in place and holds no impact, so say it was not saved."""
    rt = _runtime(db)
    try:
        captured = rt.capture(content=ORIGINAL, impact=MEANING)
        note_id = re.search(r"Continuity note ID: (\S+)", captured).group(1)
        result = rt.correct(correction=CORRECTED, target_id=note_id, impact=NEW_MEANING)
        without = rt.correct(correction=CORRECTED, target_id=note_id)
    finally:
        rt.close()

    assert result.startswith(f"Updated continuity note {note_id}."), result
    assert "The impact was not saved" in result, result
    assert without == f"Updated continuity note {note_id}.", without


@pytest.mark.parametrize("module", ["mnemos.simple_mcp:simple_mcp", "mnemos.mcp_server:mcp"])
def test_both_tool_surfaces_take_an_impact(module):
    import asyncio
    import importlib

    pytest.importorskip("mcp.server.fastmcp")
    name, attr = module.split(":")
    server = getattr(importlib.import_module(name), attr)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "impact" in tools["mnemos_correct"].inputSchema["properties"]


def test_the_correct_tool_passes_impact_through(tmp_path):
    """Over the real protocol: the server writes, this process reads back."""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = str(tmp_path / "stdio.db")

    def _text(result):
        return "\n".join(
            block.text for block in result.content
            if getattr(block, "type", None) == "text"
        )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "mnemos.cli", "serve", "--mode", "simple",
                "--db-path", db,
                "--agent-id", "meaning", "--person-id", "tester",
                "--project-scope", "stdio",
            ],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                captured = await session.call_tool(
                    "mnemos_capture", {"content": ORIGINAL, "impact": MEANING}
                )
                assert not captured.isError, _text(captured)

                kept = await session.call_tool(
                    "mnemos_correct", {"correction": CORRECTED, "query": QUERY}
                )
                assert not kept.isError, _text(kept)
                assert f'Kept what it meant: "{MEANING}"' in _text(kept), _text(kept)

                given = await session.call_tool(
                    "mnemos_correct",
                    {
                        "correction": "She got a writing residency in Lisbon: four weeks, starting in March.",
                        "query": QUERY,
                        "impact": NEW_MEANING,
                    },
                )
                assert not given.isError, _text(given)

    anyio.run(run)

    assert _live(db, "four weeks") == (NEW_MEANING, "agent")
