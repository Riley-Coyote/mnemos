"""Scope resolution for simple mode: who, with whom, where, and which store.

Extracted from simple_runtime so identity negotiation can be reasoned
about (and tested) apart from the runtime that uses it. The precedence
is always: explicit arguments > environment > config file > defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config.loader import load_config


def _slugify(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return clean or fallback


# The project scope must never be inferred from the process working
# directory. An MCP server's cwd is an accident of how the client spawned
# it — Claude Code starts it in the open project, Claude Desktop somewhere
# else entirely — so a cwd-derived scope silently partitions one agent's
# memory by launch location. Continuity written from one directory then
# becomes invisible from another while every layer still reports success.
# Scope is identity, not location: it changes only when asked to.
DEFAULT_PROJECT_SCOPE = "global"


@dataclass(frozen=True)
class MnemosScope:
    """Resolved identity and storage scope for simple mode."""

    agent_id: str
    person_id: str
    project_scope: str
    db_path: str


def resolve_scope(
    *,
    db_path: str | None = None,
    agent_id: str | None = None,
    person_id: str | None = None,
    project_scope: str | None = None,
) -> MnemosScope:
    """Resolve Mnemos identity from explicit args, env, config, then defaults."""

    try:
        config = load_config()
    except Exception:
        config = {}

    resolved_agent = _slugify(
        agent_id
        or os.environ.get("MNEMOS_AGENT_ID", "")
        or str(config.get("agent_id", ""))
        or "mnemos-agent",
        "mnemos-agent",
    )
    resolved_person = _slugify(
        person_id
        or os.environ.get("MNEMOS_PERSON_ID", "")
        or str(config.get("person_id", ""))
        or str(config.get("user_name", ""))
        or "user",
        "user",
    )
    resolved_project = _slugify(
        project_scope
        or os.environ.get("MNEMOS_PROJECT_SCOPE", "")
        or str(config.get("project_scope", ""))
        or DEFAULT_PROJECT_SCOPE,
        DEFAULT_PROJECT_SCOPE,
    )

    explicit_db = db_path or os.environ.get("MNEMOS_DB_PATH")
    if explicit_db:
        resolved_db = explicit_db
    else:
        store_config = config.get("store", {}) if isinstance(config.get("store"), dict) else {}
        configured = store_config.get("db_path")
        if configured and configured != "~/.mnemos/memory.db":
            resolved_db = str(configured)
        else:
            resolved_db = f"~/.mnemos/{resolved_agent}.db"

    return MnemosScope(
        agent_id=resolved_agent,
        person_id=resolved_person,
        project_scope=resolved_project,
        db_path=resolved_db,
    )


# The advanced MCP tools historically declared these literals as their
# parameter defaults. They were never real scopes — they are what a tool
# signature says when the caller did not choose. Treating them as
# "unspecified" is what lets both tool surfaces resolve through
# resolve_scope() and land in the same partition.
_UNSPECIFIED = {
    "agent_id": {"", "default"},
    "person_id": {"", "user"},
    "project_scope": {"", "global"},
}


def resolve_tool_scope(
    agent_id: str = "",
    person_id: str = "",
    project_scope: str = "",
    *,
    db_path: str | None = None,
) -> MnemosScope:
    """Resolve an MCP tool call's scope, honouring only deliberate arguments.

    The advanced tool surface and the simple tool surface used to disagree:
    simple resolved through ``resolve_scope`` while advanced took the literal
    defaults ``default``/``user``/``global``. One wrote continuity the other
    could not read. Routing both through here keeps a single answer to
    "whose memory, about whom, on what".
    """

    return resolve_scope(
        db_path=db_path,
        agent_id=None if agent_id in _UNSPECIFIED["agent_id"] else agent_id,
        person_id=None if person_id in _UNSPECIFIED["person_id"] else person_id,
        project_scope=(
            None if project_scope in _UNSPECIFIED["project_scope"] else project_scope
        ),
    )
