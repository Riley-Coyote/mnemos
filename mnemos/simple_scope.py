"""Scope resolution for simple mode: who, with whom, where, and which store.

Extracted from simple_runtime so identity negotiation can be reasoned
about (and tested) apart from the runtime that uses it. The precedence
is always: explicit arguments > environment > config file > defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config.loader import load_config


def _slugify(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return clean or fallback


def _default_project_scope() -> str:
    cwd = Path.cwd()
    if cwd.name:
        return _slugify(cwd.name, "global")
    return "global"


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
        or _default_project_scope(),
        "global",
    )

    # One-store invariant: the configured store is canonical and every verb
    # resolves to it. The previous resolution treated the canonical default
    # ("~/.mnemos/memory.db") as a sentinel meaning "not configured" and fell
    # through to a per-agent "~/.mnemos/{agent}.db" — which silently forked a
    # shadow store for any agent whose config did not spell a different path
    # (and could not be fixed by configuring the canonical path, because that
    # exact string was the sentinel). The shadow-oliver.db incidents of
    # 2026-07-06/07 are the case study: identity writes landed in a sibling
    # DB that nothing reads. Config (including its default) is now honored
    # verbatim; a per-agent store is a deliberate configuration, never an
    # implicit fallback.
    explicit_db = db_path or os.environ.get("MNEMOS_DB_PATH")
    if explicit_db:
        resolved_db = explicit_db
    else:
        store_config = config.get("store", {}) if isinstance(config.get("store"), dict) else {}
        resolved_db = str(store_config.get("db_path") or "~/.mnemos/memory.db")

    return MnemosScope(
        agent_id=resolved_agent,
        person_id=resolved_person,
        project_scope=resolved_project,
        db_path=resolved_db,
    )


def detect_sibling_stores(scope: MnemosScope) -> list[str]:
    """Detect shadow per-agent stores sitting beside the resolved canonical DB.

    Returns paths of ``~/.mnemos/{agent_id}.db`` files that exist but are NOT
    the store this scope resolves to. Historically the resolver itself minted
    these (see resolve_scope); any that remain are either archived-in-place
    strays or evidence that some writer still resolves paths on its own.
    Doctor treats a non-empty result as a failure: a sibling store taking
    writes is memory loss in progress.
    """
    resolved = Path(os.path.expanduser(scope.db_path)).resolve()
    siblings: list[str] = []
    candidate = Path.home() / ".mnemos" / f"{scope.agent_id}.db"
    if candidate.exists() and candidate.resolve() != resolved:
        siblings.append(str(candidate))
    return siblings
