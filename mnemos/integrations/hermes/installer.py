"""Installer and diagnostics for the Hermes Mnemos provider shim."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...file_security import atomic_write_text
from .scope import default_hermes_home, save_hermes_mnemos_config, slugify


PLUGIN_NAME = "mnemos"
PLUGIN_VERSION = "0.2.1"
PROVIDER_MODE = "provider"
SIDECAR_MODE = "sidecar"
DEFAULT_MCP_SERVER_NAME = "mnemos"


def provider_plugin_dirs(hermes_home: str | Path) -> list[Path]:
    """Return supported Hermes provider shim directories.

    Current local Hermes releases discover user memory providers from
    ``$HERMES_HOME/plugins/<name>``. The public docs describe memory providers
    under ``plugins/memory/<name>``. Writing both tiny shim directories keeps the
    Mnemos package compatible with both layouts without duplicating logic.
    """

    home = Path(hermes_home).expanduser()
    return [
        home / "plugins" / PLUGIN_NAME,
        home / "plugins" / "memory" / PLUGIN_NAME,
    ]


def render_plugin_shim() -> str:
    """Return the tiny Hermes plugin shim that imports Mnemos."""

    return (
        '"""Mnemos memory provider shim for Hermes Agent."""\n\n'
        "from agent.memory_provider import MemoryProvider\n\n"
        "from mnemos.integrations.hermes import build_memory_provider_class\n\n\n"
        "MnemosMemoryProvider = build_memory_provider_class(MemoryProvider)\n\n\n"
        "def register(ctx):\n"
        "    ctx.register_memory_provider(MnemosMemoryProvider())\n"
    )


def render_plugin_manifest() -> str:
    """Return Hermes plugin metadata."""

    return (
        "name: mnemos\n"
        f"version: {PLUGIN_VERSION}\n"
        "kind: exclusive\n"
        "description: Local-first Mnemos identity-continuity provider for Hermes\n"
        "provides_tools:\n"
        "  - mnemos_identity_capture\n"
        "  - mnemos_identity_recall\n"
        "  - mnemos_identity_correct\n"
        "  - mnemos_identity_report\n"
        "hooks:\n"
        "  - prefetch\n"
        "  - sync_turn\n"
        "  - on_session_end\n"
        "  - on_pre_compress\n"
        "  - on_memory_write\n"
    )


def render_plugin_readme() -> str:
    """Return the installed plugin readme."""

    return (
        "# Mnemos for Hermes\n\n"
        "This directory is a small Hermes memory-provider shim. The durable identity-continuity "
        "implementation lives in the installed `mnemos-continuity` Python package.\n\n"
        "Provider Mode enables Mnemos as Hermes' single active external memory provider while "
        "leaving Hermes built-in `MEMORY.md` and `USER.md` active:\n\n"
        "```bash\n"
        "hermes config set memory.provider mnemos\n"
        "```\n\n"
        "Sidecar Mode leaves `memory.provider` unchanged and exposes Mnemos through MCP/tools "
        "for agents already using Honcho, Supermemory, Mem0, or another external provider.\n\n"
        "Mnemos stores local scoped continuity in `$HERMES_HOME/mnemos/mnemos.db` by default. "
        "Provider and sidecar settings can live in `$HERMES_HOME/mnemos.json`.\n"
    )


@dataclass
class HermesInstallResult:
    hermes_home: Path
    plugin_dir: Path
    plugin_dirs: list[Path] = field(default_factory=list)
    mode: str = PROVIDER_MODE
    files_written: list[Path] = field(default_factory=list)
    config_path: Path | None = None
    mcp_config_path: Path | None = None
    mcp_server_name: str = DEFAULT_MCP_SERVER_NAME
    mcp_configured: bool = False
    active_provider: str = ""
    activated: bool = False
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode_label = "Provider Mode" if self.mode == PROVIDER_MODE else "Sidecar Mode"
        lines = [
            "Mnemos Hermes install",
            f"Mode: {mode_label}",
            f"HERMES_HOME: {self.hermes_home}",
        ]
        if self.mode == PROVIDER_MODE or self.files_written:
            dirs = self.plugin_dirs or [self.plugin_dir]
            lines.append("Provider shim: " + ", ".join(str(path) for path in dirs))
        if self.dry_run:
            lines.append("Dry run: yes")
        if self.files_written:
            lines.append("Files: " + ", ".join(str(path) for path in self.files_written))
        if self.config_path:
            lines.append(f"Config: {self.config_path}")
        if self.mcp_config_path:
            status = "configured" if self.mcp_configured else "not changed"
            lines.append(f"MCP sidecar: {self.mcp_server_name} ({status})")
            lines.append(f"Hermes config: {self.mcp_config_path}")
        if self.active_provider:
            lines.append(f"memory.provider: {self.active_provider}")
        elif self.mode == SIDECAR_MODE:
            lines.append("memory.provider: (not set)")
        if self.mode == PROVIDER_MODE:
            lines.append(f"Provider Mode active: {'yes' if self.activated else 'no'}")
        else:
            lines.append("Provider Mode active: no (left unchanged)")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings)
        if self.mode == PROVIDER_MODE and not self.activated:
            lines.append("Enable with: hermes config set memory.provider mnemos")
        if self.mode == SIDECAR_MODE:
            lines.append("Sidecar Mode keeps the existing Hermes external memory provider and exposes Mnemos through MCP/tools.")
        return "\n".join(lines)


@dataclass
class HermesQuickstartResult:
    hermes_home: Path
    mode: str
    agent_safe: bool = False
    install_result: HermesInstallResult | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    preserved_provider: str = ""
    restart_likely_needed: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.install_result is None:
            return False
        if self.warnings:
            return False
        if self.agent_safe and not self.install_result.mcp_configured:
            return False
        return True

    def summary(self) -> str:
        mode_label = "Provider Mode" if self.mode == PROVIDER_MODE else "Sidecar Mode"
        diagnostics = self.diagnostics or {}
        install = self.install_result
        active_provider = str(diagnostics.get("active_memory_provider") or "")
        mcp_configured = bool(diagnostics.get("mcp_server_configured"))
        provider_shim_ready = bool(diagnostics.get("provider_shim_ready"))
        command = str(diagnostics.get("mnemos_command") or "mnemos")
        preserved = self.preserved_provider or "(not set)"
        if install and install.mcp_configured:
            mcp_status = "configured"
        elif mcp_configured:
            mcp_status = "present"
        else:
            mcp_status = "not configured"
        lines = [
            "Mnemos Hermes quickstart",
            f"Mode: {mode_label}",
            f"Agent-safe: {'yes' if self.agent_safe else 'no'}",
            f"HERMES_HOME: {self.hermes_home}",
            "Doctor summary:",
        ]
        if self.mode == SIDECAR_MODE:
            lines.append(f"Preserved memory.provider: {preserved}")
        else:
            lines.append(f"memory.provider: {active_provider or '(not set)'}")
        lines.extend([
            f"MCP sidecar: {mcp_status}",
            f"Provider shim: {'installed' if provider_shim_ready else 'not installed'}",
            f"Command: {command}",
        ])
        if install and install.files_written:
            lines.append("Changed files: " + ", ".join(str(path) for path in install.files_written))
        if install and install.mcp_config_path:
            mcp_status = "updated" if install.mcp_configured else "not changed"
            lines.append(f"Hermes config: {install.mcp_config_path} ({mcp_status})")
        if self.agent_safe:
            lines.append("Preserved: SOUL.md, MEMORY.md, USER.md, AGENTS.md, and memory.provider")
        if self.restart_likely_needed:
            lines.append("Restart: yes; restart Hermes to load new provider or MCP config.")
        else:
            lines.append("Restart: not likely; no Hermes config change was made.")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings)
        elif self.mode == SIDECAR_MODE:
            lines.append("Ready: Mnemos is available through Hermes MCP/tools alongside the current memory provider.")
        else:
            lines.append("Ready: Mnemos is the active Hermes external memory provider.")
        return "\n".join(lines)


def install_hermes_plugin(
    *,
    hermes_home: str | Path | None = None,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
    mode: str = PROVIDER_MODE,
    configure_mcp: bool | None = None,
    mcp_server_name: str = DEFAULT_MCP_SERVER_NAME,
    activate: bool = False,
    force: bool = False,
    dry_run: bool = False,
    agent_safe: bool = False,
) -> HermesInstallResult:
    """Install the Hermes user memory-provider shim under ``$HERMES_HOME``."""

    selected_mode = str(mode or PROVIDER_MODE).strip().lower()
    if selected_mode not in {PROVIDER_MODE, SIDECAR_MODE}:
        raise ValueError(f"Unsupported Hermes Mnemos mode: {mode}")

    home = Path(hermes_home).expanduser() if hermes_home else default_hermes_home()
    plugin_dirs = provider_plugin_dirs(home)
    plugin_dir = plugin_dirs[0]
    mcp_name = slugify(mcp_server_name, DEFAULT_MCP_SERVER_NAME)
    should_configure_mcp = selected_mode == SIDECAR_MODE if configure_mcp is None else bool(configure_mcp)
    result = HermesInstallResult(
        hermes_home=home,
        plugin_dir=plugin_dir,
        plugin_dirs=plugin_dirs,
        mode=selected_mode,
        mcp_server_name=mcp_name,
        dry_run=dry_run,
        active_provider=_read_active_provider(home / "config.yaml"),
    )

    files: dict[Path, str] = {}
    if selected_mode == PROVIDER_MODE:
        for provider_dir in plugin_dirs:
            files.update({
                provider_dir / "__init__.py": render_plugin_shim(),
                provider_dir / "plugin.yaml": render_plugin_manifest(),
                provider_dir / "README.md": render_plugin_readme(),
            })

    for path, content in files.items():
        if path.exists() and path.read_text(encoding="utf-8", errors="replace") != content and not force:
            result.warnings.append(f"Existing file differs; re-run with --force to replace: {path}")
            continue
        if dry_run:
            result.files_written.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content, backup_existing=force)
        result.files_written.append(path)

    config_updates = {
        "db_path": db_path,
        "agent_id": agent_id,
        "person_id": person_id,
        "project_scope": project_scope,
    }
    if any(value not in (None, "") for value in config_updates.values()):
        config_path = home / "mnemos.json"
        if dry_run:
            result.config_path = config_path
        else:
            result.config_path = save_hermes_mnemos_config(home, config_updates)

    if should_configure_mcp:
        configured, warning, config_path = _configure_mcp_server(
            home,
            server_name=mcp_name,
            db_path=db_path,
            agent_id=agent_id,
            person_id=person_id,
            project_scope=project_scope,
            dry_run=dry_run,
            replace_existing_server=not agent_safe,
        )
        result.mcp_configured = configured
        result.mcp_config_path = config_path
        if warning:
            result.warnings.append(warning)

    if selected_mode == SIDECAR_MODE and activate:
        result.warnings.append("Sidecar Mode leaves memory.provider unchanged; ignoring --activate.")
    elif activate:
        if dry_run:
            result.activated = True
        else:
            activated, warning = _activate_memory_provider(home)
            result.activated = activated
            if warning:
                result.warnings.append(warning)
            result.active_provider = _read_active_provider(home / "config.yaml")
    else:
        result.activated = result.active_provider == PLUGIN_NAME

    return result


def quickstart_hermes(
    *,
    hermes_home: str | Path | None = None,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
    provider: bool = False,
    activate_provider: bool = False,
    agent_safe: bool = False,
    mcp_server_name: str = DEFAULT_MCP_SERVER_NAME,
    dry_run: bool = False,
) -> HermesQuickstartResult:
    """Install Mnemos for Hermes with safe defaults and an immediate doctor pass."""

    home = Path(hermes_home).expanduser() if hermes_home else default_hermes_home()
    before = build_diagnostics(home)
    preserved_provider = str(before.get("active_memory_provider") or "")
    wants_provider = bool(provider or activate_provider)
    mode = SIDECAR_MODE if agent_safe or not wants_provider else PROVIDER_MODE
    activate = mode == PROVIDER_MODE
    warnings: list[str] = []
    if agent_safe and wants_provider:
        warnings.append("Agent-safe quickstart ignores provider activation flags and keeps memory.provider unchanged.")

    install = install_hermes_plugin(
        hermes_home=home,
        db_path=db_path,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
        mode=mode,
        configure_mcp=mode == SIDECAR_MODE,
        mcp_server_name=mcp_server_name,
        activate=activate,
        dry_run=dry_run,
        agent_safe=agent_safe,
    )
    warnings.extend(install.warnings)
    after = build_diagnostics(home)
    if agent_safe:
        after_provider = str(after.get("active_memory_provider") or "")
        if after_provider != preserved_provider:
            warnings.append(
                f"Agent-safe quickstart expected to preserve memory.provider as {preserved_provider or '(not set)'}, "
                f"but found {after_provider or '(not set)'}."
            )
        if not install.mcp_configured:
            warnings.append("Agent-safe quickstart could not safely configure the Mnemos MCP sidecar.")

    restart_likely = bool(install.files_written or install.mcp_configured or install.activated)
    return HermesQuickstartResult(
        hermes_home=home,
        mode=mode,
        agent_safe=agent_safe,
        install_result=install,
        diagnostics=after,
        preserved_provider=preserved_provider,
        restart_likely_needed=restart_likely,
        warnings=warnings,
    )


def build_diagnostics(hermes_home: str | Path | None = None) -> dict[str, Any]:
    """Return a compact diagnostics payload for Mnemos/Hermes setup."""

    home = Path(hermes_home).expanduser() if hermes_home else default_hermes_home()
    plugin_dirs = provider_plugin_dirs(home)
    plugin_dir = plugin_dirs[0]
    config_path = home / "config.yaml"
    mnemos_config = home / "mnemos.json"
    db_path = _configured_db_path(home)
    command = shutil.which("mnemos") or "mnemos"
    active = _read_active_provider(config_path)
    mcp_server = _read_mcp_server(home, DEFAULT_MCP_SERVER_NAME)
    ready_dirs = [
        path for path in plugin_dirs
        if (path / "__init__.py").exists() and (path / "plugin.yaml").exists()
    ]
    provider_shim_ready = bool(ready_dirs)
    mcp_configured = bool(mcp_server)
    if active == PLUGIN_NAME:
        mode = "provider"
    elif mcp_configured:
        mode = "sidecar"
    elif provider_shim_ready:
        mode = "provider-installed"
    else:
        mode = "not-configured"
    return {
        "hermes_home": str(home),
        "plugin_dir": str(plugin_dir),
        "plugin_dirs": [str(path) for path in plugin_dirs],
        "provider_shim_ready_dirs": [str(path) for path in ready_dirs],
        "plugin_init_exists": (plugin_dir / "__init__.py").exists(),
        "plugin_manifest_exists": (plugin_dir / "plugin.yaml").exists(),
        "provider_shim_ready": provider_shim_ready,
        "hermes_config": str(config_path),
        "active_memory_provider": active,
        "mode": mode,
        "provider_mode_active": active == PLUGIN_NAME,
        "external_provider_in_slot": active not in {"", PLUGIN_NAME},
        "mcp_server_name": DEFAULT_MCP_SERVER_NAME,
        "mcp_server_configured": mcp_configured,
        "mcp_server": mcp_server,
        "mnemos_config": str(mnemos_config),
        "mnemos_config_exists": mnemos_config.exists(),
        "mnemos_command": command,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "ready": active == PLUGIN_NAME or mcp_configured,
        "restart_likely_needed": active == PLUGIN_NAME or mcp_configured or provider_shim_ready,
    }


def format_diagnostics(payload: dict[str, Any]) -> str:
    """Format diagnostics for CLI output."""

    lines = [
        "Mnemos Hermes Doctor",
        "-" * 40,
        f"HERMES_HOME: {payload['hermes_home']}",
        f"Mode:        {_diagnostic_mode_label(payload)}",
        f"Plugin shim: {'yes' if payload['provider_shim_ready'] else 'no'}",
        f"Provider:    {payload['active_memory_provider'] or '(not set)'}",
        f"MCP sidecar: {'yes' if payload['mcp_server_configured'] else 'no'}",
        f"Config:      {'yes' if payload['mnemos_config_exists'] else 'no'}",
        f"Database:    {payload['db_path']} ({'exists' if payload['db_exists'] else 'not created yet'})",
        f"Command:     {payload['mnemos_command']}",
        f"Restart:     {'likely after install/config changes' if payload['restart_likely_needed'] else 'not likely'}",
    ]
    if payload["provider_mode_active"]:
        lines.append("Provider Mode: active; Hermes built-in memory remains active alongside Mnemos.")
    elif payload["mcp_server_configured"]:
        preserved = payload["active_memory_provider"] or "(not set)"
        lines.append(f"Sidecar Mode: active; memory.provider preserved as {preserved}.")
    elif payload["active_memory_provider"] and payload["active_memory_provider"] != PLUGIN_NAME:
        lines.append(
            "Provider Mode would replace the current external provider; use "
            "`mnemos hermes install --mode sidecar` to keep it."
        )
    else:
        lines.append("Enable with: hermes config set memory.provider mnemos")
    return "\n".join(lines)


def _diagnostic_mode_label(payload: dict[str, Any]) -> str:
    mode = payload.get("mode")
    if mode == "provider":
        return "Provider Mode"
    if mode == "sidecar":
        return "Sidecar Mode"
    if mode == "provider-installed":
        return "Provider shim installed, not active"
    return "Not configured"


def _configure_mcp_server(
    home: Path,
    *,
    server_name: str,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
    dry_run: bool = False,
    replace_existing_server: bool = True,
) -> tuple[bool, str | None, Path]:
    config_path = home / "config.yaml"
    if dry_run:
        return True, None, config_path

    server = _mcp_server_config(
        db_path=db_path,
        agent_id=agent_id,
        person_id=person_id,
        project_scope=project_scope,
    )

    try:
        import yaml  # type: ignore
    except Exception:
        if config_path.exists():
            return (
                False,
                "PyYAML is not available, so existing config.yaml was not modified for MCP sidecar mode.",
                config_path,
            )
        home.mkdir(parents=True, exist_ok=True)
        atomic_write_text(config_path, _render_mcp_only_config(server_name, server))
        return True, None, config_path

    home.mkdir(parents=True, exist_ok=True)
    try:
        if config_path.exists():
            raw_existing = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if raw_existing is None:
                existing = {}
            elif isinstance(raw_existing, dict):
                existing = raw_existing
            else:
                return (
                    False,
                    f"Existing {config_path} is not a YAML mapping, so it was not modified for MCP sidecar mode.",
                    config_path,
                )
        else:
            existing = {}
        mcp_servers = existing.get("mcp_servers")
        if mcp_servers is None:
            mcp_servers = {}
        elif not isinstance(mcp_servers, dict):
            return (
                False,
                f"Existing mcp_servers in {config_path} is not a YAML mapping, so it was not modified.",
                config_path,
            )
        existing_server = mcp_servers.get(server_name)
        if existing_server and existing_server != server and not replace_existing_server:
            return (
                False,
                f"Existing mcp_servers.{server_name} differs; agent-safe mode left it unchanged.",
                config_path,
            )
        mcp_servers[server_name] = server
        existing["mcp_servers"] = mcp_servers
        atomic_write_text(
            config_path, yaml.safe_dump(existing, sort_keys=False),
            backup_existing=config_path.exists(),
        )
        return True, None, config_path
    except Exception as exc:
        return False, f"Could not update {config_path} for MCP sidecar mode: {exc}", config_path


def _mcp_server_config(
    *,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> dict[str, Any]:
    args = ["serve", "--mode", "simple"]
    if db_path:
        args.extend(["--db-path", db_path])
    if agent_id:
        args.extend(["--agent-id", agent_id])
    if person_id:
        args.extend(["--person-id", person_id])
    if project_scope:
        args.extend(["--project-scope", project_scope])
    return {
        "command": shutil.which("mnemos") or "mnemos",
        "args": args,
        "env": {},
        "timeout": 120,
        "connect_timeout": 60,
    }


def _render_mcp_only_config(server_name: str, server: dict[str, Any]) -> str:
    lines = [
        "mcp_servers:",
        f"  {server_name}:",
        f"    command: {json.dumps(str(server['command']))}",
        "    args:",
    ]
    lines.extend(f"      - {json.dumps(str(arg))}" for arg in server["args"])
    lines.extend([
        "    env: {}",
        f"    timeout: {int(server['timeout'])}",
        f"    connect_timeout: {int(server['connect_timeout'])}",
    ])
    return "\n".join(lines) + "\n"


def _read_mcp_server(home: Path, server_name: str) -> dict[str, Any]:
    config_path = home / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            servers = raw.get("mcp_servers")
            if isinstance(servers, dict):
                server = servers.get(server_name)
                return server if isinstance(server, dict) else {}
    except Exception:
        pass
    try:
        text = config_path.read_text(encoding="utf-8")
        raw_json = json.loads(text)
        servers = raw_json.get("mcp_servers") if isinstance(raw_json, dict) else None
        server = servers.get(server_name) if isinstance(servers, dict) else None
        return server if isinstance(server, dict) else {}
    except Exception:
        pass
    try:
        return _read_simple_mcp_yaml(config_path.read_text(encoding="utf-8", errors="replace"), server_name)
    except Exception:
        pass
    return {}


def _read_simple_mcp_yaml(text: str, server_name: str) -> dict[str, Any]:
    server: dict[str, Any] = {}
    in_mcp = False
    in_server = False
    in_args = False
    args: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if not line.startswith(" ") and stripped != "mcp_servers:":
            in_mcp = False
            in_server = False
            in_args = False
        if stripped == "mcp_servers:":
            in_mcp = True
            continue
        if not in_mcp:
            continue
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            in_server = stripped[:-1] == server_name
            in_args = False
            continue
        if not in_server:
            continue
        if stripped == "args:":
            in_args = True
            continue
        if in_args and stripped.startswith("- "):
            args.append(_simple_yaml_value(stripped[2:]))
            continue
        in_args = False
        if ":" in stripped:
            key, raw = stripped.split(":", 1)
            server[key.strip()] = _simple_yaml_value(raw.strip())
    if args:
        server["args"] = args
    return server


def _simple_yaml_value(raw: str) -> Any:
    if raw == "{}":
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _configured_db_path(home: Path) -> Path:
    path = home / "mnemos" / "mnemos.db"
    config_path = home / "mnemos.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            raw = str(data.get("db_path") or "").strip()
            if raw:
                raw = raw.replace("$HERMES_HOME", str(home)).replace("${HERMES_HOME}", str(home))
                path = Path(raw).expanduser()
        except Exception:
            pass
    return path


def _activate_memory_provider(home: Path) -> tuple[bool, str | None]:
    config_path = home / "config.yaml"
    try:
        import yaml  # type: ignore
    except Exception:
        if config_path.exists():
            return False, "PyYAML is not available, so existing config.yaml was not modified."
        home.mkdir(parents=True, exist_ok=True)
        atomic_write_text(config_path, "memory:\n  provider: mnemos\n")
        return True, None

    home.mkdir(parents=True, exist_ok=True)
    try:
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        if not isinstance(existing, dict):
            return False, f"Existing {config_path} is not a YAML mapping, so it was not modified."
        memory = existing.get("memory")
        if memory is None:
            existing["memory"] = {}
        elif not isinstance(memory, dict):
            return False, f"Existing memory section in {config_path} is not a mapping, so it was not modified."
        existing["memory"]["provider"] = PLUGIN_NAME
        atomic_write_text(
            config_path, yaml.safe_dump(existing, sort_keys=False),
            backup_existing=config_path.exists(),
        )
        return True, None
    except Exception as exc:
        return False, f"Could not update {config_path}: {exc}"


def _read_active_provider(config_path: Path) -> str:
    if not config_path.exists():
        return ""
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            memory = raw.get("memory")
            if isinstance(memory, dict):
                return str(memory.get("provider") or "")
    except Exception:
        pass
    try:
        for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("provider:"):
                return stripped.split(":", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("HERMES_MEMORY_PROVIDER", "")
