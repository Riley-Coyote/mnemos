"""The cue: experience that comes to the message (WP-R16).

Asking the mind to dig doesn't work. In the lab, with a notebook present, the
agent searched Mnemos about four times a question instead of seven, and lessons
for new situations fell from 82% to 35%; telling it in the instructions to
search for what it learned changed nothing. Memory that works doesn't wait to
be asked: the cue brings it back.

So a Claude Code ``UserPromptSubmit`` hook (``mnemos hook prompt``) reads each
message the human sends and prints, for the model, at most three memories that
may bear on it, one line each: the lesson when the memory has one, else its own
words, with its date and its id. Or nothing, which is the usual answer.

- **The gate.** A memory is offered only when it is among the first
  ``CUE_POOL`` of recall's own ranking (words and meaning fused, R08) and its
  meaning is close: cosine at least ``CUE_FLOOR``, or at least
  ``CUE_WORD_FLOOR`` when it also shares a rare word with the message (the
  word path: one in at most ``CUE_WORD_CUT`` of the live memories in scope).
  A shared word counts as distinctive only while it is in at most
  ``COMMON_SHARE`` of them (WP-R08c). On the lab's 20 development prompts
  every floor from 0.30 to 0.50 showed 38 or 39 lines: what a higher floor
  turned away, the word path let back. At 0.50, 17 of the 38 came by it, on
  words like "page" (in 20% of the memories) and "claude" (17%).
  Messages with fewer than ``CUE_MIN_WORDS`` content words get nothing.
- **Never twice in a session.** The session-start hook records what its
  briefing showed (so does ``mnemos_context``, for a session that fetches its
  briefing) and the cue records what it offers, in a small file per session
  under ``~/.mnemos/run`` (``SeenFile``). Anything already shown is skipped.
- **Shown is not used.** An offered line is counted as offered, in that file,
  and never reinforces anything. Recalling it by id afterwards is a use. The
  file also counts the messages the cue answered by meaning or by words, those
  it answered with nothing (silences), and those it could not answer
  (failures); the watchdog reads them (``offered_summary``).
- **Fast, with meaning.** The session's own Mnemos server keeps the embedding
  model warm (``CueAnswerer``) and answers on a unix socket named for the
  Claude Code process that spawned it. The hook finds it by ``CLAUDE_PID``,
  which Claude Code gives every hook, and which ``/clear`` does not change
  (``/clear`` gives the session a new id, and keeps its MCP servers running
  with the old one). With no answerer, a slow one, or one speaking an older
  protocol, the hook answers from words alone. The model loads at the server's
  start where the hook is in use, and otherwise only when a cue first asks.
- **Read-only.** The cue writes no memory. The per-session files and the
  socket are outside the store; the one row it keeps there, only while the
  judge is switched on, counts Jev's recent calls (below).
- **Jev decides, when switched on** (WP-R16b, off by default; ``mnemos.jev``).
  The hook asks the answerer to judge: its candidates before the cap, at most
  ``jev.CANDIDATES`` lines that cleared the floors, go to Jev with the message
  (at most ``jev.MESSAGE_CHARS`` characters of it) in one call, and the cue
  shows at most ``CUE_LINES`` of those Jev scores at
  least ``CUE_JUDGE_THRESHOLD``, the likeliest first. On a timeout or an error
  it shows nothing, and so it does without an answerer that judged: quiet beats
  noisy. The answerer counts every outcome for the health card, and keeps the
  outcomes of the last ``JUDGE_CALLS_KEPT`` calls (when, and answered, timed
  out or failed; nothing else) in one row of the store (``JUDGE_CALLS_KEY``),
  so that repeated failures are seen from every session and by
  ``mnemos doctor``.

The hook imports nothing heavy: no torch, no MCP.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import jev

log = logging.getLogger("mnemos.cue")

# The answerer and the hook speak this protocol. A change to what an answer
# means (the gate, the lines) raises it, so a hook never trusts a server still
# running older code: it answers from words alone instead. 2: a shared word
# counts only below the frequency cut, and the word path needs a rarer one
# (WP-R08c). A hook with the judge switched on also asks the answerer to judge
# and says how long it waits (``judge``, ``wait``: WP-R16b), and trusts only a
# reply that says it judged (``judge``). An older answerer ignores both fields
# and never says so, so its lines don't show; without the switch nothing
# changes, and the protocol stays 2.
CUE_PROTOCOL = 2

# What the cue offers: at most this many memories, one line each.
CUE_LINES = 3
# How long one line may run, its words cut at a sentence boundary (" […]" and
# all), before its date and id.
CUE_LINE_CHARS = 200
# How far down recall's ranking a memory may sit and still be offered.
CUE_POOL = 10
# The meaning floor: the cosine a memory must reach with the message (the
# starting floor, chosen for the lab's L06). A memory sharing a distinctive
# word with the message needs only CUE_WORD_FLOOR. On the lab's 20 development
# prompts every floor from 0.30 to 0.50 showed 38 or 39 lines, 9 of them holding
# a fact for the task: what a higher floor turns away, the word path lets back.
CUE_FLOOR = 0.40
CUE_WORD_FLOOR = 0.25
# The word path's word: shared with the message, and in at most this share of
# the live memories in scope (a shared word counts at all only at or below
# ``fts.COMMON_SHARE``). On the lab's L06 prompts, with recall's ranking as it
# was, 0.02 showed 36 lines against 38 at 0.08: every one of the 20 that bore
# on the message, and 14 that didn't against 17. A change here changes what
# an answer means: raise ``CUE_PROTOCOL`` with it.
CUE_WORD_CUT = 0.02
# Without meaning (no answerer, or one whose model is still loading): how many
# distinctive words a memory must share with the message. On the same prompts,
# 2 showed the most lines holding a fact for the task: 11 of 40 (1 word: 10 of
# 42; 3 words: 9 of 21).
CUE_WORDS_SHARED = 2
# Fewer content words than this ("ok", "yes", "beautiful") get nothing.
CUE_MIN_WORDS = 4

# The hook's hard cap, end to end, and its parts. The process takes about a
# tenth of a second to start and import before the hook's own clock starts;
# the process ends HOOK_BUDGET after that, and the work aims to finish
# HOOK_MARGIN sooner. The answerer gets CONNECT_SECONDS to be found and
# ANSWER_SECONDS to answer, which leaves room to answer from words.
HOOK_SECONDS = 0.8
HOOK_BUDGET = 0.7
HOOK_MARGIN = 0.05
CONNECT_SECONDS = 0.05
ANSWER_SECONDS = 0.35

# Jev's score a line needs to show, with the switch on (WP-R16b). On the lab's
# 20 development prompts the cue's candidates before its cap are 62 lines, 26
# of them bearing on their message (one reader's labels); without Jev it shows
# 36, 20 of them good. Through the real gate, seven passes: at 0.8 it showed
# 14 or 15 lines, 12 or 13 good (80-93%, about half the good ones), a bad line
# alone on at most one prompt; at 0.75, 16 or 17 (76-88%), a bad line alone on
# one or two; at 0.7, 17 to 20 (70-78%). Jev's score for the same line moved
# by up to 0.11 between passes.
CUE_JUDGE_THRESHOLD = 0.8
# With the switch on the hook waits for the answerer as long as its budget
# allows (there is no answering from words then), less this slack for what it
# does after; the answerer gives Jev at most ``jev.TIMEOUT`` of what the hook
# said it would wait, less this same slack for its reply. With less than
# ``JUDGE_MIN_SECONDS`` left, Jev isn't asked at all, and that counts as a
# timeout: the fastest of 148 calls in the live check took 126 ms.
JUDGE_MARGIN = 0.03
JUDGE_MIN_SECONDS = 0.1
# The payload Claude Code sends on stdin, at most.
PAYLOAD_BYTES = 1_048_576

# Per-session files and dead sockets older than this are removed on the way.
RUN_DAYS = 7
# The offers one session file keeps, newest last.
OFFERS_KEPT = 200

# The one row the answerers keep in the store while the judge is switched on
# (``meta``): the outcomes of Jev's last this-many calls, from every session,
# each as when it was and whether Jev answered ("answered"), timed out
# ("timeout") or failed ("error"). Nothing of the message, the lines or the
# key. The watchdog flags it when more than half of them failed.
JUDGE_CALLS_KEY = "cue_judge_calls"
JUDGE_CALLS_KEPT = 20
# How long an answerer waits for the store's lock to save an outcome. Past it
# the outcome is left out: the row is a count, and the reply already went.
JUDGE_SAVE_WAIT = 0.25

HEADING = "## Mnemos: from memory, maybe bearing on this message"

_CONTEXT_MARK = "\n\nContext: "
_LESSON_TAGS = frozenset({"lesson", "distilled"})
_LEADING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\b[,:;]?\s+")
_SAFE = re.compile(r"[^0-9A-Za-z-]")


class HookTimeout(Exception):
    """The hook ran out of its time. It prints nothing."""


def arm_hard_stop(seconds: float) -> threading.Event:
    """End this process, printing nothing and exiting 0, ``seconds`` from now,
    unless the returned event is set first. The hook's hard cap: it holds
    whatever the hook is waiting on, a lock or a slow disk included. Only for
    the hook's own process, never one that called it as a function."""
    finished = threading.Event()

    def stop() -> None:
        if not finished.wait(seconds):
            os._exit(0)

    threading.Thread(target=stop, name="mnemos-hook-cap", daemon=True).start()
    return finished


