"""
CLI entry point for Mnemos.

Commands:
    mnemos init                  Initialize a new memory database
    mnemos serve                 Start MCP server (stdio mode)
    mnemos inspect ID            Inspect a specific engram
    mnemos stats                 Show memory statistics
    mnemos snapshot              Print an inline Mermaid memory snapshot
    mnemos search QUERY          Search memories
    mnemos consolidate [--deep]  Run a consolidation cycle
    mnemos export [--workspace]  Export workspace files (MEMORY.md, etc.)
    mnemos setup-openclaw        Register cron jobs for OpenClaw
    mnemos bootstrap             Bootstrap a complete agent stack
    mnemos identity diff         Diff graph-derived identity against SOUL.md
    mnemos identity accept       Accept a divergence, open a new epoch
    mnemos remember CONTENT      Capture durable continuity from the CLI
    mnemos hermes install        Install Mnemos for Hermes Agent
    mnemos hermes quickstart     Safely install Mnemos for Hermes Agent
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config.loader import load_config


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
        "remember", help="Capture durable continuity from the command line"
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

    # ── hook ──
    p_hook = sub.add_parser(
        "hook", help="Emit agent-harness hook payloads (used by installed hooks)"
    )
    hook_sub = p_hook.add_subparsers(dest="hook_command")
    p_hook_start = hook_sub.add_parser(
        "session-start", help="Print the SessionStart continuity payload as JSON"
    )
    p_hook_start.add_argument("--agent-id", default=None, help="Agent identity")
    p_hook_start.add_argument("--person-id", default=None, help="Person/relationship scope")
    p_hook_start.add_argument("--project-scope", default=None, help="Project scope")
    p_hook_start.add_argument("--db-path", default=None, help="Database path")
    p_hook_start.add_argument(
        "--query",
        default="what should I know to continue our work?",
        help="Retrieval cue used to select long-term memories",
    )
    p_hook_start.add_argument(
        "--token-budget", type=int, default=2600, help="Approximate packet size"
    )
    p_hook_start.add_argument(
        "--include-graph",
        action="store_true",
        help=(
            "Also inject long-term graph recall. Off by default: the packet "
            "carries scoped continuity, which is what the agent cannot "
            "reconstruct from anywhere else."
        ),
    )

    # ── hooks ──
    p_hooks = sub.add_parser("hooks", help="Install session-start memory injection")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")
    p_hooks_install = hooks_sub.add_parser(
        "install", help="Print or write the SessionStart hook config"
    )
    p_hooks_install.add_argument(
        "client", nargs="?", default="claude-code", choices=("claude-code",),
        help="Agent harness to install the hook for",
    )
    p_hooks_install.add_argument("--agent-id", default=None, help="Agent identity")
    p_hooks_install.add_argument("--person-id", default=None, help="Person/relationship scope")
    p_hooks_install.add_argument("--project-scope", default=None, help="Project scope")
    p_hooks_install.add_argument("--db-path", default=None, help="Database path")
    p_hooks_install.add_argument("--timeout", type=int, default=15, help="Hook timeout seconds")
    p_hooks_install.add_argument(
        "--settings", default=None, help="Settings file to write (default ~/.claude/settings.json)"
    )
    p_hooks_install.add_argument(
        "--write", action="store_true", help="Write the settings file instead of printing it"
    )

    # ── daemon ──
    p_daemon = sub.add_parser(
        "daemon", help="Schedule background maintenance with the OS scheduler"
    )
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command")
    for _name, _help in (
        ("install", "Print or write the scheduled maintenance jobs"),
        ("status", "Show which maintenance jobs are scheduled"),
        ("uninstall", "Remove the scheduled maintenance jobs"),
    ):
        _p = daemon_sub.add_parser(_name, help=_help)
        _p.add_argument("--agent-id", default=None, help="Agent identity")
        _p.add_argument("--person-id", default=None, help="Person/relationship scope")
        _p.add_argument("--project-scope", default=None, help="Project scope")
        _p.add_argument("--db-path", default=None, help="Database path")
        if _name != "status":
            _p.add_argument(
                "--write",
                action="store_true",
                help="Apply the change instead of printing what it would do",
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
        "hook": _cmd_hook,
        "hooks": _cmd_hooks,
        "daemon": _cmd_daemon,
    }

    handler = handlers.get(args.command)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


def _cmd_hook(args: argparse.Namespace) -> int:
    """Emit an agent-harness hook payload.

    This is what an installed SessionStart hook actually runs. Keeping it a
    CLI subcommand rather than a generated script means the injection logic
    ships with the package and upgrades with it, instead of going stale in
    a copy someone's harness wrote out months ago.
    """
    if getattr(args, "hook_command", None) != "session-start":
        print("Usage: mnemos hook session-start", file=sys.stderr)
        return 1

    # A memory hiccup must never cost someone their session. Every failure
    # path here exits 0 with no stdout, which the harness reads as "this
    # hook contributed nothing" rather than as an error.
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        from .interface.context_packet import build_context_packet
        from .store.sqlite_store import EngramStore

        scope = _cli_scope(args)
        # Opening a store builds the schema, so a hook that ran against a
        # mistyped --db-path would silently create a fresh empty database
        # at every session start. Reading memory must never bring one into
        # existence: with no store yet, contribute nothing.
        if not Path(scope.db_path).expanduser().exists():
            return 0
        store = EngramStore(scope.db_path)
        try:
            packet = build_context_packet(
                store,
                args.query,
                agent_id=scope.agent_id,
                person_id=scope.person_id,
                project_scope=scope.project_scope,
                token_budget=args.token_budget,
                include_prompt=True,
                include_engrams=bool(getattr(args, "include_graph", False)),
            )
        finally:
            store.close()
        text = (packet.get("prompt") or "").strip()
    except Exception as exc:
        print(f"[mnemos hook] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0

    if not text:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))
    return 0


def _scope_args(args: argparse.Namespace) -> tuple[str, ...]:
    """The scope flags a scheduled job needs to reach the same memory.

    A background job runs with no session, no client and no inherited
    environment, so whatever scope this command resolved must be written
    into the job itself or the job would maintain a different store.

    Only the flags the top-level parser actually accepts are emitted, and
    they are global options, so ``command_for`` places them before the
    subcommand. Consolidation and the substrate tick are agent-scoped;
    person and project are not parameters they take.

    The path is expanded here. ``resolve_scope`` returns the default store
    as the literal string ``~/.mnemos/<agent>.db``, and launchd and systemd
    are not shells — they pass argv through verbatim, so an unexpanded
    tilde makes the job operate on a phantom store relative to its own
    working directory. It then reports a perfectly healthy cycle over an
    empty database while the real memory is never maintained.
    """
    scope = _cli_scope(args)
    db_path = str(Path(scope.db_path).expanduser())
    return ("--agent-id", scope.agent_id, "--db-path", db_path)


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Schedule Mnemos maintenance with whatever scheduler the host provides."""
    command = getattr(args, "daemon_command", None)
    if command not in {"install", "status", "uninstall"}:
        print("Usage: mnemos daemon {install|status|uninstall}", file=sys.stderr)
        return 1

    from .setup import scheduler

    scope = _cli_scope(args)
    blueprint = scheduler.plan(
        agent_id=scope.agent_id,
        mnemos_command=_mnemos_command(),
        scope_args=_scope_args(args),
    )
    backend = blueprint["backend"]

    if backend == "unsupported":
        print(
            "No supported scheduler found on this system (looked for launchd, "
            "systemd and crontab).\n"
            "Mnemos still maintains itself opportunistically while a session is "
            "open; only unattended background maintenance is unavailable.",
            file=sys.stderr,
        )
        return 1

    if command == "status":
        return _daemon_status(blueprint)
    if command == "uninstall":
        return _daemon_uninstall(blueprint, write=args.write)
    return _daemon_install(blueprint, write=args.write)


