"""One briefing, written for the one reading it.

The session-start packet is where this memory does its work: an agent reads it
at every start, and reaches for recall far less often. Two builders made it,
one for the SessionStart hook and one for ``mnemos_context``, and the packet
repeated each belief three times, showed sections that were always empty,
ranked notes against a fixed sentence so recency decided, and had lost its
"While you were away" report without anyone noticing.

One builder makes it now, for both. These tests hold it to what it promises,
driven the way an agent meets it: the real hook in another process, reading
the payload Claude Code sends, and the runtime reading its session from the
environment. They compare against literal text, so on the code before this
change they fail on behaviour rather than on a missing name.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mnemos.core.belief import Belief
from mnemos.core.engram import Engram
from mnemos.core.identity import AgentIdentity, IdentityProfile, MemoryProfile
from mnemos.dream_journal import fetch_active_dream_entry, write_dream_entry
from mnemos.simple_runtime import MnemosRuntime
from mnemos.simple_scope import MnemosScope
from mnemos.store.sqlite_store import EngramStore

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]

OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5"
OWN = "11111111-aaaa-4aaa-8aaa-111111111111"      # the session reading the packet
SIBLING = "22222222-bbbb-4bbb-8bbb-222222222222"  # the same model, working in parallel
FABLE_SESSION = "33333333-cccc-4ccc-8ccc-333333333333"
OLD_SESSION = "44444444-dddd-4ddd-8ddd-444444444444"

OWN_NOTE = (
    "Where I stopped: the changelog draft for the ferry app is half written.\n"
    "Next: ask Riley whether the tone is right."
)
SIBLING_NOTE = (
    "Release check for the ferry app. The staging deploy passed on the second try "
    "after the cache fix. Production waits for Riley's go-ahead in the morning. I left "
    "the rollback script in scripts/rollback.sh and tested it twice against staging. "
    "Nothing else is pending on this thread. The next session can take up the release "
    "notes once Riley says yes."
)
FABLE_NOTE = (
    "Fable here: the timetable scraper now runs nightly at 02:00 and writes to "
    "data/timetable.json."
)
OLD_NOTE = "An old thread about the logo colours."

FOUNDATIONAL = [
    # content, model, authored_by, created, confidence, salience
    ("Riley prefers plain words and short replies.", OPUS, "agent", "2026-08-20", 0.9, 0.9),
    (
        "Riley works best after midnight and wants to see the result running before "
        "he reads any code.",
        FABLE, "agent", "2026-07-14", 0.9, 0.9,
    ),
    ("Riley's studio is in Lisbon.", "", "unknown", "2026-06-01", 0.8, 0.8),
    ("Riley once said he likes green tea.", OPUS, "agent", "2026-05-01", 0.6, 0.5),
]
EPISODE = (
    "2026-09-18: Riley and I fixed the ferry timetable parser in ferry/parse.py; "
    "the three failing tests pass now."
)
SETTINGS_NOTE = (
    "The ferry app reads its schedule from one settings file, and every screen takes "
    "its times from there. Changing a time in two places caused last month's "
    "confusion, so everything goes through that one file now. The settings file is "
    "also where the holiday timetable lives, which the app switches to on public "
    "holidays. A second file for tests mirrors it, and the two must be kept in step "
    "whenever a route changes. Routes change about twice a year, usually in spring."
)
GRINDER = "Riley bought a new espresso grinder on 2026-09-25."
LESSON = "Check the live ferry page before calling a fix done."
BELIEFS = [
    ("Plain words carry further than clever ones.", 0.4),
    ("What's checked against the real thing outranks what's reasoned out.", 0.35),
]
QUESTION_ON = "Riley plans every trip around the ferry timetable."
QUESTION = (
    'You keep returning to "ferry" (5 memories). Is that a belief you now hold? '
    "[theme:ferry]"
)
REPORT = "Mnemos connected 4 memories that belong together."
NO_CHANGE = "Mnemos checked the stored continuity; no mechanical changes were needed."


# ── Helpers ──


def _ago(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _on(day: str) -> str:
    return f"{day}T10:00:00+00:00"


def _write(db: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _dated(db: Path, entry_id: str, when: str) -> None:
    _write(
        db,
        "UPDATE hypomnema_entries SET created_at = ?, last_revised_at = ? WHERE id = ?",
        (when, when, entry_id),
    )


def _note(
    store: EngramStore,
    content: str,
    *,
    model: str = OPUS,
    authored_by: str = "agent",
    foundational: bool = False,
    confidence: float = 0.6,
    salience: float = 0.5,
) -> str:
    return store.write_hypomnema_entry(
        content,
        **SCOPE,
        authored_by=authored_by,
        author_id="nova",
        author_model=model,
        domain="foundational" if foundational else "topical",
        foundational=foundational,
        confidence=confidence,
        salience=salience,
    )


def _engram(store: EngramStore, content: str, *, lesson: bool = False, day: str = "") -> str:
    engram = Engram(
        content=content,
        content_at_encoding=content,
        impact=content if lesson else "",
        kind="procedural" if lesson else "episodic",
        tags=["lesson", "distilled"] if lesson else ["continuity"],
        owner_agent_id=SCOPE["agent_id"],
        person_id=SCOPE["person_id"],
        project_scope=SCOPE["project_scope"],
    )
    if day:
        engram.created_at = _on(day)
    store.save_engram(engram)
    return engram.id


def _briefing_store(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A scope with something in every section of the packet."""
    db = tmp_path / "memory.db"
    ids: dict[str, str] = {}
    store = EngramStore(db)
    try:
        for key, text, model, session in (
            ("own", OWN_NOTE, OPUS, OWN),
            ("sibling", SIBLING_NOTE, OPUS, SIBLING),
            ("fable", FABLE_NOTE, FABLE, FABLE_SESSION),
            ("old", OLD_NOTE, OPUS, OLD_SESSION),
        ):
            ids[key] = store.write_handoff(
                text, **SCOPE, author_id="nova", author_model=model, author_session=session,
            )
        for index, (content, model, by, day, confidence, salience) in enumerate(FOUNDATIONAL):
            ids[f"f{index}"] = _note(
                store, content, model=model, authored_by=by, foundational=True,
                confidence=confidence, salience=salience,
            )
        ids["episode"] = _note(store, EPISODE)
        ids["settings"] = _note(store, SETTINGS_NOTE)
        ids["grinder"] = _note(store, GRINDER)
        ids["lesson"] = _engram(store, LESSON, lesson=True, day="2026-09-21")
        for content, confidence in BELIEFS:
            store.save_belief(Belief(
                agent_id="nova", content=content, confidence=confidence,
                domain="craft", source="agent",
            ))
        ids["question_on"] = _engram(store, QUESTION_ON)
        store.enqueue_reflection("belief", ids["question_on"], QUESTION, **SCOPE)
        ids["report"] = write_dream_entry(
            store, MnemosScope(db_path=str(db), **SCOPE), REPORT,
        )
    finally:
        store.close()

    for key, delta in (
        ("own", {"hours": 2, "minutes": 30}),
        ("sibling", {"hours": 5, "minutes": 30}),
        ("fable", {"hours": 26, "minutes": 30}),
        ("old", {"days": 4}),
        ("report", {"hours": 2, "minutes": 30}),
    ):
        _dated(db, ids[key], _ago(**delta))
    for index, (*_, day, _confidence, _salience) in enumerate(FOUNDATIONAL):
        _dated(db, ids[f"f{index}"], _on(day))
    for key, day in (("episode", "2026-09-18"), ("settings", "2026-09-20"), ("grinder", "2026-09-25")):
        _dated(db, ids[key], _on(day))
    return db, ids


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True, exist_ok=True)
    return home


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / "work" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _hook(db: Path, tmp_path: Path, *, session: str = OWN, model: str = OPUS,
          cwd: Path | None = None, extra: tuple[str, ...] = ()) -> str:
    """The real SessionStart hook in another process, with Claude Code's payload."""
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": session}
    if model:
        payload["model"] = model
    if cwd is not None:
        payload["cwd"] = str(cwd)
    done = subprocess.run(
        [sys.executable, "-m", "mnemos.cli", "hook", "session-start",
         "--db-path", str(db), *SCOPE_ARGS, *extra],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "HOME": str(_home(tmp_path)),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def _runtime(db: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("### ")]


def _section(text: str, heading: str) -> str:
    assert f"### {heading}\n" in text, f"the packet has no {heading!r} section"
    return text.split(f"### {heading}\n", 1)[1].split("\n\n### ", 1)[0]


def _ids_hidden(text: str) -> str:
    text = re.sub(r'mnemos_recall\("[^"]+"\)', 'mnemos_recall("<id>")', text)
    return re.sub(r'target_id="[^"]+"', 'target_id="<id>"', text)


def _count(db: Path, table: str) -> int:
    return _read(db, f"SELECT COUNT(*) FROM {table}")[0][0]


# ── The whole briefing, as the reader gets it ──

GOLDEN = """\
## Mnemos Context Packet

### Where you left off
Yours (Opus 5.5), from this session, 2 hours ago:
Where I stopped: the changelog draft for the ferry app is half written.
Next: ask Riley whether the tone is right.

Other notes from the last three days:
- Yours (Opus 5.5), from another session, 5 hours ago: Release check for the ferry app. \
The staging deploy passed on the second try after the cache fix. Production waits for \
Riley's go-ahead in the morning. I left the rollback script in scripts/rollback.sh and \
tested it twice against staging. Nothing else is pending on this thread. […] \
Whole note: mnemos_recall("<id>")
- From Fable 5, a colleague, 26 hours ago: Fable here: the timetable scraper now runs \
nightly at 02:00 and writes to data/timetable.json.
A colleague's note is theirs: take what's useful and don't claim its work as yours.

### Who you're with
- 2026-08-20, by Opus 5.5: Riley prefers plain words and short replies.
- 2026-07-14, by Fable 5: Riley works best after midnight and wants to see the result \
running before he reads any code.
- 2026-06-01, unsigned: Riley's studio is in Lisbon.

### What you're carrying
- 2026-09-21, lesson: Check the live ferry page before calling a fix done.
- 2026-09-20, by Opus 5.5: The ferry app reads its schedule from one settings file, and \
every screen takes its times from there. Changing a time in two places caused last \
month's confusion, so everything goes through that one file now. The settings file is \
also where the holiday timetable lives, which the app switches to on public holidays. A \
second file for tests mirrors it, and the two must be kept in step whenever a route \
changes. […] Whole note: mnemos_recall("<id>")
- 2026-09-18, by Opus 5.5: Riley and I fixed the ferry timetable parser in \
ferry/parse.py; the three failing tests pass now.

### Beliefs
- Plain words carry further than clever ones. (40%)
- What's checked against the real thing outranks what's reasoned out. (35%)

### One question
About: "Riley plans every trip around the ferry timetable."
You keep returning to "ferry" (5 memories). Is that a belief you now hold?
mnemos_reflect(target_id="<id>", text="…", verdict="…")
verdict: hold, decline or not_now
Answer in your own words if one comes. If nothing true does, leave it; it fades on its own.

### While you were away
Mnemos connected 4 memories that belong together.
(Mnemos's upkeep wrote this 2 hours ago; these aren't your words.)"""


def test_the_packet_reads_exactly_as_the_golden_briefing(tmp_path):
    db, _ = _briefing_store(tmp_path)
    packet = _hook(db, tmp_path, cwd=_folder(tmp_path, "ferry-app"))
    assert _ids_hidden(packet) == GOLDEN
    assert len(packet) < 6000


def test_the_hook_and_mnemos_context_give_the_same_packet(tmp_path, monkeypatch):
    db, _ = _briefing_store(tmp_path)
    folder = _folder(tmp_path, "ferry-app")
    from_hook = _hook(db, tmp_path, cwd=folder)

    # The MCP server of the same session: Claude Code names the session in
    # its environment and starts the server in the session's folder.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)
    monkeypatch.chdir(folder)
    runtime = _runtime(db)
    try:
        from_tool = runtime.context()
    finally:
        runtime.close()

    assert from_tool == from_hook
    assert "### Where you left off" in from_hook, "premise: a full packet"


def test_mnemos_context_appends_what_matches_its_query_after_the_packet(tmp_path, monkeypatch):
    db, ids = _briefing_store(tmp_path)
    monkeypatch.chdir(_folder(tmp_path, "ferry-app"))
    runtime = _runtime(db)
    try:
        shared = runtime.context()
        asked = runtime.context("espresso grinder")
    finally:
        runtime.close()

    assert '\n\n### For "espresso grinder"\n' in asked, "no results were appended"
    assert GRINDER not in shared, "premise: the packet doesn't carry it"
    assert asked.startswith(shared.rsplit("\n### One question", 1)[0])
    results = asked.split('### For "espresso grinder"\n', 1)[1]
    assert GRINDER in results
    # What the packet already shows is not repeated among the results.
    assert all(text not in results for text in (LESSON, FOUNDATIONAL[0][0]))


# ── Beliefs once, and nothing announced empty ──


def test_each_belief_appears_once_with_its_confidence(tmp_path, monkeypatch):
    db, _ = _briefing_store(tmp_path)
    # What maintenance's identity pass leaves: each belief again as a core
    # belief and as a living question, beside frozen concerns and a count.
    store = EngramStore(db)
    try:
        identity = AgentIdentity(memory_profile=MemoryProfile(agent_id="nova", name="nova"))
        identity.epoch_state.self_summary = IdentityProfile(
            persistent_concerns=[("ferry", 9)],
            core_beliefs=list(BELIEFS),
            living_questions=[
                f"Uncertain: {content} ({int(confidence * 100)}%)" for content, confidence in BELIEFS
            ],
            lessons_accumulated=3,
        ).to_summary()
        store.save_identity(identity)
    finally:
        store.close()

    from_hook = _hook(db, tmp_path)
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)
    runtime = _runtime(db)
    try:
        from_tool = runtime.context()
    finally:
        runtime.close()

    for packet in (from_hook, from_tool):
        for content, confidence in BELIEFS:
            assert packet.count(content) == 1, f"{content!r} shown {packet.count(content)} times"
            assert f"- {content} ({round(confidence * 100)}%)" in packet
        assert "Persistent concerns" not in packet
        assert "Accumulated 3 lessons" not in packet


