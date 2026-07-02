"""
CLI entry point for Mnemos.

Commands:
    mnemos init                  Initialize a new memory database
    mnemos serve                 Start MCP server (stdio mode)
    mnemos inspect ID            Inspect a specific operational engram
    mnemos inspect --review ID   Inspect a review-only engram explicitly
    mnemos inspect --audit ID    Inspect an audit-only engram explicitly
    mnemos inspect --admin ID    Inspect an engram regardless of read visibility
    mnemos stats                 Show memory statistics
    mnemos snapshot              Print an inline Mermaid memory snapshot
    mnemos search QUERY          Search memories
    mnemos consolidate [--deep]  Run a consolidation cycle
    mnemos export [--workspace]  Export workspace files (MEMORY.md, etc.)
    mnemos setup-openclaw        Register cron jobs for OpenClaw
    mnemos bootstrap             Bootstrap a complete agent stack
    mnemos identity diff         Diff graph-derived identity against SOUL.md
    mnemos identity accept       Accept a divergence, open a new epoch
    mnemos pai-import preview        Preview a PAI source manifest import
    mnemos pai-import apply          Backup DB and apply a PAI source manifest
    mnemos pai-import watch-preview  Preview a U3c watcher manifest update
    mnemos pai-import watch-apply    Backup DB and apply a U3c watcher update
    mnemos pai-import watch-once     Run one dual-life watcher poll
    mnemos pai-import watch-plist    Write a launchd plist for watch-once
    mnemos pai-import watch-doctor   Run the Step 3 launch-readiness gate
    mnemos pai-import review-gate    Run diff-focused adversarial U3c gate
    mnemos remember CONTENT      Capture continuity from the CLI
    mnemos hermes install        Install Mnemos for Hermes Agent
    mnemos hermes quickstart     Safely install Mnemos for Hermes Agent
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config.loader import load_config
from .store.read_visibility import (
    READ_VISIBILITY_AUDIT,
    READ_VISIBILITY_OPERATIONAL,
    READ_VISIBILITY_REVIEW,
)


def _resolve_default_mode() -> str:
    """Resolve the default tool surface for ``mnemos serve``.

    Precedence (highest first):
      1. ``MNEMOS_MODE`` environment variable
      2. ``server.mode`` in ~/.mnemos/config.json (also settable via
         ``MNEMOS_SERVER_MODE``)
      3. ``"simple"``

    This lets advanced mode be set persistently in config.json — surviving
    reboot — instead of only via a volatile env var or a per-client --mode flag.
    """
    env_mode = os.environ.get("MNEMOS_MODE")
    if env_mode:
        return env_mode
    try:
        return load_config().get("server", {}).get("mode", "simple")
    except Exception:
        return "simple"


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
        default=_resolve_default_mode(),
        help="MCP tool surface to expose: 'simple' (default) or 'advanced'. "
             "Persist a machine-wide default via server.mode in "
             "~/.mnemos/config.json (or the MNEMOS_MODE env var).",
    )
    p_serve.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_serve.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_serve.add_argument("--person-id", default=None, help="Person/user scope")
    p_serve.add_argument("--project-scope", default=None, help="Project/workspace scope")

    # ── inspect ──
    p_inspect = sub.add_parser("inspect", help="Inspect a specific engram")
    p_inspect.add_argument("engram_id", help="The engram ID to inspect")
    inspect_visibility = p_inspect.add_mutually_exclusive_group()
    inspect_visibility.add_argument(
        "--review",
        action="store_const",
        const=READ_VISIBILITY_REVIEW,
        dest="read_visibility",
        help="Inspect an explicitly review-only engram",
    )
    inspect_visibility.add_argument(
        "--audit",
        action="store_const",
        const=READ_VISIBILITY_AUDIT,
        dest="read_visibility",
        help="Inspect an explicitly audit-only engram",
    )
    inspect_visibility.add_argument(
        "--admin",
        action="store_const",
        const=None,
        dest="read_visibility",
        help="Inspect an engram regardless of read visibility",
    )
    p_inspect.set_defaults(read_visibility=READ_VISIBILITY_OPERATIONAL)

    # ── stats ──
    sub.add_parser("stats", help="Show memory statistics")

    # ── snapshot ──
    p_snapshot = sub.add_parser("snapshot", help="Print an inline Mermaid memory snapshot")
    p_snapshot.add_argument("--person-id", default="user", help="Person scope")
    p_snapshot.add_argument("--project-scope", default="global", help="Project scope")
    p_snapshot.add_argument("--session-id", default="", help="Optional functional-memory session")
    p_snapshot.add_argument("-n", "--max-items", type=int, default=6)

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
    p_bootstrap = sub.add_parser("bootstrap", help="Bootstrap a turnkey memory stack")
    p_bootstrap.add_argument("--agent-name", required=True, help="Agent name (e.g., Nova)")
    p_bootstrap.add_argument("--workspace", required=True, help="Workspace directory path")
    p_bootstrap.add_argument("--user-name", default="User", help="User name (default: User)")
    p_bootstrap.add_argument("--timezone", default="America/New_York", help="Timezone for crons")
    p_bootstrap.add_argument("--api-key", default="", help="Optional LLM provider API key")
    p_bootstrap.add_argument("--llm-provider", default="openrouter", help="LLM provider")
    p_bootstrap.add_argument("--model", default="anthropic/claude-sonnet-4-5", help="Memory model")

    # ── bridge ──
    p_bridge = sub.add_parser("bridge", help="Direct memory operations")
    bridge_sub = p_bridge.add_subparsers(dest="bridge_command")
    bridge_sub.add_parser("status", help="Quick memory status")
    p_br_recall = bridge_sub.add_parser("recall", help="Retrieve memories")
    p_br_recall.add_argument("query", help="Search query")
    p_br_remember = bridge_sub.add_parser("remember", help="Encode a memory")
    p_br_remember.add_argument("content", help="Memory content")
    p_br_remember.add_argument("--impact", default="", help="What it meant")

    # ── remember ──
    p_remember = sub.add_parser(
        "remember", help="Capture continuity from the command line"
    )
    p_remember.add_argument("content", help="What to remember")
    p_remember.add_argument("--context", default="", help="Where/why this came up")
    p_remember.add_argument(
        "--importance", default="auto", help="auto, or a number from 0.0 to 1.0"
    )
    p_remember.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_remember.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_remember.add_argument("--person-id", default=None, help="Person/user scope")
    p_remember.add_argument("--project-scope", default=None, help="Project/workspace scope")

    # ── doctor ──
    p_doctor = sub.add_parser("doctor", help="Check Mnemos simple-mode readiness")
    p_doctor.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_doctor.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_doctor.add_argument("--person-id", default=None, help="Person/user scope")
    p_doctor.add_argument("--project-scope", default=None, help="Project/workspace scope")

    # ── hermes ──
    p_hermes = sub.add_parser("hermes", help="Hermes Agent identity-continuity integration")
    hermes_sub = p_hermes.add_subparsers(dest="hermes_command")
    p_hermes_install = hermes_sub.add_parser("install", help="Install Mnemos for Hermes")
    p_hermes_install.add_argument("--hermes-home", default=None, help="Hermes home directory")
    p_hermes_install.add_argument("--db-path", default=None, help="Mnemos SQLite database path")
    p_hermes_install.add_argument("--agent-id", default=None, help="Fixed Mnemos agent scope")
    p_hermes_install.add_argument("--person-id", default=None, help="Fixed person/user scope")
    p_hermes_install.add_argument("--project-scope", default=None, help="Fixed project scope")
    p_hermes_install.add_argument(
        "--mode",
        choices=("provider", "sidecar"),
        default="provider",
        help="Provider Mode uses memory.provider=mnemos; Sidecar Mode preserves the existing provider and adds MCP",
    )
    p_hermes_install.add_argument("--activate", action="store_true", help="Set memory.provider to mnemos in Provider Mode")
    p_hermes_install.add_argument("--with-mcp", action="store_true", help="Also add Mnemos to Hermes mcp_servers")
    p_hermes_install.add_argument("--mcp-name", default="mnemos", help="Hermes MCP server name")
    p_hermes_install.add_argument("--force", action="store_true", help="Replace an existing shim")
    p_hermes_install.add_argument("--dry-run", action="store_true", help="Show what would be written")
    p_hermes_quickstart = hermes_sub.add_parser(
        "quickstart",
        help="Safely install Mnemos for Hermes and run doctor",
    )
    p_hermes_quickstart.add_argument("--hermes-home", default=None, help="Hermes home directory")
    p_hermes_quickstart.add_argument("--db-path", default=None, help="Mnemos SQLite database path")
    p_hermes_quickstart.add_argument("--agent-id", default=None, help="Fixed Mnemos agent scope")
    p_hermes_quickstart.add_argument("--person-id", default=None, help="Fixed person/user scope")
    p_hermes_quickstart.add_argument("--project-scope", default=None, help="Fixed project scope")
    p_hermes_quickstart.add_argument(
        "--agent-safe",
        action="store_true",
        help="Noninteractive Sidecar Mode only; never changes memory.provider",
    )
    p_hermes_quickstart.add_argument(
        "--provider",
        action="store_true",
        help="Use Provider Mode and activate memory.provider=mnemos",
    )
    p_hermes_quickstart.add_argument(
        "--activate-provider",
        action="store_true",
        help="Alias for --provider",
    )
    p_hermes_quickstart.add_argument("--mcp-name", default="mnemos", help="Hermes MCP server name")
    p_hermes_quickstart.add_argument("--dry-run", action="store_true", help="Show what would be written")
    p_hermes_doctor = hermes_sub.add_parser("doctor", help="Check Hermes Mnemos setup")
    p_hermes_doctor.add_argument("--hermes-home", default=None, help="Hermes home directory")
    hermes_sub.add_parser("shim", help="Print the Hermes provider shim")

    # ── identity ──
    p_identity = sub.add_parser("identity", help="Computed vs declared identity")
    identity_sub = p_identity.add_subparsers(dest="identity_command")
    p_id_diff = identity_sub.add_parser(
        "diff", help="Diff graph-derived identity against SOUL.md"
    )
    p_id_diff.add_argument(
        "--soul",
        default=None,
        help="Path to SOUL.md (default: $MNEMOS_WORKSPACE/SOUL.md, then ./SOUL.md)",
    )
    p_id_diff.add_argument("--json", action="store_true", help="Emit machine-readable report")
    p_id_diff.add_argument(
        "--no-note",
        action="store_true",
        help="Do not write the continuity note that surfaces at next mnemos_context",
    )
    p_id_diff.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip optional model-assisted annotation",
    )
    p_id_diff.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_id_diff.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_id_diff.add_argument("--person-id", default=None, help="Person/user scope")
    p_id_diff.add_argument("--project-scope", default=None, help="Project/workspace scope")
    p_id_accept = identity_sub.add_parser(
        "accept", help="Accept a divergence and open a new epoch"
    )
    p_id_accept.add_argument(
        "--divergence",
        type=int,
        required=True,
        metavar="N",
        help="Divergence number from the last `identity diff` run",
    )
    p_id_accept.add_argument("--note", default="", help="Optional note recorded with the transition")
    p_id_accept.add_argument("--json", action="store_true", help="Emit machine-readable result")
    p_id_accept.add_argument("--db-path", default=argparse.SUPPRESS, help="Database path")
    p_id_accept.add_argument("--agent-id", default=argparse.SUPPRESS, help="Agent identity")
    p_id_accept.add_argument("--person-id", default=None, help="Person/user scope")
    p_id_accept.add_argument("--project-scope", default=None, help="Project/workspace scope")

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

    # ── pai-import ──
    p_pai = sub.add_parser("pai-import", help="Operator PAI import workflow")
    pai_sub = p_pai.add_subparsers(dest="pai_import_command")
    p_pai_preview = pai_sub.add_parser("preview", help="Preview a PAI source manifest")
    p_pai_preview.add_argument("--manifest", required=True, help="PAI source manifest JSON")
    p_pai_preview.add_argument("--db-path", default=argparse.SUPPRESS, help="Representative SQLite DB path")
    p_pai_preview.add_argument("--artifact", default=None, help="Write preview artifact JSON")
    p_pai_preview.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow ~/.mnemos/memory.db; intended only for a deliberate final import",
    )
    p_pai_apply = pai_sub.add_parser("apply", help="Backup DB and apply a PAI source manifest")
    p_pai_apply.add_argument("--manifest", required=True, help="PAI source manifest JSON")
    p_pai_apply.add_argument("--db-path", default=argparse.SUPPRESS, help="Representative SQLite DB path")
    p_pai_apply.add_argument("--artifact", default=None, help="Write apply artifact JSON")
    p_pai_apply.add_argument("--backup-dir", default=None, help="Directory for integrity-checked DB backup")
    p_pai_apply.add_argument(
        "--backup-keep",
        type=int,
        default=None,
        help="Keep only the newest N matching PAI backups after this apply",
    )
    p_pai_apply.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow ~/.mnemos/memory.db; intended only for a deliberate final import",
    )
    p_pai_watch_preview = pai_sub.add_parser(
        "watch-preview",
        help="Preview a U3c watcher manifest update",
    )
    p_pai_watch_preview.add_argument(
        "--manifest", required=True, help="PAI source manifest JSON"
    )
    p_pai_watch_preview.add_argument(
        "--db-path",
        default=argparse.SUPPRESS,
        help="Representative SQLite DB path",
    )
    p_pai_watch_preview.add_argument(
        "--artifact", default=None, help="Write preview artifact JSON"
    )
    p_pai_watch_preview.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow ~/.mnemos/memory.db; intended only for a deliberate watcher run",
    )
    p_pai_watch_apply = pai_sub.add_parser(
        "watch-apply",
        help="Backup DB and apply a U3c watcher manifest update",
    )
    p_pai_watch_apply.add_argument(
        "--manifest", required=True, help="PAI source manifest JSON"
    )
    p_pai_watch_apply.add_argument(
        "--db-path",
        default=argparse.SUPPRESS,
        help="Representative SQLite DB path",
    )
    p_pai_watch_apply.add_argument(
        "--artifact", default=None, help="Write apply artifact JSON"
    )
    p_pai_watch_apply.add_argument(
        "--backup-dir",
        default=None,
        help="Directory for integrity-checked DB backup",
    )
    p_pai_watch_apply.add_argument(
        "--backup-keep",
        type=int,
        default=None,
        help="Keep only the newest N matching PAI backups after this apply",
    )
    p_pai_watch_apply.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow ~/.mnemos/memory.db; intended only for a deliberate watcher run",
    )
    p_pai_watch_once = pai_sub.add_parser(
        "watch-once",
        help="Run one U3c watcher poll",
    )
    p_pai_watch_once.add_argument(
        "--manifest", required=True, help="PAI source manifest JSON"
    )
    p_pai_watch_once.add_argument(
        "--db-path",
        default=argparse.SUPPRESS,
        help="Representative SQLite DB path",
    )
    p_pai_watch_once.add_argument("--state", required=True, help="Watcher state JSON path")
    p_pai_watch_once.add_argument(
        "--artifact-dir", default=None, help="Directory for watch artifacts"
    )
    p_pai_watch_once.add_argument(
        "--backup-dir",
        default=None,
        help="Directory for integrity-checked DB backups",
    )
    p_pai_watch_once.add_argument(
        "--backup-keep",
        type=int,
        default=None,
        help="Keep only the newest N matching PAI backups after this apply",
    )
    p_pai_watch_once.add_argument(
        "--apply", action="store_true", help="Apply changed-source previews"
    )
    p_pai_watch_once.add_argument(
        "--force", action="store_true", help="Run even if source hashes are unchanged"
    )
    p_pai_watch_once.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow ~/.mnemos/memory.db; intended only for a deliberate watcher run",
    )
    p_pai_watch_plist = pai_sub.add_parser(
        "watch-plist",
        help="Write a launchd plist for U3c watch-once",
    )
    p_pai_watch_plist.add_argument(
        "--manifest", required=True, help="PAI source manifest JSON"
    )
    p_pai_watch_plist.add_argument("--db-path", required=True, help="SQLite DB path")
    p_pai_watch_plist.add_argument("--state", required=True, help="Watcher state JSON path")
    p_pai_watch_plist.add_argument(
        "--artifact-dir", required=True, help="Directory for watch artifacts"
    )
    p_pai_watch_plist.add_argument(
        "--backup-dir",
        required=True,
        help="Directory for integrity-checked DB backups",
    )
    p_pai_watch_plist.add_argument("--plist", required=True, help="Output launchd plist path")
    p_pai_watch_plist.add_argument("--label", default=None, help="launchd label")
    p_pai_watch_plist.add_argument("--interval-seconds", type=int, default=60)
    p_pai_watch_plist.add_argument(
        "--backup-keep",
        type=int,
        default=None,
        help="Include bounded backup retention in generated ProgramArguments",
    )
    p_pai_watch_plist.add_argument("--python", default=None, help="Python executable for launchd")
    p_pai_watch_plist.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Include --allow-live-db in generated ProgramArguments",
    )
    p_pai_watch_doctor = pai_sub.add_parser(
        "watch-doctor",
        help="Run U3c Step 3 launch-readiness gate",
    )
    p_pai_watch_doctor.add_argument(
        "--manifest", required=True, help="PAI source manifest JSON"
    )
    p_pai_watch_doctor.add_argument(
        "--db-path",
        default=argparse.SUPPRESS,
        help="Representative SQLite DB path",
    )
    p_pai_watch_doctor.add_argument("--state", required=True, help="Watcher state JSON path")
    p_pai_watch_doctor.add_argument(
        "--artifact-dir", required=True, help="Directory for watch artifacts"
    )
    p_pai_watch_doctor.add_argument(
        "--backup-dir",
        required=True,
        help="Directory for integrity-checked DB backups",
    )
    p_pai_watch_doctor.add_argument(
        "--backup-keep",
        type=int,
        required=True,
        help="Required bounded backup retention count for launch readiness",
    )
    p_pai_watch_doctor.add_argument(
        "--plist", required=True, help="Existing launchd plist to lint"
    )
    p_pai_watch_doctor.add_argument("--python", default=None, help="Python executable")
    p_pai_watch_doctor.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow live ~/.mnemos DB paths during explicit launch-gate checks",
    )
    p_pai_review_gate = pai_sub.add_parser(
        "review-gate",
        help="Run diff-focused adversarial U3c review gate",
    )
    p_pai_review_gate.add_argument(
        "--base-ref",
        required=True,
        help="Explicit non-HEAD git ref to compare against for changed-file review",
    )
    p_pai_review_gate.add_argument(
        "--intent",
        default=None,
        help="Intent artifact path for the U3c launch diff",
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
        "snapshot": _cmd_snapshot,
        "search": _cmd_search,
        "consolidate": _cmd_consolidate,
        "export": _cmd_export,
        "setup-openclaw": _cmd_setup_openclaw,
        "bootstrap": _cmd_bootstrap,
        "substrate-tick": _cmd_substrate_tick,
        "index": _cmd_index,
        "bridge": _cmd_bridge,
        "remember": _cmd_remember,
        "doctor": _cmd_doctor,
        "hermes": _cmd_hermes,
        "identity": _cmd_identity,
        "mcp": _cmd_mcp,
        "pai-import": _cmd_pai_import,
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
    engram = store.get_engram(
        args.engram_id,
        read_visibility=getattr(args, "read_visibility", READ_VISIBILITY_OPERATIONAL),
    )
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
    print(f"Functional active:   {stats.get('functional_active', 0)}")
    print(f"Functional pinned:   {stats.get('functional_pinned', 0)}")
    print(f"Needs confirmation:  {stats.get('functional_needs_confirmation', 0)}")
    print(f"Active sessions:     {stats.get('functional_sessions_active', 0)}")
    print(f"Hypomnema active:    {stats.get('hypomnema_active', 0)}")
    print(f"Reconsolidations:    {stats.get('reconsolidation_events', 0)}")
    if "accessibility_avg" in stats:
        print(f"Avg accessibility:   {stats['accessibility_avg']:.3f}")
        print(f"Min accessibility:   {stats['accessibility_min']:.3f}")
        print(f"Max accessibility:   {stats['accessibility_max']:.3f}")

    store.close()
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Print an inline visual snapshot."""
    store = _get_store(args)
    from .interface.visual_snapshot import build_memory_visual_snapshot

    print(
        build_memory_visual_snapshot(
            store,
            agent_id=args.agent_id,
            person_id=args.person_id,
            project_scope=args.project_scope,
            session_id=args.session_id,
            max_items=args.max_items,
        )
    )
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
    """Bootstrap a turnkey memory stack."""
    from .setup.bootstrap import bootstrap, print_result

    result = bootstrap(
        agent_name=args.agent_name,
        workspace=args.workspace,
        user_name=args.user_name,
        timezone=args.timezone,
        api_key=args.api_key,
        llm_provider=args.llm_provider,
        model=args.model,
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


def _cmd_remember(args: argparse.Namespace) -> int:
    """Capture continuity via the same path as the mnemos_capture MCP tool."""
    from .simple_runtime import MnemosRuntime

    runtime = MnemosRuntime(
        db_path=getattr(args, "db_path", None),
        agent_id=getattr(args, "agent_id", None),
        person_id=getattr(args, "person_id", None),
        project_scope=getattr(args, "project_scope", None),
        use_dedicated_model=False,  # CLI capture stays local and deterministic
    )
    try:
        print(
            runtime.capture(
                args.content, context=args.context, importance=args.importance
            )
        )
        return 0
    finally:
        runtime.close()


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
        _print_affinity_status()
        print(f"Simple tools: {', '.join(SIMPLE_TOOL_NAMES)}")
        print()
        print(packet)
        return 0
    finally:
        runtime.close()


def _cmd_identity(args: argparse.Namespace) -> int:
    """Computed-vs-declared identity operations."""
    from .identity_diff import run_accept_command, run_diff_command

    if getattr(args, "identity_command", None) == "diff":
        return run_diff_command(args)
    if getattr(args, "identity_command", None) == "accept":
        return run_accept_command(args)
    print("Usage: mnemos identity {diff|accept}", file=sys.stderr)
    return 1


def _cmd_pai_import(args: argparse.Namespace) -> int:
    """Operator workflow for PAI source imports."""
    command = getattr(args, "pai_import_command", None)
    valid_commands = {
        "preview",
        "apply",
        "watch-preview",
        "watch-apply",
        "watch-once",
        "watch-plist",
        "watch-doctor",
        "review-gate",
    }
    if command not in valid_commands:
        print(
            "Usage: mnemos pai-import "
            "{preview|apply|watch-preview|watch-apply|watch-once|watch-plist|watch-doctor|review-gate}",
            file=sys.stderr,
        )
        return 1

    db_path = getattr(args, "db_path", None)
    if command not in {"watch-plist", "review-gate"} and not db_path:
        print(
            "mnemos pai-import requires --db-path; use a representative test DB",
            file=sys.stderr,
        )
        return 1

    try:
        if command == "review-gate":
            from .importer import DEFAULT_U3C_INTENT_PATH, run_pai_diff_review_gate

            report = run_pai_diff_review_gate(
                repo_root=Path.cwd(),
                base_ref=args.base_ref,
                intent_path=args.intent or DEFAULT_U3C_INTENT_PATH,
            )
            _print_pai_review_gate_report(report)
            return 0 if report.ok else 1

        if command == "watch-plist":
            from .importer import DEFAULT_WATCH_LABEL, write_pai_watch_launchd_plist

            plist = write_pai_watch_launchd_plist(
                plist_path=args.plist,
                manifest_path=args.manifest,
                db_path=args.db_path,
                state_path=args.state,
                artifact_dir=args.artifact_dir,
                backup_dir=args.backup_dir,
                label=args.label or DEFAULT_WATCH_LABEL,
                interval_seconds=args.interval_seconds,
                backup_keep=args.backup_keep,
                python_executable=args.python,
                allow_live_db=args.allow_live_db,
            )
            print(f"PAI watch launchd plist: {plist}")
            return 0

        if command == "watch-doctor":
            from .importer import run_pai_watch_doctor

            report = run_pai_watch_doctor(
                manifest_path=args.manifest,
                db_path=db_path,
                state_path=args.state,
                artifact_dir=args.artifact_dir,
                backup_dir=args.backup_dir,
                backup_keep=args.backup_keep,
                plist_path=args.plist,
                python_executable=args.python,
                allow_live_db=args.allow_live_db,
            )
            _print_pai_watch_doctor_report(report)
            return 0 if report.ok else 1

        if command == "preview":
            from .importer import ACTION_ERROR, preview_pai_manifest

            run = preview_pai_manifest(
                db_path=db_path,
                manifest_path=args.manifest,
                artifact_path=args.artifact,
                allow_live_db=args.allow_live_db,
            )
            _print_pai_operator_run(run, db_path=db_path)
            return 1 if run.preview.counts.get(ACTION_ERROR, 0) else 0

        if command == "watch-preview":
            from .importer import ACTION_ERROR, preview_pai_watch_manifest

            run = preview_pai_watch_manifest(
                db_path=db_path,
                manifest_path=args.manifest,
                artifact_path=args.artifact,
                allow_live_db=args.allow_live_db,
            )
            _print_pai_operator_run(run, db_path=db_path)
            return 1 if run.preview.counts.get(ACTION_ERROR, 0) else 0

        if command == "watch-once":
            from .importer import ACTION_ERROR, pai_watch_once

            watch = pai_watch_once(
                db_path=db_path,
                manifest_path=args.manifest,
                state_path=args.state,
                artifact_dir=args.artifact_dir,
                backup_dir=args.backup_dir,
                backup_keep=args.backup_keep,
                apply=args.apply,
                force=args.force,
                allow_live_db=args.allow_live_db,
            )
            if watch.operator_run is None:
                print("PAI watch once")
                print("--------------")
                print(f"Job:      {watch.manifest.job_id}")
                print(f"State:    {watch.state_path}")
                print("Changed:  none")
                return 0
            _print_pai_operator_run(watch.operator_run, db_path=db_path)
            if not args.apply:
                return 1 if watch.operator_run.preview.counts.get(ACTION_ERROR, 0) else 0
            return 0

        if command == "apply":
            from .importer import apply_pai_manifest

            run = apply_pai_manifest(
                db_path=db_path,
                manifest_path=args.manifest,
                artifact_path=args.artifact,
                backup_dir=args.backup_dir,
                backup_keep=args.backup_keep,
                allow_live_db=args.allow_live_db,
            )
            _print_pai_operator_run(run, db_path=db_path)
            return 0

        from .importer import apply_pai_watch_manifest

        run = apply_pai_watch_manifest(
            db_path=db_path,
            manifest_path=args.manifest,
            artifact_path=args.artifact,
            backup_dir=args.backup_dir,
            backup_keep=args.backup_keep,
            allow_live_db=args.allow_live_db,
        )
        _print_pai_operator_run(run, db_path=db_path)
        return 0
    except Exception as exc:
        print(f"PAI import {command} failed: {exc}", file=sys.stderr)
        return 1


def _print_pai_operator_run(run, *, db_path: str) -> None:
    titles = {
        "preview": "PAI import preview",
        "apply": "PAI import apply",
        "watch-preview": "PAI watch preview",
        "watch-apply": "PAI watch apply",
    }
    title = titles.get(run.mode, f"PAI import {run.mode}")
    print(title)
    print("-" * len(title))
    print(f"Job:      {run.manifest.job_id}")
    print(f"DB:       {Path(db_path).expanduser()}")
    print(f"Manifest: {run.manifest.path.expanduser()}")
    if run.backup_path is not None:
        print(f"Backup:   {run.backup_path}")
    if run.artifact_path is not None:
        print(f"Artifact: {run.artifact_path}")
    print(f"Rows:     {len(run.result.rows if run.result is not None else run.preview.rows)}")
    print(f"Counts:   {_format_counts(run.counts)}")


def _print_pai_watch_doctor_report(report) -> None:
    print("PAI watch doctor")
    print("----------------")
    for check in report.checks:
        print(f"[{check.status}] {check.ident:<3} {check.label}: {check.evidence}")
    passed = sum(1 for check in report.checks if check.status == "PASS")
    failed = sum(1 for check in report.checks if check.status == "FAIL")
    skipped = sum(1 for check in report.checks if check.status == "SKIP")
    print()
    print(f"Summary: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"Verdict: {'GREEN' if report.ok else 'RED'}")


def _print_pai_review_gate_report(report) -> None:
    print("PAI diff review gate")
    print("--------------------")
    print(f"Base:     {report.base_ref}")
    print(f"Intent:   {report.intent_path}")
    print(f"Changed:  {len(report.changed_files)} file(s)")
    for path in report.changed_files:
        print(f"  - {path}")
    if report.findings:
        print()
        print("findings{id,severity,file,description,required_proof,status,action}:")
        for finding in report.findings:
            print(
                f"  {finding.ident},{finding.severity},{finding.file},"
                f"{finding.description},{finding.required_proof},"
                f"{finding.status},{finding.action}"
            )
    print()
    print(f"Verdict: {'GREEN' if report.ok else 'RED'}")


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))


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


