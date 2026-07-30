"""Continuity that arrives without being asked for.

Two mechanisms carry memory into a session with no manual trigger: the
server instructions every MCP client reads, and the SessionStart hook that
injects the packet before the first turn. Both are easy to break silently
— instructions are just a constructor argument, and a hook that raises
looks identical to a hook with nothing to say.
"""

import json

import pytest


def _seed_home(tmp_path):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


class TestServerInstructions:
    """Without these, an agent has tools but no idea it should use them."""

    def test_both_surfaces_ship_instructions(self):
        pytest.importorskip("mcp.server.fastmcp")
        from mnemos.mcp_server import mcp
        from mnemos.simple_mcp import simple_mcp

        for server in (simple_mcp, mcp):
            assert server.instructions, "server exposes no instructions to the client"

    def test_instructions_name_the_startup_and_capture_loop(self):
        pytest.importorskip("mcp.server.fastmcp")
        from mnemos.simple_mcp import simple_mcp

        instructions = simple_mcp.instructions
        assert "mnemos_context" in instructions
        assert "mnemos_capture" in instructions
        # The agent must be told not to narrate the plumbing at the human.
        assert "Never narrate the machinery" in instructions


class TestSessionStartHook:
    def test_emits_a_valid_session_start_payload(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main
        from mnemos.simple_runtime import MnemosRuntime

        db_path = str(tmp_path / "hook.db")
        secret = "The staging deploy always runs before production"

        runtime = MnemosRuntime(db_path=db_path, agent_id="demo")
        runtime.capture(secret, importance="high")
        runtime.close()

        assert main([
            "hook", "session-start", "--db-path", db_path, "--agent-id", "demo",
        ]) == 0

        payload = json.loads(capsys.readouterr().out)
        block = payload["hookSpecificOutput"]
        assert block["hookEventName"] == "SessionStart"
        assert secret in block["additionalContext"], (
            "the hook did not carry captured continuity into the new session"
        )

    def test_a_corrupt_store_never_kills_a_session(self, tmp_path, monkeypatch, capsys):
        """A memory hiccup must cost the user nothing.

        The harness treats a non-zero exit as a failed hook. Whatever goes
        wrong in here, the session must still start — silently.
        """
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")

        assert main([
            "hook", "session-start", "--db-path", str(corrupt), "--agent-id", "demo",
        ]) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_reading_memory_never_creates_a_store(self, tmp_path, monkeypatch, capsys):
        """A mistyped --db-path must not mint an empty database each session.

        Opening a store builds its schema, so the hook would otherwise
        create a fresh empty memory at every session start and report a
        perfectly healthy — and permanently empty — packet.
        """
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        missing = tmp_path / "typo" / "memory.db"
        assert main([
            "hook", "session-start", "--db-path", str(missing), "--agent-id", "demo",
        ]) == 0
        assert capsys.readouterr().out.strip() == ""
        assert not missing.exists(), "reading memory created a database"


class TestTheHookNeverBlocksOnStdin:
    """Session start must not hang, whatever the harness does with stdin.

    The hook drains stdin (harnesses may pass a payload there), but a bare
    ``sys.stdin.read()`` reads to EOF — so a harness that spawns the hook and
    leaves stdin open blocks it forever. A hook that never returns is worse
    than one that returns nothing: it stalls the session it exists to help.

    Driven as a real subprocess with an open stdin pipe, because that is the
    only way to observe a blocking read. Points at a missing store so the hook
    returns promptly via its own no-store guard; the timeout is what fails on
    the blocking version.
    """

    def test_returns_promptly_with_stdin_held_open(self, tmp_path):
        import subprocess
        import sys

        missing = tmp_path / "nope" / "memory.db"
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "mnemos.cli", "hook", "session-start",
                "--db-path", str(missing), "--agent-id", "demo",
            ],
            # stdin is a pipe whose write end we deliberately hold open and
            # never feed. `communicate()` would close it (sending EOF), which
            # is exactly the case that does NOT hang — so we wait() instead and
            # leave stdin open, reproducing a harness that forgot to close it.
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "HOME": str(_seed_home(tmp_path)),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "MNEMOS_DISABLE_DOTENV": "1",
                "PYTHONPATH": ":".join(sys.path),
            },
        )
        try:
            # On the blocking version `sys.stdin.read()` never sees EOF, so the
            # process never exits and this raises TimeoutExpired — the failure.
            returncode = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError(
                "the hook hung with stdin held open — session start would stall"
            )
        finally:
            if proc.stdin:
                proc.stdin.close()
        assert returncode == 0


