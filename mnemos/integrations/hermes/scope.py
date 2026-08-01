"""Scope and configuration helpers for the Hermes integration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...file_security import atomic_write_text


def slugify(value: str | None, fallback: str) -> str:
    """Return a stable identifier that is safe in paths and SQLite scopes."""

    raw = (value or "").strip()
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw.lower()).strip("-._")
    return clean or fallback


def default_hermes_home() -> Path:
    """Resolve the active Hermes home without importing Hermes."""

    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return default


def _config_path(hermes_home: Path) -> Path:
    return hermes_home / "mnemos.json"


def _load_json_config(hermes_home: Path) -> dict[str, Any]:
    path = _config_path(hermes_home)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _cwd_project_scope() -> str:
    cwd = Path.cwd()
    try:
        for parent in (cwd, *cwd.parents):
            if (parent / ".git").exists():
                return slugify(parent.name, "global")
    except Exception:
        pass
    return slugify(cwd.name, "global")


@dataclass(frozen=True)
class HermesMnemosConfig:
    """Profile-local Mnemos settings stored in ``$HERMES_HOME/mnemos.json``."""

    db_path: str | None = None
    agent_id: str | None = None
    person_id: str | None = None
    project_scope: str | None = None
    auto_recall: bool = True
    auto_capture: bool = True
    auto_bootstrap: bool = True
    auto_session_distill: bool = True
    mirror_builtin_memory: bool = True
    deep_maintenance: bool = False
    max_recall_results: int = 4
    max_context_chars: int = 2200
    maintenance_interval: int = 24
    capture_uncertain: bool = True

    @classmethod
    def load(cls, hermes_home: str | Path | None = None) -> "HermesMnemosConfig":
        home = Path(hermes_home).expanduser() if hermes_home else default_hermes_home()
        raw = _load_json_config(home)
        return cls(
            db_path=str(raw.get("db_path") or "").strip() or None,
            agent_id=str(raw.get("agent_id") or "").strip() or None,
            person_id=str(raw.get("person_id") or "").strip() or None,
            project_scope=str(raw.get("project_scope") or "").strip() or None,
            auto_recall=_as_bool(raw.get("auto_recall"), True),
            auto_capture=_as_bool(raw.get("auto_capture"), True),
            auto_bootstrap=_as_bool(raw.get("auto_bootstrap"), True),
            auto_session_distill=_as_bool(raw.get("auto_session_distill"), True),
            mirror_builtin_memory=_as_bool(raw.get("mirror_builtin_memory"), True),
            deep_maintenance=_as_bool(raw.get("deep_maintenance"), False),
            max_recall_results=_as_int(raw.get("max_recall_results"), 4, minimum=1, maximum=16),
            max_context_chars=_as_int(raw.get("max_context_chars"), 2200, minimum=800, maximum=12000),
            maintenance_interval=_as_int(raw.get("maintenance_interval"), 24, minimum=0, maximum=500),
            capture_uncertain=_as_bool(raw.get("capture_uncertain"), True),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "agent_id": self.agent_id,
            "person_id": self.person_id,
            "project_scope": self.project_scope,
            "auto_recall": self.auto_recall,
            "auto_capture": self.auto_capture,
            "auto_bootstrap": self.auto_bootstrap,
            "auto_session_distill": self.auto_session_distill,
            "mirror_builtin_memory": self.mirror_builtin_memory,
            "deep_maintenance": self.deep_maintenance,
            "max_recall_results": self.max_recall_results,
            "max_context_chars": self.max_context_chars,
            "maintenance_interval": self.maintenance_interval,
            "capture_uncertain": self.capture_uncertain,
        }


@dataclass(frozen=True)
class HermesScope:
    """Resolved durable identity scope for a Hermes provider instance."""

    agent_id: str
    person_id: str
    project_scope: str
    db_path: str
    hermes_home: Path
    session_id: str
    platform: str = "cli"
    agent_context: str = "primary"


def derive_hermes_scope(
    *,
    session_id: str = "",
    hermes_home: str | Path | None = None,
    config: HermesMnemosConfig | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> HermesScope:
    """Derive Mnemos agent/person/project scope from Hermes runtime context."""

    context = dict(runtime_context or {})
    home = Path(hermes_home or context.get("hermes_home") or default_hermes_home()).expanduser()
    cfg = config or HermesMnemosConfig.load(home)

    agent_raw = (
        cfg.agent_id
        or os.environ.get("MNEMOS_AGENT_ID")
        or context.get("agent_identity")
        or context.get("profile")
        or "hermes"
    )
    person_raw = (
        cfg.person_id
        or os.environ.get("MNEMOS_PERSON_ID")
        or context.get("user_name")
        or context.get("user_id")
        or context.get("chat_id")
        or "user"
    )
    project_raw = (
        cfg.project_scope
        or os.environ.get("MNEMOS_PROJECT_SCOPE")
        or context.get("project_scope")
        or context.get("chat_name")
        or _cwd_project_scope()
    )

    explicit_db = cfg.db_path or os.environ.get("MNEMOS_DB_PATH")
    db_path = explicit_db or str(home / "mnemos" / "mnemos.db")
    db_path = db_path.replace("$HERMES_HOME", str(home)).replace("${HERMES_HOME}", str(home))

    return HermesScope(
        agent_id=slugify(str(agent_raw), "hermes"),
        person_id=slugify(str(person_raw), "user"),
        project_scope=slugify(str(project_raw), "global"),
        db_path=db_path,
        hermes_home=home,
        session_id=str(session_id or context.get("session_id") or ""),
        platform=str(context.get("platform") or "cli"),
        agent_context=str(context.get("agent_context") or "primary"),
    )


def save_hermes_mnemos_config(
    hermes_home: str | Path,
    updates: dict[str, Any],
) -> Path:
    """Merge Mnemos settings into ``$HERMES_HOME/mnemos.json``."""

    home = Path(hermes_home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    path = _config_path(home)
    existing = _load_json_config(home)
    merged = {**existing, **{k: v for k, v in updates.items() if v not in (None, "")}}
    atomic_write_text(
        path, json.dumps(merged, indent=2, sort_keys=True) + "\n",
        backup_existing=True,
    )
    return path
