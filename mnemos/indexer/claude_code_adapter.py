"""
Claude Code session adapter for the Mnemos session indexer.

Reads Claude Code .jsonl session transcripts and provides them to the
SessionIndexer for memory extraction. Claude Code sessions use a similar
JSONL format to OpenClaw but with slightly different entry structures.

The existing _read_session_transcript in session_indexer.py already handles
the core message format — this module provides a convenience function for
single-file indexing triggered by SessionEnd hooks.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mnemos.core.types import DEFAULT_AGENT_ID

logger = logging.getLogger("mnemos.indexer.claude_code")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def _resolve_agent_id(explicit: str | None) -> str:
    return (
        (explicit or "").strip()
        or os.environ.get("MNEMOS_AGENT_ID", "").strip()
        or DEFAULT_AGENT_ID
    )


def _discover_openclaw_agents() -> list[str]:
    """OpenClaw agents that have model configs, discovered from disk.

    MNEMOS_OPENCLAW_AGENTS (comma-separated) overrides discovery.
    Library code must not hardcode personal agent names.
    """
    override = _env_list("MNEMOS_OPENCLAW_AGENTS")
    if override:
        return override
    agents_root = Path.home() / ".openclaw" / "agents"
    if not agents_root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in agents_root.iterdir()
        if (entry / "agent" / "models.json").exists()
    )


def read_claude_code_transcript(path: Path, max_messages: int = 100) -> list[dict]:
    """Read a Claude Code .jsonl session file and extract messages.

    Claude Code format differs from OpenClaw:
    - Entry types are "user" and "assistant" (not "message")
    - Content lives at entry.message.content (string or array)
    """
    messages: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)

                    # Claude Code format: type is "user" or "assistant"
                    entry_type = entry.get("type", "")
                    if entry_type in ("user", "assistant"):
                        msg = entry.get("message", {})
                    # OpenClaw v3 format (fallback)
                    elif entry_type == "message":
                        msg = entry.get("message", {})
                    # Direct role format (fallback)
                    elif entry.get("role") in ("user", "assistant"):
                        msg = entry
                    else:
                        continue

                    role = msg.get("role", entry_type)
                    if role not in ("user", "assistant"):
                        continue

                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )

                    if content and len(content) > 10:
                        messages.append({
                            "role": role,
                            "content": content[:3000],
                        })
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning("Error reading %s: %s", path, e)

    return messages[-max_messages:]


def index_session(
    transcript_path: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    db_path: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Index a single Claude Code session transcript into Mnemos.

    Args:
        transcript_path: Path to the .jsonl session file
        session_id: Session identifier (defaults to filename stem)
        agent_id: Mnemos agent ID (default: MNEMOS_AGENT_ID env, then "default")
        db_path: Path to Mnemos database. Defaults through one-store scope
            resolution when omitted.
        api_key: OpenRouter API key (falls back to env/openclaw config)

    Environment:
        MNEMOS_USER_NAME / MNEMOS_AGENT_NAME — names used in transcript framing
        MNEMOS_KNOWN_PROJECTS / MNEMOS_ACTIVE_PROJECTS — comma-separated
        MNEMOS_OPENCLAW_AGENTS — comma-separated, overrides agent discovery

    Returns:
        Dict with keys: session_id, memories_encoded, skipped_reason
    """
    from mnemos.indexer.session_indexer import SessionIndexer

    agent_id = _resolve_agent_id(agent_id)

    path = Path(transcript_path)
    if not path.exists():
        logger.warning("Transcript not found: %s", transcript_path)
        return {"session_id": session_id, "memories_encoded": 0, "skipped_reason": "file_not_found"}

    session_key = session_id or path.stem
    # One-store invariant: route the default through resolve_scope (env >
    # config > canonical memory.db) instead of minting a per-agent sibling.
    from mnemos.simple_scope import resolve_scope

    db = db_path or resolve_scope(agent_id=agent_id).db_path

    # Resolve API key: explicit > env > openclaw agent config
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        # Look in OpenClaw agent model configs (they store OpenRouter keys)
        for agent_name in _discover_openclaw_agents():
            models_path = Path.home() / ".openclaw" / "agents" / agent_name / "agent" / "models.json"
            if models_path.exists():
                try:
                    models = json.loads(models_path.read_text())
                    for provider in models.get("providers", {}).values():
                        k = provider.get("apiKey", "")
                        if k and k.startswith("sk-or"):
                            key = k
                            break
                except Exception:
                    pass
            if key:
                break

    # Claude Code sessions are subscription-authed: route memory extraction
    # through the local `claude` CLI (no API key) rather than the OpenRouter
    # path, which fails with 401 when no live OpenRouter key is available.
    llm_client = None
    try:
        from mnemos.llm import ClaudeCLIClient
        llm_client = ClaudeCLIClient(
            model=os.environ.get("MNEMOS_MODEL") or "claude-haiku-4-5-20251001"
        )
    except Exception:
        llm_client = None

    indexer = SessionIndexer(
        agent_id=agent_id,
        db_path=db,
        sessions_dirs=[],  # We don't need discovery — we have the path
        user_name=os.environ.get("MNEMOS_USER_NAME", "").strip() or "User",
        agent_name=os.environ.get("MNEMOS_AGENT_NAME", "").strip() or agent_id,
        openrouter_api_key=key,
        llm_client=llm_client,
        known_projects=_env_list("MNEMOS_KNOWN_PROJECTS"),
        active_projects=_env_list("MNEMOS_ACTIVE_PROJECTS"),
    )

    # Read transcript using our Claude Code-aware reader
    messages = read_claude_code_transcript(path)
    if len(messages) < indexer.min_session_messages:
        logger.info("Skipping %s — only %d messages", session_key, len(messages))
        return {"session_id": session_key, "memories_encoded": 0, "skipped_reason": "too_few_messages"}

    file_size = path.stat().st_size
    if file_size < indexer.min_session_size_bytes:
        logger.info("Skipping %s — too small (%d bytes)", session_key, file_size)
        return {"session_id": session_key, "memories_encoded": 0, "skipped_reason": "too_small"}

    # Check deduplication
    state = indexer._load_state()
    last_size = state.get("indexed_sessions", {}).get(session_key, {}).get("size", 0)
    if file_size == last_size:
        logger.info("Skipping %s — already indexed at this size", session_key)
        return {"session_id": session_key, "memories_encoded": 0, "skipped_reason": "already_indexed"}

    # Format transcript and extract memories
    transcript = indexer._format_transcript(messages)
    raw_memories = indexer._extract_memories(transcript)

    if not raw_memories:
        logger.info("No memories extracted from %s", session_key)
        state.setdefault("indexed_sessions", {})[session_key] = {
            "size": file_size,
            "indexed_at": _now_iso(),
            "memories_encoded": 0,
        }
        indexer._save_state(state)
        return {"session_id": session_key, "memories_encoded": 0, "skipped_reason": "no_memories"}

    # Encode to Mnemos
    encoded = indexer._encode_to_mnemos(raw_memories, session_key)

    # Update state
    state.setdefault("indexed_sessions", {})[session_key] = {
        "size": file_size,
        "indexed_at": _now_iso(),
        "memories_encoded": encoded,
    }
    state["last_run"] = _now_iso()
    state["total_memories_encoded"] = state.get("total_memories_encoded", 0) + encoded
    indexer._save_state(state)

    logger.info("Encoded %d memories from %s", encoded, session_key)
    return {"session_id": session_key, "memories_encoded": encoded}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main():
    """CLI entry point for manual/hook-triggered indexing."""
    import argparse

    parser = argparse.ArgumentParser(description="Index a Claude Code session into Mnemos")
    parser.add_argument("transcript_path", help="Path to the .jsonl session file")
    parser.add_argument("--session-id", help="Session identifier (defaults to filename)")
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Mnemos agent ID (default: MNEMOS_AGENT_ID env, then 'default')",
    )
    parser.add_argument("--db-path", help="Path to Mnemos database")
    parser.add_argument("--api-key", help="OpenRouter API key")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    result = index_session(
        transcript_path=args.transcript_path,
        session_id=args.session_id,
        agent_id=args.agent_id,
        db_path=args.db_path,
        api_key=args.api_key,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
