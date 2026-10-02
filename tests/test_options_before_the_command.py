"""Scope options given before a command reach it.

`mnemos --db-path X --agent-id Y hook session-start` printed nothing. The
hook takes --db-path and --agent-id after it too, and gave them a default of
None; argparse copies every default a subcommand sets over what the main
parser had already read, so the hook resolved the default agent's store,
found none, and contributed nothing to the session. Every command that takes
the scope options after it must keep the ones given before it, and the ones
given after it still win.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import mnemos.cli as cli
from mnemos.simple_runtime import MnemosRuntime

BEFORE = [
    "--db-path", "/stores/before.db",
    "--agent-id", "nova",
    "--person-id", "riley",
    "--project-scope", "demo",
]
EXPECTED = {
    "db_path": "/stores/before.db",
    "agent_id": "nova",
    "person_id": "riley",
    "project_scope": "demo",
}

# Every command, with the handler that runs it. Most of the first group take
# some or all of the scope options after them as well.
COMMANDS = [
    (["hook", "session-start"], "_cmd_hook"),
    (["hook", "prompt"], "_cmd_hook"),
    (["hooks", "install"], "_cmd_hooks"),
    (["daemon", "install"], "_cmd_daemon"),
    (["daemon", "status"], "_cmd_daemon"),
    (["daemon", "uninstall"], "_cmd_daemon"),
    (["serve"], "_cmd_serve"),
    (["remember", "The ferry leaves at nine."], "_cmd_remember"),
    (["doctor"], "_cmd_doctor"),
    (["journal"], "_cmd_journal"),
    (["notes"], "_cmd_notes"),
    (["notes", "reply", "a-note", "Thank you."], "_cmd_notes"),
    (["snapshot"], "_cmd_snapshot"),
    (["repair-softening"], "_cmd_repair_softening"),
    (["adopt-legacy"], "_cmd_adopt_legacy"),
    (["repair-lessons"], "_cmd_repair_lessons"),
    (["repair-versions"], "_cmd_repair_versions"),
    (["repair", "min-code-version"], "_cmd_repair"),
    (["repair", "quarantine-tool-written"], "_cmd_repair"),
    (["repair", "keyword-contradictions"], "_cmd_repair"),
    (["repair", "split-notes"], "_cmd_repair"),
    (["identity", "diff"], "_cmd_identity"),
    (["identity", "accept", "--divergence", "1"], "_cmd_identity"),
    (["mcp", "install", "claude"], "_cmd_mcp"),
    (["hermes", "install"], "_cmd_hermes"),
    (["hermes", "quickstart"], "_cmd_hermes"),
    (["backup", "create"], "_cmd_backup"),
    (["backup", "restore", "backup.db"], "_cmd_backup"),
    (["init"], "_cmd_init"),
    (["stats"], "_cmd_stats"),
    (["inspect", "engram_1"], "_cmd_inspect"),
    (["search", "ferry"], "_cmd_search"),
    (["consolidate"], "_cmd_consolidate"),
    (["export"], "_cmd_export"),
    (["substrate-tick"], "_cmd_substrate_tick"),
    (["index"], "_cmd_index"),
    (["bridge", "status"], "_cmd_bridge"),
    (["embeddings", "index"], "_cmd_embeddings"),
]


def _parsed(monkeypatch, handler: str, argv: list[str]):
    """The arguments ``handler`` is called with for ``argv``; it runs nothing."""
    seen = {}

    def record(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, handler, record)
    assert cli.main(argv) == 0
    return seen["args"]


@pytest.mark.parametrize("command, handler", COMMANDS, ids=lambda c: " ".join(c) if isinstance(c, list) else c)
def test_scope_given_before_the_command_reaches_it(monkeypatch, command, handler):
    args = _parsed(monkeypatch, handler, [*BEFORE, *command])
    assert {key: getattr(args, key) for key in EXPECTED} == EXPECTED


@pytest.mark.parametrize(
    "command, handler",
    [
        (["hook", "session-start"], "_cmd_hook"),
        (["hooks", "install"], "_cmd_hooks"),
        (["daemon", "install"], "_cmd_daemon"),
        (["serve"], "_cmd_serve"),
        (["doctor"], "_cmd_doctor"),
        (["journal"], "_cmd_journal"),
        (["notes", "reply", "a-note", "Thank you."], "_cmd_notes"),
        (["snapshot"], "_cmd_snapshot"),
        (["identity", "diff"], "_cmd_identity"),
    ],
    ids=lambda c: " ".join(c) if isinstance(c, list) else c,
)
def test_scope_given_after_the_command_still_wins(monkeypatch, command, handler):
    after = ["--agent-id", "vega", "--person-id", "sam", "--project-scope", "atlas"]
    args = _parsed(monkeypatch, handler, [*BEFORE, *command, *after])
    assert (args.agent_id, args.person_id, args.project_scope) == ("vega", "sam", "atlas")
    assert args.db_path == "/stores/before.db"


@pytest.mark.parametrize("command, handler", COMMANDS, ids=lambda c: " ".join(c) if isinstance(c, list) else c)
def test_scope_given_nowhere_is_left_to_the_resolver(monkeypatch, command, handler):
    # None, so resolve_scope applies the environment, the config and the
    # defaults, the one answer every entry point gives.
    args = _parsed(monkeypatch, handler, command)
    assert {key: getattr(args, key) for key in EXPECTED} == dict.fromkeys(EXPECTED)


def test_the_hook_reads_the_store_named_before_it(tmp_path):
    """The command from the log, run as a harness runs it: another process."""
    db = tmp_path / "elsewhere.db"
    note = "The ferry timetable is kept in the blue binder by the door."
    runtime = MnemosRuntime(db_path=str(db), agent_id="nova", use_dedicated_model=False)
    try:
        runtime.handoff(note)
    finally:
        runtime.close()

    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MNEMOS_") and key != "CLAUDE_CODE_SESSION_ID"
    }
    env.update({"HOME": str(home), "MNEMOS_DISABLE_DOTENV": "1"})
    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli",
         "--db-path", str(db), "--agent-id", "nova", "hook", "session-start"],
        input="",
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "the hook contributed nothing"
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert note in context
    # And it read the store it was given, creating none where the default is.
    assert not (home / ".mnemos" / "nova.db").exists()
