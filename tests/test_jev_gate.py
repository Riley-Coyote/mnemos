"""Jev decides what fires (WP-R16b).

Riley's picture: with each message, spreading activation proposes the memories
that may bear on it, and Jev decides which of them fire. The cue's ranking and
floors propose up to six lines, before its cap of three; Jev, a typed judge,
scores each; at most three scoring at least the threshold show. Off by
default: ``"cue_judge": "jev"`` in the config or ``MNEMOS_CUE_JUDGE=jev``, and
a key. Only the message and at most six lines of 200 characters leave the
machine. On a timeout or an error the cue shows nothing, and the health card
counts it. Jev's scores never change a memory.

No test here reaches the real Jev: a callable stands in for it, or a local
HTTP server for its endpoint. The fake model gives each text a vector by the
concepts it names, as in ``test_the_cue``, so every cosine is controlled.
"""

from __future__ import annotations

import http.server
import json
import logging
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import mnemos.store.embedding_index as ei
from mnemos.core.engram import Engram
from mnemos.store.embedding_index import EmbeddingIndex
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore


class _Lazy:
    """The module under test, imported where a test first uses it, so that on
    code without it each test fails by itself (the fail-on-base proof)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        import importlib

        return getattr(importlib.import_module(self._name), attr)


cue = _Lazy("mnemos.cue")
jev = _Lazy("mnemos.jev")

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
# A key no one has: it must never be seen anywhere but in a request's header.
KEY = "jev-test-key-" + "7f3c9a1e5b2d" * 3
MESSAGE = "The lighthouse keeper asked whether the storm shutters close tonight in the fog"
GARDEN = "Which marigolds bloom first in the greenhouse this June?"
URL_PATH = "/v1/systemone"


# ── A model whose meaning is controlled (as in test_the_cue) ──

_CONCEPTS = (
    {"lighthouse", "beacon", "lamp", "keeper"},
    {"harbour", "ferry", "pier", "boat", "timetable"},
    {"garden", "marigolds", "greenhouse", "bloom"},
)


def concept_vector(text: str) -> list[float]:
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in _CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


class _Vector(list):
    def tolist(self):
        return list(self)


class _ConceptModel:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return _Vector(concept_vector(texts))
        return [_Vector(concept_vector(text)) for text in texts]


class _ConceptEmbedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = _ConceptModel()
        return self._model


@pytest.fixture
def meaning(monkeypatch):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: True)
    monkeypatch.setattr(ei, "_LocalEmbedder", _ConceptEmbedder)
    monkeypatch.setattr(ei, "_LOGGED", set(), raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME in the test's folder, by a path short enough for a unix socket."""
    real = tmp_path / "home"
    real.mkdir()
    link = None
    path = real
    if len(os.fsencode(str(real / ".mnemos" / "run" / "cue-4194304-0123456789ab.sock"))) >= 100:
        link = Path("/tmp") / f"mnj-{uuid.uuid4().hex[:10]}"
        os.symlink(real, link)
        path = link
    monkeypatch.setenv("HOME", str(path))
    yield path
    if link is not None:
        os.unlink(link)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """The stand-in for Jev listens on 127.0.0.1: no proxy in between."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "jev" / "api_key"
    path.parent.mkdir()
    path.write_text(KEY + "\n")
    path.chmod(0o600)
    return path


def _switched_on(key_path: Path) -> dict[str, str]:
    return {"MNEMOS_CUE_JUDGE": "jev", "MNEMOS_JEV_KEY_FILE": str(key_path)}


# ── A store: six memories that clear the floors, so the cap hides three ──

LIGHTS = (
    "Trim the lamp and the beacon wick each evening.",
    "The lamp keeper tends the beacon.",
    "A spare lamp for the lighthouse beacon.",
    "Oil the beacon lamp before the keeper's night watch.",
    "The lighthouse lamp room needs a new beacon lens.",
    # Shares "keeper", "storm" and "shutters" with the message: words alone find it.
    "The keeper checks the storm shutters every night by the lamp.",
)


def _memory(content: str, **fields) -> Engram:
    return Engram(content=content, kind=fields.pop("kind", "semantic"), owner_agent_id="nova",
                  person_id="riley", project_scope="demo", **fields)


def _store(db: Path, texts=LIGHTS) -> Path:
    store = EngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db))
    for text in texts:
        engram = _memory(text)
        store.save_engram(engram)
        index.index_engram(engram.id, engram.content)
    index.close()
    store.close()
    return db


def _candidates(db: Path, text: str = MESSAGE, limit: int = 6) -> list[dict]:
    """What the cue proposes, in its own order, without a judge."""
    from mnemos.simple_runtime import cue_memories

    store = ReadOnlyEngramStore(str(db))
    index = EmbeddingIndex(db_path=str(db), read_only=True)
    try:
        return cue_memories(store, index, text, limit=limit, **SCOPE)
    finally:
        store.close()


def _state(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "engrams": conn.execute(
                "SELECT id, state, strength, stability, accessibility, access_count, "
                "last_accessed, content, impact FROM engrams ORDER BY id").fetchall(),
            "connections": conn.execute("SELECT COUNT(*) FROM connections").fetchone(),
            "reinforced": conn.execute("SELECT COUNT(*) FROM session_reinforcements").fetchone(),
            "versions": conn.execute("SELECT COUNT(*) FROM versions").fetchone(),
            "beliefs": conn.execute("SELECT COUNT(*) FROM beliefs").fetchone(),
            "meta": conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
        }
    finally:
        conn.close()


# ── The judge, and the answerer that asks it ──


class _Judge:
    """Stands in for ``jev.ask``: scores each line by its words, and keeps
    every call. ``fail`` is raised instead; ``pause`` is slept first."""

    def __init__(self, scores: dict[str, float] | None = None, *, default: float = 0.1,
                 fail: BaseException | None = None, pause: float = 0.0, count: int | None = None):
        self.scores = scores or {}
        self.default = default
        self.fail = fail
        self.pause = pause
        self.count = count
        self.calls: list[dict] = []

    def __call__(self, message, lines, *, timeout):
        self.calls.append({"message": message, "lines": list(lines), "timeout": timeout})
        if self.pause:
            time.sleep(self.pause)
        if self.fail is not None:
            raise self.fail
        scores = [self.scores.get(line, self.default) for line in lines]
        return scores if self.count is None else scores[: self.count]


def _answerer(db: Path, judge=None):
    """A warm answerer, as a server starts one where the prompt hook is in use."""
    cue.mark_hook_in_use()
    answerer = cue.CueAnswerer(str(db), claude_pid=os.getpid(), judge=judge, **SCOPE)
    assert answerer.start()
    assert answerer.ready.wait(10) and answerer.warm.is_set()
    return answerer


def _hook(db: Path, prompt: str = MESSAGE, session: str = "session-one", **environ) -> str:
    payload = {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    return cue.prompt_hook(payload, db_path=str(db), environ={"CLAUDE_PID": str(os.getpid()), **environ}, **SCOPE)


def _ids(block: str) -> list[str]:
    return re.findall(r"\((engram_[0-9A-Za-z]+)\)$", block, flags=re.M)


def _seen(session: str, db: Path) -> dict:
    return cue.SeenFile(session, cue.scope_key(str(db), **SCOPE)).read()


# ── A local stand-in for Jev's endpoint ──


class _FakeJev:
    """An HTTP server on 127.0.0.1 that answers as Jev does: a ``noul``
    probability per question. It keeps what each request carried, the
    Authorization header apart from the body."""

    def __init__(self, *, score=lambda line: 0.9, delay: float = 0.0, status: int = 200,
                 raw: bytes | None = None, drop: str | None = None, echo_key: bool = False):
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                       "raw": body, "body": json.loads(body)})
                if delay:
                    time.sleep(delay)
                if raw is not None or status != 200:
                    out = raw if raw is not None else (
                        (self.headers.get("Authorization") or "").encode() if echo_key else b'{"error": "no"}')
                else:
                    sent = json.loads(body)
                    answers = {name: {"noul": score(sent["state"][name])} for name in sent["questions"]}
                    if drop:
                        answers.pop(drop, None)
                    reply = {"answers": answers, "model": "jev-fake", "usage": {"input_tokens": 1}}
                    if echo_key:
                        reply["echo"] = self.headers.get("Authorization")
                    out = json.dumps(reply).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{URL_PATH}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_jev():
    made = []

    def make(**kwargs):
        server = _FakeJev(**kwargs)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


# ── Off by default ──


def test_off_by_default_nothing_is_judged_and_nothing_leaves(tmp_path, meaning, home, key):
    """No switch in the config or the environment: off, even with a key. The
    answerer is never asked to judge and the cue shows what it did before."""
    from mnemos.config.defaults import DEFAULT_CONFIG
    from mnemos.config.loader import load_config

    assert DEFAULT_CONFIG["cue_judge"] == "off" and load_config()["cue_judge"] == "off"
    assert not jev.switched_on({}) and not jev.in_use({"MNEMOS_JEV_KEY_FILE": str(key)})

    db = _store(tmp_path / "memory.db")
    judge = _Judge(default=0.99)
    answerer = _answerer(db, judge)
    try:
        block = _hook(db, MNEMOS_JEV_KEY_FILE=str(key))
    finally:
        answerer.stop()

    assert _ids(block) == [line["id"] for line in _candidates(db, limit=3)]
    assert judge.calls == [] and answerer.judge_counts()["messages"] == 0
    assert _seen("session-one", db)["offers"][0]["via"] == "meaning"


def test_the_switch_is_the_environment_then_the_config_and_needs_a_key(tmp_path, home, key):
    config = Path(os.environ["HOME"]) / ".mnemos" / "config.json"
    config.parent.mkdir(parents=True)
    with_key = {"MNEMOS_JEV_KEY_FILE": str(key)}

    config.write_text(json.dumps({"cue_judge": "jev"}))
    assert jev.switched_on({}) and jev.in_use(with_key)
    assert not jev.in_use({"MNEMOS_JEV_KEY_FILE": str(tmp_path / "missing")}), "no key, no Jev"
    empty = tmp_path / "empty"
    empty.write_text("")
    assert not jev.in_use({"MNEMOS_JEV_KEY_FILE": str(empty)})
    assert not jev.in_use({**with_key, "MNEMOS_CUE_JUDGE": "off"}), "the environment wins"

    config.write_text(json.dumps({"cue_judge": "off"}))
    assert jev.in_use({**with_key, "MNEMOS_CUE_JUDGE": " JEV "})
    for said in ("yes", "true", "1", "on", "astra"):
        assert not jev.switched_on({"MNEMOS_CUE_JUDGE": said}), said

    config.write_text("{not json")
    assert not jev.switched_on({}), "a config it can't read is off"
    status = jev.status({**with_key, "MNEMOS_CUE_JUDGE": "jev"})
    assert status["in_use"] and status["candidates"] == 6 and status["line_chars"] == 200
    assert KEY not in json.dumps(status)


# ── Jev decides what fires ──


def test_jev_decides_what_fires_from_six_candidates_at_most_three_the_likeliest_first(
    tmp_path, meaning, home, key,
):
    """The six candidates before the cap go to the judge in one call. Those
    scoring at least the threshold show, at most three, the likeliest first:
    lines the cap of three would have hidden fire, and the ones it would have
    shown are turned away."""
    db = _store(tmp_path / "memory.db")
    order = _candidates(db)
    assert len(order) == 6, "six lines clear the floors"
    t = cue.CUE_JUDGE_THRESHOLD
    wanted = [t - 0.5, t - 0.01, t, 0.99, 0.95, t + 0.01]
    judge = _Judge({line["text"]: score for line, score in zip(order, wanted)})
    answerer = _answerer(db, judge)
    try:
        block = _hook(db, **_switched_on(key))
        again = _hook(db, session="session-two", **_switched_on(key))
    finally:
        answerer.stop()

    assert _ids(block) == [order[3]["id"], order[4]["id"], order[5]["id"]], block
    assert again == block, "the same message in another session: the same decision"
    call = judge.calls[0]
    assert call["message"] == MESSAGE
    assert call["lines"] == [line["text"] for line in order], "the candidates, in the cue's order"
    assert 0 < call["timeout"] <= jev.TIMEOUT
    offer = _seen("session-one", db)["offers"][0]
    assert offer["via"] == "jev" and offer["scores"] == [0.99, 0.95, round(t + 0.01, 4)]

    # A score equal to the threshold is enough.
    judge.scores = {line["text"]: (t if index == 2 else 0.2) for index, line in enumerate(order)}
    answerer = _answerer(db, judge)
    try:
        assert _ids(_hook(db, session="session-three", **_switched_on(key))) == [order[2]["id"]]
    finally:
        answerer.stop()


def test_nothing_above_the_threshold_shows_nothing_not_even_words(tmp_path, meaning, home, key):
    db = _store(tmp_path / "memory.db")
    judge = _Judge(default=cue.CUE_JUDGE_THRESHOLD - 0.05)
    answerer = _answerer(db, judge)
    try:
        assert _hook(db, **_switched_on(key)) == ""
        assert _hook(db, session="session-two") != "", "switched off, the same message gets lines"
        counts = answerer.judge_counts()
    finally:
        answerer.stop()
    assert counts["calls"] == 1 and counts["answered"] == 1 and counts["none_shown"] == 1
    assert _seen("session-one", db)["offers"] == []


def test_one_call_per_message_and_none_without_candidates(tmp_path, meaning, home, key):
    """One call per message, with at most six lines; none when nothing clears
    the floors, and none for a message too short for the cue."""
    db = _store(tmp_path / "memory.db")
    judge = _Judge(default=0.99)
    answerer = _answerer(db, judge)
    try:
        first = _hook(db, **_switched_on(key))
        _hook(db, GARDEN, session="session-garden", **_switched_on(key))
        _hook(db, "ok, thanks!", **_switched_on(key))
        second = _hook(db, MESSAGE + " again, the keeper and the storm", **_switched_on(key))
        counts = answerer.judge_counts()
    finally:
        answerer.stop()

    assert len(judge.calls) == 2 and all(len(call["lines"]) <= jev.CANDIDATES for call in judge.calls)
    assert len(judge.calls[0]["lines"]) == 6
    assert len(judge.calls[1]["lines"]) == 3, "what this session was shown is not sent again"
    assert not set(_ids(first)) & set(_ids(second))
    assert counts["messages"] == 3 and counts["no_candidates"] == 1 and counts["calls"] == 2


def test_judging_never_changes_memory(tmp_path, meaning, home, key):
    db = _store(tmp_path / "memory.db")
    before = _state(db)
    answerer = _answerer(db, _Judge(default=0.99))
    try:
        assert _hook(db, **_switched_on(key))
    finally:
        answerer.stop()
    assert _state(db) == before


# ── Fail quiet ──


@pytest.mark.parametrize("judge, counted", [
    (lambda: _Judge(fail=jev.JevFailed("timeout")), "timeouts"),
    (lambda: _Judge(fail=jev.JevFailed("error", "HTTP 500")), "errors"),
    (lambda: _Judge(fail=jev.JevFailed("no-key")), "errors"),
    (lambda: _Judge(fail=RuntimeError("the judge broke")), "errors"),
    (lambda: _Judge(default=0.99, count=2), "errors"),  # fewer scores than lines
    (lambda: _Judge(default=1.5), "errors"),  # not a probability
])
def test_on_a_timeout_or_an_error_the_cue_shows_nothing_and_counts_it(tmp_path, meaning, home, key, judge,
                                                                      counted):
    db = _store(tmp_path / "memory.db")
    answerer = _answerer(db, judge())
    try:
        assert _hook(db, **_switched_on(key)) == ""
        counts = answerer.judge_counts()
    finally:
        answerer.stop()
    assert counts[counted] == 1 and counts["calls"] == 1 and counts["answered"] == 0
    assert counts["last_failure"]


def test_a_judge_slower_than_the_hook_leaves_it_quiet_within_its_budget(tmp_path, meaning, home, key):
    db = _store(tmp_path / "memory.db")
    answerer = _answerer(db, _Judge(default=0.99, pause=1.5))
    try:
        started = time.monotonic()
        block = _hook(db, **_switched_on(key))
        took = time.monotonic() - started
    finally:
        answerer.stop()
    assert block == ""
    assert took < cue.HOOK_BUDGET + 0.1, took


def test_with_the_switch_on_no_answerer_that_judged_means_nothing(tmp_path, meaning, home, key):
    """Switched on, only lines an answerer judged show: without an answerer
    there is no answering from words, and an answerer that did not judge
    (older code) is not trusted."""
    db = _store(tmp_path / "memory.db")
    words_line = next(line for line in _candidates(db) if "shutters" in line["text"])

    assert _hook(db, CLAUDE_PID="999999") != "", "switched off, words answer"
    assert words_line["id"] in _hook(db, session="s2", CLAUDE_PID="999999")
    assert _hook(db, session="s3", CLAUDE_PID="999999", **_switched_on(key)) == ""

    unjudged = {"v": cue.CUE_PROTOCOL, "ok": True, "meaning": True, "lines": [
        {"id": "engram_UNJUDGED0001", "text": "An unjudged line.", "date": "2026-09-29", "key": "k0"}]}
    seen: list[dict] = []
    thread = _socket_answerer(db, unjudged, delay=0.0, seen=seen)
    assert _hook(db, session="s4", **_switched_on(key)) == ""
    thread.join(5)
    assert seen[0]["judge"] == "jev"


def _socket_answerer(db: Path, reply: dict, *, delay: float, seen: list) -> threading.Thread:
    """Something at the session's answerer socket that keeps the request it
    got and answers ``reply`` after ``delay`` seconds."""
    path = cue.answerer_path(os.getpid(), cue.scope_key(str(db), **SCOPE))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(5)

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            seen.append(json.loads(data.split(b"\n", 1)[0]))
            time.sleep(delay)
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass
        listener.close()
        os.unlink(path)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def test_the_hook_gives_the_judge_its_time_only_when_switched_on(tmp_path, meaning, home, key):
    """Switched off, the hook waits ``ANSWER_SECONDS`` for its answerer and
    then answers from words. Switched on, there are no words to fall back on,
    so it waits for the judge as long as its budget allows, and tells the
    answerer how long."""
    db = _store(tmp_path / "memory.db")
    judged = {"v": cue.CUE_PROTOCOL, "ok": True, "meaning": True, "judge": "jev", "judged": "shown",
              "lines": [{"id": "engram_JUDGEDLINE01", "text": "A judged line.", "date": "2026-09-29",
                         "key": "k1", "lesson": True, "score": 0.93}]}
    late = cue.ANSWER_SECONDS + 0.1

    seen: list[dict] = []
    thread = _socket_answerer(db, judged, delay=late, seen=seen)
    block = _hook(db, **_switched_on(key))
    thread.join(5)
    assert _ids(block) == ["engram_JUDGEDLINE01"]
    assert seen[0]["judge"] == "jev" and late < seen[0]["wait"] <= cue.HOOK_BUDGET, seen[0]
    assert _seen("session-one", db)["offers"][0]["scores"] == [0.93]

    seen.clear()
    thread = _socket_answerer(db, judged, delay=late, seen=seen)
    off = _hook(db, session="session-two")
    thread.join(5)
    assert "engram_JUDGEDLINE01" not in off and off != "", "switched off: words, as before"
    assert "judge" not in seen[0] and "wait" not in seen[0]


def test_the_answerer_fits_the_judge_inside_what_the_hook_waits(tmp_path, meaning, home):
    db = _store(tmp_path / "memory.db")
    judge = _Judge(default=0.99)
    answerer = cue.CueAnswerer(str(db), claude_pid=os.getpid(), judge=judge, **SCOPE)
    lines = [{"id": f"engram_{n}", "text": f"line {n}", "date": "2026-09-29", "key": f"k{n}"} for n in range(3)]

    shown, how = answerer._judged_lines(MESSAGE, lines, by=time.monotonic() + 10)
    assert how == "shown" and len(shown) == 3 and judge.calls[-1]["timeout"] == jev.TIMEOUT
    shown, how = answerer._judged_lines(MESSAGE, lines, by=time.monotonic() + 0.2)
    assert how == "shown" and judge.calls[-1]["timeout"] <= 0.2
    calls = len(judge.calls)
    assert answerer._judged_lines(MESSAGE, lines, by=time.monotonic() + 0.01) == ([], "timeout")
    assert len(judge.calls) == calls, "no time left: Jev isn't asked"
    assert answerer.judge_counts()["timeouts"] == 1

    assert cue._wait(0.5) == 0.5
    for odd in (None, True, "0.5", float("nan"), -1, 0, 100):
        assert cue._wait(odd) == cue.ANSWER_SECONDS, odd


# ── The client: what leaves the machine, and the key ──


def test_only_the_message_and_six_lines_of_200_leave_in_the_labs_words(tmp_path, fake_jev, key):
    fake = fake_jev(score=lambda line: 0.5 + len(line) / 1000)
    lines = [f"memory {n}: " + "the keeper trims the lamp " * 30 for n in range(8)]
    environ = {"MNEMOS_JEV_KEY_FILE": str(key)}

    scores = jev.ask(MESSAGE, lines, url=fake.url, environ=environ)

    assert len(scores) == 6 and len(fake.requests) == 1
    request = fake.requests[0]
    assert request["path"] == URL_PATH and request["authorization"] == f"Bearer {KEY}"
    assert KEY.encode() not in request["raw"]
    body = request["body"]
    assert set(body) == {"state", "model", "questions"} and body["model"] == "jev-latest"
    assert body["state"] == {"message": MESSAGE, **{f"memory_{n}": lines[n - 1][:200] for n in range(1, 7)}}
    assert all(len(text) <= 200 for name, text in body["state"].items() if name != "message")
    for n in range(1, 7):
        assert body["questions"][f"memory_{n}"] == {
            "type": "noul",
            "instructions": (f"Does memory_{n} bear directly on the message, so that recalling it would "
                             "change or sharpen a good reply?"),
            "criteria": {
                "true": ("Recalling this memory would change or sharpen what a good reply to the message "
                         "says or does: it is about the same work, decision or preference the message is "
                         "about."),
                "false": ("It is only loosely related, about a different project, or generic advice that "
                          "would not change the reply."),
            },
        }
    assert jev.ask(MESSAGE, [], url=fake.url, environ=environ) == [] and len(fake.requests) == 1


def test_a_long_message_goes_as_its_first_700_and_last_300_characters(fake_jev, key):
    """At most 1,000 characters of a message leave: a longer one goes as its
    first 700, " … " and its last 300, since people put the ask at either end
    of a pasted log. One at the cap or under it goes unchanged."""
    fake = fake_jev()
    environ = {"MNEMOS_JEV_KEY_FILE": str(key)}
    head = "Why does the lighthouse build fail? The log follows. "
    tail = " That was the whole log: what should the keeper change first?"
    logged = "the lamp beacon warmed, then the build stopped.\n"
    filler = (logged * (20_000 // len(logged) + 1))[: 20_000 - len(head) - len(tail)]
    long = head + filler + tail
    assert len(long) == 20_000

    jev.ask(long, ["a line"], url=fake.url, environ=environ)

    sent = fake.requests[-1]["body"]["state"]["message"]
    assert sent == long[:700] + " … " + long[-300:]
    assert len(sent) == 1_000 + len(" … ")
    assert sent.startswith(head) and sent.endswith(tail)

    for size in (999, 1_000):
        whole = ("keeper lamp " * 100)[:size]
        jev.ask(whole, ["a line"], url=fake.url, environ=environ)
        assert fake.requests[-1]["body"]["state"]["message"] == whole
    over = ("keeper lamp " * 100)[:1_001]
    jev.ask(over, ["a line"], url=fake.url, environ=environ)
    assert fake.requests[-1]["body"]["state"]["message"] == over[:700] + " … " + over[-300:]


@pytest.mark.parametrize("server, kind, detail", [
    (dict(delay=2.0), "timeout", ""),
    (dict(status=500), "error", "HTTP 500"),
    (dict(raw=b"<html>not json</html>"), "answer", "JSONDecodeError"),
    (dict(drop="memory_2"), "answer", "memory_2"),
    (dict(score=lambda line: 1.5), "answer", "memory_1"),
])
def test_the_client_fails_quiet_within_its_timeout(fake_jev, key, server, kind, detail):
    fake = fake_jev(**server)
    started = time.monotonic()
    with pytest.raises(jev.JevFailed) as failed:
        jev.ask(MESSAGE, ["one line", "another line"], url=fake.url, timeout=0.3,
                environ={"MNEMOS_JEV_KEY_FILE": str(key)})
    assert time.monotonic() - started < 0.3 + 0.25
    assert (failed.value.kind, failed.value.detail) == (kind, detail)


def test_no_key_no_call_and_nothing_listening_is_an_error(tmp_path, fake_jev, key):
    fake = fake_jev()
    for missing in (tmp_path / "nowhere", Path(os.devnull)):
        with pytest.raises(jev.JevFailed) as failed:
            jev.ask(MESSAGE, ["a line"], url=fake.url, environ={"MNEMOS_JEV_KEY_FILE": str(missing)})
        assert failed.value.kind == "no-key"
    assert fake.requests == []

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(jev.JevFailed) as failed:
        jev.ask(MESSAGE, ["a line"], url=f"http://127.0.0.1:{port}{URL_PATH}", timeout=1.0,
                environ={"MNEMOS_JEV_KEY_FILE": str(key)})
    assert failed.value.kind == "error"


def test_the_key_never_shows_in_an_error_a_log_a_reply_or_health(
    tmp_path, meaning, home, key, fake_jev, monkeypatch, caplog,
):
    """The key goes only into a request's header. A server that echoes it back,
    in an error or beside a good answer, gets it no further: not into an
    exception, a log line, the answerer's counts, the hook's block or the
    session's file, nor the health card."""
    caplog.set_level(logging.DEBUG)
    db = _store(tmp_path / "memory.db")
    monkeypatch.setenv("MNEMOS_JEV_KEY_FILE", str(key))
    monkeypatch.setenv("MNEMOS_CUE_JUDGE", "jev")

    refusing = fake_jev(status=401, echo_key=True)
    with pytest.raises(jev.JevFailed) as failed:
        jev.ask(MESSAGE, ["a line"], url=refusing.url)
    assert refusing.requests[0]["authorization"] == f"Bearer {KEY}"
    assert failed.value.detail == "HTTP 401"
    assert KEY not in str(failed.value) and KEY not in repr(failed.value)

    monkeypatch.setattr("mnemos.jev.JEV_URL", refusing.url)
    answerer = _answerer(db)  # the real client
    try:
        refused = _hook(db, **_switched_on(key))
        monkeypatch.setattr("mnemos.jev.JEV_URL", fake_jev(echo_key=True).url)
        shown = _hook(db, session="session-two", **_switched_on(key))
        counts = answerer.judge_counts()
        card, data = _health(db, answerer, monkeypatch)
    finally:
        answerer.stop()

    assert refused == "" and counts["errors"] == 1 and counts["last_failure"] == "error (HTTP 401)"
    assert len(_ids(shown)) == 3
    run = Path(os.environ["HOME"]) / ".mnemos" / "run"
    files = "".join(path.read_text() for path in run.glob("shown-*.json"))
    for text in (caplog.text, json.dumps(counts), shown, files, card, json.dumps(data, default=str)):
        assert KEY not in text


