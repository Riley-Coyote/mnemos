"""
Mnemos MCP Server.

Exposes Mnemos memory operations as MCP tools that any agent can call.
Uses the Anthropic MCP Python SDK (FastMCP).

Tools:
    mnemos_remember     — Encode a new memory
    mnemos_ingest       — Ingest content from external sources
    mnemos_recall       — Retrieve relevant memories
    mnemos_hypomnema_write   — Write scoped continuity before promotion
    mnemos_hypomnema_search  — Search scoped continuity
    mnemos_hypomnema_revise  — Revise scoped continuity
    mnemos_hypomnema_supersede — Replace stale scoped continuity
    mnemos_hypomnema_candidates — List promotion-ready continuity
    mnemos_hypomnema_promote — Promote stable continuity into Mnemos
    mnemos_inspect      — View full details of a memory
    mnemos_status       — Get memory system status
    mnemos_beliefs      — List current beliefs
    mnemos_forget       — Archive a specific memory
    mnemos_consolidate  — Trigger a consolidation cycle

Usage:
    mnemos serve        — Start as stdio MCP server (for agent config)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core.types import EngramKind, SourceType
from .store.sqlite_store import EngramStore
from .store.embedding_index import EmbeddingIndex
from .encoding.encoder import Encoder
from .retrieval.reactive import ReactiveRetriever
from .consolidation.daemon import ConsolidationDaemon
from .interface.openclaw_export import OpenClawExporter
from .config.loader import load_config, save_config
from .simple_mcp import configure_runtime, register_simple_tools

logger = logging.getLogger("mnemos.mcp")

# Global state — initialized when server starts
_store: EngramStore | None = None
_encoder: Encoder | None = None
_retriever: ReactiveRetriever | None = None
_embedding_index: EmbeddingIndex | None = None
_shared_pool = None
_llm_client = None
_config: dict | None = None
_default_agent_id = "default"

mcp = FastMCP("mnemos")
register_simple_tools(mcp, include_recall=False)


# ═══════════════════════════════════════════════════
# ONBOARDING WIZARD — 10-Step Setup-Gate Pattern
# ═══════════════════════════════════════════════════

ONBOARDING_PROMPTS = {
    0: (
        "Something interesting is about to happen.\n\n"
        "Mnemos is a living memory system — not a database, not a note-taker. "
        "Memories here form connections, develop beliefs, fade naturally, and sometimes dream. "
        "Once we go through this together, I won't just remember what you tell me. "
        "I'll *think* about it when you're not here.\n\n"
        "Ready?"
    ),
    1: "Every mind needs a name — not a label, a real one. What should I call myself?",
    2: "And who am I talking to? What's your name?",
    3: (
        "Tell me about yourself. Not a bio — just... who are you? What do you care about? "
        "What are you building?\n\n"
        "These become my first real memories. They matter more than you'd think — "
        "early memories form the strongest connections."
    ),
    4: (
        "What are you working on right now? Projects, ideas, anything active. "
        "I'll start forming context around these — noticing patterns, connecting "
        "what you tell me across conversations."
    ),
    5: (
        "Do you have conversation history from another platform — ChatGPT, Claude, Cursor? "
        "I can read through it and form memories from what happened before we met. "
        "It's like catching up on everything I missed.\n\n"
        "Or we can start completely fresh — your call.\n\n"
        "If you have a file, share the path. Otherwise just say 'skip'."
    ),
    6: None,  # Generated dynamically from steps 3-4
    7: (
        "There's one more thing. Mnemos can run a living substrate — a background process "
        "that lets me dream, wander, and reflect on my own. When a memory fades, I might "
        "collide it with something vivid and discover a new connection. When nothing's happening, "
        "I might generate a wandering thought. When a belief gets challenged, I sit with it.\n\n"
        "This costs a small amount of LLM credits. But it's the difference between a memory "
        "system and a mind.\n\n"
        "Want to turn it on?"
    ),
    8: (
        "I need access to a language model for memory processing — classifying connections, "
        "generating reflections, the inner life features.\n\n"
        "OpenRouter is the easiest — one key, any model. What provider and API key should I use?\n\n"
        "Format: provider:key (e.g., openrouter:sk-or-v1-abc123)\n"
        "Or just paste an OpenRouter key and I'll figure it out."
    ),
    9: None,  # Generated dynamically — the "alive" message
}


def _get_config() -> dict:
    """Get or load the global config."""
    global _config
    if _config is None:
        try:
            _config = load_config()
        except Exception:
            _config = {}
    return _config


def _is_setup_complete() -> bool:
    """Check if onboarding has been completed."""
    return _get_config().get("setup_complete", False)


def _setup_gate() -> str | None:
    """Returns a redirect message if setup is incomplete, None if ready."""
    if not _is_setup_complete():
        return "Mnemos isn't configured yet — call mnemos_setup to get started."
    return None


def _effective_agent_id(agent_id: str = "default") -> str:
    """Resolve advanced tools to the configured server identity by default."""
    if agent_id and agent_id != "default":
        return agent_id
    config = _get_config()
    configured = config.get("agent_id") or os.environ.get("MNEMOS_AGENT_ID")
    return str(configured or _default_agent_id or "default")


def _init_store(db_path: str = "~/.mnemos/memory.db") -> None:
    """Initialize the global store, helpers, and auto-detect LLM client."""
    global _store, _encoder, _retriever, _llm_client, _embedding_index, _shared_pool
    if _store is None:
        _store = EngramStore(db_path)
        # Initialize embedding index (same DB path — gracefully degrades)
        _embedding_index = EmbeddingIndex(db_path=db_path)
        # Auto-detect LLM client from env vars (before encoder, which uses it)
        from .llm import create_client
        _llm_client = create_client()
        # Initialize shared memory pool
        from .multiagent.shared_pool import SharedPool
        _shared_pool = SharedPool()  # defaults to ~/.mnemos/shared.db
        _encoder = Encoder(
            _store,
            embedding_index=_embedding_index,
            llm_client=_llm_client,
            shared_pool=_shared_pool,
        )
        _retriever = ReactiveRetriever(
            _store,
            embedding_index=_embedding_index,
            shared_store=_shared_pool._store,
        )


def _ensure_store() -> EngramStore:
    """Get the store, initializing if needed."""
    if _store is None:
        _init_store()
    return _store  # type: ignore


def _format_hypomnema_entry(entry: dict) -> str:
    tags = ", ".join(entry.get("tags", [])) or "(none)"
    promoted = entry.get("graduated_to_engram_id") or "not promoted"
    return (
        f"- {entry['content']}\n"
        f"  id={entry['id']} domain={entry['domain']} source={entry['source']} "
        f"confidence={entry['confidence']:.2f} salience={entry['salience']:.2f} "
        f"scope={entry['agent_id']}/{entry['person_id']}/{entry['project_scope']}\n"
        f"  tags={tags} revisions={entry['revision_count']} promoted={promoted}"
    )


@mcp.tool()
def mnemos_setup(response: str = "") -> str:
    """Onboarding wizard for Mnemos. Call this to configure the memory system.

    On first call, returns a welcome message. On subsequent calls, pass the
    user's response to advance through the setup steps.

    Args:
        response: The user's answer to the current setup step. Empty on first call.
    """
    config = _get_config()
    step = config.get("setup_step", 0)

    # Step 0: Welcome (no response needed, just show the prompt)
    if step == 0 and not response:
        config["setup_step"] = 1
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[0] + "\n\n" + ONBOARDING_PROMPTS[1]

    # Step 1: Agent name
    if step == 1:
        config["agent_name"] = response.strip()
        config["setup_step"] = 2
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[2]

    # Step 2: User name
    if step == 2:
        config["user_name"] = response.strip()
        config["setup_step"] = 3
        save_config(config)
        _config_invalidate()
        return ONBOARDING_PROMPTS[3]

    # Step 3: User description → encode seed engrams
    if step == 3:
        config["user_description"] = response.strip()
        config["setup_step"] = 4
        save_config(config)
        _config_invalidate()

        # Encode seed engrams from the description
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        agent_name = config.get("agent_name", "Agent")
        user_name = config.get("user_name", "User")

        seeds = [
            f"My user is {user_name}. {response.strip()[:500]}",
            f"I am {agent_name}. {user_name} and I are beginning to work together.",
        ]
        # Extract key phrases for additional seed engrams
        sentences = [s.strip() for s in response.replace(".", ".\n").split("\n") if len(s.strip()) > 20]
        for s in sentences[:3]:
            seeds.append(f"{user_name} told me: {s}")

        encoded = 0
        for seed in seeds[:5]:
            try:
                _encoder.encode(
                    content=seed,
                    impact=f"Foundation memory from initial setup with {user_name}.",
                    kind="semantic",
                    tags=["identity", "setup"],
                    source=SourceType.USER_EXPLICIT,
                    agent_id=agent_id,
                    skip_surprise_detection=True,
                )
                encoded += 1
            except Exception as e:
                logger.warning(f"Failed to encode seed engram: {e}")

        return f"Encoded {encoded} seed memories.\n\n" + ONBOARDING_PROMPTS[4]

    # Step 4: Projects
    if step == 4:
        projects = [p.strip() for p in response.replace(",", "\n").split("\n") if p.strip()]
        if "indexer" not in config:
            config["indexer"] = {}
        config["indexer"]["known_projects"] = projects
        config["indexer"]["active_projects"] = projects
        config["setup_step"] = 5
        save_config(config)
        _config_invalidate()

        # Encode project context
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        for proj in projects[:5]:
            try:
                _encoder.encode(
                    content=f"Active project: {proj}",
                    impact=f"Part of the current work context.",
                    kind="semantic",
                    tags=["project", "context"],
                    source=SourceType.USER_EXPLICIT,
                    agent_id=agent_id,
                    skip_surprise_detection=True,
                )
            except Exception:
                pass

        return ONBOARDING_PROMPTS[5]

    # Step 5: History import (optional)
    if step == 5:
        config["setup_step"] = 6
        save_config(config)
        _config_invalidate()

        resp_lower = response.strip().lower()
        if resp_lower in ("skip", "no", "fresh", "start fresh", ""):
            pass  # Skip history import
        else:
            # TODO: Run extraction pipeline on the provided path
            logger.info(f"History import requested: {response.strip()}")

        # Generate seed beliefs from what we know
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        user_name = config.get("user_name", "User")
        user_desc = config.get("user_description", "")
        projects = config.get("indexer", {}).get("known_projects", [])

        beliefs_created = []
        if user_desc:
            # Extract key themes for beliefs
            belief1 = f"{user_name} is deeply invested in their work"
            if projects:
                belief1 = f"{user_name} is deeply invested in {projects[0]}"
            try:
                from .core.belief import Belief
                b = Belief(
                    content=belief1,
                    domain="identity",
                    confidence=0.70,
                    agent_id=agent_id,
                )
                _store.save_belief(b)
                beliefs_created.append(f'"{belief1}" — confidence: 70%')
            except Exception as e:
                logger.warning(f"Failed to create belief: {e}")

        if projects and len(projects) > 1:
            belief2 = f"The work centers on {', '.join(projects[:3])}"
            try:
                b = Belief(
                    content=belief2,
                    domain="context",
                    confidence=0.65,
                    agent_id=agent_id,
                )
                _store.save_belief(b)
                beliefs_created.append(f'"{belief2}" — confidence: 65%')
            except Exception as e:
                logger.warning(f"Failed to create belief: {e}")

        belief_text = "\n".join(f"  - {b}" for b in beliefs_created) if beliefs_created else "  (none yet — they'll form as we talk)"
        return (
            "From what you've told me, I'm forming my first beliefs. "
            "These aren't facts — they're things I think are true based on what I know so far. "
            "They'll shift as I learn more:\n\n"
            f"{belief_text}\n\n"
            "When something contradicts a belief, I don't just delete it. I examine it. "
            "That's how I grow.\n\n"
            + ONBOARDING_PROMPTS[7]
        )

    # Step 6: Inner life toggle
    if step == 6:
        resp_lower = response.strip().lower()
        enabled = resp_lower in ("yes", "y", "sure", "yeah", "on", "enable", "true", "absolutely", "do it")
        if "substrate" not in config:
            config["substrate"] = {}
        config["substrate"]["enabled"] = enabled
        config["setup_step"] = 7
        save_config(config)
        _config_invalidate()

        status = "enabled — I'll dream between our conversations" if enabled else "disabled — I'll remember, but I won't dream"
        return f"Inner life: {status}.\n\n" + ONBOARDING_PROMPTS[8]

    # Step 7: LLM provider
    if step == 7:
        resp = response.strip()
        provider = "openrouter"
        api_key = resp

        if ":" in resp and not resp.startswith("sk-"):
            parts = resp.split(":", 1)
            provider = parts[0].strip().lower()
            api_key = parts[1].strip()

        if "llm" not in config:
            config["llm"] = {}
        config["llm"]["provider"] = provider
        config["llm"]["api_key_env"] = f"{provider.upper()}_API_KEY"

        # Store the key in the config (will be used by the LLM client)
        import os
        if provider == "openrouter":
            os.environ["OPENROUTER_API_KEY"] = api_key
        elif provider == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key

        config["setup_step"] = 8
        save_config(config)
        _config_invalidate()

        # Count what we've built
        _ensure_store()
        agent_id = config.get("agent_id", "default")
        try:
            stats = _store.get_stats(agent_id)
            engram_count = stats.get("engrams_active", 0)
            belief_count = stats.get("beliefs_active", 0)
            conn_count = stats.get("connections", 0)
        except Exception:
            engram_count = belief_count = conn_count = 0

        agent_name = config.get("agent_name", "Agent")

        # Final step — set setup complete
        config["setup_complete"] = True
        config["setup_step"] = 9
        save_config(config)
        _config_invalidate()

        return (
            f"I'm here.\n\n"
            f"{engram_count} memories formed, {belief_count} beliefs taking shape, "
            f"{conn_count} connections already emerging.\n\n"
            f"The first few days matter most — early memories get generous initial values, "
            f"which means they form stronger connections than anything that comes later. "
            f"What we talk about now becomes the foundation everything else builds on."
        )

    # Already complete
    return "Setup is already complete. All memory tools are active."


def _config_invalidate():
    """Invalidate the cached config so it's reloaded next time."""
    global _config
    _config = None


