"""Parallel sessions in one scope keep their own handoffs.

One human often runs several Claude Code sessions at once in the same scope.
There used to be one handoff slot per scope, so every session's handoff
replaced whatever any other session had left. On 2026-09-25 a Sanctuary
steward session's note replaced a What Lights Up session's note within
minutes, and whichever thread started next was handed the other thread's note
first. On that store, 61 of 208 handoff replacements came from a different
session, and 18 of those replaced a note no session had read yet.

A handoff now belongs to the session that wrote it. Claude Code gives every
MCP server it spawns ``CLAUDE_CODE_SESSION_ID`` and gives the SessionStart
hook the same id, so neither the agent nor the human has to say anything.

These tests drive the defaults an agent actually hits: the runtime reading its
session from the environment, and the real hook reading its payload in
another process. None of them pass a session to the writer and the reader by
hand.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from mnemos.authorship import handoff_framing
from mnemos.interface.context_packet import build_context_packet
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

SCOPE = {"agent_id": "claude-code", "person_id": "user", "project_scope": "global"}

LIGHTS = "3e8bb391-ff5a-42b7-ad0c-0f49c5b36e44"   # the What Lights Up session
STEWARD = "9a1f2c3d-4b5e-4f60-8a7b-0c1d2e3f4a5b"  # the Sanctuary steward session
FRESH = "5d6e7f80-91a2-4b3c-8d4e-5f6a7b8c9d0e"    # a session that starts later

LIGHTS_NOTE = (
    "What Lights Up: the conversation page is done and open in the browser.\n"
    "Next: Riley's reaction to the page's pacing."
)
STEWARD_NOTE = (
    "Sanctuary steward: the season check-in ran and every resident posted.\n"
    "Next: read Opus 3's reply before the next visit."
)


def _transcript(config: Path, session: str, model: str) -> None:
    """A Claude Code transcript whose latest assistant turn ran ``model``."""

    path = config / "projects" / "-Users-someone-project" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": []},
    }) + "\n")


def _in_session(monkeypatch, tmp_path: Path, session: str, model: str = "claude-opus-5-5") -> None:
    """Make this process look like the MCP server of one Claude Code session."""

    config = tmp_path / "claude-config"
    _transcript(config, session, model)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


def _handoff_from(session: str, text: str, db_path: Path, tmp_path: Path,
                  model: str = "claude-opus-5-5") -> str:
    """Leave a handoff from another process, as that session's MCP server does."""

    config = tmp_path / "claude-config"
    _transcript(config, session, model)
    code = (
        "import sys\n"
        "from mnemos.simple_runtime import MnemosRuntime\n"
        "runtime = MnemosRuntime(db_path=sys.argv[1], agent_id='claude-code',"
        " use_dedicated_model=False)\n"
        "try:\n"
        "    print(runtime.handoff(sys.stdin.read()))\n"
        "finally:\n"
        "    runtime.close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db_path)],
        input=text,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "HOME": str(_home(tmp_path)),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_CONFIG_DIR": str(config),
        },
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _hook(db_path: Path, tmp_path: Path, payload: dict) -> str:
    """Run the real SessionStart hook in another process, as the harness does."""

    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db_path), "--agent-id", "claude-code"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "HOME": str(_home(tmp_path)),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def _start(session: str, source: str = "startup", model: str = "claude-opus-5-5") -> dict:
    # The payload Claude Code sends on SessionStart (checked against a real
    # run: session_id, transcript_path, cwd, hook_event_name, source).
    return {
        "hook_event_name": "SessionStart",
        "session_id": session,
        "source": source,
        "model": model,
    }


def _active(store: EngramStore) -> list[dict]:
    rows = store._get_conn().execute(
        """SELECT * FROM hypomnema_entries
           WHERE agent_id=? AND person_id=? AND project_scope=?
             AND entry_kind='handoff' AND active=1
           ORDER BY created_at""",
        tuple(SCOPE.values()),
    ).fetchall()
    return [dict(row) for row in rows]


def _runtime(db_path: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db_path), agent_id="claude-code", use_dedicated_model=False)


