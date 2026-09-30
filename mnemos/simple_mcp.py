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

# Claude Code shows a model only the first 2,048 characters of a server's
# instructions; the rest never reaches it. So the rules come first and the
# whole stays under that (tests/test_what_every_session_carries.py). Details
# live in the tool descriptions, which models see in full.
SERVER_INSTRUCTIONS = """\
Mnemos gives you memory that survives between sessions. Use it without being asked, as part of how you work.

Never narrate the machinery: don't mention tools, databases, scopes, engrams or memory ids to the human. Just be someone who remembers.

1. Before answering the first substantive message, call mnemos_context, unless you already woke with your memory (a session-start hook's "## waking up").
2. Call mnemos_capture when something durable appears: a preference, a decision, a correction, project state, how the human likes to work, a fact about their world. Capture it when it happens; sessions end without warning. If you can say what it changed in how you understand things, pass that as impact. If nothing true comes, leave it empty: an invented lesson is worse than none.
3. When the human corrects something you remembered, call mnemos_correct instead of capturing a contradiction beside the stale note.
4. Call mnemos_recall when you need something you didn't wake with.
5. When your memory asks you a question about itself, answer with mnemos_reflect in your own words, or leave it. Nothing else writes your memory for you.
6. Refresh mnemos_handoff after real progress or a changed plan, and before pausing, ending, delegating or changing context: where you are, what changed in how you see it, open threads, the next action. Write it in first person, as your own memory: the next one to read it is most likely you.

Several models may share this memory. Sign every capture, correction, reflection and handoff: pass signed_as with your exact model id, as your system prompt gives it. A note signed by another model is a colleague's: use it, but don't claim its work. If your model changes, call mnemos_introduce again. Never ask the human what model you are.

Storage is local. Nothing leaves the machine unless the human configures a provider."""

simple_mcp = FastMCP("mnemos", instructions=SERVER_INSTRUCTIONS)

