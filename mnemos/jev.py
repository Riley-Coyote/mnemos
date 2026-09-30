"""Jev, the cue's judge (WP-R16b): which of the cue's memories bear on a message.

Riley's picture: with each message, spreading activation proposes the memories
that may bear on it, and Jev decides which of them fire. The cue's ranking and
floors propose (``simple_runtime.cue_memories``, at most ``CANDIDATES`` lines,
before the cue's cap of three); Jev answers one yes-or-no question per line, as
a probability; the cue shows at most three of the lines it scores at least
``cue.CUE_JUDGE_THRESHOLD``. Jev is a typed judge (typesafe.ai's systemone).

- **Off by default.** On only with ``"cue_judge": "jev"`` in
  ``~/.mnemos/config.json`` or ``MNEMOS_CUE_JUDGE=jev`` (the environment wins),
  and a key: ``~/.config/jev/api_key``, or the file ``MNEMOS_JEV_KEY_FILE``
  names. No key, no Jev: the cue runs as it does with the switch off.
- **The key** is read from its file at each call and goes only into that
  call's Authorization header. It is never an argument, a log line, an error's
  words or part of a reply.
- **What leaves the machine:** the message, up to ``MESSAGE_CHARS`` characters
  (a longer one goes as its start and its end: ``message_sent``), and at most
  ``CANDIDATES`` memory lines of at most ``LINE_CHARS`` characters each. No
  id, date or scope.
- **Typed decisions only.** Jev's scores decide what the cue shows. They never
  change a memory, a link or a belief, and no words of Jev's are kept.
- **Fail quiet.** One call per message, at most ``TIMEOUT`` seconds end to end
  (name lookup included), no retry. A timeout or an error raises ``JevFailed``,
  and the cue then shows nothing.

This module imports only the standard library, and ``urllib`` only when it
calls: the prompt hook reads the switch through it on every message.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

JEV = "jev"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_HOST = "api.typesafe.ai"
JEV_MODEL = "jev-latest"

# The switch: the environment, else the config file's key. Only "jev" is on.
SWITCH_ENV = "MNEMOS_CUE_JUDGE"
SWITCH_KEY = "cue_judge"
KEY_FILE_ENV = "MNEMOS_JEV_KEY_FILE"

# What leaves the machine with one message: at most this many lines, each cut
# to at most this many characters (the cue's own line length).
CANDIDATES = 6
LINE_CHARS = 200
# And the message, up to this many characters. A longer one goes as its first
# MESSAGE_HEAD and last MESSAGE_TAIL characters with MESSAGE_MARK between:
# people put what they ask at either end of a pasted log. The mark counts
# toward the cap, so what leaves is never more than MESSAGE_CHARS.
MESSAGE_CHARS = 1000
MESSAGE_HEAD = 700
MESSAGE_MARK = " … "
MESSAGE_TAIL = MESSAGE_CHARS - MESSAGE_HEAD - len(MESSAGE_MARK)  # 297
# Jev's time, end to end, in seconds.
TIMEOUT = 0.4

# The lab's wording (mnemos-lab, notebook/jev-cue-gate/jev_cue_test.py), which
# the offline check of 2026-09-29 was scored with: the message, then one
# question per memory line.
QUESTION = (
    "Does memory_{n} bear directly on the message, so that recalling it would "
    "change or sharpen a good reply?"
)
CRITERIA = {
    "true": (
        "Recalling this memory would change or sharpen what a good reply to the "
        "message says or does: it is about the same work, decision or preference "
        "the message is about."
    ),
    "false": (
        "It is only loosely related, about a different project, or generic advice "
        "that would not change the reply."
    ),
}


class JevFailed(Exception):
    """Jev gave no usable answer. ``kind`` is "timeout", "error", "no-key" or
    "answer" (it answered, but not every question with a probability);
    ``detail`` is a status code or an exception's type, never a message or a
    body, so the key can't ride along."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# ── The switch ──


def key_file(environ: Mapping[str, str] | None = None) -> Path:
    """Where the key is read from: ``MNEMOS_JEV_KEY_FILE``, else
    ``~/.config/jev/api_key``."""
    env = os.environ if environ is None else environ
    named = (env.get(KEY_FILE_ENV) or "").strip()
    return Path(named).expanduser() if named else Path.home() / ".config" / "jev" / "api_key"


