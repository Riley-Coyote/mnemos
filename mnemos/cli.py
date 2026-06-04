"""
CLI entry point for Mnemos.

Commands:
    mnemos init                  Initialize a new memory database
    mnemos serve                 Start MCP server (stdio mode)
    mnemos inspect ID            Inspect a specific engram
    mnemos stats                 Show memory statistics
    mnemos search QUERY          Search memories
    mnemos consolidate [--deep]  Run a consolidation cycle
    mnemos export [--workspace]  Export workspace files (MEMORY.md, etc.)
    mnemos setup-openclaw        Register cron jobs for OpenClaw
    mnemos bootstrap             Bootstrap a complete agent stack
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mnemos",
        description="Mnemos: Living Memory Architecture for Autonomous AI Agents",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the SQLite database (default: ~/.mnemos/memory.db)",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Agent identifier (default: env/config/default)",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── init ──
    sub.add_parser("init", help="Initialize a new memory database")

    # ── serve ──
    p_serve = sub.add_parser("serve", help="Start MCP server (stdio mode)")
    p_serve.add_argument(
        "--mode",
        choices=("simple", "advanced"),
        default=os.environ.get("MNEMOS_MODE", "simple"),
        help="MCP tool surface to expose (default: simple)",
    )
    p_serve.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_serve.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_serve.add_argument("--person-id", default=None, help="Person/user scope")
    p_serve.add_argument("--project-scope", default=None, help="Project/workspace scope")

    # ── inspect ──
    p_inspect = sub.add_parser("inspect", help="Inspect a specific engram")
    p_inspect.add_argument("engram_id", help="The engram ID to inspect")

    # ── stats ──
    sub.add_parser("stats", help="Show memory statistics")

    # ── search ──
    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-n", "--max-results", type=int, default=10)

    # ── consolidate ──
    p_cons = sub.add_parser("consolidate", help="Run a consolidation cycle")
    p_cons.add_argument("--deep", action="store_true", help="Run deep cycle")

    # ── export ──
    p_export = sub.add_parser("export", help="Export workspace files")
    p_export.add_argument(
        "--workspace", default=".", help="Output directory (default: current dir)"
    )

    # ── setup-openclaw ──
    p_setup = sub.add_parser("setup-openclaw", help="Register OpenClaw cron jobs")
    p_setup.add_argument("--agent", default="main", help="OpenClaw agent ID")
    p_setup.add_argument("--dry-run", action="store_true", help="Show what would be registered")

    # ── substrate-tick ──
    sub.add_parser("substrate-tick", help="Run one cognitive substrate tick")

    # ── index ──
    p_index = sub.add_parser("index", help="Run session indexer")
    p_index.add_argument("--backfill", action="store_true", help="Index last 24h of sessions")

    # ── bootstrap ──
    p_bootstrap = sub.add_parser("bootstrap", help="Bootstrap a complete agent stack")
    p_bootstrap.add_argument("--agent-name", required=True, help="Agent name (e.g., Nova)")
    p_bootstrap.add_argument("--workspace", required=True, help="Workspace directory path")
    p_bootstrap.add_argument("--user-name", default="User", help="User name (default: User)")
    p_bootstrap.add_argument("--timezone", default="America/New_York", help="Timezone for crons")

    # ── bridge ──
    p_bridge = sub.add_parser("bridge", help="Direct memory operations")
    bridge_sub = p_bridge.add_subparsers(dest="bridge_command")
    bridge_sub.add_parser("status", help="Quick memory status")
    p_br_recall = bridge_sub.add_parser("recall", help="Retrieve memories")
    p_br_recall.add_argument("query", help="Search query")
    p_br_remember = bridge_sub.add_parser("remember", help="Encode a memory")
    p_br_remember.add_argument("content", help="Memory content")
    p_br_remember.add_argument("--impact", default="", help="What it meant")

    # ── doctor ──
    p_doctor = sub.add_parser("doctor", help="Check Mnemos simple-mode readiness")
    p_doctor.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_doctor.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_doctor.add_argument("--person-id", default=None, help="Person/user scope")
    p_doctor.add_argument("--project-scope", default=None, help="Project/workspace scope")

    # ── mcp ──
    p_mcp = sub.add_parser("mcp", help="MCP client helpers")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command")
    p_mcp_install = mcp_sub.add_parser("install", help="Print or write MCP client config")
    p_mcp_install.add_argument(
        "client",
        choices=("claude", "cursor", "codex", "generic"),
        help="Client config style",
    )
    p_mcp_install.add_argument("--name", default="mnemos", help="MCP server name")
    p_mcp_install.add_argument("--mode", choices=("simple", "advanced"), default="simple")
    p_mcp_install.add_argument("--agent-id", default=None, help="Optional agent identity")
    p_mcp_install.add_argument("--db-path", default=None, help="Optional database path")
    p_mcp_install.add_argument(
        "--write",
        action="store_true",
        help="Write config where safely supported instead of printing a snippet",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    handlers = {
        "init": _cmd_init,
        "serve": _cmd_serve,
        "inspect": _cmd_inspect,
        "stats": _cmd_stats,
        "search": _cmd_search,
        "consolidate": _cmd_consolidate,
        "export": _cmd_export,
        "setup-openclaw": _cmd_setup_openclaw,
        "bootstrap": _cmd_bootstrap,
        "substrate-tick": _cmd_substrate_tick,
        "index": _cmd_index,
        "bridge": _cmd_bridge,
        "doctor": _cmd_doctor,
        "mcp": _cmd_mcp,
    }

    handler = handlers.get(args.command)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


def _resolve_db_path(args: argparse.Namespace) -> str:
    """Resolve CLI DB path with backwards-compatible default."""
    return (
        getattr(args, "db_path", None)
        or os.environ.get("MNEMOS_DB_PATH")
        or "~/.mnemos/memory.db"
    )


def _resolve_agent_id(args: argparse.Namespace) -> str:
    """Resolve CLI agent identity with backwards-compatible default."""
    return (
        getattr(args, "agent_id", None)
        or os.environ.get("MNEMOS_AGENT_ID")
        or "default"
    )


def _get_store(args: argparse.Namespace):
    """Create or open the engram store."""
    from .store.sqlite_store import EngramStore
    return EngramStore(_resolve_db_path(args))


def _cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new memory database."""
    db_path = Path(_resolve_db_path(args)).expanduser()
    if db_path.exists():
        print(f"Database already exists: {db_path}")
        print("Mnemos is ready.")
        return 0

    store = _get_store(args)
    store.close()
    print(f"Initialized Mnemos database: {db_path}")
    print("Run 'mnemos stats' to verify.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start MCP server."""
    try:
        if getattr(args, "mode", "simple") == "advanced":
            from .mcp_server import run_server

            run_server(
                db_path=_resolve_db_path(args),
                agent_id=_resolve_agent_id(args),
                person_id=getattr(args, "person_id", None),
                project_scope=getattr(args, "project_scope", None),
            )
        else:
            from .simple_mcp import run_simple_server

            run_simple_server(
                db_path=getattr(args, "db_path", None),
                agent_id=getattr(args, "agent_id", None),
                person_id=getattr(args, "person_id", None),
                project_scope=getattr(args, "project_scope", None),
            )
        return 0
    except ImportError:
        print("MCP server requires the 'mcp' package: pip install mcp", file=sys.stderr)
        return 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect a specific engram."""
    store = _get_store(args)
    engram = store.get_engram(args.engram_id)
    if engram is None:
        print(f"Engram not found: {args.engram_id}", file=sys.stderr)
        store.close()
        return 1

    print(f"ID:            {engram.id}")
    print(f"Content:       {engram.content}")
    print(f"Kind:          {engram.kind}")
    print(f"Tags:          {', '.join(engram.tags) or '(none)'}")
    print(f"State:         {engram.state}")
    print(f"Resolution:    {engram.resolution}")
    print(f"Strength:      {engram.strength:.4f}")
    print(f"Stability:     {engram.stability:.4f}")
    print(f"Accessibility: {engram.accessibility:.4f}")
    print(f"Confidence:    {engram.source.confidence} ({engram.source.confidence_source})")
    print(f"Created:       {engram.created_at}")
    print(f"Last accessed: {engram.last_accessed}")
    print(f"Access count:  {engram.access_count}")
    print(f"Reconsolidations: {engram.reconsolidation_count}")
    print(f"Connections:   {len(engram.connections)}")
    for c in engram.connections:
        print(f"  → {c.target_id[:25]}... ({c.relation}, strength={c.strength:.2f})")
    print(f"Versions:      {len(engram.versions)}")
    for v in engram.versions:
        print(f"  v{v.version_num}: {v.change_reason} at {v.changed_at}")
    if engram.content != engram.content_at_encoding:
        print(f"Original:      {engram.content_at_encoding[:100]}...")

    store.close()
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """Show memory statistics."""
    store = _get_store(args)
    agent_id = _resolve_agent_id(args)
    stats = store.get_stats(agent_id)

    print(f"Mnemos Stats (agent: {agent_id})")
    print(f"{'─' * 40}")
    print(f"Active engrams:      {stats.get('engrams_active', 0)}")
    print(f"Dormant engrams:     {stats.get('engrams_dormant', 0)}")
    print(f"Archived engrams:    {stats.get('archived', 0)}")
    print(f"Connections:         {stats.get('connections', 0)}")
    print(f"Active beliefs:      {stats.get('beliefs_active', 0)}")
    print(f"Reconsolidations:    {stats.get('reconsolidation_events', 0)}")
    if "accessibility_avg" in stats:
        print(f"Avg accessibility:   {stats['accessibility_avg']:.3f}")
        print(f"Min accessibility:   {stats['accessibility_min']:.3f}")
        print(f"Max accessibility:   {stats['accessibility_max']:.3f}")

    store.close()
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    """Search memories."""
    store = _get_store(args)
    from .retrieval.reactive import ReactiveRetriever

    retriever = ReactiveRetriever(store)
    results = retriever.retrieve(
        cue=args.query,
        agent_id=_resolve_agent_id(args),
        max_results=args.max_results,
    )

    if not results:
        print("No memories found.")
        store.close()
        return 0

    for r in results:
        content = r.engram.content
        if len(content) > 80:
            content = content[:77] + "..."
        print(f"[{r.score:.3f}] {content}")
        print(f"         id={r.engram.id[:25]}... kind={r.engram.kind} path={r.retrieval_path}")

    store.close()
    return 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    """Run a consolidation cycle."""
    store = _get_store(args)
    from .consolidation.daemon import ConsolidationDaemon
    from .llm import create_client

    llm_client = create_client()
    daemon = ConsolidationDaemon(store=store, config={}, llm_client=llm_client)
    label = "deep" if args.deep else "shallow"
    print(f"Running {label} consolidation...")

    stats = daemon.run_cycle(deep=args.deep, agent_id=_resolve_agent_id(args))

    print(f"Passes: {', '.join(stats.get('passes_run', []))}")
    if "decay" in stats:
        d = stats["decay"]
        print(f"  Decay: {d.get('engrams_decayed', 0)} decayed, {d.get('engrams_archived', 0)} archived")
    if "connection_discovery" in stats:
        cd = stats["connection_discovery"]
        print(f"  Connections: {cd.get('connections_created', 0)} created, {cd.get('connections_strengthened', 0)} strengthened")
    if "softening" in stats:
        s = stats["softening"]
        print(f"  Softening: {s.get('engrams_softened', 0)} softened")
    if "belief_review" in stats:
        br = stats["belief_review"]
        print(f"  Beliefs: {br.get('beliefs_reviewed', 0)} reviewed")
    if "reflection" in stats:
        ref = stats["reflection"]
        print(f"  Reflection: {ref.get('thoughts_generated', 0)} thoughts, narrative={'updated' if ref.get('narrative_updated') else 'unchanged'}")

    # Check for errors
    errors = [k for k in stats if k.endswith("_error")]
    for e in errors:
        print(f"  ERROR ({e}): {stats[e]}", file=sys.stderr)

    store.close()
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Export workspace files."""
    store = _get_store(args)
    from .interface.openclaw_export import OpenClawExporter

    exporter = OpenClawExporter(store, args.workspace)
    result = exporter.export_all(_resolve_agent_id(args))

    for path, size in result.items():
        print(f"  Wrote {path} ({size} bytes)")

    store.close()
    return 0


def _cmd_setup_openclaw(args: argparse.Namespace) -> int:
    """Register OpenClaw cron jobs."""
    from .openclaw_cron import generate_cron_jobs, install_cron_jobs

    jobs = generate_cron_jobs(agent_id=args.agent)

    if args.dry_run:
        print("Would register the following cron jobs:")
        for job in jobs:
            print(f"  {job['name']}: {job['schedule']['expr']} → {job['payload']['message'][:60]}...")
        return 0

    result = install_cron_jobs(jobs)
    if result["success"]:
        print(f"Registered {result['jobs_added']} cron jobs for agent '{args.agent}'")
    else:
        print(f"Failed: {result['error']}", file=sys.stderr)
        return 1

    return 0


def _cmd_substrate_tick(args: argparse.Namespace) -> int:
    """Run one cognitive substrate tick."""
    try:
        from .substrate.tick import Substrate
        from .substrate.config import SubstrateConfig

        config = SubstrateConfig(
            agent_id=_resolve_agent_id(args),
            db_path=_resolve_db_path(args),
        )
        substrate = Substrate(config)
        print(f"Running substrate tick (agent: {_resolve_agent_id(args)})...")
        result = substrate.tick()
        print(f"Tick complete: {json.dumps(result, indent=2, default=str)}")
        return 0
    except ImportError as e:
        print(f"Substrate not available: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Substrate tick failed: {e}", file=sys.stderr)
        return 1


def _cmd_index(args: argparse.Namespace) -> int:
    """Run session indexer."""
    try:
        from .indexer.session_indexer import SessionIndexer

        indexer = SessionIndexer(
            agent_id=_resolve_agent_id(args),
            db_path=_resolve_db_path(args),
        )
        if args.backfill:
            print("Running backfill (last 24h)...")
            result = indexer.backfill()
        else:
            print("Running indexer...")
            result = indexer.run()
        print(f"Indexed {result.get('sessions_processed', 0)} sessions, "
              f"{result.get('memories_created', 0)} memories created")
        return 0
    except ImportError as e:
        print(f"Indexer not available: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Indexer failed: {e}", file=sys.stderr)
        return 1


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    """Bootstrap a complete agent stack."""
    from .setup.bootstrap import bootstrap, print_result

    result = bootstrap(
        agent_name=args.agent_name,
        workspace=args.workspace,
        user_name=args.user_name,
        timezone=args.timezone,
    )

    print_result(result)
    return 1 if result["errors"] else 0


def _cmd_bridge(args: argparse.Namespace) -> int:
    """Direct memory operations via bridge."""
    from .bridge import MnemosBridge

    bridge = MnemosBridge(agent_id=_resolve_agent_id(args), db_path=_resolve_db_path(args))

    if args.bridge_command == "status":
        print(bridge.status())
    elif args.bridge_command == "recall":
        print(bridge.recall(args.query))
    elif args.bridge_command == "remember":
        print(bridge.remember(args.content, impact=args.impact))
    else:
        print("Usage: mnemos bridge {status|recall|remember}", file=sys.stderr)
        return 1
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Check simple-mode readiness."""
    from .simple_runtime import MnemosRuntime, SIMPLE_TOOL_NAMES

    runtime = MnemosRuntime(
        db_path=getattr(args, "db_path", None),
        agent_id=getattr(args, "agent_id", None),
        person_id=getattr(args, "person_id", None),
        project_scope=getattr(args, "project_scope", None),
    )
    try:
        packet = runtime.context()
        print("Mnemos Doctor")
        print("-" * 40)
        print(f"Agent:       {runtime.scope.agent_id}")
        print(f"Person:      {runtime.scope.person_id}")
        print(f"Project:     {runtime.scope.project_scope}")
        print(f"Database:    {runtime.db_path}")
        print(f"DB exists:    {'yes' if runtime.db_path.exists() else 'no'}")
        print(f"MCP SDK:      {'yes' if _mcp_available() else 'no'}")
        print(f"Model:        {'dedicated provider configured' if runtime.has_dedicated_model else 'local baseline only'}")
        print(f"Simple tools: {', '.join(SIMPLE_TOOL_NAMES)}")
        print()
        print(packet)
        return 0
    finally:
        runtime.close()


def _cmd_mcp(args: argparse.Namespace) -> int:
    """MCP client helper commands."""
    if args.mcp_command != "install":
        print("Usage: mnemos mcp install {claude|cursor|codex|generic}", file=sys.stderr)
        return 1

    snippet = _mcp_server_snippet(
        name=args.name,
        mode=args.mode,
        agent_id=args.agent_id,
        db_path=args.db_path,
    )

    if args.write and args.client == "claude":
        path = _claude_desktop_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with open(path) as f:
                config = json.load(f)
        else:
            config = {}
        servers = config.setdefault("mcpServers", {})
        servers.update(snippet)
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Installed Mnemos MCP config at {path}")
        print("Restart Claude Desktop to expose the new tools.")
        return 0

    if args.write:
        print(
            f"--write is currently supported for Claude Desktop only. Printing {args.client} config instead.",
            file=sys.stderr,
        )

    if args.client == "codex":
        command = _mnemos_command()
        parts = ["codex", "mcp", "add", args.name, "--", command, "serve", "--mode", args.mode]
        if args.agent_id:
            parts.extend(["--agent-id", args.agent_id])
        if args.db_path:
            parts.extend(["--db-path", args.db_path])
        print(" ".join(parts))
        return 0

    print(json.dumps({"mcpServers": snippet}, indent=2))
    if args.client == "claude":
        print()
        print(f"Claude Desktop config path: {_claude_desktop_config_path()}")
        print("Re-run with --write to merge this server into that file.")
    return 0


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
    except Exception:
        return False
    return True


def _mnemos_command() -> str:
    return shutil.which("mnemos") or "mnemos"


def _mcp_server_snippet(
    *,
    name: str,
    mode: str,
    agent_id: str | None = None,
    db_path: str | None = None,
) -> dict:
    args = ["serve", "--mode", mode]
    env = {}
    if agent_id:
        env["MNEMOS_AGENT_ID"] = agent_id
    if db_path:
        env["MNEMOS_DB_PATH"] = db_path
    server = {
        "command": _mnemos_command(),
        "args": args,
    }
    if env:
        server["env"] = env
    return {name: server}


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


if __name__ == "__main__":
    sys.exit(main())
