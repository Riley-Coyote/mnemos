"""Simple Mnemos MCP surface.

Simple mode exposes the real continuity architecture through a small,
agent-friendly tool set. Advanced/admin tools remain in ``mnemos.mcp_server``.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from mcp import types
from mcp.server.fastmcp import Context
from mcp.server.fastmcp import FastMCP

from .simple_runtime import MnemosRuntime, SIMPLE_TOOL_NAMES

logger = logging.getLogger("mnemos.simple_mcp")

simple_mcp = FastMCP("mnemos")

_runtime: MnemosRuntime | None = None
_runtime_kwargs: dict[str, Any] = {}


def _annotations(
    *,
    title: str,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def configure_runtime(
    *,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> None:
    """Configure the runtime used by simple tools."""

    global _runtime, _runtime_kwargs
    if _runtime is not None:
        _runtime.close()
    _runtime = None
    _runtime_kwargs = {
        "db_path": db_path,
        "agent_id": agent_id,
        "person_id": person_id,
        "project_scope": project_scope,
    }


def _get_runtime() -> MnemosRuntime:
    global _runtime
    if _runtime is None:
        _runtime = MnemosRuntime(**_runtime_kwargs)
    return _runtime


async def _sample_text(ctx: Context | None, prompt: str, *, max_tokens: int = 350) -> str:
    """Ask the host MCP client model for optional in-band assistance."""

    if ctx is None:
        return ""
    try:
        result = await ctx.session.create_message(
            messages=[
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt),
                )
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            system_prompt=(
                "You help Mnemos distill durable AI-agent continuity. "
                "Return concise plain text only. Do not invent facts."
            ),
            related_request_id=ctx.request_id,
        )
    except Exception as exc:
        try:
            await ctx.debug(f"Mnemos host-model sampling unavailable: {exc}")
        except Exception:
            pass
        return ""

    content = result.content
    if isinstance(content, list):
        text_parts = [part.text for part in content if getattr(part, "type", None) == "text"]
        return "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    if getattr(content, "type", None) == "text":
        return content.text.strip()
    return ""


def register_simple_tools(server: FastMCP, *, include_recall: bool = True) -> None:
    """Register the simple continuity tools on a FastMCP server."""

    @server.tool(
        annotations=_annotations(
            title="Get continuity context",
            read_only=False,
            idempotent=False,
        )
    )
    def mnemos_context(query: str = "", max_results: int = 5) -> str:
        """Get the startup continuity packet for this agent/session.

        Call at the beginning of a session. It auto-creates local storage on
        first run, runs lightweight maintenance, and returns relevant
        continuity without requiring setup.
        """

        return _get_runtime().context(query=query, max_results=max_results)

    @server.tool(
        annotations=_annotations(
            title="Capture continuity",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    async def mnemos_capture(
        content: str,
        context: str = "",
        importance: str | float = "auto",
        ctx: Context | None = None,
    ) -> str:
        """Capture durable continuity from the current conversation.

        Use for preferences, decisions, project state, corrections, workflows,
        and anything the agent should carry across sessions. Tags, memory type,
        scope, and maintenance are handled internally.
        """

        sampled = await _sample_text(
            ctx,
            (
                "Distill this into one durable continuity note for a future AI-agent session. "
                "Keep concrete names and preferences. Return an empty string if the original is already optimal.\n\n"
                f"Content:\n{content}\n\nContext:\n{context}"
            ),
        )
        capture_content = sampled or content
        capture_context = context
        if sampled:
            capture_context = (context + "\n\n" if context else "") + f"Original capture: {content}"

        result = _get_runtime().capture(
            content=capture_content,
            context=capture_context,
            importance=importance,
        )
        if sampled:
            result += "\nHost model assistance: applied via MCP sampling."
        return result

    if include_recall:
        @server.tool(
            annotations=_annotations(
                title="Recall continuity",
                read_only=False,
                destructive=False,
                idempotent=False,
            )
        )
        def mnemos_recall(query: str, max_results: int = 5) -> str:
            """Recall relevant continuity and durable memories."""

            return _get_runtime().recall(query=query, max_results=max_results)

    @server.tool(
        annotations=_annotations(
            title="Correct continuity",
            read_only=False,
            destructive=True,
            idempotent=False,
        )
    )
    def mnemos_correct(
        correction: str,
        target_id: str = "",
        query: str = "",
        action: str = "update",
    ) -> str:
        """Correct, supersede, or archive stale continuity.

        If target_id is omitted, Mnemos captures the correction as fresh
        high-confidence continuity. Set action to forget/archive/remove/delete
        to archive a target or closest query match.
        """

        return _get_runtime().correct(
            correction=correction,
            target_id=target_id,
            query=query,
            action=action,
        )

    @server.tool(
        annotations=_annotations(
            title="Maintain continuity",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    async def mnemos_maintain(deep: bool = False, ctx: Context | None = None) -> str:
        """Run the best available maintenance without additional setup.

        Baseline maintenance is local and deterministic. If a dedicated model
        is configured, deep maintenance can also run model-mediated passes.
        """

        runtime = _get_runtime()
        result = runtime.maintain(deep=deep)
        if deep and not runtime.has_dedicated_model:
            sampled = await _sample_text(
                ctx,
                (
                    "Mnemos just ran local maintenance without a dedicated provider. "
                    "Write one brief maintenance reflection that could help future continuity. "
                    "If there is nothing useful to add, return an empty string.\n\n"
                    f"Maintenance result:\n{result}"
                ),
                max_tokens=220,
            )
            if sampled:
                runtime.capture(
                    f"Maintenance reflection: {sampled}",
                    context="Generated by the host MCP client model during mnemos_maintain.",
                    importance="low",
                )
                result += "\nHost model assistance: captured maintenance reflection via MCP sampling."
        return result


register_simple_tools(simple_mcp)


def run_simple_server(
    *,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> None:
    """Start the simple MCP server in stdio mode."""

    configure_runtime(
        db_path=db_path,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down Mnemos simple MCP server...")
        if _runtime is not None:
            _runtime.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Mnemos simple MCP server starting with tools: %s", ", ".join(SIMPLE_TOOL_NAMES))
    simple_mcp.run()
