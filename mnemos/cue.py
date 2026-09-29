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
  briefing showed and the cue records what it offers, in a small file per
  session under ``~/.mnemos/run`` (``SeenFile``). Anything already shown is
  skipped.
- **Shown is not used.** An offered line is counted as offered, in that file,
  and never reinforces anything. Recalling it by id afterwards is a use.
- **Fast, with meaning.** The session's own Mnemos server keeps the embedding
  model warm (``CueAnswerer``) and answers on a unix socket named for the
  Claude Code process that spawned it. The hook finds it by ``CLAUDE_PID``,
  which Claude Code gives every hook, and which ``/clear`` does not change
  (``/clear`` gives the session a new id, and keeps its MCP servers running
  with the old one). With no answerer, a slow one, or one speaking an older
  protocol, the hook answers from words alone. The model loads at the server's
  start where the hook is in use, and otherwise only when a cue first asks.
- **Read-only.** The cue writes nothing to the store. The per-session files and
  the socket are outside it.

The hook imports nothing heavy: no torch, no MCP.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("mnemos.cue")

# The answerer and the hook speak this protocol. A change to what an answer
# means (the gate, the lines) raises it, so a hook never trusts a server still
# running older code: it answers from words alone instead. 2: a shared word
# counts only below the frequency cut, and the word path needs a rarer one
# (WP-R08c).
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
# The payload Claude Code sends on stdin, at most.
PAYLOAD_BYTES = 1_048_576

# Per-session files and dead sockets older than this are removed on the way.
RUN_DAYS = 7
# The offers one session file keeps, newest last.
OFFERS_KEPT = 200

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
    briefing showed and the cue offered, and the cue's offers counted.

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
        empty = {"shown": [], "texts": [], "offered": 0, "offers": []}
        if not self.session:
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        return {
            "shown": [i for i in data.get("shown") or [] if isinstance(i, str)],
            "texts": [t for t in data.get("texts") or [] if isinstance(t, str)],
            "offered": int(data.get("offered") or 0),
            "offers": [o for o in data.get("offers") or [] if isinstance(o, dict)],
        }

    def record(
        self,
        ids: Iterable[str],
        texts: Iterable[str] = (),
        *,
        offer: Mapping[str, Any] | None = None,
    ) -> None:
        """Add what was just shown. An offer (the cue's) is also counted."""
        if not self.session:
            return
        folder = ensure_run_dir()
        data = self.read()
        shown = list(dict.fromkeys([*data["shown"], *ids]))
        known = list(dict.fromkeys([*data["texts"], *texts]))
        offers = data["offers"]
        offered = data["offered"]
        if offer is not None:
            offers = [*offers, dict(offer)][-OFFERS_KEPT:]
            offered += len(offer.get("ids") or [])
        _write_private(self.path, {
            "v": CUE_PROTOCOL,
            "session": self.session,
            "scope": self.key,
            "shown": shown,
            "texts": known,
            "offered": offered,
            "offers": offers,
            "updated": datetime.now(timezone.utc).isoformat(),
        })
        sweep(folder)


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


def offered_summary(*, days: float = RUN_DAYS) -> dict[str, Any]:
    """The cue's offers across sessions, from the per-session files: for the
    watchdog and the lab. Reads only."""
    summary = {"sessions": 0, "offered": 0, "offers": 0, "by_via": {}}
    cutoff = time.time() - days * 86400
    try:
        entries = [e for e in os.scandir(run_dir()) if e.name.startswith("shown-") and e.name.endswith(".json")]
    except OSError:
        return summary
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                continue
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        offers = [o for o in data.get("offers") or [] if isinstance(o, dict)]
        if not offers:
            continue
        summary["sessions"] += 1
        summary["offered"] += int(data.get("offered") or 0)
        summary["offers"] += len(offers)
        for offer in offers:
            via = str(offer.get("via") or "")
            summary["by_via"][via] = summary["by_via"].get(via, 0) + len(offer.get("ids") or [])
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
) -> dict[str, Any] | None:
    """Ask this session's answerer for the cue: its reply, or None when there
    is none, it is slow, it fails, or it speaks another protocol."""
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
            try:
                lines = self._lines(
                    str(request.get("text") or ""),
                    exclude=[i for i in request.get("exclude") or [] if isinstance(i, str)],
                    texts=[t for t in request.get("texts") or [] if isinstance(t, str)],
                )
                why = None if lines is not None else "no store"
            except Exception as exc:  # the hook answers from words instead
                lines, why = None, f"{type(exc).__name__}: {exc}"[:200]
            reply = {"v": CUE_PROTOCOL, "ok": lines is not None, "meaning": True,
                     "lines": lines or [], "why": why}
        reply["ms"] = round((time.perf_counter() - started) * 1000, 1)
        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        self.answered += 1

    def _lines(self, text: str, *, exclude: list[str], texts: list[str]) -> list[dict[str, Any]] | None:
        if self._store is None:
            if not Path(self.db_path).is_file():
                return None
            from .store.sqlite_store import ReadOnlyEngramStore

            self._store = ReadOnlyEngramStore(self.db_path)
        from .simple_runtime import cue_memories

        return cue_memories(
            self._store, self._index, text, exclude=exclude, exclude_texts=texts, **self.scope,
        )


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
    ``""``. Records what it offers. Writes nothing to the store.

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

    from .store.sqlite_store import ReadOnlyEngramStore

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
    if block:
        seen_file.record(
            [line["id"] for line in lines],
            [line["key"] for line in lines],
            offer={
                "at": datetime.now(timezone.utc).isoformat(),
                "ids": [line["id"] for line in lines],
                "similarity": [line.get("similarity") for line in lines],
                "via": via,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
    return block


def _valid_lines(lines: Any) -> list[dict[str, Any]]:
    """An answerer's lines, each checked for what the block prints."""
    valid = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        if not all(isinstance(line.get(field), str) and line.get(field) for field in ("id", "text", "date", "key")):
            continue
        similarity = line.get("similarity")
        valid.append({
            "id": line["id"], "text": line["text"][:CUE_LINE_CHARS], "date": line["date"][:10],
            "key": line["key"], "lesson": bool(line.get("lesson")),
            "similarity": similarity if isinstance(similarity, (int, float)) else None,
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
