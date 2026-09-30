"""What every session carries (WP-R04b).

Claude Code shows a model only the first 2,048 characters of a server's
instructions. Mnemos's were 3,021 characters (3,420 in advanced mode), so the
rules at their end, never narrate the machinery, sign every write, never ask
the human what model you are, storage is local, never reached a model in
Claude Code: a session's copy ended at character 2,047, mid-sentence (the
lab's L05c, 2026-09-27). The instructions now put the rules first and fit, in
every mode. Details live in the tool descriptions, which models see in full.

A belief changes only when a correction names it by id (since WP-R07), but
the briefing showed beliefs without one. Each belief line now carries its id,
and a correction naming that id replaces or retires the belief.

The instructions are read as a client receives them, from a server started
the way Claude Code starts one; the briefing is read from the real
SessionStart hook in another process.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
OPUS = "claude-opus-5-5"
SESSION = "11111111-aaaa-4aaa-8aaa-111111111111"

# Claude Code shows a model only the first 2,048 characters of an MCP
# server's instructions and drops the rest (found in the lab's L05c,
# 2026-09-27: a session's copy of Mnemos's instructions ended at character
# 2,047, mid-sentence). Nothing past it reaches a model, so every mode's
# instructions must fit in it.
SHOWN = 2048

# Rules every model must be shown. Before WP-R04b each of them sat past
# character 2,048, so no model in Claude Code ever saw one.
RULES = (
    "Never narrate the machinery",
    "signed_as",
    "mnemos_introduce",
    "Never ask the human what model you are",
    "Storage is local",
)

MODES = ("simple", "advanced")

# The packet shows each belief in its own words, and keeps its id and
# confidence for the tools, in the order the beliefs were shown.
BELIEF_HEADING = re.compile(r"^### what (?:i've come to see|i'm starting to see)$", re.M)
BELIEF_IDS = re.compile(r"^- beliefs, in order: (?P<ids>.+)$", re.M)
BELIEF_ID = re.compile(r"(?P<id>belief_\w+) \((?P<percent>\d+)%\)")


# ── Helpers ──


def _home(folder: Path) -> Path:
    home = folder / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


def _served_instructions(mode: str, folder: Path) -> str:
    """What a client receives on initialize from ``mnemos serve --mode <mode>``."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {
        "HOME": str(_home(folder)),
        "PATH": "/usr/bin:/bin",
        "MNEMOS_DISABLE_DOTENV": "1",
        "PYTHONPATH": ":".join(sys.path),
    }
    if mode == "advanced":
        env["MNEMOS_ENABLE_EXPERIMENTAL"] = "1"  # advanced mode refuses to start without it
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mnemos.cli", "serve", "--mode", mode,
              "--db-path", str(folder / "memory.db"), *SCOPE_ARGS],
        env=env,
    )
    received: dict[str, str] = {}

    async def run() -> None:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                result = await session.initialize()
                received["instructions"] = result.instructions or ""

    anyio.run(run)
    return received["instructions"]


@pytest.fixture(scope="module")
def served(tmp_path_factory) -> dict[str, str]:
    return {mode: _served_instructions(mode, tmp_path_factory.mktemp(mode)) for mode in MODES}


def _hook(db: Path, folder: Path) -> str:
    """The briefing from the real SessionStart hook in another process."""
    payload = {
        "hook_event_name": "SessionStart", "source": "startup",
        "session_id": SESSION, "model": OPUS,
    }
    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db), *SCOPE_ARGS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "HOME": str(_home(folder)),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def _belief_lines(packet: str) -> dict[str, tuple[int, str]]:
    """Each belief the packet shows: its words, to (percent, id), paired in
    the order the packet shows them."""
    heading = BELIEF_HEADING.search(packet)
    assert heading, packet
    section = packet[heading.end() + 1:].split("\n\n### ", 1)[0]
    contents = [line[2:] for line in section.splitlines() if line.startswith("- ")]
    [ids] = BELIEF_IDS.findall(packet)
    shown = [(int(match["percent"]), match["id"]) for match in BELIEF_ID.finditer(ids)]
    assert len(shown) == len(contents), packet
    return dict(zip(contents, shown))


# ── The instructions a model is shown ──


def test_every_modes_instructions_fit_in_what_claude_code_shows(served):
    assert all(served[mode] for mode in MODES), "a mode served no instructions"
    # Claude Code shows a model only the first 2,048 characters.
    too_long = {mode: len(served[mode]) for mode in MODES if len(served[mode]) > SHOWN}
    assert not too_long, (
        f"characters of instructions served, by mode: {too_long}; "
        f"Claude Code shows a model only the first {SHOWN:,}"
    )


def test_the_rules_are_in_what_a_model_is_shown(served):
    never_shown = {}
    for mode in MODES:
        shown = " ".join(served[mode][:SHOWN].split())
        missing = [rule for rule in RULES if rule not in shown]
        if missing:
            never_shown[mode] = missing
    assert not never_shown, f"rules a model is never shown, by mode: {never_shown}"


def test_advanced_mode_keeps_the_same_loop_first(served):
    from mnemos.mcp_server import ADVANCED_INSTRUCTIONS
    from mnemos.simple_mcp import SERVER_INSTRUCTIONS

    assert served["simple"] == SERVER_INSTRUCTIONS
    assert served["advanced"] == ADVANCED_INSTRUCTIONS
    assert served["advanced"].startswith(served["simple"] + "\n\n")
    addendum = served["advanced"][len(served["simple"]):]
    for tool in ("mnemos_context_packet", "mnemos_review_queue", "mnemos_status"):
        assert tool in addendum


# ── A belief the briefing shows is one a correction can name ──


def test_each_belief_line_carries_the_id_a_correction_takes(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    kept = Belief(
        agent_id="nova", content="Plain words carry further than clever ones.",
        confidence=0.55, domain="craft", source="agent",
    )
    retired = Belief(
        agent_id="nova", content="Tabs read better than spaces.",
        confidence=0.35, domain="craft", source="agent",
    )
    store = EngramStore(db)
    try:
        store.save_belief(kept)
        store.save_belief(retired)
    finally:
        store.close()

    shown = _belief_lines(_hook(db, tmp_path))
    assert shown == {
        kept.content: (55, kept.id),
        retired.content: (35, retired.id),
    }, "a belief line does not carry the belief's id"

    # The reader corrects one belief and retires the other, naming each by
    # the id its line carries.
    monkeypatch.setenv("HOME", str(_home(tmp_path)))
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    runtime = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    try:
        replaced = runtime.correct(
            "Plain words carry further than clever ones, and short ones further still.",
            target_id=shown[kept.content][1],
            signed_as=OPUS,
        )
        forgotten = runtime.correct("", target_id=shown[retired.content][1], action="forget")
    finally:
        runtime.close()
    assert "Replaced the belief" in replaced, replaced
    assert "Retired the belief" in forgotten, forgotten

    # The next session's briefing, in another process, shows the new words
    # under the new belief's id, and neither old belief.
    after = _belief_lines(_hook(db, tmp_path))
    [(words, (percent, belief_id))] = after.items()
    assert words == "Plain words carry further than clever ones, and short ones further still."
    assert percent == 40
    assert belief_id not in (kept.id, retired.id)
    store = EngramStore(db)
    try:
        current = store.get_belief(belief_id)
        assert current is not None and current.content == words
        assert store.get_belief(kept.id).superseded_by == belief_id
        assert store.get_belief(retired.id).superseded_by
    finally:
        store.close()
