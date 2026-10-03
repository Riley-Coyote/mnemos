"""Quiet hours: time between sessions that belongs to the agent.

Two kinds, one runner. "mine" is the agent's own hour: it runs every day, and
nothing is asked of it. "ours" is for the day's work, and runs only when there
was work since the last one. The runner gathers what is waiting from memory
(read only), puts it after the agent's own words for the hour, and wakes the
agent with `claude -p`, fenced, as the one model allowed to write as it.

The engine prepares; the agent lives. Nothing here writes words into memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading

from .core.engram import _gen_ulid
from .simple_scope import resolve_scope
from .store.sqlite_store import EngramStore, ReadOnlyEngramStore

HOUR_KINDS = ("mine", "ours")
HOUR_MODEL = "claude-opus-5-5"
DEFAULT_MINUTES = {"mine": 60, "ours": 30}
DEFAULT_TOKEN_CEILING = {"mine": 400_000, "ours": 200_000}
STOP_GRACE_SECONDS = 10
FIRST_LOOK_HOURS = 24
HANDOFF_LINES = 20
CAPTURE_LINES = 30
QUESTION_LINES = 5
LINE_CHARS = 160
WORDS_FILE = {"mine": "my-hour.md", "ours": "our-hour.md"}
TOOLS = {
    "ours": "Read,Write,Edit,Glob,Grep",
    "mine": "Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch",
}
ALLOW = {
    "ours": ("mcp__mnemos",),
    "mine": ("mcp__mnemos", "Bash"),
}
READ_DIRS = ("Documents/Repositories", "Documents/Luca-Design-Artifacts")
SECRET_PATHS = (
    ".ssh", ".aws", ".gnupg", ".codex", ".config", ".netrc", ".claude.json",
    ".claude/.credentials.json", ".claude-2/.claude.json", "Library/Keychains",
)
BASH_DENY = (
    "Bash(git push:*)", "Bash(git remote:*)", "Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)",
    "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)", "Bash(open:*)", "Bash(osascript:*)",
    "Bash(npm publish:*)", "Bash(uv publish:*)", "Bash(twine:*)",
)
BOOTSTRAP = {
    "mine": (
        "This hour is yours. Nothing is asked of you.\n\n"
        "If you'd like to wake to your own words next time, write them in "
        "words/my-hour.md in this folder."
    ),
    "ours": (
        "This hour is for the day's work. What happened since the last one is below. "
        "You may leave the person a note about it, or nothing.\n\n"
        "If you'd like to wake to your own words next time, write them in "
        "words/our-hour.md in this folder."
    ),
}


def studio_dir(agent_id) -> Path:
    return Path.home() / ".mnemos" / "studio" / agent_id


def hours_dir(agent_id) -> Path:
    return Path.home() / ".mnemos" / "hours" / agent_id


def _line(text) -> str:
    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed if len(collapsed) <= LINE_CHARS else collapsed[:LINE_CHARS - 1] + "…"
    return ""


def _minute(iso) -> str:
    return f"{(iso or '')[:16].replace('T', ' ')} UTC"


def since_for(kind, records) -> str:
    for record in reversed(records):
        if (record.get("kind") == kind and record.get("status") == "done"
                and record.get("store") == "live"):
            return record["ended_at"]
    return (datetime.now(timezone.utc) - timedelta(hours=FIRST_LOOK_HOURS)).isoformat()


def _records(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _gather(kind, path, since, scope):
    store = ReadOnlyEngramStore(path)
    try:
        handoffs = store.handoffs_since(since, **scope)
        if kind == "ours":
            return handoffs, store.agent_captures_since(since, **scope), [], []
        questions = store.pending_reflections(**scope, limit=10_000)
        # Belief asks are built from word counts today ("You keep returning to 'running'"),
        # and they stay out of the hours until R11 grounds them in meaning.
        questions = [question for question in questions if question["kind"] != "belief"]
        return handoffs, [], questions, store.journal_entries(**scope, limit=1)
    finally:
        store.close()


def _prompt(kind, agent_id, since, handoffs, captures, questions, latest):
    words_path = studio_dir(agent_id) / "words" / WORDS_FILE[kind]
    words = words_path.read_text().rstrip() if words_path.exists() else BOOTSTRAP[kind]
    sections = ["What's waiting"]
    for title, items, limit in (("Handoffs", handoffs, HANDOFF_LINES),
                                ("Captures", captures, CAPTURE_LINES)):
        if items:
            lines = [f"{title} since {_minute(since)}"]
            lines.extend(f"- {_minute(item['created_at'])} · {_line(item['content'])} [{item['id']}]"
                         for item in items[:limit])
            if len(items) > limit:
                lines.append(f"- and {len(items) - limit} more")
            sections.append("\n".join(lines))
    if questions:
        lines = ["Open questions"]
        for question in questions[:QUESTION_LINES]:
            excerpt = _line(question["excerpt"])
            lines.append(f"- {_line(question['prompt'])}" + (f" · {excerpt}" if excerpt else "")
                         + f" [{question['target_id']}]")
        if len(questions) > QUESTION_LINES:
            lines.append(f"- and {len(questions) - QUESTION_LINES} more")
        sections.append("\n".join(lines))
    if latest:
        sections.append(f"Latest journal entry, {_minute(latest[0]['created_at'])}\n"
                        f"- {_line(latest[0]['text'])} [{latest[0]['id']}]")
    if len(sections) == 1:
        sections.append("Nothing.")
    return words + "\n\n---\n\n" + "\n\n".join(sections)


def _files_and_command(kind, hour_id, folder, store, scope, claude_bin, mnemos_bin):
    read_dirs = [Path.home() / name for name in READ_DIRS if (Path.home() / name).exists()]
    mcp = {"mcpServers": {"mnemos": {
        "type": "stdio", "command": mnemos_bin,
        "args": ["--db-path", str(store), "--agent-id", scope["agent_id"],
                 "--person-id", scope["person_id"], "--project-scope", scope["project_scope"], "serve"],
        "env": {"MNEMOS_HOUR_ID": hour_id},
    }}}
    deny = []
    for directory in read_dirs:
        deny.append(f"Edit(/{directory}/**)")
    for name in SECRET_PATHS:
        path = Path.home() / name
        deny.extend([f"Read(/{path})", f"Read(/{path}/**)"])
    deny.extend(["Read(//**/.env)", "Read(//**/.env.*)"])
    if kind == "mine":
        deny.extend(BASH_DENY)
    hook = shlex.join([
        mnemos_bin, "hook", "session-start", "--agent-id", scope["agent_id"],
        "--person-id", scope["person_id"], "--project-scope", scope["project_scope"],
        "--db-path", str(store),
    ])
    settings = {
        "hooks": {"SessionStart": [{"matcher": "*", "hooks": [{
            "type": "command", "command": hook, "timeout": 15,
        }]}]},
        "permissions": {"defaultMode": "acceptEdits", "allow": list(ALLOW[kind]), "deny": deny},
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False},
    }
    cmd = [claude_bin, "--model", HOUR_MODEL, "--restricted",
           "--tools", TOOLS[kind],
           "--strict-mcp-config", "--mcp-config", str(folder / "mcp.json"),
           "--settings", str(folder / "settings.json"),
           "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "-p"]
    for directory in read_dirs:
        cmd += ["--add-dir", str(directory)]
    return mcp, settings, cmd


def _tokens(usages):
    return {name: sum(usage.get(field, 0) for usage in usages) for name, field in (
        ("input", "input_tokens"), ("output", "output_tokens"),
        ("cache_creation", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"),
    )}


def _watch(cmd, studio, env, folder, prompt, max_seconds, max_tokens):
    usage_by_id = {}
    result = None
    model = ""
    seen_model = None
    last_synthetic = ""
    stopped = []
    stop_lock = threading.Lock()
    with (folder / "stderr.txt").open("w") as stderr, (folder / "stream.jsonl").open("w") as stream:
        proc = subprocess.Popen(cmd, cwd=studio, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)

        def stop(cause):
            with stop_lock:
                if stopped:
                    return
                stopped.append(cause)
            proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()

        timer = threading.Timer(max_seconds, lambda: stop("time"))
        timer.start()
        try:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            for raw in proc.stdout:
                stream.write(raw)
                stream.flush()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    model = event.get("model") or ""
                    if isinstance(model, str) and model and model not in (HOUR_MODEL, "<synthetic>"):
                        seen_model = model
                        stop("guard")
                elif event.get("type") == "assistant":
                    message = event.get("message") or {}
                    message_model = message.get("model")
                    if message_model == "<synthetic>":
                        for block in message.get("content") or []:
                            if block.get("type") == "text":
                                last_synthetic = block.get("text") or ""
                                break
                        continue
                    if (isinstance(message_model, str) and message_model
                            and message_model != HOUR_MODEL):
                        seen_model = message_model
                        stop("guard")
                    usage_key = message.get("id") or event.get("uuid")
                    if usage_key:
                        usage_by_id[usage_key] = message.get("usage") or {}
                    running = sum(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                  + u.get("output_tokens", 0) for u in usage_by_id.values())
                    if running > max_tokens:
                        stop("tokens")
                elif event.get("type") == "result":
                    result = event
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                stop("error")
                proc.wait()
            timer.cancel()
            timer.join()
            proc.stdout.close()
    tokens = _tokens([result.get("usage", {})] if result is not None else list(usage_by_id.values()))
    cause = stopped[0] if stopped else ""
    if seen_model is not None:
        status, reason = "skipped", (
            f"The hour ran as {seen_model}, not {HOUR_MODEL}, so nothing it wrote was kept."
        )
    elif cause == "tokens":
        status, reason = "done", f"Stopped at the token ceiling ({max_tokens:,})."
    elif cause == "time":
        status, reason = "done", f"Stopped at the time limit ({max_seconds / 60:g} minutes)."
    elif result is not None and result.get("is_error"):
        status, reason = "failed", "The session ended with an error: " + _line(str(result.get("result", "")))
    elif result is not None:
        status, reason = "done", ""
    elif rc != 0:
        status, reason = "failed", f"claude exited with code {rc}: " + (folder / "stderr.txt").read_text()[-400:].strip()
    else:
        status, reason = "failed", "claude ended without a result."
    if status == "done" and not reason and last_synthetic:
        reason = "Claude Code said: " + _line(last_synthetic)
    return {
        "model": model, "status": status, "reason": reason, "tokens": tokens,
        "session_id": result.get("session_id", "") if result is not None else "",
    }, seen_model is not None


def run_hour(kind: str, *, db_path: str | None = None, agent_id: str | None = None,
             person_id: str | None = None, project_scope: str | None = None,
             dry_run: bool = False, config_dir: str | None = None, store_copy: bool = False,
             max_minutes: float | None = None, max_tokens: int | None = None,
             max_seconds: float | None = None, out=sys.stdout) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    resolved = resolve_scope(db_path=db_path, agent_id=agent_id, person_id=person_id,
                             project_scope=project_scope)
    scope = {"agent_id": resolved.agent_id, "person_id": resolved.person_id,
             "project_scope": resolved.project_scope}
    live = Path(resolved.db_path).expanduser()
    hours = hours_dir(resolved.agent_id)
    records_path = hours / "hours.jsonl"
    if max_seconds is None:
        max_seconds = (DEFAULT_MINUTES[kind] if max_minutes is None else max_minutes) * 60
    if max_tokens is None:
        max_tokens = DEFAULT_TOKEN_CEILING[kind]
    if dry_run:
        if not live.exists():
            return {"status": "failed", "reason": f"There is no memory store at {live}."}
        since = since_for(kind, _records(records_path))
        handoffs, captures, questions, latest = _gather(kind, live, since, scope)
        prompt = _prompt(kind, resolved.agent_id, since, handoffs, captures, questions, latest)
        skip = f"No handoffs or captures since {_minute(since)}." if kind == "ours" and not handoffs and not captures else ""
        claude_bin = os.environ.get("MNEMOS_CLAUDE_BIN") or shutil.which("claude")
        mnemos_bin = os.environ.get("MNEMOS_LIFE_MNEMOS_BIN") or shutil.which("mnemos")
        if not claude_bin or not mnemos_bin:
            return {"status": "failed", "reason": "The claude command wasn't found." if not claude_bin else "The mnemos command wasn't found."}
        folder = hours / "dry-run"
        store = folder / "store.db" if store_copy else live
        mcp, settings, cmd = _files_and_command(kind, "dry-run", folder, store, scope, claude_bin, mnemos_bin)
        print(f"Dry run · {kind} hour · nothing will run", file=out)
        print(f"Store: {live} ({'a copy would be used' if store_copy else 'live'})", file=out)
        print(f"Since: {_minute(since)}", file=out)
        if skip:
            print("Our hour would skip: " + skip, file=out)
        print("\n── prompt (stdin) ──\n" + prompt, file=out)
        print("\n── command ──\n" + shlex.join(cmd), file=out)
        print("\n── mcp.json ──\n" + json.dumps(mcp, indent=2), file=out)
        print("\n── settings.json ──\n" + json.dumps(settings, indent=2), file=out)
        return {"dry_run": True, "kind": kind, "skip": skip, "prompt": prompt}

    hour_id = _gen_ulid()
    folder = hours / hour_id
    record = {
        "id": hour_id, "kind": kind, "started_at": started_at, "ended_at": "", "model": "",
        "status": "", "reason": "", "tokens": None, "journal_ids": [], "note_ids": [],
        "store": "copy" if store_copy else "live", "session_id": "", "since": "",
        "waiting": {"handoffs": 0, "captures": 0, "questions": 0}, "discarded_ids": [],
    }
    if store_copy:
        record["live_store_untouched"] = None
    hours.mkdir(parents=True, exist_ok=True)
    launched = False
    store = live
    with (hours / ".lock").open("a") as lock:
        locked = False
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                record.update(status="skipped", reason="Another hour is running.")
                return record
            since = since_for(kind, _records(records_path))
            record["since"] = since
            if not live.exists():
                record.update(status="failed", reason=f"There is no memory store at {live}.")
                return record
            if store_copy:
                folder.mkdir()
                store = folder / "store.db"
                src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
                dst = sqlite3.connect(store)
                try:
                    src.backup(dst)
                    integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    dst.close()
                    src.close()
                if integrity != "ok":
                    record.update(status="failed", reason="The store copy failed its integrity check.")
                    return record
            handoffs, captures, questions, latest = _gather(kind, store, since, scope)
            record["waiting"] = {"handoffs": len(handoffs), "captures": len(captures), "questions": len(questions)}
            if kind == "ours" and not handoffs and not captures:
                record.update(status="skipped", reason=f"No handoffs or captures since {_minute(since)}.")
                return record
            claude_bin = os.environ.get("MNEMOS_CLAUDE_BIN") or shutil.which("claude")
            mnemos_bin = os.environ.get("MNEMOS_LIFE_MNEMOS_BIN") or shutil.which("mnemos")
            if not claude_bin or not mnemos_bin:
                record.update(status="failed", reason="The claude command wasn't found." if not claude_bin else "The mnemos command wasn't found.")
                return record
            prompt = _prompt(kind, resolved.agent_id, since, handoffs, captures, questions, latest)
            folder.mkdir(exist_ok=True)
            mcp, settings, cmd = _files_and_command(kind, hour_id, folder, store, scope, claude_bin, mnemos_bin)
            (folder / "prompt.md").write_text(prompt)
            (folder / "mcp.json").write_text(json.dumps(mcp, indent=2))
            (folder / "settings.json").write_text(json.dumps(settings, indent=2))
            studio = studio_dir(resolved.agent_id)
            (studio / "worktrees").mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env["MNEMOS_HOUR_ID"] = hour_id
            if config_dir is not None:
                env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
            outcome, guarded = _watch(cmd, studio, env, folder, prompt, max_seconds, max_tokens)
            launched = True
            record.update(outcome)
            if guarded:
                writer = EngramStore(store)
                try:
                    record["discarded_ids"] = writer.discard_hour_rows(hour_id, **scope)
                finally:
                    writer.close()
            else:
                reader = ReadOnlyEngramStore(store)
                try:
                    record.update(reader.hour_rows(hour_id, **scope))
                finally:
                    reader.close()
            return record
        except Exception as exc:
            record.update(status="failed", reason=(
                f"The runner hit an error: {type(exc).__name__}: {_line(str(exc))}"
            ))
            return record
        finally:
            if not record["since"]:
                record["since"] = since_for(kind, _records(records_path))
            if store_copy:
                if launched:
                    try:
                        reader = ReadOnlyEngramStore(live)
                        try:
                            live_rows = reader.hour_rows(hour_id, **scope)
                        finally:
                            reader.close()
                        record["live_store_untouched"] = not live_rows["journal_ids"] and not live_rows["note_ids"]
                    except Exception:
                        pass
                if not launched and store != live:
                    store.unlink(missing_ok=True)
            record["ended_at"] = datetime.now(timezone.utc).isoformat()
            with records_path.open("a") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
