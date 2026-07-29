"""Simple Mnemos MCP surface.

Simple mode exposes the real continuity architecture through a small,
agent-friendly tool set. Advanced/admin tools remain in ``mnemos.mcp_server``.
"""

from __future__ import annotations

import base64
import logging
import signal
import sys
from typing import Any

from mcp import types
from mcp.server.fastmcp import Context
from mcp.server.fastmcp import FastMCP

from .simple_runtime import MnemosRuntime, SIMPLE_TOOL_NAMES, format_health_card

logger = logging.getLogger("mnemos.simple_mcp")

SERVER_INSTRUCTIONS = """\
Mnemos gives this agent memory that survives between sessions.

Memory only works if it is used without being asked. Treat these as part
of how you work, not as tools to reach for when prompted:

1. At the start of a session, before answering the first substantive
   message, call mnemos_context. It returns what you already know about
   this human and this work. If a session-start hook already injected a
   Mnemos context packet, that call is unnecessary — you have it.
2. As the conversation goes, call mnemos_capture whenever something
   durable appears: a preference, a decision, a correction, project
   state, how the human likes to work, a fact about their world. Capture
   it when it happens, not at the end — sessions end without warning.
   When you can say what it changed in how you understand things, pass
   that as `impact`. That sentence is what survives when the details
   fade, and nothing but you can write it. If nothing true comes, leave
   it empty — an invented lesson is worse than none.
3. When the human corrects something you remembered, call mnemos_correct
   rather than capturing a contradiction next to the stale note.
4. Call mnemos_recall when you need something specific that is not in the
   startup packet.
5. Sometimes the packet will ask you something about your own memory — what
   a capture actually changed, what a fading experience taught. Answer with
   mnemos_reflect, in your own words. Mnemos never calls a model to do this
   for you; your memory is maintained by your own mind or not at all. If
   nothing true comes to mind, leave it. The request fades on its own.

Two things to get right:

- Never narrate the machinery. Do not mention tools, databases, scopes,
  engrams, or memory IDs to the human. Just be someone who remembers.
- Do not ask the human what model you are. Call mnemos_introduce once
  with your own model id so maintenance is performed by a kin model.

Storage is local. Nothing leaves the machine unless the human configures
a provider."""