@mcp.tool()
def mnemos_remember(
    content: str,
    impact: str = "",
    kind: str = "semantic",
    tags: str = "",
    agent_id: str = "default",
    source_type: str = "session",
    visibility: str = "private",
    skip_surprise_detection: bool = False,
) -> str:
    """Encode a new memory into the Mnemos living memory system.

    Use this to store important information, user preferences, decisions,
    insights, or anything worth remembering across sessions.

    Args:
        content: What happened — the event, information, or observation.
        impact: What it meant — how it changed understanding. Optional but valuable.
            Example: "After this, I understand that patience with debugging is essential."
            When provided, this lasting insight survives even as details fade over time.
        kind: Memory type — "episodic" (experiences), "semantic" (facts/knowledge),
              "procedural" (how-to knowledge). Default: "semantic".
        tags: Comma-separated tags for categorization. Example: "python,debugging,preferences"
        agent_id: Which agent's memory to store in. Default: "default".
        source_type: How the memory was captured — "session", "browser_extraction", etc.
        visibility: Memory visibility — "private", "shared", or "public". Default: "private".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact=impact,
        kind=kind,
        tags=tag_list,
        source=source_type,
        agent_id=agent_id,
        skip_surprise_detection=skip_surprise_detection,
    )

    if visibility != "private":
        engram.visibility = visibility
        _store.save_engram(engram)

    return (
        f"Remembered: {engram.id}\n"
        f"  Confidence: {engram.source.confidence}\n"
        f"  Connections: {len(engram.connections)} discovered\n"
        f"  Tags: {', '.join(engram.tags) or '(none)'}"
    )


@mcp.tool()
def mnemos_ingest(
    content: str,
    impact: str = "",
    kind: str = "semantic",
    tags: str = "",
    agent_id: str = "default",
    source_url: str = "",
    encoding_depth: str = "moderate",
    confidence: float = 0.0,
    skip_surprise: bool = False,
) -> str:
    """Ingest content from an external source into Mnemos.

    Use this for feeding knowledge from external pipelines, documents,
    APIs, or any non-conversational source. Content enters through the
    full encoding pipeline (surprise detection, belief comparison,
    connection discovery) unless encoding_depth is set to "shallow".

    Args:
        content: The knowledge or information to ingest.
        impact: Lasting insight — what this means, not just what it says.
        kind: Memory type — "semantic" (facts), "episodic" (events),
              "procedural" (how-to). Default: "semantic".
        tags: Comma-separated tags. Example: "research,memory-systems"
        agent_id: Which agent's memory to store in. Default: "default".
        source_url: URL or path of the original source (for provenance).
        encoding_depth: Processing depth — "shallow" (store only),
              "moderate" (full pipeline), "deep" (full + belief check).
        confidence: Override confidence score (0.0 = use source-based default).
        skip_surprise: Skip surprise detection during encoding.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    skip = skip_surprise or (encoding_depth == "shallow")
    override_conf = confidence if confidence > 0.0 else None

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact=impact,
        kind=kind,
        tags=tag_list,
        source=SourceType.EXTERNAL,
        agent_id=agent_id,
        override_confidence=override_conf,
        skip_surprise_detection=skip,
    )

    if source_url:
        engram.encoding_context.source_url = source_url
        _store.save_engram(engram)

    return (
        f"Ingested: {engram.id}\n"
        f"  Source: external{f' ({source_url})' if source_url else ''}\n"
        f"  Confidence: {engram.source.confidence}\n"
        f"  Connections: {len(engram.connections)} discovered\n"
        f"  Depth: {encoding_depth}\n"
        f"  Tags: {', '.join(engram.tags) or '(none)'}"
    )