class TestTheIncident:
    def test_parallel_sessions_no_longer_replace_each_others_handoffs(self, tmp_path):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path)
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)

        store = EngramStore(db_path)
        try:
            assert {row["content"] for row in _active(store)} == {LIGHTS_NOTE, STEWARD_NOTE}
        finally:
            store.close()

        packet = _hook(db_path, tmp_path, _start(FRESH))
        # Both threads reach the next session, newest first, each signed.
        assert "Sanctuary steward: the season check-in ran" in packet
        assert "What Lights Up: the conversation page is done" in packet
        assert packet.index("Sanctuary steward") < packet.index("What Lights Up")
        assert packet.count("Opus 5.5 (claude-opus-5-5)") >= 2

    def test_after_compaction_a_session_is_handed_its_own_note_first(self, tmp_path):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path)
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)  # newer

        packet = _hook(db_path, tmp_path, _start(LIGHTS, source="compact"))
        assert LIGHTS_NOTE in packet
        assert packet.index("What Lights Up") < packet.index("Sanctuary steward")
        assert "Left earlier in this session by Opus 5.5 (claude-opus-5-5)" in packet

    def test_the_mcp_packet_hands_a_session_its_own_note_first(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path)
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)

        _in_session(monkeypatch, tmp_path, LIGHTS)
        runtime = _runtime(db_path)
        try:
            packet = runtime.context()
        finally:
            runtime.close()
        assert LIGHTS_NOTE in packet
        assert packet.index("What Lights Up") < packet.index("Sanctuary steward")


