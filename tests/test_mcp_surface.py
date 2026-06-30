"""Tests for simple and advanced MCP tool surfaces."""

import asyncio
import sys

import anyio
import pytest

from mnemos.simple_runtime import SIMPLE_TOOL_NAMES


pytest.importorskip("mcp.server.fastmcp")


def _tool_names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


def _tools_by_name(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_simple_mcp_exposes_only_simple_tools():
    from mnemos.simple_mcp import simple_mcp

    assert _tool_names(simple_mcp) == set(SIMPLE_TOOL_NAMES)


def test_advanced_mcp_preserves_admin_tools_and_includes_simple_tools():
    from mnemos.mcp_server import mcp

    names = _tool_names(mcp)

    assert set(SIMPLE_TOOL_NAMES).issubset(names)
    assert "mnemos_remember" in names
    assert "mnemos_hypomnema_promote" in names
    assert "mnemos_consolidate" in names


def test_advanced_context_packet_schema_exposes_packet_mode():
    from mnemos.mcp_server import mcp

    schema = _tools_by_name(mcp)["mnemos_context_packet"].inputSchema

    assert "packet_mode" in schema.get("properties", {})


def test_simple_tools_have_protocol_risk_annotations():
    from mnemos.simple_mcp import simple_mcp

    tools = _tools_by_name(simple_mcp)

    assert tools["mnemos_context"].annotations.openWorldHint is False
    assert tools["mnemos_context"].annotations.readOnlyHint is False
    assert tools["mnemos_recall"].annotations.readOnlyHint is False
    assert tools["mnemos_capture"].annotations.destructiveHint is False
    assert tools["mnemos_correct"].annotations.destructiveHint is True
    assert tools["mnemos_maintain"].annotations.destructiveHint is False
    assert tools["mnemos_introduce"].annotations.readOnlyHint is False
    assert tools["mnemos_introduce"].annotations.destructiveHint is False
    assert tools["mnemos_introduce"].annotations.idempotentHint is True
    assert tools["mnemos_health"].annotations.readOnlyHint is True
    assert tools["mnemos_health"].annotations.destructiveHint is False
    assert tools["mnemos_health"].annotations.idempotentHint is True


def test_simple_tool_schemas_do_not_expose_injected_context():
    from mnemos.simple_mcp import simple_mcp

    tools = _tools_by_name(simple_mcp)

    assert "ctx" not in tools["mnemos_capture"].inputSchema.get("properties", {})
    assert "ctx" not in tools["mnemos_maintain"].inputSchema.get("properties", {})
    assert "include_graph" in tools["mnemos_context"].inputSchema.get("properties", {})
    assert "graph_max_nodes" in tools["mnemos_context"].inputSchema.get("properties", {})


def test_simple_capture_accepts_numeric_or_string_importance():
    from mnemos.simple_mcp import simple_mcp

    schema = _tools_by_name(simple_mcp)["mnemos_capture"].inputSchema
    importance_schema = schema["properties"]["importance"]

    assert "anyOf" in importance_schema
    assert {entry["type"] for entry in importance_schema["anyOf"]} >= {"number", "string"}


def test_hypomnema_candidates_tool_excludes_non_operational_prose(
    monkeypatch,
    store,
):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    store.write_hypomnema_entry(
        "Operational MCP candidate can be listed.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.9,
        salience=0.8,
        foundational=True,
        read_visibility="operational_context",
    )
    store.write_hypomnema_entry(
        "Review-only MCP candidate must stay out of raw listing.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        read_visibility="review_only",
    )
    store.write_hypomnema_entry(
        "Audit-only MCP candidate must stay out of raw listing.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.99,
        salience=0.95,
        foundational=True,
        read_visibility="audit_only",
    )

    output = mcp_server.mnemos_hypomnema_candidates(
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Operational MCP candidate can be listed." in output
    assert "Review-only MCP candidate must stay out" not in output
    assert "Audit-only MCP candidate must stay out" not in output


def test_hypomnema_promote_rejects_non_operational_entries(monkeypatch, store):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    review_id = store.write_hypomnema_entry(
        "Review-only promotion prose must not be disclosed.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        read_visibility="review_only",
    )
    audit_id = store.write_hypomnema_entry(
        "Audit-only promotion prose must never be disclosed.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.99,
        salience=0.95,
        foundational=True,
        read_visibility="audit_only",
    )

    review_dry_run = mcp_server.mnemos_hypomnema_promote(
        review_id,
        dry_run=True,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )
    audit_promote = mcp_server.mnemos_hypomnema_promote(
        audit_id,
        dry_run=False,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    review_entry = store.get_hypomnema_entry(
        review_id,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )
    audit_entry = store.get_hypomnema_entry(
        audit_id,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Active operational hypomnema entry not found" in review_dry_run
    assert "Active operational hypomnema entry not found" in audit_promote
    assert "Review-only promotion prose" not in review_dry_run
    assert "Audit-only promotion prose" not in audit_promote
    assert review_entry["graduated_to_engram_id"] is None
    assert audit_entry["graduated_to_engram_id"] is None


def test_hypomnema_revise_and_supersede_reject_non_operational_entries(
    monkeypatch,
    store,
):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    review_id = store.write_hypomnema_entry(
        "Review-only hypomnema prose must not be revised by MCP.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.75,
        salience=0.7,
        read_visibility="review_only",
    )
    audit_id = store.write_hypomnema_entry(
        "Audit-only hypomnema prose must not be superseded by MCP.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.8,
        salience=0.75,
        read_visibility="audit_only",
    )

    review_revise = mcp_server.mnemos_hypomnema_revise(
        review_id,
        "Leaked review-only revision content.",
        "ordinary MCP revise",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )
    audit_supersede = mcp_server.mnemos_hypomnema_supersede(
        audit_id,
        "Leaked audit-only replacement content.",
        "ordinary MCP supersede",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    review_entry = store.get_hypomnema_entry(
        review_id,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )
    audit_entry = store.get_hypomnema_entry(
        audit_id,
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )
    leaked_replacements = store.search_hypomnema(
        "Leaked",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        read_visibility=None,
    )

    assert "Hypomnema revision failed" in review_revise
    assert "Hypomnema supersession failed" in audit_supersede
    assert "Review-only hypomnema prose" not in review_revise
    assert "Audit-only hypomnema prose" not in audit_supersede
    assert review_entry["content"] == "Review-only hypomnema prose must not be revised by MCP."
    assert review_entry["revision_count"] == 0
    assert audit_entry["content"] == "Audit-only hypomnema prose must not be superseded by MCP."
    assert audit_entry["active"] is True
    assert audit_entry["superseded_by"] is None
    assert all("Leaked" not in entry["content"] for entry in leaked_replacements)


def test_inspect_and_forget_reject_non_operational_engrams(monkeypatch, store):
    from mnemos import mcp_server
    from mnemos.core.engram import Engram

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    review = Engram(
        content="Review-only engram prose must not be disclosed by inspect.",
        read_visibility="review_only",
    )
    audit = Engram(
        content="Audit-only engram prose must not be archived or disclosed.",
        read_visibility="audit_only",
    )
    store.save_engram(review)
    store.save_engram(audit)

    review_inspect = mcp_server.mnemos_inspect(review.id)
    audit_inspect = mcp_server.mnemos_inspect(audit.id)
    review_forget = mcp_server.mnemos_forget(review.id)
    audit_forget = mcp_server.mnemos_forget(audit.id)

    loaded_review = store.get_engram(review.id)
    loaded_audit = store.get_engram(audit.id)

    assert "Memory not found" in review_inspect
    assert "Memory not found" in audit_inspect
    assert "Memory not found" in review_forget
    assert "Memory not found" in audit_forget
    assert "Review-only engram prose" not in review_inspect
    assert "Audit-only engram prose" not in audit_inspect
    assert "Review-only engram prose" not in review_forget
    assert "Audit-only engram prose" not in audit_forget
    assert loaded_review is not None
    assert loaded_audit is not None
    assert loaded_review.state == "active"
    assert loaded_audit.state == "active"


def test_review_queue_opts_into_review_candidate_prose(monkeypatch, store):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    store.write_hypomnema_entry(
        "Review queue may disclose review-only candidate prose.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.95,
        salience=0.9,
        foundational=True,
        read_visibility="review_only",
    )
    store.write_hypomnema_entry(
        "Review queue must not disclose audit-only candidate prose.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        confidence=0.99,
        salience=0.95,
        foundational=True,
        read_visibility="audit_only",
    )

    output = mcp_server.mnemos_review_queue(
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert "Review queue may disclose review-only candidate prose." in output
    assert "Review queue must not disclose audit-only" not in output


def test_functional_review_tools_exclude_audit_only_confirmation_prose(
    monkeypatch,
    store,
):
    from mnemos import mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_ensure_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_setup_gate", lambda: None)
    store.write_functional_memory(
        "Functional tools may disclose review-only confirmation prose.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        read_visibility="review_only",
    )
    store.write_functional_memory(
        "Functional tools must not disclose audit-only confirmation prose.",
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        memory_type="open_question",
        needs_confirmation=True,
        read_visibility="audit_only",
    )

    list_output = mcp_server.mnemos_functional_list(
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
        needs_confirmation_only=True,
    )
    queue_output = mcp_server.mnemos_review_queue(
        agent_id="vektor",
        person_id="riley",
        project_scope="mnemos",
    )

    assert (
        "Functional tools may disclose review-only confirmation prose." in list_output
    )
    assert (
        "Functional tools may disclose review-only confirmation prose." in queue_output
    )
    assert "Functional tools must not disclose audit-only" not in list_output
    assert "Functional tools must not disclose audit-only" not in queue_output


def test_simple_stdio_server_lists_and_calls_context(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run_smoke():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mnemos.cli",
                "serve",
                "--mode",
                "simple",
                "--db-path",
                str(tmp_path / "stdio.db"),
                "--agent-id",
                "smoke",
                "--person-id",
                "tester",
                "--project-scope",
                "stdio",
            ],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == set(SIMPLE_TOOL_NAMES)

                result = await session.call_tool("mnemos_context", {})
                text = "\n".join(
                    block.text for block in result.content
                    if getattr(block, "type", None) == "text"
                )
                assert "Mnemos continuity packet" in text
                assert "agent=smoke" in text

                introduced = await session.call_tool(
                    "mnemos_introduce", {"agent_model": "claude-sonnet-4-6"}
                )
                introduced_text = "\n".join(
                    block.text for block in introduced.content
                    if getattr(block, "type", None) == "text"
                )
                assert "Introduction recorded." in introduced_text

                health = await session.call_tool("mnemos_health", {})
                assert not health.isError
                assert health.structuredContent is not None
                assert health.structuredContent["scope"]["agent_id"] == "smoke"
                health_text = "\n".join(
                    block.text for block in health.content
                    if getattr(block, "type", None) == "text"
                )
                assert "Mnemos health card" in health_text

    anyio.run(run_smoke)


def test_simple_stdio_context_can_return_identity_graph(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run_smoke():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mnemos.cli",
                "serve",
                "--mode",
                "simple",
                "--db-path",
                str(tmp_path / "graph.db"),
                "--agent-id",
                "graph-smoke",
                "--person-id",
                "tester",
                "--project-scope",
                "stdio",
            ],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await session.call_tool(
                    "mnemos_capture",
                    {
                        "content": "Graph smoke wants an optional identity graph artifact.",
                        "importance": 0.9,
                    },
                )
                result = await session.call_tool(
                    "mnemos_context",
                    {"include_graph": True, "graph_max_nodes": 10},
                )

                assert not result.isError
                assert [block.type for block in result.content] == ["text", "image"]
                assert result.content[1].mimeType == "image/svg+xml"
                assert result.structuredContent is not None
                graph = result.structuredContent["identity_graph"]
                assert graph["scope"]["agent_id"] == "graph-smoke"
                assert graph["nodes"]
                assert graph["edges"]

    anyio.run(run_smoke)


def test_advanced_tools_inherit_server_scope(monkeypatch):
    """Advanced tools must inherit the server's configured person_id/project_scope,
    not just agent_id.

    Regression: run_server persisted only _default_agent_id, so the CLI's
    --person-id/--project-scope were dropped. Default-arg scoped reads/writes then
    silently queried (user, global) and missed data stored under the configured
    scope — e.g. 0 of 1245 hypomnema at (oliver, david, pai).
    """
    import mnemos.mcp_server as srv

    # Hermetic: no config-file or env influence.
    monkeypatch.setattr(srv, "_config", {})
    monkeypatch.setattr(srv, "_default_agent_id", "default")
    monkeypatch.setattr(srv, "_default_person_id", "user")
    monkeypatch.setattr(srv, "_default_project_scope", "global")
    for var in ("MNEMOS_AGENT_ID", "MNEMOS_PERSON_ID", "MNEMOS_PROJECT_SCOPE"):
        monkeypatch.delenv(var, raising=False)

    # Before any server config, sentinel defaults resolve to the bare defaults.
    assert srv._effective_scope() == ("default", "user", "global")

    # Server launches configured with a specific scope (the run_server path).
    srv._set_server_defaults(agent_id="oliver", person_id="david", project_scope="pai")

    # The fix: default-arg tool calls now inherit ALL THREE dimensions.
    assert srv._effective_scope() == ("oliver", "david", "pai")
    assert srv._effective_person_id() == "david"
    assert srv._effective_project_scope() == "pai"

    # Explicit non-default overrides are still respected.
    assert srv._effective_scope("a", "b", "c") == ("a", "b", "c")