def test_a_section_with_nothing_in_it_is_left_out(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    EngramStore(db).close()
    assert _hook(db, tmp_path) == "", "a memory with nothing in it still printed a packet"

    store = EngramStore(db)
    try:
        store.write_handoff(OWN_NOTE, **SCOPE, author_model=OPUS, author_session=OWN)
    finally:
        store.close()
    from_hook = _hook(db, tmp_path)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)
    runtime = _runtime(db)
    try:
        from_tool = runtime.context()
    finally:
        runtime.close()

    for packet in (from_hook, from_tool):
        assert _headings(packet) == ["### Where you left off"], packet
        assert "Scope" not in packet and "How To Use" not in packet


# ── Where you left off ──


def test_the_readers_own_handoff_comes_first_and_whole(tmp_path):
    db, _ = _briefing_store(tmp_path)

    as_opus = _section(_hook(db, tmp_path), "Where you left off")
    assert as_opus.startswith(f"Yours (Opus 5.5), from this session, 2 hours ago:\n{OWN_NOTE}\n")
    # The same model in a parallel session is the reader too; another model
    # is a colleague, and only its note carries the line about claiming.
    assert as_opus.index("- Yours (Opus 5.5), from another session") < as_opus.index(
        "- From Fable 5, a colleague"
    )
    assert OLD_NOTE not in as_opus, "a four-day-old note of another session was shown"

    as_fable = _section(
        _hook(db, tmp_path, session=FABLE_SESSION, model=FABLE), "Where you left off",
    )
    assert as_fable.startswith(f"Yours (Fable 5), from this session, 26 hours ago:\n{FABLE_NOTE}\n")
    assert "- From Opus 5.5, a colleague, 2 hours ago" in as_fable
    assert as_fable.endswith("don't claim its work as yours.")


