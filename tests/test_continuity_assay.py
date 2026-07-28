"""The amnesia test: does anything actually survive a goodbye?

Every other test in this suite runs inside one process, with the store
already open and the scope already decided. That is not the claim Mnemos
makes. The claim is that something captured in one session is present in
the next one — a different process, started later, told nothing.

This file is the gate. It spawns real subprocesses and passes **no scope
arguments at all**, because defaults are what an agent actually gets. The
scope split-brain survived 202 green tests precisely because every scope
test hand-matched the writer and the reader; here, nothing is matched by
hand. If these fail, Mnemos does not do the one thing it exists to do,
regardless of what the rest of the suite says.
"""

import json
import subprocess
import sys
import uuid

import pytest


def _run(*args, home, cwd=None, timeout=180):
    """Run the Mnemos CLI in a genuinely separate process.

    ``cwd`` matters more than it looks. An MCP server is started by
    whichever client spawned it — Claude Code in the open project, a
    desktop app somewhere else entirely — so the writer and the reader
    routinely run from different directories. Scope derived from the
    working directory therefore partitions one agent's memory by launch
    location, which is exactly the bug that shipped.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            # A developer's real provider keys must never reach the assay.
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    return result


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".mnemos").mkdir(parents=True)
    return h


class TestContinuityCrossesAProcessBoundary:
    def test_a_capture_survives_into_a_later_session(self, home):
        """The whole product, in one assertion.

        Process one captures. Process two — started fresh, given no scope,
        no session id, nothing — must be handed that memory back.
        """
        token = f"cold-brew-{uuid.uuid4().hex[:12]}"

        wrote = _run("remember", f"Riley drinks {token} every morning", home=home)
        assert wrote.returncode == 0, wrote.stderr

        # A different process. Nothing carried over but the filesystem.
        read = _run("hook", "session-start", home=home)
        assert read.returncode == 0, read.stderr
        assert read.stdout.strip(), "the hook contributed nothing at all"

        packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]
        assert token in packet, (
            "continuity did not survive the process boundary — this is the "
            f"amnesia the system exists to prevent.\n\nPacket was:\n{packet}"
        )

    def test_continuity_survives_being_written_from_a_different_directory(
        self, home, tmp_path
    ):
        """Memory belongs to the agent, not to a folder.

        The MCP server is started by whichever client spawned it, so the
        session that captures and the session that reads routinely run
        from different working directories. When scope was derived from
        cwd, continuity written in one project became invisible from
        another while both layers reported success.
        """
        project_a = tmp_path / "project-alpha"
        project_b = tmp_path / "project-beta"
        project_a.mkdir()
        project_b.mkdir()

        token = f"cwd-{uuid.uuid4().hex[:12]}"
        wrote = _run(
            "remember", f"A decision recorded as {token}", home=home, cwd=project_a
        )
        assert wrote.returncode == 0, wrote.stderr

        read = _run("hook", "session-start", home=home, cwd=project_b)
        assert read.returncode == 0, read.stderr
        assert read.stdout.strip(), (
            "the packet was empty when read from a different directory than "
            "the one the capture was made in"
        )

        packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]
        assert token in packet, (
            "continuity captured in one directory was invisible from another — "
            "scope is following the working directory again"
        )

    def test_the_writer_and_the_reader_agree_on_scope_by_default(self, home):
        """Neither side is told a scope, and they must still meet.

        This is the exact failure that shipped: capture resolved through
        resolve_scope while the packet took literal parameter defaults, so
        the write and the read landed in different partitions and both
        reported success.
        """
        token = f"scope-{uuid.uuid4().hex[:12]}"
        assert _run("remember", f"A fact tagged {token}", home=home).returncode == 0

        doctor = _run("doctor", home=home)
        assert doctor.returncode == 0, doctor.stderr

        read = _run("hook", "session-start", home=home)
        packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]

        # The scope the packet reports must be the scope doctor resolves.
        for line in doctor.stdout.splitlines():
            if line.startswith("Agent:"):
                agent = line.split(":", 1)[1].strip()
                assert f"agent: {agent}" in packet, (
                    f"doctor resolved agent={agent} but the packet reports a "
                    f"different scope — writer and reader disagree"
                )
        assert token in packet

    def test_continuity_is_not_reset_by_a_later_session(self, home):
        """Later captures must not displace earlier ones from the packet."""
        first = f"first-{uuid.uuid4().hex[:8]}"
        second = f"second-{uuid.uuid4().hex[:8]}"

        assert _run("remember", f"Earliest fact {first}", home=home).returncode == 0
        assert _run("remember", f"Later fact {second}", home=home).returncode == 0

        read = _run("hook", "session-start", home=home)
        packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]

        assert second in packet, "the most recent capture is missing"
        assert first in packet, (
            "an earlier capture was displaced — continuity that only holds "
            "the last thing said is not continuity"
        )

    def test_a_correction_replaces_rather_than_accumulates(self, home):
        """A corrected fact must not sit beside its own stale version."""
        assert _run(
            "remember", "Riley's favourite editor is vim", home=home
        ).returncode == 0

        corrected = _run(
            "remember", "Correction: Riley's favourite editor is Zed, not vim",
            home=home,
        )
        assert corrected.returncode == 0

        read = _run("hook", "session-start", home=home)
        packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Zed" in packet


class TestReadingIsSafe:
    def test_reading_never_creates_a_store(self, home):
        """A read must not bring a memory into existence.

        `mnemos hook session-start` runs before every session. If reading
        creates the store, a mistyped path yields a permanently empty
        memory that reports itself as healthy.
        """
        read = _run("hook", "session-start", home=home)
        assert read.returncode == 0
        assert not list((home / ".mnemos").glob("*.db")), (
            "reading memory created a database"
        )

    def test_the_hook_never_fails_a_session(self, home):
        """Whatever goes wrong, the session must still start."""
        corrupt = home / ".mnemos" / "mnemos-agent.db"
        corrupt.write_bytes(b"this is not a sqlite database")

        read = _run("hook", "session-start", home=home)
        assert read.returncode == 0, "a broken store took the session down with it"
        assert read.stdout.strip() == "", "a broken store emitted a packet anyway"