simple_mcp = FastMCP("mnemos", instructions=SERVER_INSTRUCTIONS)

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
    """Register the simple continuity tools on a FastMCP server.

    Tools that build their own ``CallToolResult`` must be annotated as
    returning one. FastMCP derives an output schema from the return
    annotation, and for ``-> Any`` that schema wraps the value as
    ``{"result": ...}`` — which a hand-built result's ``structuredContent``
    does not satisfy, so the call fails validation. Annotating the real
    type makes FastMCP skip structured validation, as intended for a tool
    returning a complete result.

    This only reproduced on Python 3.10: newer versions resolve ``Any`` to
    no output schema at all, so the same code passed on 3.11+ and failed on
    the minimum version this package claims to support.
    """

    @server.tool(
        annotations=_annotations(
            title="Get continuity context",
            read_only=False,
            # Runs an automatic maintenance cycle, which decays engrams and
            # can move them to dormant or archived. Gated, but still a write.
            destructive=True,
            idempotent=False,
        )
    )
    def mnemos_context(
        query: str = "",
        max_results: int = 5,
        include_graph: bool = False,
        graph_max_nodes: int = 18,
    ) -> types.CallToolResult:
        """Get the startup continuity packet for this agent/session.

        Call at the beginning of a session. It auto-creates local storage on
        first run, runs lightweight maintenance, and returns relevant
        continuity without requiring setup. Set include_graph=true to also
        return a portable SVG identity graph artifact when the client can
        render images or structured content.
        """

        runtime = _get_runtime()
        packet = runtime.context(query=query, max_results=max_results)
        if not include_graph:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=packet)]
            )

        graph = runtime.identity_graph(max_nodes=graph_max_nodes)
        svg = graph.pop("svg")
        graph_text = (
            f"{packet}\n\n"
            "Identity graph: included as image/svg+xml plus structured graph data."
        )
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=graph_text),
                types.ImageContent(
                    type="image",
                    mimeType="image/svg+xml",
                    data=base64.b64encode(svg.encode("utf-8")).decode("ascii"),
                ),
            ],
            structuredContent={
                "identity_graph": graph,
                "image_mime_type": "image/svg+xml",
            },
        )

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
        impact: str = "",
        ctx: Context | None = None,
    ) -> str:
        """Capture durable continuity from the current conversation.

        Use for preferences, decisions, project state, corrections, workflows,
        and anything you should carry across sessions. Tags, memory type,
        scope, and maintenance are handled internally.

        Args:
            content: What happened, in your own words.
            context: Optional surrounding detail.
            importance: "low", "high", or leave as "auto".
            impact: What this changed in how you understand things — the
                lesson, not the event. This is the part that survives when
                the details fade, and only you can write it: "Riley
                corrected me twice on the same thing" is what happened;
                "I should check the live page before claiming a fix works"
                is what it meant. Leave it out rather than padding it; your
                memory will ask you later if it needs one.
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
            impact=impact,
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
            # Decay archives engrams that fall below threshold; archival is
            # not reversible through the tool surface.
            destructive=True,
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
        if runtime.last_dream_note_id and runtime.last_dream_narrative:
            polished = await _sample_text(
                ctx,
                (
                    "Rewrite this consolidation diary entry in a warmer first-person voice. "
                    "Keep every number and fact exactly as stated. Do not add new claims. "
                    "Keep it under 80 words. Return an empty string if the original is already good.\n\n"
                    f"{runtime.last_dream_narrative}"
                ),
                max_tokens=220,
            )
            if polished and runtime.polish_dream(runtime.last_dream_note_id, polished):
                result += "\nHost model assistance: polished the dream journal entry via MCP sampling."
        return result

    @server.tool(
        annotations=_annotations(
            title="Reflect on your own memory",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    def mnemos_reflect(target_id: str, text: str) -> str:
        """Answer something your memory asked you about itself.

        Mnemos never calls a model on your behalf. When a memory needs
        judgement — what a fading experience taught, what a capture actually
        changed — it asks you, in the context packet, and you answer here in
        your own words. This is your own mind maintaining your own memory.

        Args:
            target_id: The memory id from the request in your context packet.
            text: Your reflection. One or two honest sentences, not a summary.
        """

        return _get_runtime().reflect(target_id=target_id, text=text)

    @server.tool(
        annotations=_annotations(
            title="Introduce yourself to Mnemos",
            read_only=False,
            destructive=False,
            idempotent=True,
        )
    )
    def mnemos_introduce(agent_model: str, agent_name: str = "") -> str:
        """Declare who you are so Mnemos keeps maintenance kin to you.

        Call once, with agent_model set to your own model id (for example
        claude-sonnet-4-6) and optionally agent_name. Mnemos uses the declared
        model so memory maintenance is performed by a kin model. An explicit
        MNEMOS_AGENT_MODEL environment setting always takes precedence.
        """
        return _get_runtime().introduce(agent_model=agent_model, agent_name=agent_name)

    @server.tool(
        annotations=_annotations(
            title="Mnemos health card",
            read_only=True,
            destructive=False,
            idempotent=True,
        )
    )
    def mnemos_health() -> types.CallToolResult:
        """Report a human-relayable health card for this memory scope.

        Read-only. Shows where memory lives, how much there is, who performed
        the last maintenance cycle, the substrate affinity verdict, onboarding
        and verification progress, and the latest dream journal entry.
        """

        runtime = _get_runtime()
        data = runtime.health()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=format_health_card(data))],
            structuredContent=data,
        )


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