def test_a_cut_note_is_recalled_whole_by_its_id(tmp_path, monkeypatch):
    db, ids = _briefing_store(tmp_path)
    packet = _hook(db, tmp_path, cwd=_folder(tmp_path, "ferry-app"))

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)
    runtime = _runtime(db)
    try:
        note = runtime.recall(ids["settings"])
        handoff = runtime.recall(ids["sibling"])
        lesson = runtime.recall(ids["lesson"])
        runtime.correct("", target_id=ids["settings"], action="forget")
        forgotten = runtime.recall(ids["settings"])
    finally:
        runtime.close()

    assert SETTINGS_NOTE in note, f"the note's id did not bring it back whole:\n{note}"
    assert handoff.startswith(f"Yours (Opus 5.5), from another session, 5 hours ago:\n{SIBLING_NOTE}")
    assert LESSON in lesson
    assert SETTINGS_NOTE not in forgotten, "a forgotten note came back by its id"

    # Those are the ids the packet gives for the notes it cut.
    def id_in(section: str, words: str) -> str:
        [line] = [line for line in _section(packet, section).splitlines() if words in line]
        return re.search(r'Whole note: mnemos_recall\("([^"]+)"\)$', line).group(1)

    assert SETTINGS_NOTE not in packet and SIBLING_NOTE not in packet, "premise: both are cut"
    assert id_in("What you're carrying", "one settings file") == ids["settings"]
    assert id_in("Where you left off", "Release check") == ids["sibling"]