# ── Where the cue keeps its files ──


def run_dir() -> Path:
    """``~/.mnemos/run``: the per-session files and the answerers' sockets.
    Nothing in it is memory, and nothing in it is needed: a file lost here only
    means a line may be shown twice."""
    return Path.home() / ".mnemos" / "run"


def ensure_run_dir() -> Path:
    """The run folder, made private (0700) if it is not already."""
    folder = run_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(folder.stat().st_mode) != 0o700:
        os.chmod(folder, 0o700)
    return folder


def scope_key(db_path: str, agent_id: str, person_id: str, project_scope: str) -> str:
    """A short name for one store and scope, the same in the server and the
    hook however each was told the path."""
    real = os.path.realpath(os.path.expanduser(str(db_path)))
    digest = hashlib.sha256("\0".join((real, agent_id, person_id, project_scope)).encode())
    return digest.hexdigest()[:12]


def text_key(text: str) -> str:
    """The words a line showed, as a key: the same words shown under another
    id (a lesson and the memory it was drawn from) are not shown twice."""
    words = " ".join((text or "").lower().split())
    return hashlib.sha256(words.encode()).hexdigest()[:16]


# ── Never twice in a session ──


class SeenFile:
    """What one session has been shown, for one scope: the ids and words the
    briefing showed and the cue offered, and the cue's offers counted; and
    how the cue answered each message it looked at (``answers``, by meaning,
    by words or by Jev), how many of them it answered with nothing
    (``silences``) and how many it could not answer (``failures``, the last
    one's reason kept).

    One small JSON file per session under ``~/.mnemos/run`` (0600, the folder
    0700), replaced whole on every write. The session is Claude Code's session
    id: compaction keeps it, so what was shown before stays shown; ``/clear``
    starts a new one along with an empty context, and the briefing it brings is
    recorded there.
    """

    def __init__(self, session: str, key: str) -> None:
        self.session = _SAFE.sub("", session or "")[:64]
        self.key = key
        self.path = run_dir() / f"shown-{self.session}-{key}.json"

    def read(self) -> dict[str, Any]:
        empty = {"shown": [], "texts": [], "offered": 0, "offers": [], "answers": {},
                 "silences": 0, "failures": 0, "last_failure": None}
        if not self.session:
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
        last = data.get("last_failure")
        return {
            "shown": [i for i in data.get("shown") or [] if isinstance(i, str)],
            "texts": [t for t in data.get("texts") or [] if isinstance(t, str)],
            "offered": int(data.get("offered") or 0),
            "offers": [o for o in data.get("offers") or [] if isinstance(o, dict)],
            "answers": {str(via): _count_of(n) for via, n in answers.items()},
            "silences": _count_of(data.get("silences")),
            "failures": _count_of(data.get("failures")),
            "last_failure": last if isinstance(last, str) else None,
        }

    def record(
        self,
        ids: Iterable[str],
        texts: Iterable[str] = (),
        *,
        offer: Mapping[str, Any] | None = None,
        answered: str | None = None,
        failure: str | None = None,
    ) -> None:
        """Add what was just shown. An offer (the cue's) is also counted.

        ``answered`` is how the cue answered a message it looked at (its
        ``via``): with an offer, or else with nothing, a silence. ``failure``
        says why the cue could not answer one (it ran out of time, broke, or
        its judge failed); a failure is not a silence."""
        if not self.session:
            return
        folder = ensure_run_dir()
        data = self.read()
        shown = list(dict.fromkeys([*data["shown"], *ids]))
        known = list(dict.fromkeys([*data["texts"], *texts]))
        offers = data["offers"]
        offered = data["offered"]
        answers = dict(data["answers"])
        silences, failures, last_failure = data["silences"], data["failures"], data["last_failure"]
        if offer is not None:
            offers = [*offers, dict(offer)][-OFFERS_KEPT:]
            offered += len(offer.get("ids") or [])
            answered = answered or str(offer.get("via") or "") or None
        if failure:
            failures += 1
            last_failure = str(failure)[:200]
        elif answered:
            answers[answered] = answers.get(answered, 0) + 1
            if offer is None:
                silences += 1
        _write_private(self.path, {
            "v": CUE_PROTOCOL,
            "session": self.session,
            "scope": self.key,
            "shown": shown,
            "texts": known,
            "offered": offered,
            "offers": offers,
            "answers": answers,
            "silences": silences,
            "failures": failures,
            "last_failure": last_failure,
            "updated": datetime.now(timezone.utc).isoformat(),
        })
        sweep(folder)