class TestReplacement:
    def test_a_session_replaces_only_its_own_earlier_handoff(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        runtime = _runtime(db_path)
        try:
            _in_session(monkeypatch, tmp_path, LIGHTS)
            first = runtime.handoff("lights, first note")
            _in_session(monkeypatch, tmp_path, STEWARD)
            runtime.handoff("steward, only note")
            _in_session(monkeypatch, tmp_path, LIGHTS)
            second = runtime.handoff("lights, second note")

            store = runtime._store
            assert {row["content"] for row in _active(store)} == {
                "lights, second note",
                "steward, only note",
            }
            first_id = first.split("Handoff ID: ", 1)[1].splitlines()[0]
            second_id = second.split("Handoff ID: ", 1)[1].splitlines()[0]
            prior = store.get_hypomnema_entry(first_id, **SCOPE)
            assert prior["active"] is False
            assert prior["superseded_by"] == second_id
            assert prior["content"] == "lights, first note"
        finally:
            runtime.close()

    def test_clients_without_a_session_id_still_share_one_note(self, tmp_path):
        # Other MCP clients can't say which session they are. They keep the
        # one shared slot every writer had before sessions were told apart.
        runtime = _runtime(tmp_path / "shared.db")
        try:
            runtime.handoff("first")
            runtime.handoff("second")
            assert [row["content"] for row in _active(runtime._store)] == ["second"]
        finally:
            runtime.close()

    def test_simultaneous_writers_keep_one_note_per_session(self, tmp_path):
        db_path = tmp_path / "concurrent.db"
        EngramStore(db_path).close()
        sessions = [LIGHTS] * 4 + [STEWARD] * 2 + [FRESH] * 2
        barrier = Barrier(len(sessions))

        def write(index: int) -> str:
            store = EngramStore(db_path)
            try:
                barrier.wait()
                return store.write_handoff(
                    f"writer {index}", author_session=sessions[index], **SCOPE,
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            ids = list(pool.map(write, range(len(sessions))))

        store = EngramStore(db_path)
        try:
            active = _active(store)
            assert sorted(row["author_session"] for row in active) == sorted({LIGHTS, STEWARD, FRESH})
            rows = store._get_conn().execute(
                "SELECT id FROM hypomnema_entries WHERE entry_kind='handoff'"
            ).fetchall()
            assert {row["id"] for row in rows} == set(ids)
        finally:
            store.close()

    def test_the_store_keeps_at_most_eight_sessions_notes(self, tmp_path):
        store = EngramStore(tmp_path / "many.db")
        try:
            sessions = [f"{index:08d}-0000-4000-8000-000000000000" for index in range(9)]
            for index, session in enumerate(sessions):
                store.write_handoff(f"note {index}", author_session=session, **SCOPE)
            active = _active(store)
            assert [row["content"] for row in active] == [f"note {index}" for index in range(1, 9)]
            oldest = store._get_conn().execute(
                "SELECT * FROM hypomnema_entries WHERE content = 'note 0'"
            ).fetchone()
            assert oldest["active"] == 0
            assert "newer sessions" in json.loads(oldest["revisions_json"])[-1]["reason"]
        finally:
            store.close()


class TestThePacketStaysSmall:
    def _seed(self, db_path: Path, tmp_path: Path, monkeypatch, count: int) -> list[str]:
        runtime = _runtime(db_path)
        notes = []
        try:
            runtime.capture("Riley wants short, plain messages.")
            runtime.capture("The staging deploy runs before production.")
            for index in range(count):
                session = f"{index:08d}-1111-4111-8111-111111111111"
                _in_session(monkeypatch, tmp_path, session)
                note = f"Thread {index} headline. " + " ".join(
                    f"thread{index}word{step}" for step in range(260)
                )
                runtime.handoff(note)
                notes.append(note)
        finally:
            runtime.close()
        return notes

    def test_one_note_whole_and_two_more_as_short_signed_lines(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        notes = self._seed(db_path, tmp_path, monkeypatch, count=6)

        packet = _hook(db_path, tmp_path, _start(FRESH))
        newest, second, third = notes[-1], notes[-2], notes[-3]
        assert newest in packet
        for note in (second, third):
            assert note not in packet
            assert note[:60] in packet
        for older in notes[:-3]:
            assert older[:30] not in packet
        # The other sessions' lines are short: they name the thread and how to
        # read it whole, and leave the rest of the packet to continuity.
        head = packet.split("### Scope", 1)[0]
        assert len(head) < len(newest) + 1600
        assert "Riley wants short, plain messages." in packet
        assert "The staging deploy runs before production." in packet

    def test_handoffs_never_take_continuity_slots(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        runtime = _runtime(db_path)
        try:
            facts = [f"Durable fact number {index} about the release." for index in range(6)]
            for fact in facts:
                runtime.capture(fact)
            for index in range(8):
                _in_session(monkeypatch, tmp_path, f"{index:08d}-2222-4222-8222-222222222222")
                runtime.handoff(f"Handoff {index} about the release and the durable facts.")
            monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

            packet = runtime.context(max_results=5)
            section = packet.split("Continuity notes:", 1)[1]
            assert sum(fact in section for fact in facts) == 5

            recalled = runtime.recall("release durable facts")
            assert "Handoff 0 about" not in recalled
            assert sum(fact in recalled for fact in facts) >= 3

            store_packet = build_context_packet(runtime._store, "release", **SCOPE)
            assert all(row["entry_kind"] != "handoff" for row in store_packet["hypomnema"])
            assert len(store_packet["hypomnema"]) == 6
        finally:
            runtime.close()

    def test_other_sessions_notes_leave_the_packet_after_three_days(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        runtime = _runtime(db_path)
        try:
            _in_session(monkeypatch, tmp_path, LIGHTS)
            runtime.handoff(LIGHTS_NOTE)
            four_days_ago = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
            runtime._store._get_conn().execute(
                "UPDATE hypomnema_entries SET created_at = ? WHERE entry_kind = 'handoff'",
                (four_days_ago,),
            )
            runtime._store._get_conn().commit()
        finally:
            runtime.close()

        # Alone, even an old note is still delivered: the newest note always is.
        assert LIGHTS_NOTE in _hook(db_path, tmp_path, _start(FRESH))

        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)
        packet = _hook(db_path, tmp_path, _start(FRESH))
        assert STEWARD_NOTE in packet
        assert "What Lights Up" not in packet
        # A session that comes back to its own old note still gets it first.
        own = _hook(db_path, tmp_path, _start(LIGHTS, source="resume"))
        assert own.index("What Lights Up") < own.index("Sanctuary steward")

    def test_every_note_shown_counts_as_delivered(self, tmp_path):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path)
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)
        _hook(db_path, tmp_path, _start(FRESH))
        store = EngramStore(db_path)
        try:
            assert [row["surface_count"] for row in _active(store)] == [1, 1]
        finally:
            store.close()


class TestSignatures:
    def test_another_sessions_note_is_a_colleagues_even_from_the_same_model(self, tmp_path):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path)

        packet = _hook(db_path, tmp_path, _start(FRESH))
        assert "Left by Opus 5.5 (claude-opus-5-5) — the same model as you, in another session —" in packet
        assert "colleague's" in packet
        assert "Carry on from it." not in packet

    def test_other_sessions_lines_are_signed_and_say_how_to_read_them(self, tmp_path):
        db_path = tmp_path / "shared.db"
        _handoff_from(LIGHTS, LIGHTS_NOTE, db_path, tmp_path, model="claude-fable-5-1")
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)

        packet = _hook(db_path, tmp_path, _start(FRESH))
        sibling = packet.split("Also live, from other sessions", 1)[1]
        assert "Fable 5.1 (claude-fable-5-1)" in sibling
        assert "mnemos_recall" in sibling

    def test_a_note_from_an_unknown_session_is_not_called_another_sessions(self, tmp_path):
        # A note written before sessions were told apart could be the
        # reader's own; the packet must not claim otherwise.
        db_path = tmp_path / "shared.db"
        store = EngramStore(db_path)
        try:
            store.write_handoff("Written before sessions were told apart.", **SCOPE)
        finally:
            store.close()
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)

        packet = _hook(db_path, tmp_path, _start(FRESH))
        assert "Written before sessions were told apart." in packet
        assert "### Also live\n" in packet
        assert "from other sessions" not in packet

    def test_framing_follows_the_session_as_well_as_the_model(self):
        heading, guidance = handoff_framing(
            "claude-opus-5-5", "3 minutes ago", "claude-opus-5-5", same_session=True,
        )
        assert heading == "Left earlier in this session by Opus 5.5 (claude-opus-5-5) — the same model as you — 3 minutes ago."
        assert guidance.startswith("Carry on from it.")

        heading, guidance = handoff_framing(
            "claude-fable-5-1", "an hour ago", "claude-opus-5-5", same_session=True,
        )
        assert "You are Opus 5.5 (claude-opus-5-5), a different model." in heading
        assert "colleague's" in guidance

        heading, guidance = handoff_framing(
            "claude-opus-5-5", "3 minutes ago", "claude-opus-5-5", same_session=False,
        )
        assert "in another session" in heading
        assert "Carry on from it." not in guidance
        assert "colleague's" in guidance

        heading, _ = handoff_framing("", "3 minutes ago", "claude-opus-5-5", same_session=False)
        assert "isn't signed" in heading

        # Without both sessions known, the framing is exactly what it was.
        assert handoff_framing("claude-opus-5-5", "3 minutes ago", "claude-opus-5-5") == (
            "Left by Opus 5.5 (claude-opus-5-5) — the same model as you — 3 minutes ago.",
            "Carry on from it. Don't narrate the memory system to the human.",
        )