# ── What you're carrying ──


def test_what_you_are_carrying_follows_the_folder_and_its_repository(tmp_path):
    db = tmp_path / "memory.db"
    store = EngramStore(db)
    try:
        harbour = _note(store, "Riley keeps the harbour ledger in ledger/2026.csv.")
        newer = [_note(store, f"Riley asked for garden change {n} on the planner.") for n in range(3)]
    finally:
        store.close()
    _dated(db, harbour, _on("2026-08-01"))
    for n, note_id in enumerate(newer):
        _dated(db, note_id, _on(f"2026-09-0{n + 1}"))

    def carrying(folder: Path) -> str:
        return _section(_hook(db, tmp_path, cwd=folder), "What you're carrying")

    # Nowhere in particular: the three newest.
    assert "harbour ledger" not in carrying(_folder(tmp_path, "scratch"))
    # In a folder named for it, it comes first.
    assert carrying(_folder(tmp_path, "harbour-ledger")).startswith(
        "- 2026-08-01, by Opus 5.5: Riley keeps the harbour ledger"
    )
    # A worktree and a folder deep in a checkout are named for their repository.
    repository = tmp_path / "repos" / "harbour"
    (repository / ".git" / "worktrees" / "wt-7").mkdir(parents=True)
    worktree = tmp_path / "trees" / "wt-7"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repository}/.git/worktrees/wt-7\n")
    deep = repository / "src" / "deep"
    deep.mkdir(parents=True)
    for folder in (worktree, deep):
        assert "harbour ledger" in carrying(folder).splitlines()[0], folder