def _daemon_install(blueprint: dict, *, write: bool) -> int:
    backend = blueprint["backend"]
    agent_id = blueprint["agent_id"]
    entries = blueprint["entries"]

    print(f"Scheduler:   {backend}")
    print(f"Agent:       {agent_id}")
    for entry in entries:
        job = entry["job"]
        print(f"\n  {job.name} — {job.description}")
        print(f"    schedule: {entry['schedule']}")
        print(f"    command:  {entry['command']}")
        print(f"    log:      {entry['log']}")
    for job in blueprint["skipped"]:
        print(
            f"\n  {job.name} — skipped: needs a model provider "
            f"(set MNEMOS_LLM_PROVIDER and a key, then reinstall)"
        )

    from .setup import scheduler as _scheduler

    warning = _scheduler.tcc_warning(_mnemos_command(), backend=backend)
    if warning:
        print(f"\n{warning}")

    if not write:
        print("\nThis is a preview. Re-run with --write to schedule these jobs.")
        return 0

    Path(Path.home() / ".mnemos" / "logs").mkdir(parents=True, exist_ok=True)

    if backend == "launchd":
        for entry in entries:
            path = entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unload an existing job before overwriting it; launchd keeps
            # running the version it loaded, so writing alone changes nothing.
            _launchctl("bootout", path)
            path.write_bytes(entry["content"])
            _launchctl("bootstrap", path)
    elif backend == "systemd":
        systemd_dir = None
        for entry in entries:
            for path, content in entry["units"].items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                systemd_dir = path.parent
        if systemd_dir is not None:
            _systemctl("daemon-reload")
            for entry in entries:
                _systemctl("enable", "--now", entry["timer_name"])
    else:
        merged = scheduler_merge_crontab(entries, agent_id)
        if merged is None:
            return 1

    print(f"\nScheduled {len(entries)} job(s). Background maintenance is active.")
    print("Check them any time with: mnemos daemon status")
    return 0