def _count_of(value: Any) -> int:
    """A count read from a session file: a whole number, else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _write_private(path: Path, data: Mapping[str, Any]) -> None:
    """Replace ``path`` with ``data`` in one step, readable by the owner only."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def sweep(folder: Path | None = None, *, days: float = RUN_DAYS) -> int:
    """Remove per-session files older than ``days``, and old sockets nothing
    answers on. Returns how many went. Never raises."""
    folder = folder or run_dir()
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
            name = entry.name
            if name.startswith(("shown-", ".shown-")) and (name.endswith(".json") or name.endswith(".tmp")):
                os.unlink(entry.path)
                removed += 1
            elif name.startswith("cue-") and name.endswith(".sock") and not _answers(entry.path):
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def offered_summary(*, days: float = RUN_DAYS, key: str | None = None) -> dict[str, Any]:
    """What the cue did across sessions, from the per-session files changed in
    the last ``days`` (only one scope's with ``key``, a ``scope_key``): for the
    watchdog and the lab. Reads only, and makes nothing: no folder, no file.

    ``messages`` the cue looked at in ``sessions`` sessions; ``offers`` (the
    messages it brought lines to), ``offered`` (those lines) and
    ``silences`` (the messages it answered with nothing); ``answered_by``,
    those messages by how the cue answered ("meaning", "words" or "jev"), and
    ``by_via``, the lines offered the same way; and ``failures``, the messages
    it could not answer, with ``last_failure``, the newest one's reason. A file
    written before messages were counted gives its offers only."""
    summary: dict[str, Any] = {
        "sessions": 0, "messages": 0, "offers": 0, "offered": 0, "silences": 0,
        "failures": 0, "last_failure": None, "answered_by": {}, "by_via": {},
    }
    cutoff = time.time() - days * 86400
    suffix = f"-{key}.json" if key else ".json"
    try:
        entries = [e for e in os.scandir(run_dir()) if e.name.startswith("shown-") and e.name.endswith(suffix)]
    except OSError:
        return summary
    newest_failure = 0.0
    for entry in entries:
        try:
            changed = entry.stat().st_mtime
            if changed < cutoff:
                continue
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        offers = [o for o in data.get("offers") or [] if isinstance(o, dict)]
        answers = data.get("answers") if isinstance(data.get("answers"), dict) else None
        if answers is None:  # written before messages were counted: its offers
            answers = {}
            for offer in offers:
                via = str(offer.get("via") or "")
                answers[via] = answers.get(via, 0) + 1
        answers = {str(via): _count_of(n) for via, n in answers.items()}
        silences, failures = _count_of(data.get("silences")), _count_of(data.get("failures"))
        if not offers and not failures and not sum(answers.values()):
            continue  # only what a briefing showed: the cue never answered here
        summary["sessions"] += 1
        summary["offered"] += _count_of(data.get("offered"))
        summary["offers"] += len(offers)
        summary["silences"] += silences
        summary["failures"] += failures
        summary["messages"] += sum(answers.values()) + failures
        for via, count in answers.items():
            summary["answered_by"][via] = summary["answered_by"].get(via, 0) + count
        for offer in offers:
            via = str(offer.get("via") or "")
            summary["by_via"][via] = summary["by_via"].get(via, 0) + len(offer.get("ids") or [])
        if failures and isinstance(data.get("last_failure"), str) and changed >= newest_failure:
            newest_failure, summary["last_failure"] = changed, data["last_failure"]
    return summary