def test_one_concrete_episode_is_always_carried(tmp_path):
    db = tmp_path / "memory.db"
    store = EngramStore(db)
    try:
        for day, text in (
            ("2026-09-22", "Harbour work goes better in small steps."),
            ("2026-09-21", "Ask before changing the harbour schedule."),
            ("2026-09-20", "Keep harbour names plain."),
        ):
            _engram(store, text, lesson=True, day=day)
        summary = _note(store, "the harbour work is mostly about keeping the data tidy.")
        episode = _note(store, "2026-08-02: Riley and I moved the harbour ledger to ledger/2026.csv.")
    finally:
        store.close()
    _dated(db, summary, _on("2026-09-15"))
    _dated(db, episode, _on("2026-08-02"))

    lines = _section(
        _hook(db, tmp_path, cwd=_folder(tmp_path, "harbour")), "What you're carrying",
    ).splitlines()
    # Ranked, the three lessons come first. One place goes to what happened.
    assert lines == [
        "- 2026-09-22, lesson: Harbour work goes better in small steps.",
        "- 2026-09-21, lesson: Ask before changing the harbour schedule.",
        "- 2026-08-02, by Opus 5.5: Riley and I moved the harbour ledger to ledger/2026.csv.",
    ]


# ── The budget ──


def test_the_packet_stays_under_six_thousand_characters(tmp_path):
    def sentences(count: int) -> str:
        return " ".join(
            f"Riley and I went through the harbour ledger line by line on 2026-09-{day:02d}."
            for day in range(1, count + 1)
        )

    db, _ = _briefing_store(tmp_path)
    handoff = f"Where I stopped, at length. {sentences(40)}"
    store = EngramStore(db)
    try:
        store.write_handoff(handoff, **SCOPE, author_model=OPUS, author_session=OWN)
        for index in range(5):
            _note(store, f"Foundation {index}. {sentences(20)}", foundational=True,
                  confidence=0.95, salience=0.95)
        for index in range(10):
            _note(store, f"Ferry note {index}. {sentences(20)}")
    finally:
        store.close()

    packet = _hook(db, tmp_path, cwd=_folder(tmp_path, "ferry-app"))
    assert len(packet) < 6000, len(packet)
    assert len(handoff) > 2500 and handoff in packet, "the reader's handoff was cut"
    cut = [line for line in packet.splitlines() if " Whole note: mnemos_recall(" in line]
    assert len(cut) >= 6, "premise: notes were cut to fit"
    for line in cut:
        kept = line.split(" […] Whole note: ", 1)[0]
        assert kept.endswith("."), f"cut mid-sentence: …{kept[-50:]}"