# ── Health says so ──


class _Counted:
    """An answerer as the health card sees it: its judge's counts."""

    def __init__(self, judged: bool = True) -> None:
        self.judged = judged

    def judge_counts(self):
        if not self.judged:
            return {"messages": 0, "no_candidates": 0, "calls": 0, "answered": 0, "shown": 0,
                    "none_shown": 0, "timeouts": 0, "errors": 0, "last_failure": None}
        return {"messages": 5, "no_candidates": 1, "calls": 4, "answered": 2, "shown": 3,
                "none_shown": 0, "timeouts": 1, "errors": 1, "last_failure": "error (HTTP 500)"}


def _health(db: Path, answerer, monkeypatch) -> tuple[str, dict]:
    import mnemos.simple_mcp as simple_mcp

    # The server's globals come back as they were when the test ends.
    monkeypatch.setattr(simple_mcp, "_runtime_kwargs", dict(simple_mcp._runtime_kwargs))
    monkeypatch.setattr(simple_mcp, "_cue_answerer", answerer)
    simple_mcp.configure_runtime(db_path=str(db), **SCOPE)
    try:
        result = simple_mcp.simple_mcp._tool_manager.get_tool("mnemos_health").fn()
    finally:
        simple_mcp.configure_runtime()
    return result.content[0].text, result.structuredContent