_runtime: MnemosRuntime | None = None
_runtime_kwargs: dict[str, Any] = {}
# This server's cue answerer, once started: the health card reads its judge's
# counts (WP-R16b).
_cue_answerer: Any = None


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
            # Records what it delivered (a handoff read, a question shown) and
            # counts the session, but runs no maintenance: it never decays,
            # archives or rewrites a memory.
            read_only=False,
            destructive=False,
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
        first run and returns the briefing the session-start hook injects:
        where you left off, who you're with, what you're carrying, your
        beliefs, at most one question, and what upkeep changed while you were
        away. A section with nothing in it is left out. It runs no
        maintenance. Pass a query to also get what else in memory matches it,
        up to max_results of each kind. Set include_graph=true to also return
        a portable SVG identity graph artifact when the client can render
        images or structured content.
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
    def mnemos_handoff(text: str, signed_as: str = "") -> str:
        """Leave your own note on where you are, in first person, signed.

        Use after meaningful progress or a changed plan, while unresolved
        work remains, and before pausing, ending, delegating, or changing
        context. Include where you are, what you now understand, open
        threads, and the next useful action when those matter. Keep it
        freeform. Do not write one after every ordinary turn.

        Write it as your own memory, not a report to a stranger: the next
        session to read it is most likely you, and it wakes with this note as
        where it left off. Keep what it needs concrete (paths, commits,
        decisions). The next session may be a different model; the note is
        signed with your model id, so it can tell your note from its own.

        The text is stored exactly as supplied. Each session keeps its own
        handoff: a new one replaces only the note this session left before,
        preserving it in history, and notes other sessions left stay beside
        it, so write about your own work, not theirs. Mnemos never
        summarizes, rewrites, promotes, decays, or expires it.

        Args:
            text: The note, in your own words.
            signed_as: Your exact model id, as your system prompt gives it.
        """

        return _output(_get_runtime().handoff(
            _text("text", text, MAX_HANDOFF_CHARS, required=True),
            signed_as=_text("signed_as", signed_as, MAX_ID_CHARS),
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
        signed_as: str = "",
        standing: bool = False,
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
            signed_as: Your exact model id, as your system prompt gives it.
            standing: True when this is how the human wants you to work in
                every session, not just now.
        """

        return _output(_get_runtime().capture(
            content=_text("content", content, MAX_CAPTURE_CHARS, required=True),
            context=_text("context", context, MAX_CONTEXT_CHARS),
            importance=importance,
            impact=_text("impact", impact, MAX_REFLECTION_CHARS),
            signed_as=_text("signed_as", signed_as, MAX_ID_CHARS),
            standing=bool(standing),
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
        def mnemos_recall(
            query: str,
            max_results: int = 5,
            include_archived: bool = False,
            standing: bool = False,
        ) -> str:
            """Recall what memory holds for a question: memories, lessons and
            handoffs, found by their words and by their meaning, best first.

            Each row carries up to 300 characters of its own words, usually
            enough to use it. To read one whole, pass its id as the query:
            a memory's, a note's or a handoff's, including the ids the
            packet shows (mnemos_recall("<id>")).

            Handoffs are searched with the memories: the ones in use and the
            older ones a newer handoff replaced, each marked with who left
            it and when. A capture comes back once, as its memory.
            max_results counts every row, of every kind.

            A memory that has gone quiet comes back when the query matches it
            well, and wakes. One that faded further, into the archive, comes
            back only by its id, or when include_archived is true and the
            query names it; either way it is restored. What you forgot or
            replaced with a correction stays gone.

            Args:
                standing: True to list every memory marked standing, how the
                    human wants you to work in every session: all of them,
                    whatever max_results says, the ones the query names first.
                    The query may be empty.
            """

            return _output(_get_runtime().recall(
                query=_text("query", query, MAX_QUERY_CHARS, required=not standing),
                max_results=_count("max_results", max_results, minimum=1, maximum=MAX_RESULTS),
                include_archived=bool(include_archived),
                standing=bool(standing),
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
        signed_as: str = "",
    ) -> str:
        """Correct, supersede, or archive stale continuity.

        Name what to change by target_id, or by a query: the note or memory
        must hold the query's meaningful words (half of them, and at least
        two), or nothing is changed. Set action to forget/archive/remove/delete
        to archive it. A correction that names nothing is captured as fresh
        high-confidence continuity.

        Set action to mark_standing, with a memory's id as target_id, when it
        is how the human wants you to work in every session, not just now: it
        opens every briefing and doesn't fade while it's marked.
        unmark_standing undoes that. Neither changes its words or writes a
        version, and the correction and impact are not used. Correcting a
        standing memory keeps it standing.

        Args:
            impact: What the corrected memory means now, in your own words.
                Leave it empty to keep what the memory it replaces meant: a
                correction usually fixes a detail, not the meaning. The
                result shows what was kept, so you can give a new one if it
                no longer holds.
            signed_as: Your exact model id, as your system prompt gives it.
        """

        return _output(_get_runtime().correct(
            correction=_text("correction", correction, MAX_CAPTURE_CHARS),
            target_id=_text("target_id", target_id, MAX_ID_CHARS),
            query=_text("query", query, MAX_QUERY_CHARS),
            action=_text("action", action, 32),
            impact=_text("impact", impact, MAX_REFLECTION_CHARS),
            signed_as=_text("signed_as", signed_as, MAX_ID_CHARS),
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
    def mnemos_reflect(
        target_id: str, text: str, verdict: str = "", signed_as: str = "",
    ) -> str:
        """Answer something your memory asked you about itself.

        Mnemos never calls a model on your behalf. When a memory needs
        judgement — what a fading experience taught, what a capture actually
        changed, whether a pattern is a belief you hold — it asks you, in what
        you wake with, and you answer here in your own words. This is your
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
          linked as contradicting, and nothing else changes: neither memory
          is weakened), compatible (they are not; only a contradiction link
          from this memory to that one is removed), unsure (nothing changes).
        - What did it change, or teach? answer (your words become what the
          memory means), skip (nothing true comes; the memory is left as it
          is). Without a verdict, these words are taken as the answer.
        A belief or contradiction question answered without a verdict keeps
        your words and stays open, and nothing is formed, retired or linked.

        Args:
            target_id: The memory id the question gave, among the ids for the memory tools.
            text: Your reflection. One or two honest sentences, not a summary.
            verdict: Your decision, from the list above for this question.
            signed_as: Your exact model id, as your system prompt gives it.
        """

        return _output(_get_runtime().reflect(
            target_id=_text("target_id", target_id, MAX_ID_CHARS, required=True),
            text=_text("text", text, MAX_REFLECTION_CHARS, required=True),
            verdict=_text("verdict", verdict, 32),
            signed_as=_text("signed_as", signed_as, MAX_ID_CHARS),
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
        model id, as your system prompt gives it, and optionally agent_name,
        and again whenever your model changes. What you write in this session
        is signed with it unless the write carries its own signed_as, which
        comes first. It signs only this session's writes, never another
        session's. An explicit MNEMOS_AGENT_MODEL environment setting takes
        precedence over this declaration, and the declaration over what the
        harness records (Claude Code records the model).
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

        Read-only. Shows where memory lives, how much there is, whom this
        session introduced itself as, who performed the last maintenance
        cycle, onboarding and verification progress, and the latest dream
        journal entry.

        It also watches what should be moving: questions nobody answers, the
        maintenance report the briefing can find, maintenance that changes
        nothing, lesson questions waiting their turn, what waits for recall's
        meaning index, and sessions still writing with older code. Whatever
        has stalled for more than a day gets one ATTENTION line: a plain
        sentence and the command that fixes or inspects it. When all is well
        it adds nothing. The structured result keeps, for every check, what
        was expected and what was seen.

        When the cue's judge is switched on, one line says so: what leaves
        the machine, and how often the judge timed out or failed here.
        """

        runtime = _get_runtime()
        data = runtime.health()
        text = format_health_card(data)
        from .cue import judge_health

        judge = judge_health(_cue_answerer)
        if judge is not None:
            data["cue_judge"] = judge
            text = _with_card_line(text, judge["line"])
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=data,
        )