def _cmd_hermes(args: argparse.Namespace) -> int:
    """Hermes integration helper commands."""
    from .integrations.hermes.installer import (
        build_diagnostics,
        format_diagnostics,
        install_hermes_plugin,
        quickstart_hermes,
        render_plugin_shim,
    )

    if args.hermes_command == "install":
        result = install_hermes_plugin(
            hermes_home=args.hermes_home,
            db_path=args.db_path,
            agent_id=args.agent_id,
            person_id=args.person_id,
            project_scope=args.project_scope,
            mode=args.mode,
            configure_mcp=True if args.with_mcp else None,
            mcp_server_name=args.mcp_name,
            activate=args.activate,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(result.summary())
        return 0 if not result.warnings else 1

    if args.hermes_command == "quickstart":
        result = quickstart_hermes(
            hermes_home=args.hermes_home,
            db_path=args.db_path,
            agent_id=args.agent_id,
            person_id=args.person_id,
            project_scope=args.project_scope,
            provider=args.provider,
            activate_provider=args.activate_provider,
            agent_safe=args.agent_safe,
            mcp_server_name=args.mcp_name,
            dry_run=args.dry_run,
        )
        print(result.summary())
        return 0 if result.ok else 1

    if args.hermes_command == "doctor":
        print(format_diagnostics(build_diagnostics(args.hermes_home)))
        return 0

    if args.hermes_command == "shim":
        print(render_plugin_shim())
        return 0

    print("Usage: mnemos hermes {install|quickstart|doctor|shim}", file=sys.stderr)
    return 1


def _print_affinity_status() -> None:
    """Doctor section: who would perform this agent's maintenance."""
    from .llm import resolve_affinity_status

    status = resolve_affinity_status()
    agent = status["agent_model"] or "(unset — set MNEMOS_AGENT_MODEL)"
    if status["substrate_resolved"]:
        substrate = f"{status['substrate_model']} via {status['substrate_provider']}"
    else:
        substrate = "(none — rule-based local passes only)"
    verdict = "ok" if status["allowed"] else "BLOCKED"
    print(f"Affinity:     policy={status['policy']} agent={agent}")
    print(f"              substrate={substrate}")
    print(f"              verdict={verdict}: {status['message']}")


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
