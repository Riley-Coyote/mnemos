"""Who wrote a note: the model behind the agent, not only the agent scope.

One Mnemos scope is often shared by several models over time. The same
``claude-code`` store is written by whichever model the human happens to be
running that day, and a note that reaches the next session as "your own
words" makes every later model inherit the previous one's first person. A
signed note says who wrote it, so the reader can tell a colleague's note from
its own.

Signatures are resolved on every write, because the human can switch models
in the middle of a session. They come from, in order:

1. ``signed_as`` — the model id the agent gives on the write itself (an agent
   always knows its own model);
2. ``MNEMOS_AGENT_MODEL`` — an operator's explicit setting;
3. ``mnemos_introduce`` — the model *this* session last introduced itself
   as. Never another session's: one store is shared by many sessions and
   models, and a store-wide declaration once signed every session's notes
   with whichever model had introduced itself last;
4. the harness itself, when it records the model — Claude Code writes the
   model id on every assistant turn of the session transcript;
5. nothing. An unsigned note is recorded as unsigned, never guessed.

Who is *reading* a session-start packet is found the same way, and then, only
there, from the harness's launch line (``launch_model``): at session start
there is no transcript yet.

Detection reads only the tail of the transcript and keeps only the model id.

Notes and memories are also marked with the harness session that wrote them.
Several sessions often run at once in one scope, and a note left by another
session is a colleague's even when the same model wrote it.

Every memory also says what kind of writer wrote its words (``AUTHOR_KINDS``),
set when it is written and never inferred later: only the agent's own words
make it who it is.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")
_TAIL_START = 256 * 1024
_TAIL_MAX = 4 * 1024 * 1024

#: Who wrote a memory's words: the agent itself, a tool working for Mnemos (a
#: program or a model, such as the transcript indexer), Mnemos's own templates,
#: something brought in from elsewhere, or no one known.
AUTHOR_KINDS = ("agent", "tool", "system", "import", "unknown")

# Where a store keeps the model each harness session last introduced itself as.
_SESSION_INTRODUCTION = "session_model:"


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


def clean_session_id(value: object) -> str:
    """A harness session id if ``value`` looks like one, else ``""``."""

    if not isinstance(value, str):
        return ""
    session = value.strip()
    return session if _SESSION_ID.fullmatch(session) else ""


def harness_session(environ: Mapping[str, str] | None = None) -> str:
    """The Claude Code session this process serves, or ``""``.

    Claude Code gives every MCP server it spawns ``CLAUDE_CODE_SESSION_ID``,
    and its SessionStart hook receives the same id as ``session_id``, so a
    handoff written here and the packet read at the next start agree on which
    session is which without the agent saying anything. The id survives
    compaction. Clients that don't set it get ``""``.
    """

    env = os.environ if environ is None else environ
    return clean_session_id(env.get("CLAUDE_CODE_SESSION_ID"))


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
    session_id = harness_session(env)
    if not session_id:
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


# ``--model claude-opus-5-5`` or ``--model=claude-opus-5-5`` in a launch line.
_LAUNCH_MODEL = re.compile(r"(?:^|\s)--model(?:=|\s+)(\S+)")


def launch_model(
    environ: Mapping[str, str] | None = None,
    *,
    command_line: Callable[[int], str | None] | None = None,
) -> str:
    """The model the Claude Code session running this hook was launched with,
    if its launch line names one.

    A SessionStart hook runs before the session has a transcript, and Claude
    Code's payload doesn't name the model on a fresh start, so nothing else can
    say who is about to read the packet. Claude Code gives its hooks
    ``CLAUDE_PID``, its own process, and the Claude desktop app launches every
    session with ``--model <id>``; the launch line says which model runs it.
    Only a model Mnemos can name counts: an alias such as ``opus`` or
    ``opus[1m]`` names no particular model and returns ``""``, as does no
    ``CLAUDE_PID`` (not a Claude Code hook) or any failure. A later ``/model``
    switch isn't in the launch line, so this comes after the transcript, which
    follows switches.
    """

    env = os.environ if environ is None else environ
    pid = (env.get("CLAUDE_PID") or "").strip()
    if not pid.isdigit() or int(pid) <= 1 or os.name == "nt":
        return ""
    args = (command_line or _command_line)(int(pid))
    match = _LAUNCH_MODEL.search(args or "")
    if not match:
        return ""
    model = clean_model_id(match.group(1).strip("'\""))
    return model if model and display_name(model) != model else ""


def _command_line(pid: int) -> str | None:
    """A process's command line, from ``ps``; ``None`` on failure."""

    import subprocess

    try:
        done = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def resolve_author_model(
    declared: str = "",
    environ: Mapping[str, str] | None = None,
    *,
    signed_as: str = "",
) -> str:
    """The signature for a write: the model the agent signed it as, the
    operator's setting, this session's own introduction (``declared``), then
    the harness. ``""`` when none of them says; never a guess."""

    env = os.environ if environ is None else environ
    return (
        clean_model_id(signed_as)
        or clean_model_id(env.get("MNEMOS_AGENT_MODEL", ""))
        or clean_model_id(declared)
        or detect_harness_model(env)
    )


def session_introduction_key(session_id: str) -> str:
    """The meta key holding what one harness session introduced itself as."""

    return f"{_SESSION_INTRODUCTION}{session_id}"


def session_introduction_record(model: str, name: str = "") -> str:
    """What a store keeps for a session's introduction: the model, the name
    the agent goes by, and when."""

    return json.dumps(
        {"model": model, "name": name, "at": datetime.now(timezone.utc).isoformat()},
        sort_keys=True,
    )


def introduced_as(record: str | None) -> tuple[str, str]:
    """The model and name a stored session introduction names, or ``""``s."""

    try:
        value = json.loads(record or "")
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(value, dict):
        return "", ""
    name = value.get("name")
    return clean_model_id(value.get("model")), name.strip() if isinstance(name, str) else ""


def introduced_model(record: str | None) -> str:
    """The model a stored session introduction names, or ``""``."""

    return introduced_as(record)[0]


def session_introduction(
    get_meta: Callable[[str], str | None], environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The model and name the current harness session last introduced itself
    as, from a store's record (``get_meta`` reads it); ``""``s when the
    session isn't known or never introduced itself. Never another session's."""

    session = harness_session(environ)
    if not session:
        return "", ""
    try:
        return introduced_as(get_meta(session_introduction_key(session)))
    except Exception:
        return "", ""


def note_signature(entry: Mapping[str, object]) -> str:
    """How a note is signed in a packet: the model, ``Mnemos``, or ``unsigned``."""

    if entry.get("authored_by") == "system":
        return "Mnemos"
    name = display_name(str(entry.get("author_model") or ""))
    if name:
        return f"by {name}"
    return "co-formed" if entry.get("authored_by") == "coauthored" else "unsigned"


def lesson_signature(entry: Mapping[str, object]) -> str:
    """How a lesson is signed in a packet: ``lesson``, unless a tool wrote it.

    A lesson copies the words it was drawn from. One drawn from the transcript
    indexer's output is that model's words, and a packet presenting it as a
    lesson hands the agent another writer's words as its own.
    """

    if entry.get("author_kind") == "tool":
        return "from a tool, not yours"
    return "lesson"


def from_same_session(reader_session: str, note_session: object) -> bool | None:
    """Whether a note came from the reader's own session; ``None`` if unknown.

    Both sides must be known. A note written before sessions were told apart,
    or read by a client that can't say which session it is, is neither.
    """

    reader = clean_session_id(reader_session)
    note = clean_session_id(note_session)
    if not reader or not note:
        return None
    return reader == note