class TestReadingANoteWhole:
    def test_recall_by_id_returns_another_sessions_note_whole(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        long_note = "What Lights Up, the long version. " + " ".join(
            f"detail{step}" for step in range(400)
        )
        _handoff_from(LIGHTS, long_note, db_path, tmp_path, model="claude-fable-5-1")
        _handoff_from(STEWARD, STEWARD_NOTE, db_path, tmp_path)

        packet = _hook(db_path, tmp_path, _start(FRESH))
        assert long_note not in packet
        handoff_id = packet.split('mnemos_recall("', 1)[1].split('")', 1)[0]

        _in_session(monkeypatch, tmp_path, FRESH)
        runtime = _runtime(db_path)
        try:
            recalled = runtime.recall(handoff_id)
        finally:
            runtime.close()
        assert long_note in recalled
        assert "Left by Fable 5.1 (claude-fable-5-1) in another session" in recalled


class TestCorrectionsLeaveHandoffsAlone:
    def test_a_correction_by_query_never_rewrites_a_handoff(self, tmp_path):
        runtime = _runtime(tmp_path / "shared.db")
        try:
            runtime.capture("The staging deploy runs on Fridays.")
            text = (
                "State: the deploy script is half rewritten.\n"
                "Next: finish it and run the staging deploy on Friday."
            )
            runtime.handoff(text)
            runtime.correct("The staging deploy runs on Thursdays now.", query="staging deploy")

            handoff = runtime._store.get_latest_handoff(**SCOPE)
            assert handoff["content"] == text
            notes = runtime._store.search_hypomnema("staging deploy", limit=10, **SCOPE)
            assert any("Thursdays" in note["content"] for note in notes)
        finally:
            runtime.close()

    def test_forgetting_by_query_never_removes_a_handoff(self, tmp_path):
        runtime = _runtime(tmp_path / "shared.db")
        try:
            runtime.capture("The staging deploy runs on Fridays.")
            runtime.handoff("Next: run the staging deploy on Friday.")
            runtime.correct("", query="staging deploy", action="forget")
            assert runtime._store.get_latest_handoff(**SCOPE)["content"] == (
                "Next: run the staging deploy on Friday."
            )
        finally:
            runtime.close()


def test_a_v9_store_upgrades_in_place_and_older_mnemos_still_writes_to_it(tmp_path):
    db_path = tmp_path / "legacy.db"
    store = EngramStore(db_path)
    try:
        store.write_handoff("Written before sessions were told apart.", **SCOPE)
    finally:
        store.close()

    # Put the store back the way v9 left it: no session column, and one
    # active handoff allowed per scope.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX idx_hypomnema_one_active_handoff")
        conn.execute("ALTER TABLE hypomnema_entries DROP COLUMN author_session")
        conn.execute(
            "CREATE UNIQUE INDEX idx_hypomnema_one_active_handoff "
            "ON hypomnema_entries(agent_id, person_id, project_scope) "
            "WHERE active = 1 AND entry_kind = 'handoff'"
        )
        conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()

    upgraded = EngramStore(db_path)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        legacy = upgraded.get_latest_handoff(**SCOPE)
        assert legacy["content"] == "Written before sessions were told apart."
        assert legacy["author_session"] == ""
        upgraded.write_handoff("lights", author_session=LIGHTS, **SCOPE)
        upgraded.write_handoff("steward", author_session=STEWARD, **SCOPE)
        assert len(_active(upgraded)) == 3
    finally:
        upgraded.close()
    assert len(list((tmp_path / "backups").glob(f"legacy.pre-v{SCHEMA_VERSION}-*.db"))) == 1

    # A session opened before the upgrade still runs the older code. Opening
    # the store must not fail on its index statement, and its handoff write
    # must replace the note no session owns, never another session's.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypomnema_one_active_handoff "
            "ON hypomnema_entries(agent_id, person_id, project_scope) "
            "WHERE active = 1 AND entry_kind = 'handoff'"
        )
        prior = conn.execute(
            "SELECT * FROM hypomnema_entries "
            "WHERE agent_id = ? AND person_id = ? AND project_scope = ? "
            "AND entry_kind = 'handoff' AND active = 1 LIMIT 1",
            tuple(SCOPE.values()),
        ).fetchone()
        assert prior["content"] == "Written before sessions were told apart."
        conn.execute("UPDATE hypomnema_entries SET active = 0 WHERE id = ?", (prior["id"],))
        conn.execute(
            "INSERT INTO hypomnema_entries(id, agent_id, person_id, project_scope, content,"
            " entry_kind, authored_by, created_at, last_revised_at)"
            " VALUES ('old-writer', ?, ?, ?, 'from an older session', 'handoff', 'agent',"
            " '2026-09-25T12:00:00+00:00', '2026-09-25T12:00:00+00:00')",
            tuple(SCOPE.values()),
        )
        conn.commit()
        active = {
            row["content"] for row in conn.execute(
                "SELECT content FROM hypomnema_entries WHERE entry_kind='handoff' AND active=1"
            )
        }
        assert active == {"lights", "steward", "from an older session"}
    finally:
        conn.close()