def scheduler_merge_crontab(entries: list[dict], agent_id: str) -> str | None:
    """Write our crontab entries while preserving everything already there."""
    from .setup import scheduler

    existing = scheduler.read_crontab()
    merged = scheduler.merge_cron_lines(
        existing, [entry["line"] for entry in entries], agent_id
    )
    result = subprocess.run(["crontab", "-"], input=merged, text=True)
    if result.returncode != 0:
        print("Failed to write crontab.", file=sys.stderr)
        return None
    return merged


def _daemon_status(blueprint: dict) -> int:
    backend = blueprint["backend"]
    agent_id = blueprint["agent_id"]

    print(f"Scheduler:   {backend}")
    print(f"Agent:       {agent_id}")

    installed_any = False
    for entry in blueprint["entries"]:
        job = entry["job"]
        if backend == "launchd":
            installed = entry["path"].exists()
        elif backend == "systemd":
            installed = all(path.exists() for path in entry["units"])
        else:
            installed = f"{scheduler_marker(agent_id)}{job.name}" in _read_crontab_safe()
        installed_any = installed_any or installed
        state = "scheduled" if installed else "not scheduled"
        print(f"  {job.name:<16} {state:<15} {entry['schedule']}")

    for job in blueprint["skipped"]:
        print(f"  {job.name:<16} {'unavailable':<15} needs a model provider")

    if not installed_any:
        print("\nNo background maintenance scheduled. Install it with:")
        print("  mnemos daemon install --write")
    return 0


def scheduler_marker(agent_id: str) -> str:
    from .setup import scheduler

    return f"{scheduler.CRON_MARKER}:{agent_id}:"


def _read_crontab_safe() -> str:
    from .setup import scheduler

    return scheduler.read_crontab()


def _daemon_uninstall(blueprint: dict, *, write: bool) -> int:
    backend = blueprint["backend"]
    agent_id = blueprint["agent_id"]
    entries = blueprint["entries"]

    if not write:
        print(f"Would remove {len(entries)} scheduled job(s) ({backend}):")
        for entry in entries:
            print(f"  {entry['job'].name}")
        print("\nRe-run with --write to remove them.")
        return 0

    removed = 0
    if backend == "launchd":
        for entry in entries:
            path = entry["path"]
            if path.exists():
                _launchctl("bootout", path)
                path.unlink()
                removed += 1
    elif backend == "systemd":
        for entry in entries:
            _systemctl("disable", "--now", entry["timer_name"])
            for path in entry["units"]:
                if path.exists():
                    path.unlink()
                    removed += 1
        _systemctl("daemon-reload")
    else:
        from .setup import scheduler

        existing = scheduler.read_crontab()
        merged = scheduler.merge_cron_lines(existing, [], agent_id)
        subprocess.run(["crontab", "-"], input=merged, text=True)
        removed = len(entries)

    print(f"Removed {removed} scheduled item(s).")
    return 0


