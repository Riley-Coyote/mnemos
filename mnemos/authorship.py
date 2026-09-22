"""Who wrote a note: the model behind the agent, not only the agent scope.

One Mnemos scope is often shared by several models over time. The same
``claude-code`` store is written by whichever model the human happens to be
running that day, and a note that reaches the next session as "your own
words" makes every later model inherit the previous one's first person. A
signed note says who wrote it, so the reader can tell a colleague's note from
its own.

Signatures come from, in order:

1. ``MNEMOS_AGENT_MODEL`` — an operator's explicit setting;
2. ``mnemos_introduce`` in the current session (tracked by the runtime);
3. the harness itself, when it records the model — Claude Code writes the
   model id on every assistant turn of the session transcript;
4. nothing. An unsigned note is recorded as unsigned, never guessed.

Detection reads only the tail of the transcript and keeps only the model id.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")
_TAIL_START = 256 * 1024
_TAIL_MAX = 4 * 1024 * 1024


def clean_model_id(value: object) -> str:
    """A model id if ``value`` looks like one, else ``""``.

    Harness placeholders such as Claude Code's ``<synthetic>`` (recorded for
    turns no model produced) are not signatures.
    """

    if isinstance(value, Mapping):
        value = value.get("id") or value.get("model") or ""
    if not isinstance(value, str):
        return ""
    model = value.strip()
    if not model or not _MODEL_ID.fullmatch(model):
        return ""
    return model


def display_name(model: str) -> str:
    """A readable name for a model id: ``claude-opus-5-5`` -> ``Opus 5.5``.

    Unknown formats are returned unchanged; a signature is never invented.
    """

    model = clean_model_id(model)
    if not model:
        return ""
    base = re.sub(r"\[[^\]]*\]$", "", model)  # context-window suffixes, e.g. [1m]
    base = base.rsplit("/", 1)[-1]  # anthropic/claude-…
    base = re.sub(r"^(?:[a-z]{2}\.)?anthropic\.", "", base)  # Bedrock ids
    base = re.sub(r"-v\d+(?::\d+)?$", "", base)
    base = re.sub(r"-\d{8}$", "", base)  # dated snapshots
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?", base)
    if match:
        family, major, minor = match.groups()
        return f"{family.capitalize()} {major}{'.' + minor if minor else ''}"
    match = re.fullmatch(r"claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)", base)
    if match:
        major, minor, family = match.groups()
        return f"{family.capitalize()} {major}{'.' + minor if minor else ''}"
    return model


def same_model(a: str, b: str) -> bool:
    """Whether two model ids name the same model (dated snapshots included)."""

    a, b = clean_model_id(a), clean_model_id(b)
    if not a or not b:
        return False
    return a == b or display_name(a) == display_name(b)


def signature(model: str) -> str:
    """``Opus 5.5 (claude-opus-5-5)``, or just the id when no nicer name exists."""

    model = clean_model_id(model)
    if not model:
        return ""
    name = display_name(model)
    return model if name == model else f"{name} ({model})"


def detect_harness_model(environ: Mapping[str, str] | None = None) -> str:
    """The model the current harness session is running, if it says so.

    Claude Code gives every MCP server it spawns ``CLAUDE_CODE_SESSION_ID`` and
    records ``message.model`` on each assistant turn of that session's
    transcript. The most recent one is the model making the current call,
    which also follows a mid-session model switch. Any failure — no session,
    no transcript, an unreadable file — returns ``""``: an unsigned note is
    better than a wrong signature, and a write must never fail over this.
    """

    env = os.environ if environ is None else environ
    session_id = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not _SESSION_ID.fullmatch(session_id):
        return ""
    try:
        transcript = _claude_code_transcript(session_id, env)
        return _last_assistant_model(transcript) if transcript else ""
    except Exception:
        return ""


def _claude_code_transcript(session_id: str, env: Mapping[str, str]) -> Path | None:
    config = env.get("CLAUDE_CONFIG_DIR") or ""
    root = Path(config).expanduser() if config else Path.home() / ".claude"
    projects = root / "projects"
    if not projects.is_dir():
        return None
    # The project folder follows the session's working directory, which can
    # change mid-session, so look the session up by its id every time rather
    # than remembering where it was.
    matches = [path for path in projects.glob(f"*/{session_id}.jsonl") if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _last_assistant_model(path: Path) -> str:
    size = path.stat().st_size
    window = min(size, _TAIL_START)
    with path.open("rb") as handle:
        while True:
            handle.seek(size - window)
            lines = handle.read(window).split(b"\n")
            if window < size:
                lines = lines[1:]  # the first line of a window may be partial
            for raw in reversed(lines):
                if b'"assistant"' not in raw or b'"model"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                model = clean_model_id(message.get("model") if isinstance(message, dict) else None)
                if model:
                    return model
            if window >= size or window >= _TAIL_MAX:
                return ""
            window = min(size, window * 4, _TAIL_MAX)


def resolve_author_model(declared: str = "", environ: Mapping[str, str] | None = None) -> str:
    """The signature for a write: operator setting, declaration, then harness."""

    env = os.environ if environ is None else environ
    return (
        clean_model_id(env.get("MNEMOS_AGENT_MODEL", ""))
        or clean_model_id(declared)
        or detect_harness_model(env)
    )


def note_signature(entry: Mapping[str, object]) -> str:
    """How a note is signed in a packet: the model, ``Mnemos``, or ``unsigned``."""

    if entry.get("authored_by") == "system":
        return "Mnemos"
    name = display_name(str(entry.get("author_model") or ""))
    if name:
        return f"by {name}"
    return "co-formed" if entry.get("authored_by") == "coauthored" else "unsigned"


def handoff_framing(author_model: str, age: str, reader_model: str = "") -> tuple[str, str]:
    """Heading and guidance for a handoff, honest about who wrote it.

    The reader is named only when the harness has said which model it is;
    otherwise the guidance asks the reader to compare the signature itself.
    A handoff is never presented as the reader's own words unless it is.
    """

    quiet = "Don't narrate the memory system to the human."
    author = signature(author_model)
    reader = signature(reader_model)
    if not author:
        return (
            f"Left by an earlier session, {age}. It isn't signed.",
            "Several models may have worked here, so don't assume you wrote it. "
            f"Take what's useful. {quiet}",
        )
    if reader and same_model(author_model, reader_model):
        return (
            f"Left by {author} — the same model as you — {age}.",
            f"Carry on from it. {quiet}",
        )
    if reader:
        return (
            f"Left by {author}, {age}. You are {reader}, a different model.",
            "This is a colleague's note, not your memory: take what's useful and "
            f"don't claim their work as yours. {quiet}",
        )
    return (
        f"Left by {author}, {age}.",
        "Several models share this memory, and every note is signed by the one "
        "that wrote it. If this signature isn't yours, it's a colleague's note, "
        f"not something you did: take what's useful and don't claim it. {quiet}",
    )