# ── While you were away ──


def test_the_latest_report_is_found_however_many_notes_outrank_it(tmp_path):
    db = tmp_path / "memory.db"
    scope = MnemosScope(db_path=str(db), **SCOPE)
    store = EngramStore(db)
    try:
        for index in range(60):
            _note(store, f"Durable note {index} about the harbour.", confidence=0.9, salience=0.9)
        first = write_dream_entry(store, scope, "Mnemos connected 2 memories that belong together.")
        found = fetch_active_dream_entry(store, scope)
        second = write_dream_entry(store, scope, REPORT)
        active = store.get_hypomnema_entries_by_tag("dream-journal", **SCOPE, limit=10)
    finally:
        store.close()

    assert found is not None and found["id"] == first, "the report was not found"
    assert [entry["id"] for entry in active] == [second], "a new report piled up beside the last"
    assert f"### While you were away\n{REPORT}\n" in _hook(db, tmp_path)


def test_a_report_that_changed_nothing_is_not_shown(tmp_path):
    db, _ = _briefing_store(tmp_path)
    store = EngramStore(db)
    try:
        write_dream_entry(store, MnemosScope(db_path=str(db), **SCOPE), NO_CHANGE)
    finally:
        store.close()

    packet = _hook(db, tmp_path)
    assert NO_CHANGE not in packet
    assert "### While you were away" not in packet
    assert REPORT not in packet, "a replaced report was shown"


# ── Older code, and upkeep ──


def test_older_code_shows_no_question_and_spends_no_showing(tmp_path):
    db, _ = _briefing_store(tmp_path)
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")
    asks = _read(db, "SELECT id, surfaced_count, answered_at FROM reflection_queue ORDER BY id")

    packet = _hook(db, tmp_path)
    assert OWN_NOTE in packet, "premise: older code still gets the packet"
    assert "### One question" not in packet
    assert 'You keep returning to "ferry"' not in packet
    assert _read(db, "SELECT id, surfaced_count, answered_at FROM reflection_queue ORDER BY id") == asks


def test_building_the_packet_runs_no_maintenance(tmp_path, monkeypatch):
    home = _home(tmp_path)
    # Upkeep that rides on use waits five idle minutes by default; without the
    # wait, code that maintains while building the packet does so every time.
    (home / ".mnemos" / "config.json").write_text(
        json.dumps({"consolidation": {"min_idle_minutes": 0}})
    )
    monkeypatch.setenv("HOME", str(home))
    db = tmp_path / "memory.db"
    runtime = _runtime(db)
    try:
        runtime.capture("Riley keeps the harbour ledger in ledger/2026.csv.")
        cycles = _count(db, "consolidation_log")
        runtime.context()
        runtime.context("harbour ledger")
    finally:
        runtime.close()
    assert _count(db, "consolidation_log") == cycles, "mnemos_context ran a maintenance cycle"

    _hook(db, tmp_path)
    assert _count(db, "consolidation_log") == cycles, "the hook ran a maintenance cycle"