# ── The lines ──


def memory_line(engram: Any) -> tuple[str, bool]:
    """What the cue shows of a memory, and whether it is a lesson.

    A lesson is a memory tagged as one (its words are the lesson), or one whose
    impact the agent wrote: then the line is that impact. Otherwise it is the
    memory's own words, without the context its capture kept after them. An
    impact the server filled in, or one a model extracted, is not the agent's.
    """
    from .core.placeholders import written_lesson

    content = (getattr(engram, "content", "") or "").split(_CONTEXT_MARK, 1)[0].strip()
    impact = (getattr(engram, "impact", "") or "").strip()
    if _LESSON_TAGS & set(getattr(engram, "tags", None) or []):
        return content or impact, True
    lesson = written_lesson(content, impact, getattr(engram, "impact_source", "") or "")
    if lesson:
        return lesson, True
    return content or impact, False


def line_parts(text: str, created_at: str | None) -> tuple[str, str]:
    """A line's date and words: a leading date in the words is the date (and
    leaves them), else the memory's own; the words cut at a sentence boundary
    under ``CUE_LINE_CHARS``."""
    from .interface.context_packet import cut_at_sentence

    words = " ".join((text or "").split())
    match = _LEADING_DATE.match(words)
    if match:
        date, words = match.group(1), words[match.end():]
    else:
        date = (created_at or "")[:10] or "undated"
    cut, _ = cut_at_sentence(words, CUE_LINE_CHARS - len(" […]"))
    return date, cut


def format_block(lines: Iterable[Mapping[str, Any]]) -> str:
    """The block the hook prints for the model: a heading and one line per
    memory, or ``""``."""
    rows = []
    for line in lines:
        mark = ", lesson" if line.get("lesson") else ""
        rows.append(f"- {line['date']}{mark}: {line['text']} ({line['id']})")
    return "\n".join([HEADING, *rows]) if rows else ""


# ── Finding the session's answerer ──


def answerer_path(claude_pid: int, key: str) -> Path:
    """The socket of the answerer for one scope in one Claude Code process."""
    return run_dir() / f"cue-{int(claude_pid)}-{key}.sock"


def _parent_of(pid: int) -> int | None:
    """A process's parent, when this platform says cheaply; else None."""
    proc = Path(f"/proc/{pid}/stat")
    if proc.exists():
        try:
            return int(proc.read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=0.03, check=False,
        ).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def claude_pids(environ: Mapping[str, str] | None = None, *, deadline: float | None = None) -> list[int]:
    """The process that is this hook's Claude Code: ``CLAUDE_PID``, which
    Claude Code gives every hook (its own pid, the parent of every MCP server
    it spawned). A Claude Code too old to give it: this process's parent, then
    theirs, nearest first."""
    env = os.environ if environ is None else environ
    try:
        claude = int(env.get("CLAUDE_PID") or 0)
    except ValueError:
        claude = 0
    if claude > 1:
        return [claude]
    found: list[int] = []
    pid = os.getppid()
    for _ in range(3):
        if pid is None or pid <= 1:
            break
        if pid not in found:
            found.append(pid)
        if deadline is not None and time.monotonic() >= deadline:
            break
        pid = _parent_of(pid)
    return found


def ask_answerer(
    key: str,
    text: str,
    *,
    exclude: Collection[str] = (),
    texts: Collection[str] = (),
    environ: Mapping[str, str] | None = None,
    connect_seconds: float = CONNECT_SECONDS,
    answer_seconds: float = ANSWER_SECONDS,
    judge: str = "",
) -> dict[str, Any] | None:
    """Ask this session's answerer for the cue: its reply, or None when there
    is none, it is slow, it fails, or it speaks another protocol.

    ``judge`` ("jev") asks it to have its candidates judged (WP-R16b), and
    tells it how long this waits, so the judge's time fits inside it. An
    answerer that did judge says so in its reply (``judge``)."""
    if not hasattr(socket, "AF_UNIX"):
        return None
    started = time.monotonic()
    connect_by = started + connect_seconds
    sock = None
    for pid in claude_pids(environ, deadline=connect_by):
        path = answerer_path(pid, key)
        if not path.exists():
            continue
        remaining = connect_by - time.monotonic()
        if remaining <= 0:
            break
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        candidate.settimeout(remaining)
        try:
            candidate.connect(str(path))
        except OSError:
            candidate.close()
            continue
        sock = candidate
        break
    if sock is None:
        return None
    try:
        request = {"v": CUE_PROTOCOL, "op": "cue", "text": text,
                   "exclude": list(exclude), "texts": list(texts)}
        if judge:
            request.update({"judge": judge, "wait": round(answer_seconds, 3)})
        answer_by = time.monotonic() + answer_seconds
        sock.settimeout(answer_seconds)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        reply = _read_line(sock, answer_by)
    except OSError:
        return None
    finally:
        sock.close()
    if reply is None:
        return None
    try:
        data = json.loads(reply)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != CUE_PROTOCOL or not data.get("ok"):
        return None
    if not isinstance(data.get("lines"), list):
        return None
    return data


def _read_line(sock: socket.socket, deadline: float, limit: int = 1_048_576) -> str | None:
    chunks = bytearray()
    while b"\n" not in chunks:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(chunks) > limit:
            return None
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return None
        if not chunk:
            break
        chunks.extend(chunk)
    if b"\n" not in chunks:
        return None
    return bytes(chunks).split(b"\n", 1)[0].decode("utf-8", "replace")


