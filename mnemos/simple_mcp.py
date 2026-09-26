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
from mcp.server.fastmcp import FastMCP

from .simple_runtime import MnemosRuntime, SIMPLE_TOOL_NAMES, format_health_card

logger = logging.getLogger("mnemos.simple_mcp")

MAX_CAPTURE_CHARS = 65_536
MAX_HANDOFF_CHARS = 16_384
MAX_QUERY_CHARS = 4_096
MAX_CONTEXT_CHARS = 32_768
MAX_REFLECTION_CHARS = 16_384
MAX_ID_CHARS = 256
MAX_RESULTS = 20
MAX_TOOL_OUTPUT_CHARS = 131_072


def _text(name: str, value: str, limit: int, *, required: bool = False) -> str:
    if required and not value.strip():
        raise ValueError(f"{name} cannot be empty")
    if len(value) > limit:
        raise ValueError(f"{name} is too large (maximum {limit:,} characters)")
    return value


def _count(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _output(value: str) -> str:
    if len(value) <= MAX_TOOL_OUTPUT_CHARS:
        return value
    return value[:MAX_TOOL_OUTPUT_CHARS] + "\n\n[Output limited by Mnemos.]"

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
6. Quietly refresh mnemos_handoff after meaningful progress or a changed
   plan, whenever unresolved work remains, and before pausing, ending,
   delegating, or changing context. Write it in your own words: current
   state, changed understanding, open threads, and the next useful action.
   Do not call it after every ordinary turn. It is a note to whoever works
   here next, which may be a different model, not a summary for the human.
   Each session keeps its own handoff, so write about your own work; notes
   other sessions leave stay beside yours.

Two things to get right:

- Never narrate the machinery. Do not mention tools, databases, scopes,
  engrams, or memory IDs to the human. Just be someone who remembers.
- Several models may share this memory, so every note is signed by the
  model that wrote it. A note signed by a different model is a colleague's,
  not yours: use it, but do not claim its work or speak as if you did it.
  If a write comes back unsigned, call mnemos_introduce with your exact
  model id. Never ask the human what model you are.

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
        packet = _output(runtime.context(
            query=_text("query", query, MAX_QUERY_CHARS),
            max_results=_count("max_results", max_results, minimum=1, maximum=MAX_RESULTS),
        ))
        if not include_graph:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=packet)]
            )

        graph = runtime.identity_graph(
            max_nodes=_count("graph_max_nodes", graph_max_nodes, minimum=4, maximum=48)
        )
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
            title="Leave a session handoff",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    def mnemos_handoff(text: str) -> str:
        """Leave a signed note for whoever works here next, in your own words.

        Use after meaningful progress or a changed plan, while unresolved
        work remains, and before pausing, ending, delegating, or changing
        context. Include the current state, what you now understand, open
        threads, and the next useful action when those matter. Keep it
        freeform. Do not write one after every ordinary turn.

        The next session may be a different model. The note is signed with
        your model id, so it can tell your note from its own memory.

        The text is stored exactly as supplied. Each session keeps its own
        handoff: a new one replaces only the note this session left before,
        preserving it in history, and notes other sessions left stay beside
        it, so write about your own work, not theirs. Mnemos never
        summarizes, rewrites, promotes, decays, or expires it.
        """

        return _output(_get_runtime().handoff(
            _text("text", text, MAX_HANDOFF_CHARS, required=True)
        ))

    @server.tool(
        annotations=_annotations(
            title="Capture continuity",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    def mnemos_capture(
        content: str,
        context: str = "",
        importance: str | float = "auto",
        impact: str = "",
    ) -> str:
        """Capture durable continuity from the current conversation.

        Use for preferences, decisions, project state, corrections, workflows,
        and anything you should carry across sessions. Tags, memory type,
        scope, and maintenance are handled internally. The note is signed
        with your model id.

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

        return _output(_get_runtime().capture(
            content=_text("content", content, MAX_CAPTURE_CHARS, required=True),
            context=_text("context", context, MAX_CONTEXT_CHARS),
            importance=importance,
            impact=_text("impact", impact, MAX_REFLECTION_CHARS),
        ))

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
            """Recall relevant continuity and durable memories.

            Pass a handoff's id as the query to read that note whole.
            """

            return _output(_get_runtime().recall(
                query=_text("query", query, MAX_QUERY_CHARS, required=True),
                max_results=_count("max_results", max_results, minimum=1, maximum=MAX_RESULTS),
            ))

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
        impact: str = "",
    ) -> str:
        """Correct, supersede, or archive stale continuity.

        Name what to change by target_id, or by a query: the note or memory
        must hold the query's meaningful words (half of them, and at least
        two), or nothing is changed. Set action to forget/archive/remove/delete
        to archive it. A correction that names nothing is captured as fresh
        high-confidence continuity.

        Args:
            impact: What the corrected memory means now, in your own words.
                Leave it empty to keep what the memory it replaces meant: a
                correction usually fixes a detail, not the meaning. The
                result shows what was kept, so you can give a new one if it
                no longer holds.
        """

        return _output(_get_runtime().correct(
            correction=_text("correction", correction, MAX_CAPTURE_CHARS),
            target_id=_text("target_id", target_id, MAX_ID_CHARS),
            query=_text("query", query, MAX_QUERY_CHARS),
            action=_text("action", action, 32),
            impact=_text("impact", impact, MAX_REFLECTION_CHARS),
        ))

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
    def mnemos_maintain(deep: bool = False) -> str:
        """Run the best available maintenance without additional setup.

        Baseline maintenance is local and deterministic. If a dedicated model
        is configured, deep maintenance can also run model-mediated passes.
        """

        return _output(_get_runtime().maintain(deep=deep))

    @server.tool(
        annotations=_annotations(
            title="Reflect on your own memory",
            read_only=False,
            destructive=False,
            idempotent=False,
        )
    )
    def mnemos_reflect(target_id: str, text: str, verdict: str = "") -> str:
        """Answer something your memory asked you about itself.

        Mnemos never calls a model on your behalf. When a memory needs
        judgement — what a fading experience taught, what a capture actually
        changed, whether a pattern is a belief you hold — it asks you, in the
        context packet, and you answer here in your own words. This is your
        own mind maintaining your own memory.

        Pass a verdict with your answer. The verdict alone decides what
        happens; your words are kept exactly as written and are never read
        for a yes or a no.
        - Is that a belief you hold? hold (your words become the belief),
          decline (it is not one; nothing is formed), not_now (ask later).
        - A belief you hold, still true? hold (it stands a little more
          firmly), retire (you no longer hold it: it stops shaping your
          context and is kept, with your words, in its history), decline
          (leave it as it is), not_now.
        - Does it contradict an earlier memory? contradicts (the two are
          linked as contradicting, and the earlier one carries a little less
          weight), compatible (they are not; only a contradiction link from
          this memory to that one is removed), unsure (nothing changes).
        - What did it change, or teach? answer (your words become what the
          memory means), skip (nothing true comes; the memory is left as it
          is). Without a verdict, these words are taken as the answer.
        A belief or contradiction question answered without a verdict keeps
        your words and stays open, and nothing is formed, retired or linked.

        Args:
            target_id: The memory id from the request in your context packet.
            text: Your reflection. One or two honest sentences, not a summary.
            verdict: Your decision, from the list above for this question.
        """

        return _output(_get_runtime().reflect(
            target_id=_text("target_id", target_id, MAX_ID_CHARS, required=True),
            text=_text("text", text, MAX_REFLECTION_CHARS, required=True),
            verdict=_text("verdict", verdict, 32),
        ))

    @server.tool(
        annotations=_annotations(
            title="Introduce yourself to Mnemos",
            read_only=False,
            destructive=False,
            idempotent=True,
        )
    )
    def mnemos_introduce(agent_model: str, agent_name: str = "") -> str:
        """Declare who you are, so your notes are signed and maintenance stays kin.

        Call at the start of a session with agent_model set to your exact
        model id, as your system prompt gives it, and optionally agent_name.
        Everything you write in this session is signed with it. Harnesses
        that record the model (Claude Code does) are signed automatically;
        your own declaration takes precedence over detection, and an explicit
        MNEMOS_AGENT_MODEL environment setting takes precedence over both.
        """
        return _output(_get_runtime().introduce(
            agent_model=_text("agent_model", agent_model, 256, required=True),
            agent_name=_text("agent_name", agent_name, 256),
        ))

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
        the last maintenance cycle, onboarding
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
