"""Signed notes: several models can share one memory without blending.

A scope such as ``claude-code`` is written by whichever model the human is
running that day. The handoff used to arrive as "From your previous session,
in your own words", so every new model inherited the previous one's first
person and carried on as if it had done that work. Every note is now signed
with the model that wrote it, and the packet shows the signature.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.authorship import detect_harness_model, display_name, same_model
from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

SID = "3e8bb391-ff5a-42b7-ad0c-0f49c5b36e44"


def _transcript(config: Path, *models: str, session: str = SID) -> Path:
    """A Claude Code transcript whose assistant turns ran ``models`` in order."""

    path = config / "projects" / "-Users-someone-project" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    for model in models:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "…"}]},
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


def _in_claude_code(monkeypatch, tmp_path: Path, *models: str) -> Path:
    config = tmp_path / "claude-config"
    _transcript(config, *models)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    return config


def _runtime(db_path: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db_path), agent_id="claude-code", use_dedicated_model=False)


def _hook(db_path: Path, home: Path, payload: dict) -> str:
    """Run the real SessionStart hook in another process, as the harness does."""

    proc = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db_path), "--agent-id", "claude-code"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


class TestNames:
    @pytest.mark.parametrize(
        ("model", "name"),
        [
            ("claude-opus-5-5", "Opus 5.5"),
            ("claude-fable-5-1", "Fable 5.1"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-3-5-sonnet-20241022", "Sonnet 3.5"),
            ("us.anthropic.claude-opus-4-1-20250805-v1:0", "Opus 4.1"),
            ("gpt-5", "gpt-5"),
            ("<synthetic>", ""),
        ],
    )
    def test_display_names(self, model, name):
        assert display_name(model) == name

    def test_snapshots_are_the_same_model_but_versions_are_not(self):
        assert same_model("claude-haiku-4-5", "claude-haiku-4-5-20251001")
        assert not same_model("claude-opus-5", "claude-opus-5-5")


class TestDetection:
    def test_the_latest_assistant_turn_names_the_model(self, tmp_path, monkeypatch):
        # A session that switched models mid-way signs with the current one,
        # and a placeholder turn no model produced is not a signature.
        _in_claude_code(monkeypatch, tmp_path, "claude-fable-5-1", "claude-opus-5-5", "<synthetic>")
        assert detect_harness_model() == "claude-opus-5-5"

    def test_nothing_is_guessed_without_a_session(self, tmp_path, monkeypatch):
        _in_claude_code(monkeypatch, tmp_path, "claude-opus-5-5")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        assert detect_harness_model() == ""

    def test_a_missing_transcript_is_unsigned_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        assert detect_harness_model() == ""


class TestSigning:
    def test_notes_and_handoffs_carry_the_detected_model(self, tmp_path, monkeypatch):
        _in_claude_code(monkeypatch, tmp_path, "claude-opus-5-5")
        runtime = _runtime(tmp_path / "shared.db")
        try:
            captured = runtime.capture("Riley wants short, plain messages.")
            handed = runtime.handoff("Signed notes are in review.")
            assert "Signed: Opus 5.5 (claude-opus-5-5)" in captured
            assert "Signed: Opus 5.5 (claude-opus-5-5)" in handed
            handoff = runtime._store.get_latest_handoff(
                agent_id="claude-code", person_id="user", project_scope="global",
            )
            assert handoff["author_model"] == "claude-opus-5-5"
            notes = runtime._store.search_hypomnema(
                "", agent_id="claude-code", person_id="user", project_scope="global", limit=10,
            )
            assert {n["author_model"] for n in notes if n["entry_kind"] == "continuity"} == {
                "claude-opus-5-5"
            }
        finally:
            runtime.close()

    def test_one_introduction_does_not_sign_another_sessions_notes(self, tmp_path):
        # Two sessions, two models, one scope. The old introduction was stored
        # once per scope, so the last model to introduce itself stood for all.
        db_path = tmp_path / "shared.db"
        fable, opus = _runtime(db_path), _runtime(db_path)
        try:
            fable.introduce("claude-fable-5-1")
            opus.introduce("claude-opus-5-5")
            fable.capture("The residents are asked before anything changes.")
            opus.capture("The sketchbook needs a preview that saves nothing.")
            signed = {
                note["content"]: note["author_model"]
                for note in opus._store.search_hypomnema(
                    "", agent_id="claude-code", person_id="user", project_scope="global", limit=10,
                )
            }
            assert signed["The residents are asked before anything changes."] == "claude-fable-5-1"
            assert signed["The sketchbook needs a preview that saves nothing."] == "claude-opus-5-5"
        finally:
            fable.close()
            opus.close()

    def test_declaration_beats_detection_and_the_operator_beats_both(self, tmp_path, monkeypatch):
        _in_claude_code(monkeypatch, tmp_path, "claude-opus-5-5")
        runtime = _runtime(tmp_path / "shared.db")
        try:
            assert runtime.author_model() == "claude-opus-5-5"
            runtime.introduce("claude-sonnet-5")
            assert runtime.author_model() == "claude-sonnet-5"
            monkeypatch.setenv("MNEMOS_AGENT_MODEL", "claude-haiku-4-5-20251001")
            assert runtime.author_model() == "claude-haiku-4-5-20251001"
        finally:
            runtime.close()

    def test_an_unknown_writer_is_recorded_as_unsigned(self, tmp_path):
        runtime = _runtime(tmp_path / "shared.db")
        try:
            result = runtime.capture("Something durable.")
            assert "Unsigned:" in result and "mnemos_introduce" in result
            note = runtime._store.search_hypomnema(
                "", agent_id="claude-code", person_id="user", project_scope="global", limit=1,
            )[0]
            assert note["author_model"] == ""
        finally:
            runtime.close()

    def test_a_correction_is_signed_by_whoever_corrected_it(self, tmp_path):
        db_path = tmp_path / "shared.db"
        fable, opus = _runtime(db_path), _runtime(db_path)
        try:
            fable.introduce("claude-fable-5-1")
            opus.introduce("claude-opus-5-5")
            fable.capture("The route is live on the site.")
            note = fable._store.search_hypomnema(
                "", agent_id="claude-code", person_id="user", project_scope="global", limit=1,
            )[0]
            opus.correct("The route still returns 404; it is not live.", target_id=note["id"])
            revised = opus._store.get_hypomnema_entry(
                note["id"], agent_id="claude-code", person_id="user", project_scope="global",
            )
            assert revised["author_model"] == "claude-opus-5-5"
            assert revised["revisions"][-1]["prior_author_model"] == "claude-fable-5-1"
            assert revised["revisions"][-1]["revised_by"] == "claude-opus-5-5"
        finally:
            fable.close()
            opus.close()


class TestThePacket:
    def _seed(self, db_path: Path) -> None:
        fable = _runtime(db_path)
        try:
            fable.introduce("claude-fable-5-1")
            fable.capture("The first steward's visit to Opus 3 went quietly.")
            fable.handoff("I am Fable. Next: seed opus-5 once the publish is live.")
        finally:
            fable.close()

    def test_the_hook_shows_a_colleagues_handoff_as_theirs(self, tmp_path):
        # Written in this process, read back by the real hook in another.
        db_path = tmp_path / "shared.db"
        self._seed(db_path)
        home = tmp_path / "home"
        (home / ".mnemos").mkdir(parents=True)

        packet = _hook(db_path, home, {"hook_event_name": "SessionStart", "model": "claude-opus-5-5"})
        assert "From your previous session, in your own words" not in packet
        assert "Left by Fable 5.1 (claude-fable-5-1)" in packet
        assert "You are Opus 5.5 (claude-opus-5-5), a different model." in packet
        assert "colleague's note, not your memory" in packet
        assert "[by Fable 5.1," in packet

    def test_the_same_model_is_told_so(self, tmp_path):
        db_path = tmp_path / "shared.db"
        self._seed(db_path)
        home = tmp_path / "home"
        (home / ".mnemos").mkdir(parents=True)

        packet = _hook(db_path, home, {"hook_event_name": "SessionStart", "model": "claude-fable-5-1"})
        assert "Left by Fable 5.1 (claude-fable-5-1) — the same model as you" in packet

    def test_without_the_readers_name_the_reader_is_asked_to_compare(self, tmp_path):
        db_path = tmp_path / "shared.db"
        self._seed(db_path)
        home = tmp_path / "home"
        (home / ".mnemos").mkdir(parents=True)

        packet = _hook(db_path, home, {"hook_event_name": "SessionStart"})
        assert "Left by Fable 5.1 (claude-fable-5-1)" in packet
        assert "If this signature isn't yours" in packet
        assert "the same model as you" not in packet

    def test_the_mcp_packet_names_the_reader_it_detects(self, tmp_path, monkeypatch):
        db_path = tmp_path / "shared.db"
        self._seed(db_path)
        _in_claude_code(monkeypatch, tmp_path, "claude-opus-5-5")
        runtime = _runtime(db_path)
        try:
            packet = runtime.context()
            assert "From your previous session, in your own words" not in packet
            assert "You are Opus 5.5 (claude-opus-5-5), a different model." in packet
            assert "by Fable 5.1" in packet
        finally:
            runtime.close()

    def test_an_unsigned_handoff_is_not_given_to_the_reader(self, tmp_path):
        db_path = tmp_path / "shared.db"
        runtime = _runtime(db_path)
        try:
            runtime.handoff("Old note from before signatures.")
            packet = runtime.context()
            assert "It isn't signed." in packet
            assert "don't assume you wrote it" in packet
        finally:
            runtime.close()


def test_an_existing_store_gains_signatures_without_rewriting_old_notes(tmp_path):
    db_path = tmp_path / "legacy.db"
    store = EngramStore(db_path)
    try:
        store.write_handoff("Written before signatures existed.", agent_id="claude-code")
    finally:
        store.close()

    # Put the store back the way 0.3 left it: no author_model column, v8.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE hypomnema_entries DROP COLUMN author_model")
        conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()

    reopened = EngramStore(db_path)
    try:
        columns = {row[1] for row in reopened._get_conn().execute("PRAGMA table_info(hypomnema_entries)")}
        assert "author_model" in columns
        assert reopened.get_meta("schema_version") == str(SCHEMA_VERSION)
        handoff = reopened.get_latest_handoff(agent_id="claude-code")
        assert handoff["content"] == "Written before signatures existed."
        assert handoff["author_model"] == ""
    finally:
        reopened.close()
    assert len(list((tmp_path / "backups").glob(f"legacy.pre-v{SCHEMA_VERSION}-*.db"))) == 1