def test_health_says_so_in_one_line_when_the_switch_is_on(tmp_path, meaning, home, key, monkeypatch):
    db = _store(tmp_path / "memory.db")

    for answerer in (None, _Counted(judged=False)):
        card, data = _health(db, answerer, monkeypatch)
        assert "Cue judge" not in card and "cue_judge" not in data, "off: not a word"
    # Off as this server reads it, but a prompt hook asked it to judge (the
    # switch in the hook's environment only): lines did leave, so it says so.
    card, _ = _health(db, _Counted(), monkeypatch)
    assert "4 asked" in next(line for line in card.splitlines() if line.startswith("Cue judge:"))

    monkeypatch.setenv("MNEMOS_CUE_JUDGE", "jev")
    monkeypatch.setenv("MNEMOS_JEV_KEY_FILE", str(key))
    card, data = _health(db, _Counted(), monkeypatch)
    lines = [line for line in card.splitlines() if line.startswith("Cue judge:")]
    assert len(lines) == 1 and "Cue judge" not in card.replace(lines[0], "")
    [line] = lines
    assert "the message (up to 1,000 characters) and up to 6 lines of 200" in line
    assert line.endswith("go to api.typesafe.ai. This session: 4 asked, 1 timed out, 1 failed, "
                         "and the cue showed nothing for those.")
    assert "4 asked, 1 timed out, 1 failed" in line
    assert card.index("Last dream:") < card.index("Cue judge:") < card.index("Everything on this card")
    assert data["cue_judge"]["counts"]["timeouts"] == 1 and data["cue_judge"]["in_use"]

    card, _ = _health(db, None, monkeypatch)
    [line] = [line for line in card.splitlines() if line.startswith("Cue judge:")]
    assert "asked" not in line, "no answerer here: nothing to count"

    monkeypatch.setenv("MNEMOS_JEV_KEY_FILE", str(tmp_path / "no-key-here"))
    card, data = _health(db, None, monkeypatch)
    [line] = [line for line in card.splitlines() if line.startswith("Cue judge:")]
    assert "no key" in line and "nothing is sent" in line and not data["cue_judge"]["in_use"]