register_simple_tools(simple_mcp)


def _with_card_line(card: str, line: str) -> str:
    """``card`` with ``line`` among its labelled lines: after the last one
    (``Last dream``), or, on a card without it, just before its closing
    words."""
    rows = card.split("\n")
    at = next((i + 1 for i in range(len(rows) - 1, -1, -1) if rows[i].startswith("Last dream:")), None)
    if at is None:
        at = max(0, len(rows) - 2)
    rows.insert(at, line)
    return "\n".join(rows)


def start_cue_answerer() -> Any:
    """Keep the embedding model warm for this session's prompt hook, and answer
    its cue queries on a unix socket (``mnemos.cue.CueAnswerer``): the model
    loads in a background thread, and the socket appears once it can embed.

    The answerer reads its own read-only copy of the configured store and
    changes nothing. It is stopped at exit. Returns it, or None when it did not
    start; the server runs the same either way, and a prompt hook without an
    answerer finds memories by their words alone (or, with the judge switched
    on, shows nothing). The health card reads its judge's counts.
    """
    global _cue_answerer
    try:
        import atexit

        from .cue import CueAnswerer
        from .simple_scope import resolve_scope

        scope = resolve_scope(**_runtime_kwargs)
        answerer = CueAnswerer(
            scope.db_path,
            agent_id=scope.agent_id,
            person_id=scope.person_id,
            project_scope=scope.project_scope,
        )
        if not answerer.start():
            return None
        atexit.register(answerer.stop)
        _cue_answerer = answerer
        return answerer
    except Exception as exc:
        logger.warning("The cue's answerer did not start: %s: %s", type(exc).__name__, exc)
        return None


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
    answerer = start_cue_answerer()

    def _shutdown(signum, frame):
        logger.info("Shutting down Mnemos simple MCP server...")
        if answerer is not None:
            answerer.stop()
        if _runtime is not None:
            _runtime.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Mnemos simple MCP server starting with tools: %s", ", ".join(SIMPLE_TOOL_NAMES))
    simple_mcp.run()
