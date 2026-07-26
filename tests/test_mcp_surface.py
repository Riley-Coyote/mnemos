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


def test_result_building_tools_declare_no_output_schema():
    """Tools that build their own CallToolResult must not declare one.

    FastMCP derives an output schema from the return annotation and then
    validates structuredContent against it. For `-> Any` that schema wraps
    the value as {"result": ...}, which a hand-built result never matches,
    so every call fails validation.

    The annotation is checked rather than the derived schema, because the
    derived schema is exactly what varies: Python 3.10 built a wrapper
    schema for `-> Any` while 3.11+ built none, so the bug passed CI on
    three versions and broke only on the minimum supported one. Asserting
    the annotation catches a regression on every version.
    """
    import typing

    from mcp import types as mcp_types

    from mnemos.simple_mcp import simple_mcp

    for name in ("mnemos_context", "mnemos_health"):
        tool = simple_mcp._tool_manager.get_tool(name)
        annotation = typing.get_type_hints(tool.fn).get("return")
        assert annotation is mcp_types.CallToolResult, (
            f"{name} builds its own CallToolResult, so it must be annotated "
            f"`-> types.CallToolResult`; FastMCP otherwise derives an output "
            f"schema wrapping the value as {{'result': ...}} and every call "
            f"fails structured validation. Found: {annotation!r}"
        )
        assert tool.fn_metadata.output_schema is None


def test_simple_tools_have_protocol_risk_annotations():
    from mnemos.simple_mcp import simple_mcp

    tools = _tools_by_name(simple_mcp)

    assert tools["mnemos_context"].annotations.openWorldHint is False
    assert tools["mnemos_context"].annotations.readOnlyHint is False
    # context() and maintain() both run a consolidation cycle, and decay
    # archives engrams that fall below threshold. Archival is not reversible
    # through the tool surface, so both must declare themselves destructive.
    assert tools["mnemos_context"].annotations.destructiveHint is True
    assert tools["mnemos_maintain"].annotations.destructiveHint is True
    assert tools["mnemos_recall"].annotations.readOnlyHint is False
    assert tools["mnemos_capture"].annotations.destructiveHint is False
    assert tools["mnemos_correct"].annotations.destructiveHint is True
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