# ── Across processes: a real server judges for the hook ──

_SERVER = r'''
import math, re, sys
import mnemos.store.embedding_index as ei
import mnemos.jev
CONCEPTS = CONCEPTS_HERE
mnemos.jev.JEV_URL = JEV_URL_HERE

def vector(text):
    words = re.findall(r"[a-z]+", text.lower())
    raw = [float(sum(1 for w in words if w in group)) for group in CONCEPTS] + [0.05]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]

class Vector(list):
    def tolist(self):
        return list(self)

class Model:
    def encode(self, texts, normalize_embeddings=True, batch_size=32):
        if isinstance(texts, str):
            return Vector(vector(texts))
        return [Vector(vector(t)) for t in texts]

class Embedder(ei._LocalEmbedder):
    def _get_model(self):
        if self._model is None:
            self._model = Model()
        return self._model

ei._check_local_deps = lambda: True
ei._LocalEmbedder = Embedder
from mnemos.cli import main
sys.exit(main(sys.argv[1:]))
'''


def _env(home: Path, **extra) -> dict[str, str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "MNEMOS_DISABLE_DOTENV": "1",
           "PYTHONPATH": ":".join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


def test_a_real_server_judges_for_the_hook_across_processes(tmp_path, meaning, home, key, fake_jev):
    """`mnemos serve`, started as Claude Code starts it, judges for the prompt
    hook in its own process: one request to Jev per message, only the judged
    lines printed, the key in no process's output, the store unchanged."""
    db = _store(tmp_path / "memory.db")
    order = _candidates(db)
    before = _state(db)
    fake = fake_jev(score=lambda line: 0.97 if line in (order[4]["text"], order[5]["text"]) else 0.3)
    script = _SERVER.replace("CONCEPTS_HERE", repr(tuple(sorted(group) for group in _CONCEPTS)))
    script = script.replace("JEV_URL_HERE", repr(fake.url))
    cue.mark_hook_in_use()
    server = subprocess.Popen(
        [sys.executable, "-c", script, "serve", "--db-path", str(db), *SCOPE_ARGS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_env(Path(os.environ["HOME"]), MNEMOS_JEV_KEY_FILE=str(key)),
    )
    path = cue.answerer_path(os.getpid(), cue.scope_key(str(db), **SCOPE))
    hooks = []
    try:
        deadline = time.monotonic() + 60
        while not path.exists() and time.monotonic() < deadline and server.poll() is None:
            time.sleep(0.05)
        assert path.exists(), server.stderr.read().decode() if server.poll() is not None else "no socket"
        for session in ("judged-one", "judged-two"):
            payload = json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit",
                                  "prompt": MESSAGE})
            hooks.append(subprocess.run(
                [sys.executable, "-m", "mnemos.cli", "hook", "prompt", "--db-path", str(db), *SCOPE_ARGS],
                input=payload.encode(), capture_output=True, timeout=60,
                env=_env(Path(os.environ["HOME"]), CLAUDE_PID=str(os.getpid()), **_switched_on(key)),
            ))
    finally:
        server.stdin.close()
        server.wait(timeout=30)
    server_err = server.stderr.read().decode(errors="replace")

    for done in hooks:
        assert done.returncode == 0, done.stderr
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        assert _ids(context) == [order[4]["id"], order[5]["id"]], context
        assert KEY not in done.stdout.decode() and KEY not in done.stderr.decode()
    assert len(fake.requests) == 2, "one call per message"
    assert all(len(request["body"]["questions"]) == 6 for request in fake.requests)
    assert KEY not in server_err
    assert _seen("judged-one", db)["offers"][0]["via"] == "jev"
    assert _state(db) == before, "the judge's scores changed nothing"