def _launchctl(action: str, plist: Path) -> None:
    """Best-effort launchctl call. A missing job is not an error to report."""
    import os

    domain = f"gui/{os.getuid()}"
    if action == "bootout":
        target = f"{domain}/{plist.stem}"
        subprocess.run(
            ["launchctl", "bootout", target],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )


def _systemctl(*args: str) -> None:
    subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def _claude_code_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _cmd_hooks(args: argparse.Namespace) -> int:
    """Install the SessionStart hook that injects memory before turn one."""
    if getattr(args, "hooks_command", None) != "install":
        print("Usage: mnemos hooks install [claude-code] [--write]", file=sys.stderr)
        return 1

    command_parts = [_mnemos_command(), "hook", "session-start"]
    for flag, value in (
        ("--agent-id", args.agent_id),
        ("--person-id", args.person_id),
        ("--project-scope", args.project_scope),
        # Expanded for the same reason as the scheduler: a hook runner is
        # not guaranteed to be a shell, and an unexpanded tilde silently
        # points the packet at a store that does not exist.
        ("--db-path", str(Path(args.db_path).expanduser()) if args.db_path else None),
    ):
        if value:
            command_parts.extend([flag, value])

    entry = {
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": " ".join(command_parts),
            "timeout": args.timeout,
        }],
    }

    path = Path(args.settings).expanduser() if args.settings else _claude_code_settings_path()

    if not args.write:
        print(json.dumps({"hooks": {"SessionStart": [entry]}}, indent=2))
        print()
        print(f"Merge this into {path}, or re-run with --write to do it automatically.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with open(path) as f:
                settings = json.load(f)
        except json.JSONDecodeError as exc:
            # Never clobber a settings file we cannot parse — that file is
            # the user's whole harness configuration, not just ours.
            print(f"Refusing to overwrite unparseable {path}: {exc}", file=sys.stderr)
            return 1
    else:
        settings = {}

    session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    existing = [
        e for e in session_start
        if any("mnemos" in h.get("command", "") for h in e.get("hooks", []))
    ]
    for stale in existing:
        session_start.remove(stale)
    session_start.append(entry)

    with open(path, "w") as f:
        json.dump(settings, f, indent=2)

    action = "Replaced" if existing else "Installed"
    print(f"{action} the Mnemos SessionStart hook in {path}")
    print(f"  Command: {entry['hooks'][0]['command']}")
    print("Start a new session — memory is injected before the first turn.")
    return 0


def _cli_scope(args: argparse.Namespace):
    """Resolve CLI identity and storage through the one shared resolver.

    The CLI used to answer "which agent, which database" on its own
    (``default`` / ``~/.mnemos/memory.db``) while simple mode answered
    through ``resolve_scope`` (``mnemos-agent`` / ``~/.mnemos/<agent>.db``).
    So ``mnemos stats`` and ``mnemos serve`` reported on different stores,
    and ``mnemos serve --mode advanced`` used a third combination again.
    One resolver, one answer, every entry point.
    """
    from .simple_scope import resolve_scope

    return resolve_scope(
        db_path=getattr(args, "db_path", None),
        agent_id=getattr(args, "agent_id", None),
        person_id=getattr(args, "person_id", None),
        project_scope=getattr(args, "project_scope", None),
    )


def _resolve_db_path(args: argparse.Namespace) -> str:
    """Resolve the CLI's database path."""
    return _cli_scope(args).db_path


def _resolve_agent_id(args: argparse.Namespace) -> str:
    """Resolve the CLI's agent identity."""
    return _cli_scope(args).agent_id


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
    except ImportError as exc:
        # Surface the real import that failed instead of assuming it is `mcp`
        # (mcp is now a core dependency). A deep ImportError in the runtime
        # should not be misreported as a missing MCP package.
        print(
            f"Failed to start the MCP server: {exc}\n"
            "If the 'mcp' package is missing, reinstall: pip install mnemos-continuity",
            file=sys.stderr,
        )
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
    try:
        daemon_config = load_config()
    except Exception:
        daemon_config = {}
    daemon = ConsolidationDaemon(store=store, config=daemon_config, llm_client=llm_client)
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
        _print_background_status(runtime.scope)
        _print_semantic_status(runtime)
        print(f"Simple tools: {', '.join(SIMPLE_TOOL_NAMES)}")
        _print_continuity_status(runtime)
        print()
        print(packet)
        return 0
    finally:
        runtime.close()


def _print_semantic_status(runtime) -> None:
    """Say which retrieval this install actually has.

    `sentence-transformers` is an optional extra, so retrieval is either
    semantic or keyword-only depending on what happens to be installed —
    and nothing told the user which. Two very different qualities of
    recall, silently selected, is the failure pattern this project keeps
    producing.
    """
    try:
        index = getattr(runtime, "_embedding_index", None)
        if index is not None and getattr(index, "_available", False):
            embedder = type(index._embedder).__name__.strip("_")
            print(f"Retrieval:    semantic + keyword ({embedder})")
        else:
            print(
                "Retrieval:    keyword only — "
                "pip install 'mnemos-continuity[embeddings]' for semantic recall"
            )
    except Exception:
        print("Retrieval:    unknown")


def _print_continuity_status(runtime) -> None:
    """Report whether continuity is actually reaching this agent."""
    try:
        signals = runtime.continuity_signals()
    except Exception:
        return
    notes = signals["notes_active"]
    print(f"Continuity:   {notes} note(s), {signals['empty_context_streak']} empty packet(s) in a row")
    for warning in signals["warnings"]:
        print(f"  ATTENTION:  {warning}")


def _print_background_status(scope) -> None:
    """Tell the user whether memory does anything between sessions.

    Without a scheduler, Mnemos only maintains itself opportunistically
    while a session happens to be open — which is easy to mistake for a
    system that is quietly working in the background.
    """
    try:
        from .setup import scheduler

        backend = scheduler.detect_backend()
        if backend == "unsupported":
            print("Background:   unavailable (no launchd, systemd or crontab)")
            return

        jobs = scheduler.jobs_for(has_model=scheduler.model_is_configured())
        if backend == "launchd":
            installed = sum(
                1 for job in jobs
                if scheduler.launchd_plist_path(scope.agent_id, job).exists()
            )
        elif backend == "systemd":
            installed = sum(
                1 for job in jobs
                if (scheduler.systemd_unit_dir()
                    / scheduler.systemd_unit_names(scope.agent_id, job)[1]).exists()
            )
        else:
            crontab = scheduler.read_crontab()
            installed = sum(
                1 for job in jobs
                if f"{scheduler.CRON_MARKER}:{scope.agent_id}:{job.name}" in crontab
            )

        if installed:
            print(f"Background:   {installed} job(s) scheduled via {backend}")
        else:
            print(
                f"Background:   not scheduled — run 'mnemos daemon install --write' "
                f"({backend})"
            )
    except Exception:
        print("Background:   unknown")


def _cmd_identity(args: argparse.Namespace) -> int:
    """Computed-vs-declared identity operations."""
    from .identity_diff import run_accept_command, run_diff_command

    if getattr(args, "identity_command", None) == "diff":
        return run_diff_command(args)
    if getattr(args, "identity_command", None) == "accept":
        return run_accept_command(args)
    print("Usage: mnemos identity {diff|accept}", file=sys.stderr)
    return 1


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
    """Absolute path to *this* Mnemos installation's CLI.

    A bare PATH lookup can resolve to a different installation than the one
    the user just ran — a Homebrew copy shadowing a pipx or venv one. The
    generated hook and client configs would then invoke the wrong Mnemos,
    against a different environment and a different store, or none at all.
    Prefer the running script, then this interpreter's own bin directory.
    """
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if argv0:
        candidate = Path(argv0).resolve()
        if candidate.is_file() and candidate.stem == "mnemos":
            return str(candidate)

    # Running as `python -m mnemos.cli`: the console script installed
    # alongside this interpreter is still the right installation.
    sibling = Path(sys.executable).parent / "mnemos"
    if sibling.is_file():
        return str(sibling)

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