def test_a_session_start_raises_the_stores_code_version(tmp_path):
    """The hook is the first thing a new session runs, and the store learns
    there that newer code has arrived: an older server still running stops
    maintaining it from then on, not only once the new session makes its
    first tool call."""
    from mnemos.code_version import MAINTENANCE_CODE_VERSION

    minimum = "SELECT value FROM meta WHERE key = 'min_code_version'"
    for left_by in ("2", None):  # older code's mark, or a store from before marks
        db = tmp_path / f"memory-{left_by}.db"
        store = EngramStore(db)
        try:
            store.write_handoff(OWN_NOTE, **SCOPE, author_model=OPUS, author_session=OWN)
        finally:
            store.close()
        if left_by is None:
            _write(db, "DELETE FROM meta WHERE key = 'min_code_version'")
        else:
            _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
                   (left_by,))
        assert _read(db, minimum) == ([(left_by,)] if left_by else []), "premise"

        packet = _hook(db, tmp_path)
        assert OWN_NOTE in packet
        assert _read(db, minimum) == [(str(MAINTENANCE_CODE_VERSION),)], (
            f"the hook left the store's minimum at {left_by!r}"
        )

    # Only ever raised: a store newer code has opened keeps its mark.
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")
    _hook(db, tmp_path)
    assert _read(db, minimum) == [("999",)]


# ── A use, what was shown, and one budget (review of #84) ──

TRACE = (
    "SELECT access_count, last_accessed, reconsolidation_count, strength, stability, "
    "accessibility FROM engrams WHERE id = ?"
)
ALL_TRACES = (
    "SELECT id, access_count, reconsolidation_count, strength, stability, accessibility "
    "FROM engrams ORDER BY id"
)


def _accessed(db: Path, engram_id: str) -> int:
    return _read(db, "SELECT access_count FROM engrams WHERE id = ?", (engram_id,))[0][0]


def _recall_in(monkeypatch, db: Path, session: str, what: str) -> str:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    runtime = _runtime(db)
    try:
        return runtime.recall(what)
    finally:
        runtime.close()


def test_recalling_a_memory_by_its_id_is_a_use_once_a_session(tmp_path, monkeypatch):
    db, ids = _briefing_store(tmp_path)
    lesson = ids["lesson"]
    accessed = _accessed(db, lesson)

    for _ in range(2):
        assert LESSON in _recall_in(monkeypatch, db, OWN, lesson)
    assert _accessed(db, lesson) == accessed + 1, "recalling it by its id was not a use"
    assert LESSON in _recall_in(monkeypatch, db, SIBLING, lesson)
    assert _accessed(db, lesson) == accessed + 2, "another session's use was not counted"

    # A note has no reinforcement: reading one whole by its id changes no memory.
    before = _read(db, ALL_TRACES)
    assert SETTINGS_NOTE in _recall_in(monkeypatch, db, FABLE_SESSION, ids["settings"])
    assert _read(db, ALL_TRACES) == before

    # Code older than the store reads the memory and changes nothing.
    _write(db, "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")
    assert LESSON in _recall_in(monkeypatch, db, OLD_SESSION, lesson)
    assert _read(db, ALL_TRACES) == before


def test_the_packet_showing_a_memory_is_not_a_use(tmp_path, monkeypatch):
    db, ids = _briefing_store(tmp_path)
    folder = _folder(tmp_path, "ferry-app")
    lesson = ids["lesson"]
    before = _read(db, TRACE, (lesson,))
    links = (
        "SELECT COUNT(*) FROM connections WHERE relation = 'co_activated' "
        "AND (source_id = ? OR target_id = ?)"
    )
    assert _read(db, links, (lesson, lesson)) == [(0,)]

    packets = [
        _hook(db, tmp_path, cwd=folder),
        _hook(db, tmp_path, cwd=folder, extra=("--include-graph", "--query", "live ferry page")),
    ]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.chdir(folder)
    runtime = _runtime(db)
    try:
        packets.append(runtime.context())
        packets.append(runtime.context("live ferry page"))
    finally:
        runtime.close()

    for packet in packets:
        assert f"- 2026-09-21, lesson: {LESSON}" in _section(packet, "What you're carrying")
    assert _read(db, TRACE, (lesson,)) == before, "the packet's own pick was reinforced"
    assert _read(db, links, (lesson, lesson)) == [(0,)]