def _answers(path: str) -> bool:
    """Whether something is listening on a socket path."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.05)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ── The answerer, inside the session's own Mnemos server ──

# Touched by the prompt hook each time it runs: the cue is in use on this
# machine. A server warms its model at start only when it is.
HOOK_MARK = "prompt-hook"


def mark_hook_in_use() -> None:
    """Record that the prompt hook ran just now. Never raises."""
    try:
        path = ensure_run_dir() / HOOK_MARK
        path.touch(mode=0o600)
        os.utime(path)
    except OSError:
        pass


def hook_in_use(*, days: float = RUN_DAYS) -> bool:
    """Whether the prompt hook has run on this machine in the last ``days``."""
    try:
        return time.time() - (run_dir() / HOOK_MARK).stat().st_mtime < days * 86400
    except OSError:
        return False


class CueAnswerer:
    """Keeps the embedding model warm and answers cue queries on a unix socket.

    Started by the simple MCP server (``run_simple_server``), in a background
    thread. Where the prompt hook is in use (it ran in the last week), the
    thread loads the model first and only then opens the socket, at
    ``~/.mnemos/run/cue-<Claude Code pid>-<scope key>.sock`` (0600, the folder
    0700): a hook never waits on a model still loading, and until the socket
    is there it answers from words. Where it isn't, loading the model would
    cost every session several hundred megabytes for nothing, so the socket
    opens at once and the model loads when the first cue asks; that cue, and
    any before the model is ready, are told to answer from words.

    Queries run on the answerer's own read-only connections to the store and
    its index, one at a time, and change nothing. It never takes a live
    answerer's socket, replaces a dead one's, and on stop removes the socket
    only while it is still its own. Without meaning to offer (no embedding
    backend, a model that can't load) it stops, and the server goes on as it
    was.

    Asked to judge (the hook's switch is on, WP-R16b), it takes up to
    ``jev.CANDIDATES`` of its lines before the cap, makes one call to ``judge``
    (``jev.ask`` unless a test gives another) within the time the hook said it
    would wait, and answers with at most ``CUE_LINES`` of the lines scoring at
    least ``CUE_JUDGE_THRESHOLD``, or with none when the judge timed out or
    failed. It counts each outcome (``judge_counts``) for the health card, and
    once the reply has gone keeps how each call ended in the store's one row
    for the judge (``_keep_judged``): the only thing it ever writes there.
    """

    def __init__(
        self,
        db_path: str,
        *,
        agent_id: str,
        person_id: str,
        project_scope: str,
        claude_pid: int | None = None,
        index_factory: Any = None,
        judge: Callable[..., Sequence[float]] | None = None,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.scope = {"agent_id": agent_id, "person_id": person_id, "project_scope": project_scope}
        self.key = scope_key(self.db_path, agent_id, person_id, project_scope)
        self.claude_pid = os.getppid() if claude_pid is None else claude_pid
        self.path = answerer_path(self.claude_pid, self.key) if self.claude_pid > 1 else None
        self._index_factory = index_factory
        self._index: Any = None
        self._store: Any = None
        self._listener: socket.socket | None = None
        self._inode: tuple[int, int] | None = None
        self._stopped = threading.Event()
        self._warming = threading.Lock()
        self._warm_started = False
        # The socket is open; the model is loaded.
        self.ready = threading.Event()
        self.warm = threading.Event()
        self.thread: threading.Thread | None = None
        self.answered = 0
        self._judge = judge
        self._judged_lock = threading.Lock()
        # One write of the judge's row at a time; stop() waits on it.
        self._keeping = threading.Lock()
        self._judged: dict[str, Any] = {
            # Messages the hook asked to have judged; those with nothing to
            # judge (no call); calls made; calls that answered; lines shown;
            # answered calls that showed nothing; timeouts; errors (a missing
            # key or an unusable answer among them), the last one's kind.
            "messages": 0, "no_candidates": 0, "calls": 0, "answered": 0,
            "shown": 0, "none_shown": 0, "timeouts": 0, "errors": 0,
            "last_failure": None,
        }
        # The outcomes of calls made since the last reply went, waiting to be
        # kept in the store's row (``_keep_judged``).
        self._unkept: list[tuple[str, str]] = []

    def start(self) -> bool:
        """Start in the background. False when this process has no Claude
        Code to answer (or no unix sockets)."""
        if self.path is None or not hasattr(socket, "AF_UNIX"):
            return False
        self.thread = threading.Thread(target=self._run, name="mnemos-cue", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self._stopped.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            for close in (lambda: listener.shutdown(socket.SHUT_RDWR), listener.close):
                try:
                    close()
                except OSError:
                    pass
        if self.path is not None and self._inode is not None:
            try:
                now = os.stat(self.path)
                if (now.st_dev, now.st_ino) == self._inode:
                    os.unlink(self.path)
            except OSError:
                pass
            self._inode = None
        # The row is written after each reply has gone, so a stop right after
        # a reply would lose it: keep what was judged before stopping, waiting
        # for a write already under way.
        self._keep_judged()

    # The threads

    def _run(self) -> None:
        try:
            if hook_in_use():
                self._warm_started = True
                if not self._warm() or self._stopped.is_set():
                    return
            if not self._bind():
                return
            self.ready.set()
            self._serve()
        except Exception as exc:  # the server goes on without the cue
            log.warning("The cue's answerer stopped: %s: %s", type(exc).__name__, exc)
        finally:
            self.stop()

    def _warm_later(self) -> None:
        """Load the model in the background, once: the first cue asked."""
        with self._warming:
            if self._warm_started:
                return
            self._warm_started = True

        def warm() -> None:
            try:
                if not self._warm():
                    self.stop()
            except Exception as exc:
                log.warning("The cue's answerer stopped: %s: %s", type(exc).__name__, exc)
                self.stop()

        threading.Thread(target=warm, name="mnemos-cue-warm", daemon=True).start()

    def _warm(self) -> bool:
        """Load the embedding model. False when there is no meaning to offer:
        no backend, or a model that cannot load."""
        if self._index_factory is not None:
            index = self._index_factory(self.db_path)
        else:
            from .store.embedding_index import EmbeddingIndex

            index = EmbeddingIndex(db_path=self.db_path, read_only=True)
        if not getattr(index, "available", False):
            return False
        if not index.verify():
            log.info("The cue answers from words alone: %s", index.unavailable_reason)
            return False
        self._index = index
        self.warm.set()
        return True

    def _bind(self) -> bool:
        assert self.path is not None
        ensure_run_dir()
        path = str(self.path)
        if len(os.fsencode(path)) >= 100:
            log.info("The cue's socket path is too long for this system: %s", path)
            return False
        if os.path.lexists(path):
            if _answers(path):
                log.info("Another server already answers the cue here: %s", path)
                return False
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(8)
        except OSError:
            listener.close()
            raise
        made = os.stat(path)
        self._inode = (made.st_dev, made.st_ino)
        self._listener = listener
        return True

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        # A blocked accept() is not woken on every system when another thread
        # closes the socket, so it wakes itself to see whether it should stop.
        listener.settimeout(1.0)
        while not self._stopped.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stopped.is_set() or exc.errno in (errno.EBADF, errno.EINVAL):
                    return
                continue
            conn.settimeout(None)
            with conn:
                try:
                    self._answer(conn)
                except Exception as exc:
                    log.debug("A cue query failed: %s: %s", type(exc).__name__, exc)

    def _answer(self, conn: socket.socket) -> None:
        started = time.perf_counter()
        line = _read_line(conn, time.monotonic() + 0.5)
        if line is None:
            return
        received = time.monotonic()
        try:
            request = json.loads(line)
        except ValueError:
            request = None
        if not isinstance(request, dict) or request.get("v") != CUE_PROTOCOL or request.get("op") != "cue":
            reply: dict[str, Any] = {"v": CUE_PROTOCOL, "ok": False, "why": "unknown request"}
        elif not self.warm.is_set():
            self._warm_later()
            reply = {"v": CUE_PROTOCOL, "ok": False, "why": "warming"}
        else:
            text = str(request.get("text") or "")
            judged = request.get("judge") == jev.JEV
            try:
                lines = self._lines(
                    text,
                    exclude=[i for i in request.get("exclude") or [] if isinstance(i, str)],
                    texts=[t for t in request.get("texts") or [] if isinstance(t, str)],
                    # Judged, the candidates before the cap; else the cap.
                    limit=jev.CANDIDATES if judged else None,
                )
                why = None if lines is not None else "no store"
            except Exception as exc:  # the hook answers from words instead
                lines, why = None, f"{type(exc).__name__}: {exc}"[:200]
            reply = {"v": CUE_PROTOCOL, "ok": lines is not None, "meaning": True,
                     "lines": lines or [], "why": why}
            if judged and lines is not None:
                by = received + _wait(request.get("wait")) - JUDGE_MARGIN
                reply["lines"], reply["judged"] = self._judged_lines(text, lines, by=by)
                reply["judge"] = jev.JEV
        reply["ms"] = round((time.perf_counter() - started) * 1000, 1)
        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        self.answered += 1
        # After the reply has gone, so the hook never waits on it.
        self._keep_judged()

    def _lines(
        self, text: str, *, exclude: list[str], texts: list[str], limit: int | None = None,
    ) -> list[dict[str, Any]] | None:
        if self._store is None:
            if not Path(self.db_path).is_file():
                return None
            from .store.sqlite_store import ReadOnlyEngramStore

            self._store = ReadOnlyEngramStore(self.db_path)
        from .simple_runtime import cue_memories

        return cue_memories(
            self._store, self._index, text, exclude=exclude, exclude_texts=texts, limit=limit,
            **self.scope,
        )

    # The judge (WP-R16b)

    def _judged_lines(
        self, text: str, lines: list[dict[str, Any]], *, by: float,
    ) -> tuple[list[dict[str, Any]], str]:
        """What the cue shows of ``lines`` once judged: at most ``CUE_LINES``
        scoring at least ``CUE_JUDGE_THRESHOLD``, the likeliest first, each
        with its score; and how the judging went ("shown", "none",
        "no-candidates", "timeout" or "error"). One call, which must end by
        ``by`` (a ``time.monotonic()`` value) and never outlasts
        ``jev.TIMEOUT``. On a timeout or an error, nothing."""
        self._count(messages=1)
        if not lines:
            self._count(no_candidates=1)
            return [], "no-candidates"
        room = min(jev.TIMEOUT, by - time.monotonic())
        if room < JUDGE_MIN_SECONDS:
            self._count(timeouts=1, last_failure="timeout")
            return [], "timeout"
        judge = self._judge or jev.ask
        self._count(calls=1)
        try:
            scores = [float(score) for score in judge(text, [line["text"] for line in lines], timeout=room)]
            if len(scores) != len(lines) or not all(0.0 <= score <= 1.0 for score in scores):
                raise jev.JevFailed("answer", "scores")
        except jev.JevFailed as failed:
            said = failed.kind + (f" ({failed.detail})" if failed.detail else "")
            if failed.kind == "timeout":
                self._count(timeouts=1, last_failure=said, outcome="timeout")
                return [], "timeout"
            self._count(errors=1, last_failure=said, outcome="error")
            return [], "error"
        except Exception as exc:  # a judge that broke: nothing shows
            self._count(errors=1, last_failure=f"error ({type(exc).__name__})", outcome="error")
            return [], "error"
        chosen = sorted(
            (index for index, score in enumerate(scores) if score >= CUE_JUDGE_THRESHOLD),
            key=lambda index: (-scores[index], index),
        )[:CUE_LINES]
        shown = [{**lines[index], "score": round(scores[index], 4)} for index in chosen]
        self._count(answered=1, shown=len(shown), none_shown=0 if shown else 1, outcome="answered")
        return shown, "shown" if shown else "none"

    def _count(
        self, *, last_failure: str | None = None, outcome: str | None = None, **increments: int,
    ) -> None:
        """Add to the judge's counts. ``outcome`` is how a call to Jev ended
        ("answered", "timeout" or "error"), kept for the store's row once the
        reply has gone (``_keep_judged``)."""
        with self._judged_lock:
            for name, n in increments.items():
                self._judged[name] += n
            if last_failure is not None:
                self._judged["last_failure"] = last_failure
            if outcome is not None:
                self._unkept.append((datetime.now(timezone.utc).isoformat(), outcome))

    def judge_counts(self) -> dict[str, Any]:
        """What judging has done in this process: a copy of the counts."""
        with self._judged_lock:
            return dict(self._judged)

    def _keep_judged(self) -> None:
        """Keep how this process's calls to Jev ended in the store's one row
        for them (``keep_judged_calls``), beside the other sessions' calls, so
        repeated failures are seen from every session and by ``mnemos
        doctor``. Only calls a hook asked for (its switch is on), and only by
        code at least as new as the store: what the row holds is newer code's
        to decide. Never raises: the row is a count, and the reply already
        went."""
        with self._keeping:
            with self._judged_lock:
                unkept, self._unkept = self._unkept, []
            if not unkept:
                return
            try:
                from .code_version import MAINTENANCE_CODE_VERSION

                minimum = self._store.min_code_version() if self._store is not None else None
                if minimum is not None and minimum > MAINTENANCE_CODE_VERSION:
                    return
                keep_judged_calls(self.db_path, unkept)
            except Exception as exc:
                log.debug("The judge's outcomes were not kept: %s: %s", type(exc).__name__, exc)


def judged_calls(value: Any) -> list[dict[str, str]]:
    """The calls a judge row (``JUDGE_CALLS_KEY``) holds, oldest first, each
    ``{"at": ..., "outcome": ...}``; nothing for a row it can't read."""
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    calls = data.get("calls") if isinstance(data, dict) else None
    kept = []
    for call in calls if isinstance(calls, list) else []:
        if isinstance(call, dict) and isinstance(call.get("at"), str) and call.get("outcome") in (
            "answered", "timeout", "error",
        ):
            kept.append({"at": call["at"], "outcome": call["outcome"]})
    return kept


def keep_judged_calls(
    db_path: str | Path, outcomes: Sequence[tuple[str, str]], *, wait: float = JUDGE_SAVE_WAIT,
) -> bool:
    """Add ``outcomes`` (``(at, outcome)`` pairs) to the store's judge row,
    keeping the last ``JUDGE_CALLS_KEPT``, in one short transaction on a
    connection of its own. Only in a store that is already there. False when
    it could not (no store, a lock held past ``wait``)."""
    import sqlite3

    path = Path(db_path).expanduser()
    if not path.is_file():
        return False
    conn = sqlite3.connect(str(path), timeout=wait)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (JUDGE_CALLS_KEY,)).fetchone()
        calls = judged_calls(row[0] if row else None)
        calls += [{"at": at, "outcome": outcome} for at, outcome in outcomes]
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (JUDGE_CALLS_KEY, json.dumps({"calls": calls[-JUDGE_CALLS_KEPT:]})),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return False
    finally:
        conn.close()


def _wait(value: Any) -> float:
    """How long the hook said it waits for this answer, when that makes sense;
    else what a hook waits without the judge."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        if 0 < value <= HOOK_SECONDS:
            return float(value)
    return ANSWER_SECONDS


def judge_health(answerer: Any = None, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The health card's word on the judge (WP-R16b), or None while it is
    off and nothing was judged here: the switch as this process sees it (never
    the key), this process's answerer's counts, and ``line``, the one line the
    card prints, which says what leaves the machine. Never raises."""
    try:
        status = jev.status(environ)
        counts = answerer.judge_counts() if answerer is not None and hasattr(answerer, "judge_counts") else {}
    except Exception:
        return None
    if not status["switched_on"] and not counts.get("messages"):
        return None
    label = f"{'Cue judge:':<15}"
    if status["switched_on"] and not status["key"] and not counts.get("calls"):
        line = (f"{label}switched to Jev, but there is no key at {status['key_file']}, "
                "so nothing is sent and the cue shows what it did before.")
    else:
        line = (f"{label}Jev decides which memories come to each message: the message "
                f"(up to {status['message_chars']:,} characters) and up to {status['candidates']} "
                f"lines of {status['line_chars']} characters from memory go to {status['host']}.")
        if counts.get("messages"):
            line += (f" This session: {counts['calls']} asked, {counts['timeouts']} timed out, "
                     f"{counts['errors']} failed, and the cue showed nothing for those.")
    return {**status, "counts": counts, "line": line}


# ── The hook ──


def prompt_hook(
    payload: Mapping[str, Any],
    *,
    db_path: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    environ: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> str:
    """What ``mnemos hook prompt`` prints for one message: the block, or
    ``""``. Records what it offers, and how it answered the message (by
    meaning, by words or by Jev; with lines or with nothing) or why it could
    not (it ran out of time, broke, or its judge failed), in the session's
    file (``SeenFile``). Writes nothing to the store.

    ``payload`` is Claude Code's UserPromptSubmit input. ``deadline`` (a
    ``time.monotonic()`` value) bounds the work: past it, nothing is printed.
    """
    from .authorship import clean_session_id
    from .code_version import MAINTENANCE_CODE_VERSION
    from .store.fts import meaningful_words

    env = os.environ if environ is None else environ
    deadline = deadline if deadline is not None else time.monotonic() + HOOK_BUDGET
    started = time.perf_counter()
    mark_hook_in_use()

    event = payload.get("hook_event_name")
    if event is not None and event != "UserPromptSubmit":
        return ""
    text = payload.get("prompt")
    if not isinstance(text, str) or len(meaningful_words(text)) < CUE_MIN_WORDS:
        return ""
    db = Path(db_path).expanduser()
    if not db.is_file():
        return ""
    key = scope_key(str(db), agent_id, person_id, project_scope)
    session = clean_session_id(payload.get("session_id")) or clean_session_id(env.get("CLAUDE_CODE_SESSION_ID"))
    seen_file = SeenFile(session, key)
    seen = seen_file.read()
    # Jev decides (WP-R16b), when the switch is on and there is a key.
    judged = jev.in_use(env)

    from .store.sqlite_store import ReadOnlyEngramStore

    # Why the cue could not answer this message, when it could not.
    failure: str | None = None
    try:
        store = ReadOnlyEngramStore(db)
        try:
            # No read may outlast the hook: a locked store waits only until the
            # deadline, and a query still running then is stopped.
            conn = store._get_conn()
            conn.execute(f"PRAGMA busy_timeout = {max(1, int((deadline - time.monotonic()) * 1000))}")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            minimum = store.min_code_version()
            if minimum is not None and minimum > MAINTENANCE_CODE_VERSION:
                return ""  # a newer Mnemos has opened this store
            _check(deadline)
            room = deadline - time.monotonic()
            if judged:
                # Only lines an answerer judged show. With no answerer that
                # judged, a slow one, or a judge that failed, nothing does. There
                # is no answering from words, so the answerer gets that time too.
                reply = ask_answerer(
                    key, text, exclude=seen["shown"], texts=seen["texts"], environ=env, judge=jev.JEV,
                    connect_seconds=min(CONNECT_SECONDS, max(0.0, room)),
                    answer_seconds=max(0.0, room - CONNECT_SECONDS - JUDGE_MARGIN),
                )
                ruled = reply is not None and reply.get("judge") == jev.JEV
                lines, via = (_valid_lines(reply["lines"]) if ruled else []), jev.JEV
                if not ruled:
                    failure = "no answerer judged"
                elif reply.get("judged") in ("timeout", "error"):
                    failure = f"judge {reply['judged']}"
            else:
                reply = ask_answerer(
                    key, text, exclude=seen["shown"], texts=seen["texts"], environ=env,
                    connect_seconds=min(CONNECT_SECONDS, max(0.0, room)),
                    answer_seconds=min(ANSWER_SECONDS, max(0.0, room - CONNECT_SECONDS - 0.15)),
                )
                if reply is not None:
                    lines, via = _valid_lines(reply["lines"]), "meaning"
                else:
                    _check(deadline)
                    from .simple_runtime import cue_memories

                    lines = cue_memories(
                        store, None, text, exclude=seen["shown"], exclude_texts=seen["texts"],
                        agent_id=agent_id, person_id=person_id, project_scope=project_scope,
                    )
                    via = "words"
        finally:
            store.close()
        shown = set(seen["shown"])
        known = set(seen["texts"])
        lines = [line for line in lines if line["id"] not in shown and line["key"] not in known][:CUE_LINES]
        _check(deadline)
        block = format_block(lines)
    except HookTimeout:
        _failed(seen_file, "timeout")
        raise
    except Exception as exc:
        _failed(seen_file, f"error ({type(exc).__name__})")
        raise
    if block:
        judged_scores = {"scores": [line.get("score") for line in lines]} if via == jev.JEV else {}
        seen_file.record(
            [line["id"] for line in lines],
            [line["key"] for line in lines],
            offer={
                "at": datetime.now(timezone.utc).isoformat(),
                "ids": [line["id"] for line in lines],
                "similarity": [line.get("similarity") for line in lines],
                "via": via,
                **judged_scores,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
            answered=via,
        )
    else:
        # Nothing to show: a silence, or a failure the cue went quiet on.
        seen_file.record((), (), answered=via, failure=failure)
    return block


def _failed(seen_file: SeenFile, reason: str) -> None:
    """Count a message the cue could not answer. Never raises: the hook's own
    failure is what the caller sees."""
    try:
        seen_file.record((), (), failure=reason)
    except Exception:
        pass


def _valid_lines(lines: Any) -> list[dict[str, Any]]:
    """An answerer's lines, each checked for what the block prints."""
    valid = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        if not all(isinstance(line.get(field), str) and line.get(field) for field in ("id", "text", "date", "key")):
            continue
        similarity = line.get("similarity")
        score = line.get("score")
        valid.append({
            "id": line["id"], "text": line["text"][:CUE_LINE_CHARS], "date": line["date"][:10],
            "key": line["key"], "lesson": bool(line.get("lesson")),
            "similarity": similarity if isinstance(similarity, (int, float)) else None,
            "score": score if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        })
    return valid


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise HookTimeout()


def read_payload(stream: Any, *, deadline: float) -> bytes:
    """What Claude Code wrote on the hook's stdin: until it closes it or a
    whole JSON object has come, at most ``PAYLOAD_BYTES``, never waiting past
    ``deadline``."""
    import select

    received = bytearray()
    fd = stream.fileno()
    while len(received) <= PAYLOAD_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        received.extend(chunk)
        if received.rstrip().endswith(b"}"):
            try:
                json.loads(bytes(received).decode("utf-8"))
                break
            except ValueError:
                continue
    return bytes(received[:PAYLOAD_BYTES])


def record_briefing(
    session: str,
    *,
    db_path: str,
    agent_id: str,
    person_id: str,
    project_scope: str,
    ids: Iterable[str],
    texts: Iterable[str],
) -> None:
    """The session-start hook's part: what its briefing showed, so the cue
    never offers it again in this session. Never raises."""
    try:
        key = scope_key(db_path, agent_id, person_id, project_scope)
        SeenFile(session, key).record(ids, [text_key(t) for t in texts if t])
    except Exception as exc:
        print(f"[mnemos hook] the briefing's ids were not recorded: {type(exc).__name__}: {exc}", file=sys.stderr)