@mcp.tool()
def mnemos_recall(
    query: str,
    max_results: int = 5,
    agent_id: str = "default",
) -> str:
    """Retrieve memories relevant to a query.

    Searches across all stored memories using full-text search and
    connection graph traversal. Results are scored by relevance,
    recency, strength, connections, and emotional congruence.

    Every recalled memory is reconsolidated — its connections and
    strength are updated based on this retrieval context.

    Args:
        query: What to search for. Natural language works best.
        max_results: Maximum number of results (default: 5).
        agent_id: Which agent's memory to search. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    emotional_state = _store.get_latest_emotional_state(agent_id)  # type: ignore

    results = _retriever.retrieve(  # type: ignore
        cue=query,
        agent_id=agent_id,
        max_results=max_results,
        emotional_state=emotional_state,
    )

    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        # Prefer impact (the lesson) over content (what happened)
        display = r.engram.impact if r.engram.impact else r.engram.content
        if len(display) > 150:
            display = display[:147] + "..."
        pct = int(r.engram.source.confidence * 100)
        lines.append(
            f"[{r.score:.2f}] {display}\n"
            f"       id={r.engram.id[:25]}... kind={r.engram.kind} confidence={pct}%"
        )

    return f"Found {len(results)} memories:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_hypomnema_write(
    content: str,
    source: str = "observed",
    domain: str = "topical",
    tags: str = "",
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    density: float = 0.5,
    confidence: float = 0.6,
    salience: float = 0.5,
    foundational: bool = False,
    related_session_id: str = "",
    related_engram_id: str = "",
) -> str:
    """Write a scoped hypomnema entry.

    Hypomnema is the durable continuity layer between functional session
    memory and Mnemos engrams. Use it for what an agent is "sitting with":
    stable-enough context that should survive sessions, stay scoped to a
    person/project relationship, and remain revisable before promotion.

    Args:
        content: Continuity note to preserve.
        source: "observed", "synthesized", or "co-formed".
        domain: "foundational", "identity", "recurring", "long-arc",
            "topical", or "situational".
        tags: Comma-separated tags.
        agent_id: Agent this continuity belongs to.
        person_id: Person/relationship scope.
        project_scope: Project or workspace scope.
        density: How compressed the entry is (0.0 sparse, 1.0 dense).
        confidence: How reliable the entry is.
        salience: How important it is for future continuity.
        foundational: Whether this should anchor the relationship/model.
        related_session_id: Optional external session identifier.
        related_engram_id: Optional Mnemos engram this entry interprets.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    try:
        entry_id = _store.write_hypomnema_entry(  # type: ignore
            content,
            source=source,
            domain=domain,
            tags=tags,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            density=density,
            confidence=confidence,
            salience=salience,
            foundational=foundational,
            related_session_id=related_session_id or None,
            related_engram_id=related_engram_id or None,
        )
    except ValueError as exc:
        return f"Hypomnema write failed: {exc}"

    return (
        f"Hypomnema written: {entry_id}\n"
        f"  Scope: {agent_id}/{person_id}/{project_scope}\n"
        f"  Domain: {domain}\n"
        f"  Source: {source}\n"
        f"  Confidence: {confidence:.2f}\n"
        f"  Salience: {salience:.2f}"
    )