class TestContinuityOnlyPacket:
    """What the hook injects into every session, and what it must not.

    Mnemos is a continuity and identity layer running alongside whatever
    memory system the human already has. The long-term graph accumulates
    general recall; on one live store a session-start packet spent five of
    six graph slots on paraphrases of one fact. The packet defaults to
    continuity, which is what an agent cannot reconstruct elsewhere.
    """

    def _seed(self, tmp_path):
        from mnemos.simple_runtime import MnemosRuntime

        db_path = str(tmp_path / "packet.db")
        runtime = MnemosRuntime(db_path=db_path, agent_id="demo")
        runtime.capture("Riley prefers cold brew", importance="high")
        runtime.close()
        return db_path

    def test_the_hook_omits_the_graph_by_default(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        db_path = self._seed(tmp_path)
        assert main([
            "hook", "session-start", "--db-path", db_path, "--agent-id", "demo",
        ]) == 0

        context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "Mnemos Graph" not in context, (
            "the graph section must be absent, not merely empty — a heading "
            "announcing nothing is still noise at the top of every session"
        )
        assert "Riley prefers cold brew" in context, "continuity was dropped too"

    def test_the_graph_can_be_opted_back_in(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        db_path = self._seed(tmp_path)
        assert main([
            "hook", "session-start", "--db-path", db_path,
            "--agent-id", "demo", "--include-graph",
        ]) == 0

        context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "Mnemos Graph" in context

    def test_one_long_note_cannot_consume_the_whole_packet(self, tmp_path, monkeypatch):
        """Breadth beats depth in a block injected before every session.

        Continuity notes can run to thousands of characters. Left whole, two
        or three consumed the entire budget and everything after them — other
        notes, later sections — was hard-cut mid-word by the clamp. On the
        live store this was cutting a sentence off at "...and every".
        """
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.interface.context_packet import build_context_packet
        from mnemos.simple_runtime import MnemosRuntime
        from mnemos.store.sqlite_store import EngramStore

        db_path = str(tmp_path / "long.db")
        runtime = MnemosRuntime(db_path=db_path, agent_id="demo")
        for i in range(6):
            runtime.capture(
                f"Note {i}: " + ("the quick brown fox jumps over the lazy dog. " * 90),
                importance="high",
            )
        runtime.capture("Riley prefers cold brew", importance="high")
        runtime.close()

        store = EngramStore(db_path)
        try:
            packet = build_context_packet(
                store, "", agent_id="demo", person_id="user",
                project_scope="global", token_budget=2600,
            )
        finally:
            store.close()

        prompt = packet["prompt"]
        assert "[context packet truncated" not in prompt, (
            "the packet still overflows its budget and is being hard-cut"
        )
        # Every section must survive, not just the ones that sorted first.
        assert "### Hypomnema" in prompt
        assert "### Review Queue" in prompt
        # And no note may end mid-word.
        for line in prompt.splitlines():
            if line.startswith("- ") and "…" in line:
                assert " […]" in line, f"clipped mid-word: {line[-40:]!r}"

    def test_dream_journal_notes_are_not_shown_as_continuity(self, tmp_path, monkeypatch):
        """Consolidation diary is not continuity about the human or the work.

        "I connected 148 memories that belong together" was appearing as an
        ordinary continuity note in this path, spending a packet slot on the
        machinery describing itself.
        """
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.dream_journal import DREAM_JOURNAL_TAG, write_dream_entry
        from mnemos.interface.context_packet import build_context_packet
        from mnemos.simple_runtime import MnemosRuntime
        from mnemos.store.sqlite_store import EngramStore

        db_path = str(tmp_path / "dream.db")
        runtime = MnemosRuntime(db_path=db_path, agent_id="demo")
        runtime.capture("Riley ships at 3am", importance="high")
        write_dream_entry(runtime._store, runtime.scope, "I connected 148 memories.")
        runtime.close()

        store = EngramStore(db_path)
        try:
            packet = build_context_packet(
                store, "", agent_id="demo", person_id="user", project_scope="global",
            )
        finally:
            store.close()

        for entry in packet["hypomnema"]:
            assert DREAM_JOURNAL_TAG not in (entry.get("tags") or [])
        assert "148 memories" not in packet["prompt"]


class TestHookInstaller:
    def test_write_preserves_unrelated_settings_and_hooks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {"SessionStart": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "echo unrelated"}],
            }]},
        }))

        assert main(["hooks", "install", "--write", "--settings", str(settings)]) == 0

        data = json.loads(settings.read_text())
        entries = data["hooks"]["SessionStart"]
        commands = [h["command"] for e in entries for h in e["hooks"]]
        assert data["model"] == "opus", "unrelated settings keys were dropped"
        assert "echo unrelated" in commands, "someone else's hook was dropped"
        assert any("mnemos" in c for c in commands)

    def test_reinstall_replaces_rather_than_stacks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        settings = tmp_path / "settings.json"
        for _ in range(3):
            assert main(["hooks", "install", "--write", "--settings", str(settings)]) == 0

        data = json.loads(settings.read_text())
        commands = [
            h["command"]
            for e in data["hooks"]["SessionStart"]
            for h in e["hooks"]
            if "mnemos" in h["command"]
        ]
        assert len(commands) == 1, f"hook stacked on reinstall: {commands}"

    def test_refuses_to_clobber_an_unparseable_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        settings = tmp_path / "settings.json"
        settings.write_text("{ this is not json")

        assert main(["hooks", "install", "--write", "--settings", str(settings)]) == 1
        assert settings.read_text() == "{ this is not json"

    def test_hook_points_at_the_running_installation(self, tmp_path, monkeypatch):
        """Not whatever `mnemos` a PATH lookup happens to find first.

        A Homebrew copy shadowing a pipx or venv install would otherwise be
        baked into the hook, pointing every future session at a different
        installation and a different store than the one just set up.
        """
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        wrong = tmp_path / "elsewhere" / "mnemos"
        wrong.parent.mkdir(parents=True)
        wrong.write_text("#!/bin/sh\nexit 0\n")
        wrong.chmod(0o755)
        monkeypatch.setattr("shutil.which", lambda _name: str(wrong))

        running = tmp_path / "bin" / "mnemos"
        running.parent.mkdir(parents=True)
        running.write_text("#!/bin/sh\nexit 0\n")
        running.chmod(0o755)
        monkeypatch.setattr("sys.argv", [str(running), "hooks", "install"])

        settings = tmp_path / "settings.json"
        assert main(["hooks", "install", "--write", "--settings", str(settings)]) == 0

        command = json.loads(settings.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert command.startswith(str(running)), command
        assert str(wrong) not in command

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(_seed_home(tmp_path)))
        from mnemos.cli import main

        settings = tmp_path / "settings.json"
        assert main(["hooks", "install", "--settings", str(settings)]) == 0
        assert not settings.exists()
        assert "SessionStart" in capsys.readouterr().out