def test_what_the_packet_carries_is_not_repeated_after_it(tmp_path, monkeypatch):
    db, _ = _briefing_store(tmp_path)
    folder = _folder(tmp_path, "ferry-app")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.chdir(folder)
    runtime = _runtime(db)
    try:
        asked = runtime.context("live ferry page")
    finally:
        runtime.close()
    graph = _hook(db, tmp_path, cwd=folder, extra=("--include-graph", "--query", "live ferry page"))

    for packet, heading in (
        (asked, '### For "live ferry page"'), (graph, "### Mnemos Graph"),
    ):
        assert heading in packet, "premise: something else was found"
        carried, after = packet.split(heading, 1)
        assert LESSON in carried, "premise: the lesson is carried"
        assert LESSON not in after, f"the carried lesson was repeated under {heading!r}"
        assert QUESTION_ON in after, "premise: the rest of what the query finds is there"


LIGHTHOUSE = "the lighthouse lamp wants a new wick before winter."


def test_a_note_left_out_for_room_still_comes_back_for_a_query(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    store = EngramStore(db)
    try:
        # The session's own handoff is never cut. This one leaves room for the
        # briefing's first section only: the note it carries and its question
        # are left out, and a few hundred characters remain.
        handoff = store.write_handoff(
            ("Where I stopped: the harbour ledger, checked page by page. " * 100)[:5601],
            **SCOPE, author_model=OPUS, author_session=OWN,
        )
        _note(store, LIGHTHOUSE)
        asked_about = _engram(
            store,
            "Riley and I spent the evening sorting the tide tables by season, then by "
            "port, then by the hour the tide turns, so that every table reads the same "
            "way from the first page to the last page of the book.",
        )
        store.enqueue_reflection(
            "impact", asked_about,
            "What did this change in how you understand things? One sentence.", **SCOPE,
        )
    finally:
        store.close()
    _dated(db, handoff, _ago(hours=2, minutes=30))

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OWN)
    monkeypatch.setenv("MNEMOS_AGENT_MODEL", OPUS)
    runtime = _runtime(db)
    try:
        asked = runtime.context("lighthouse wick")
    finally:
        runtime.close()

    packet, heading, results = asked.partition('### For "lighthouse wick"\n')
    assert heading, f"no results were appended:\n{asked[-300:]}"
    assert LIGHTHOUSE not in packet and "### One question" not in packet, (
        "premise: the budget left the note and the question out"
    )
    assert LIGHTHOUSE in results, "a note the packet didn't show was treated as shown"
    assert len(asked) < 6000


def test_graph_recall_after_the_packet_stays_within_the_budget(tmp_path):
    db, _ = _briefing_store(tmp_path)
    store = EngramStore(db)
    try:
        store.write_handoff(
            ("Where I stopped: the harbour ledger, checked page by page. " * 50)[:2700],
            **SCOPE, author_model=OPUS, author_session=OWN,
        )
        found = {
            _engram(
                store,
                f"Harbour ledger entry {n}: Riley and I matched the berth fees against the "
                "receipts for the whole summer, one line at a time, and marked every line "
                "that disagreed with the ledger so the harbour office can look again.",
            ): n
            for n in range(6)
        }
    finally:
        store.close()
    before = {engram_id: _accessed(db, engram_id) for engram_id in found}

    for budget, extra in ((6000, ()), (4000, ("--token-budget", "1000"))):
        packet = _hook(db, tmp_path, extra=("--include-graph", "--query", "harbour ledger", *extra))
        assert len(packet) < budget, f"{len(packet)} characters against a budget of {budget}"
        if budget == 6000:
            graph = packet.split("### Mnemos Graph\n", 1)[1].splitlines()
            assert 0 < len(graph) < len(found), "premise: some entries fit and some don't"
            assert all(line.endswith("%]") for line in graph), "an entry was cut"
            shown = {
                engram_id for engram_id, n in found.items()
                if f"Harbour ledger entry {n}:" in packet
            }
            # Only what the reader was shown is a use.
            assert {
                engram_id for engram_id in found if _accessed(db, engram_id) > before[engram_id]
            } == shown