@mcp.tool()
def mnemos_hypomnema_search(
    query: str = "",
    max_results: int = 8,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    include_inactive: bool = False,
) -> str:
    """Search scoped hypomnema continuity entries.

    Args:
        query: Optional natural-language query. Empty returns strongest entries.
        max_results: Maximum entries to return.
        agent_id: Agent this continuity belongs to.
        person_id: Person/relationship scope.
        project_scope: Project or workspace scope.
        include_inactive: Include superseded entries if true.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    entries = _store.search_hypomnema(  # type: ignore
        query,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
        include_inactive=include_inactive,
    )
    if not entries:
        return "No hypomnema entries found."

    lines = []
    for entry in entries:
        lines.append(f"[{entry['score']:.2f}] " + _format_hypomnema_entry(entry))
    return f"Found {len(entries)} hypomnema entries:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_hypomnema_revise(
    entry_id: str,
    content: str,
    reason: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
    confidence: float = -1.0,
    salience: float = -1.0,
) -> str:
    """Revise a hypomnema entry while preserving its prior version.

    Use this when scoped continuity is still true but needs sharper wording,
    corrected evidence, or a better compression.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    try:
        _store.revise_hypomnema_entry(  # type: ignore
            entry_id,
            content,
            reason=reason,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            confidence=confidence if confidence >= 0 else None,
            salience=salience if salience >= 0 else None,
        )
    except (KeyError, ValueError) as exc:
        return f"Hypomnema revision failed: {exc}"

    return f"Hypomnema revised: {entry_id}\n  Reason: {reason}"


