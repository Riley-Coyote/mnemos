"""The seam between the two tool surfaces: what is written must be readable.

Mnemos exposes continuity through two families of tools. The simple tools
(``mnemos_capture``) resolved scope through ``resolve_scope``; the advanced
tools (``mnemos_context_packet``) took the literal parameter defaults
``default``/``user``/``global``. They disagreed, so capture wrote into one
partition and the startup packet read from another — and both reported
success.

Nothing caught it, because every existing scope test passes the *same*
explicit scope to the writer and the reader. These tests deliberately pass
no scope at all, which is exactly how an agent calls these tools.
"""

import json
import os
from pathlib import Path

import pytest


def _seed_home(tmp_path, config=None):
    """A throwaway HOME so config.json and ~/.mnemos never touch the developer's."""
    home = tmp_path / "home"
    mnemos_dir = home / ".mnemos"
    mnemos_dir.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (mnemos_dir / "config.json").write_text(json.dumps(config))
    return home


def test_project_scope_does_not_follow_the_working_directory(tmp_path, monkeypatch):
    """Scope is identity, not launch location.

    An MCP server's cwd is chosen by whichever client spawned it. When the
    scope was derived from it, the same agent got a different memory
    partition depending on where the process happened to start.
    """
    from mnemos.simple_scope import resolve_scope

    monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))

    workdir_a = tmp_path / "project-alpha"
    workdir_b = tmp_path / "project-beta"
    workdir_a.mkdir()
    workdir_b.mkdir()

    monkeypatch.chdir(workdir_a)
    from_a = resolve_scope(agent_id="claude-code")
    monkeypatch.chdir(workdir_b)
    from_b = resolve_scope(agent_id="claude-code")

    assert from_a.project_scope == from_b.project_scope == "global"
    assert from_a.db_path == from_b.db_path


def test_explicit_project_scope_still_wins(tmp_path, monkeypatch):
    """Killing the cwd default must not disable deliberate partitioning."""
    from mnemos.simple_scope import resolve_scope

    monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))

    assert resolve_scope(project_scope="sanctuary").project_scope == "sanctuary"

    monkeypatch.setenv("MNEMOS_PROJECT_SCOPE", "vektor")
    assert resolve_scope().project_scope == "vektor"


def test_legacy_tool_defaults_resolve_to_the_configured_scope(tmp_path, monkeypatch):
    """``default``/``user``/``global`` are "unspecified", not real scopes.

    They are what a tool signature says when the caller did not choose, so
    they must resolve to the same place the simple tools write.
    """
    from mnemos.simple_scope import resolve_tool_scope

    monkeypatch.setenv("HOME", str(_seed_home(tmp_path, {
        "agent_id": "nova",
        "person_id": "alex",
        "project_scope": "sanctuary",
    })))

    resolved = resolve_tool_scope("default", "user", "global")
    assert (resolved.agent_id, resolved.person_id, resolved.project_scope) == (
        "nova", "alex", "sanctuary",
    )

    deliberate = resolve_tool_scope("nova", "alex", "other-project")
    assert deliberate.project_scope == "other-project"


def test_capture_then_context_packet_round_trips_with_no_explicit_scope(
    tmp_path, monkeypatch
):
    """The regression test for the whole bug.

    Capture through the simple surface, then read through the advanced
    surface, passing no scope to either — the way an agent actually calls
    them. Before the fix this returned "No scoped continuity entries
    matched" while the note sat in the store under a different partition.
    """
    monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))

    from mnemos.interface.context_packet import build_context_packet
    from mnemos.simple_runtime import MnemosRuntime
    from mnemos.simple_scope import resolve_tool_scope
    from mnemos.store.sqlite_store import EngramStore

    db_path = str(tmp_path / "roundtrip.db")
    secret = "Riley takes his coffee as cold brew, no sugar"

    runtime = MnemosRuntime(db_path=db_path, agent_id="claude-code")
    runtime.capture(secret, importance="high")
    runtime.close()

    # The reader resolves the same way a tool call does, with nothing given.
    scope = resolve_tool_scope("claude-code", "", "")
    store = EngramStore(db_path)
    try:
        packet = build_context_packet(
            store,
            "what should I know about Riley?",
            agent_id=scope.agent_id,
            person_id=scope.person_id,
            project_scope=scope.project_scope,
        )
    finally:
        store.close()

    contents = [entry["content"] for entry in packet["hypomnema"]]
    assert any(secret in c for c in contents), (
        "continuity captured through the simple surface was invisible to the "
        f"advanced context packet; packet scope was {packet['scope']}"
    )


def test_context_packet_tool_reads_what_the_capture_tool_wrote(tmp_path, monkeypatch):
    """Same round trip, but through the registered MCP tool functions."""
    monkeypatch.setenv("HOME", str(_seed_home(tmp_path, {"setup_complete": True})))
    db_path = str(tmp_path / "tools.db")
    monkeypatch.setenv("MNEMOS_DB_PATH", db_path)
    monkeypatch.setenv("MNEMOS_AGENT_ID", "claude-code")

    import mnemos.mcp_server as server
    import mnemos.simple_mcp as simple

    # Rebuild module state so it picks up this test's HOME and db path.
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_default_agent_id", "claude-code")
    simple.configure_runtime(db_path=db_path, agent_id="claude-code")

    secret = "The deploy target is always staging before production"
    simple._get_runtime().capture(secret, importance="high")

    server._init_store(db_path)
    packet = server.mnemos_context_packet(query="deploy target")

    assert secret in packet, (
        "mnemos_context_packet did not surface what mnemos_capture wrote:\n"
        f"{packet}"
    )
