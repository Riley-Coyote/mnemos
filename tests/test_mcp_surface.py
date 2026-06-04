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


def test_simple_tools_have_protocol_risk_annotations():
    from mnemos.simple_mcp import simple_mcp

    tools = _tools_by_name(simple_mcp)

    assert tools["mnemos_context"].annotations.openWorldHint is False
    assert tools["mnemos_context"].annotations.readOnlyHint is False
    assert tools["mnemos_recall"].annotations.readOnlyHint is False
    assert tools["mnemos_capture"].annotations.destructiveHint is False
    assert tools["mnemos_correct"].annotations.destructiveHint is True
    assert tools["mnemos_maintain"].annotations.destructiveHint is False


def test_simple_tool_schemas_do_not_expose_injected_context():
    from mnemos.simple_mcp import simple_mcp

    tools = _tools_by_name(simple_mcp)

    assert "ctx" not in tools["mnemos_capture"].inputSchema.get("properties", {})
    assert "ctx" not in tools["mnemos_maintain"].inputSchema.get("properties", {})


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

    anyio.run(run_smoke)