def has_key(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a key file is there and not empty. Reads its size, not the key."""
    try:
        return key_file(environ).stat().st_size > 0
    except (OSError, RuntimeError):  # RuntimeError: no home to find it under
        return False


def switched_on(environ: Mapping[str, str] | None = None, *, config_path: str | Path | None = None) -> bool:
    """Whether the switch names Jev: ``MNEMOS_CUE_JUDGE`` when set, else
    ``cue_judge`` in the config file. Off whenever the setting can't be read."""
    env = os.environ if environ is None else environ
    said = (env.get(SWITCH_ENV) or "").strip()
    if not said:
        try:
            from .config.loader import load_config

            value = load_config(config_path).get(SWITCH_KEY)
        except Exception:
            return False
        said = value.strip() if isinstance(value, str) else ""
    return said.lower() == JEV


def in_use(environ: Mapping[str, str] | None = None, *, config_path: str | Path | None = None) -> bool:
    """Whether the cue asks Jev: the switch is on and there is a key."""
    return switched_on(environ, config_path=config_path) and has_key(environ)


def status(environ: Mapping[str, str] | None = None, *, config_path: str | Path | None = None) -> dict[str, Any]:
    """The switch as this process sees it, for health: never the key."""
    on = switched_on(environ, config_path=config_path)
    key = has_key(environ)
    path = key_file(environ)
    try:
        shown = "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        shown = str(path)
    return {"switched_on": on, "key": key, "key_file": shown, "in_use": on and key,
            "host": JEV_HOST, "message_chars": MESSAGE_CHARS, "candidates": CANDIDATES,
            "line_chars": LINE_CHARS}


# ── One call ──


def message_sent(message: str) -> str:
    """What Jev is sent of a message: all of it up to ``MESSAGE_CHARS``
    characters; a longer one as its first ``MESSAGE_HEAD``, then
    ``MESSAGE_MARK``, then its last ``MESSAGE_TAIL``."""
    text = str(message)
    if len(text) <= MESSAGE_CHARS:
        return text
    return text[:MESSAGE_HEAD] + MESSAGE_MARK + text[-MESSAGE_TAIL:]


def request_body(message: str, lines: Sequence[str]) -> dict[str, Any]:
    """What Jev is sent, and all of it: the message and each line, and one
    question per line in the lab's words."""
    state: dict[str, str] = {"message": message}
    questions: dict[str, Any] = {}
    for n, line in enumerate(lines, start=1):
        state[f"memory_{n}"] = line
        questions[f"memory_{n}"] = {
            "type": "noul",
            "instructions": QUESTION.format(n=n),
            "criteria": dict(CRITERIA),
        }
    return {"state": state, "model": JEV_MODEL, "questions": questions}


def ask(
    message: str,
    lines: Sequence[str],
    *,
    timeout: float = TIMEOUT,
    environ: Mapping[str, str] | None = None,
    url: str | None = None,
) -> list[float]:
    """Jev's probability that each line bears on the message, in order: one
    call for all of them. At most ``CANDIDATES`` lines are sent, each cut to
    ``LINE_CHARS``, and the message as ``message_sent`` cuts it; only as many
    scores come back. No lines, no call. ``url`` is ``JEV_URL`` unless a test
    names its own stand-in.

    Raises ``JevFailed`` on a timeout (``timeout`` seconds, end to end), an
    error, a missing key, or an answer without a probability for every line.
    """
    sent = [str(line)[:LINE_CHARS] for line in list(lines)[:CANDIDATES]]
    if not sent:
        return []
    key = _read_key(environ)
    body = json.dumps(request_body(message_sent(message), sent)).encode("utf-8")
    target = url or JEV_URL
    finished = threading.Event()
    outcome: dict[str, Any] = {}

    def call() -> None:
        try:
            outcome["payload"] = _post(target, body, key, timeout)
        except BaseException as exc:  # noqa: BLE001 - carried to the caller as a kind
            outcome["failed"] = _failure(exc)
        finally:
            finished.set()

    # The whole call runs on its own thread, so a stalled name lookup or a
    # server that trickles its answer can't hold the caller past ``timeout``.
    threading.Thread(target=call, name="mnemos-jev", daemon=True).start()
    if not finished.wait(timeout):
        raise JevFailed("timeout")
    if "failed" in outcome:
        raise outcome["failed"]
    return _scores(outcome.get("payload"), len(sent))


def _read_key(environ: Mapping[str, str] | None) -> str:
    try:
        key = key_file(environ).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, RuntimeError):
        raise JevFailed("no-key") from None
    if not key:
        raise JevFailed("no-key")
    return key


def _post(url: str, body: bytes, key: str, timeout: float) -> Any:
    import urllib.request

    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - a fixed https URL
        return json.loads(response.read())


def _failure(exc: BaseException) -> JevFailed:
    """What went wrong, as a kind and a detail that can't hold the key."""
    import socket
    import urllib.error

    if isinstance(exc, JevFailed):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return JevFailed("error", f"HTTP {exc.code}")
    reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return JevFailed("timeout")
    if isinstance(exc, ValueError):
        return JevFailed("answer", type(exc).__name__)
    return JevFailed("error", type(reason if isinstance(reason, BaseException) else exc).__name__)


def _scores(payload: Any, count: int) -> list[float]:
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevFailed("answer", "no answers")
    scores = []
    for n in range(1, count + 1):
        answer = answers.get(f"memory_{n}")
        value = answer.get("noul") if isinstance(answer, dict) else None
        try:
            score = float(value)
        except (TypeError, ValueError):
            raise JevFailed("answer", f"memory_{n}") from None
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise JevFailed("answer", f"memory_{n}")
        scores.append(score)
    return scores