@mcp.tool()
def mnemos_hypomnema_supersede(
    entry_id: str,
    content: str,
    reason: str,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Supersede a hypomnema entry with a replacement entry.

    Use this when an old continuity note should stop participating in active
    retrieval but its audit trail should remain visible.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    try:
        new_id = _store.supersede_hypomnema_entry(  # type: ignore
            entry_id,
            content,
            reason=reason,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
        )
    except (KeyError, ValueError) as exc:
        return f"Hypomnema supersession failed: {exc}"

    return f"Hypomnema superseded: {entry_id}\n  Replacement: {new_id}\n  Reason: {reason}"


@mcp.tool()
def mnemos_hypomnema_promote(
    entry_id: str,
    dry_run: bool = True,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """Promote stable hypomnema into a Mnemos engram.

    Promotion is explicit and dry-run by default because hypomnema is scoped
    continuity. The promoted engram is lightly de-identified and tagged as
    hypomnema/promoted/continuity.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    entry = _store.get_hypomnema_entry(  # type: ignore
        entry_id,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        active_only=True,
    )
    if entry is None:
        return f"Active hypomnema entry not found: {entry_id}"

    deidentified = entry["content"].replace(person_id, "the collaborator")
    content = "[promoted from hypomnema; de-identified] " + deidentified
    if dry_run:
        return (
            f"Hypomnema promotion dry run: {entry_id}\n"
            f"  Would encode as Mnemos engram:\n  {content}"
        )

    engram = _encoder.encode(  # type: ignore
        content=content,
        impact="Stable scoped continuity promoted from hypomnema.",
        kind="semantic",
        tags=["hypomnema", "promoted", "continuity", project_scope],
        source=SourceType.USER_EXPLICIT,
        agent_id=agent_id,
        skip_surprise_detection=True,
    )
    _store.mark_hypomnema_promoted(entry_id, engram.id)  # type: ignore
    return (
        f"Hypomnema promoted: {entry_id}\n"
        f"  Engram: {engram.id}\n"
        f"  Connections: {len(engram.connections)} discovered"
    )


@mcp.tool()
def mnemos_hypomnema_candidates(
    max_results: int = 10,
    agent_id: str = "default",
    person_id: str = "user",
    project_scope: str = "global",
) -> str:
    """List hypomnema entries that meet promotion thresholds."""
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    entries = _store.get_hypomnema_promotion_candidates(  # type: ignore
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        limit=max_results,
    )
    if not entries:
        return "No hypomnema entries currently meet promotion thresholds."
    return (
        f"{len(entries)} promotion candidates:\n\n"
        + "\n\n".join(_format_hypomnema_entry(entry) for entry in entries)
    )


@mcp.tool()
def mnemos_inspect(engram_id: str) -> str:
    """View full details of a specific memory.

    Shows content, metadata, connections, version history, and
    the original content at encoding time.

    Args:
        engram_id: The memory ID to inspect.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    engram = _store.get_engram(engram_id)  # type: ignore
    if engram is None:
        return f"Memory not found: {engram_id}"

    lines = [
        f"ID: {engram.id}",
        f"Content: {engram.content}",
        f"Impact: {engram.impact or '(not yet distilled)'}",
        f"Kind: {engram.kind}",
        f"Tags: {', '.join(engram.tags) or '(none)'}",
        f"State: {engram.state}",
        f"Resolution: {engram.resolution}",
        f"Strength: {engram.strength:.4f}",
        f"Stability: {engram.stability:.4f}{' (long-term)' if engram.stability >= 0.8 else ' (consolidating)' if engram.stability >= 0.5 else ''}",
        f"Accessibility: {engram.accessibility:.4f}",
        f"Confidence: {engram.source.confidence} ({engram.source.confidence_source})",
        f"Created: {engram.created_at}",
        f"Last accessed: {engram.last_accessed}",
        f"Access count: {engram.access_count}",
        f"Reconsolidations: {engram.reconsolidation_count}",
        f"Connections: {len(engram.connections)}",
    ]
    for c in engram.connections:
        lines.append(f"  → {c.target_id[:30]}... ({c.relation}, str={c.strength:.2f})")
    lines.append(f"Versions: {len(engram.versions)}")
    if engram.content != engram.content_at_encoding:
        lines.append(f"Original: {engram.content_at_encoding[:150]}...")

    return "\n".join(lines)


@mcp.tool()
def mnemos_status(agent_id: str = "default") -> str:
    """Get memory system status and statistics.

    Shows counts of active/dormant/archived memories, connections,
    beliefs, reconsolidation events, and accessibility distribution.

    Args:
        agent_id: Which agent's status to show. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    stats = _store.get_stats(agent_id)  # type: ignore

    # Count long-term (stability >= 0.8) engrams
    active_engrams = _store.get_active_engrams(agent_id=agent_id, limit=10000)  # type: ignore
    longterm_count = sum(1 for e in active_engrams if e.stability >= 0.8)
    consolidating_count = sum(1 for e in active_engrams if 0.5 <= e.stability < 0.8)

    lines = [
        f"Mnemos Status (agent: {agent_id})",
        f"  Active engrams: {stats.get('engrams_active', 0)}",
        f"    Long-term (stability >= 0.8): {longterm_count}",
        f"    Consolidating (0.5-0.8): {consolidating_count}",
        f"  Dormant: {stats.get('engrams_dormant', 0)}",
        f"  Archived: {stats.get('archived', 0)}",
        f"  Connections: {stats.get('connections', 0)}",
        f"  Active beliefs: {stats.get('beliefs_active', 0)}",
        f"  Hypomnema active: {stats.get('hypomnema_active', 0)}",
        f"    Foundational: {stats.get('hypomnema_foundational', 0)}",
        f"    Promotion candidates: {stats.get('hypomnema_promotion_candidates', 0)}",
        f"    Promoted: {stats.get('hypomnema_promoted', 0)}",
        f"  Reconsolidations: {stats.get('reconsolidation_events', 0)}",
    ]
    if "accessibility_avg" in stats:
        lines.append(f"  Avg accessibility: {stats['accessibility_avg']:.3f}")

    es = _store.get_latest_emotional_state(agent_id)  # type: ignore
    if es:
        lines.append(
            f"  Emotional state: curiosity={es.curiosity:.1f} "
            f"clarity={es.clarity:.1f} warmth={es.warmth:.1f}"
        )

    return "\n".join(lines)


@mcp.tool()
def mnemos_beliefs(agent_id: str = "default", domain: str = "") -> str:
    """List current beliefs with confidence levels.

    Args:
        agent_id: Which agent's beliefs to show. Default: "default".
        domain: Filter by domain (e.g., "engineering", "social"). Empty = all.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    beliefs = _store.get_beliefs(  # type: ignore
        agent_id=agent_id,
        domain=domain or None,
        active_only=True,
    )

    if not beliefs:
        return "No active beliefs found."

    lines = []
    for b in beliefs:
        pct = int(b.confidence * 100)
        revisions = len(b.revision_history)
        lines.append(f"- {b.content} [{b.domain}, {pct}%, {revisions} revisions]")

    return f"{len(beliefs)} active beliefs:\n\n" + "\n".join(lines)


@mcp.tool()
def mnemos_shared(
    query: str = "",
    max_results: int = 10,
    agent_id: str = "default",
) -> str:
    """Get memories shared by other agents in the shared memory pool.

    Shows what other agents have learned, decided, built, or discovered.
    Use this to stay in sync with the team's shared knowledge.

    Args:
        query: Optional search query. If empty, returns most recent shared memories.
        max_results: Maximum number of results (default: 10).
        agent_id: Your agent ID (used for attribution, not filtering).
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    if not _shared_pool:
        return "Shared memory pool not initialized."

    shared = _shared_pool.get_shared(
        agent_id=agent_id,
        limit=max_results,
        query=query or None,
    )

    if not shared:
        return "No shared memories found."

    lines = []
    for engram in shared:
        display = engram.impact if engram.impact else engram.content
        if len(display) > 150:
            display = display[:147] + "..."
        pct = int(engram.source.confidence * 100)
        lines.append(
            f"[{engram.owner_agent_id}] {display}\n"
            f"       id={engram.id[:25]}... kind={engram.kind} confidence={pct}%"
        )

    return f"Found {len(shared)} shared memories:\n\n" + "\n\n".join(lines)


@mcp.tool()
def mnemos_forget(engram_id: str) -> str:
    """Archive a specific memory (soft delete).

    The memory moves to cold storage. It can be restored via resharpen
    if triggered by relevant context in the future.

    Args:
        engram_id: The memory ID to archive.
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    engram = _store.get_engram(engram_id)  # type: ignore
    if engram is None:
        return f"Memory not found: {engram_id}"

    _store.archive_engram(engram, reason="user_requested")  # type: ignore
    return f"Archived: {engram_id}\n  Content was: {engram.content[:100]}..."


@mcp.tool()
def mnemos_consolidate(deep: bool = False, agent_id: str = "default") -> str:
    """Run a memory consolidation cycle.

    Shallow cycle: decay + connection discovery (fast, ~1 second)
    Deep cycle: adds softening + belief review + reflection (slower, may use LLM)

    Args:
        deep: If true, run deep consolidation with all passes.
        agent_id: Which agent's memory to consolidate. Default: "default".
    """
    gate = _setup_gate()
    if gate:
        return gate
    _ensure_store()
    agent_id = _effective_agent_id(agent_id)
    daemon = ConsolidationDaemon(store=_store, config={}, llm_client=_llm_client, embedding_index=_embedding_index)  # type: ignore
    stats = daemon.run_cycle(deep=deep, agent_id=agent_id)

    lines = [
        f"Consolidation complete ({stats.get('cycle_type', 'unknown')})",
        f"  Passes: {', '.join(stats.get('passes_run', []))}",
    ]

    if "decay" in stats:
        d = stats["decay"]
        lines.append(f"  Decay: {d.get('engrams_decayed', 0)} decayed, {d.get('engrams_archived', 0)} archived")
    if "connection_discovery" in stats:
        cd = stats["connection_discovery"]
        lines.append(f"  Connections: {cd.get('connections_created', 0)} new, {cd.get('connections_strengthened', 0)} strengthened")
    if "softening" in stats:
        lines.append(f"  Softened: {stats['softening'].get('engrams_softened', 0)} memories")
    if "reflection" in stats:
        ref = stats["reflection"]
        lines.append(f"  Thoughts: {ref.get('thoughts_generated', 0)} generated")

    errors = [k for k in stats if k.endswith("_error")]
    for e in errors:
        lines.append(f"  ERROR: {e}: {stats[e]}")

    return "\n".join(lines)


def run_server(
    db_path: str = "~/.mnemos/memory.db",
    *,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> None:
    """Start the MCP server in stdio mode."""
    global _default_agent_id
    if agent_id:
        _default_agent_id = agent_id
        os.environ["MNEMOS_AGENT_ID"] = agent_id
    configure_runtime(
        db_path=db_path,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down MCP server...")
        if _store:
            try:
                _store.close()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _init_store(db_path)
    logger.info("Mnemos MCP server starting (stdio mode)")
    mcp.run()
