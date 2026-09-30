"""No engine voice (WP-R21).

Words in memory come only from the agent. Three paths still wrote the
engine's own words there, or reached the agent's words through the wrong
note:

1. The deep cycle's reflection pass wrote "thoughts" as memories: Mnemos's
   template ("Recurring theme: continuity (appeared in 46 recent memories)",
   author_kind system), or a configured model's lines. It writes none now,
   and sends the agent's memories to no model.
2. Promotion wrote "Stable continuity promoted during simple maintenance."
   where a memory's meaning goes, and made a note Mnemos wrote into a memory
   Mnemos wrote. The meaning stays as the note left it (a note has none, so
   empty), the memory asks for one later, and Mnemos's notes are not
   promoted.
3. The impact pass reached memories through notes that only name them
   (``related_engram_id``). It matches on the pair
   (``graduated_to_engram_id``), the note the answer is written into.
4. On a store full of candidates, a shallow cycle with its promotions and a
   deep cycle leave no memory Mnemos wrote and no meaning from
   ``TEMPLATED_IMPACTS``.
5. Code from before this change stands down once this code opens the store.

No test reaches a real network, a real model or a real ~/.mnemos.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import mnemos.store.embedding_index as ei
from mnemos.cli import main
from mnemos.code_version import MAINTENANCE_CODE_VERSION
from mnemos.core.emotional_state import EmotionalState
from mnemos.core.identity import AgentIdentity
from mnemos.core.placeholders import TEMPLATED_IMPACTS
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
SCOPE_ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
MODEL = "claude-opus-5-5"

# Four things the agent captured today, sharing a place, so a pass that looks
# for recurring themes has one to find.
HARBOUR = (
    "The harbour ferry leaves at noon on weekdays and at ten on Sundays.",
    "The pier timetable is posted by the harbour office every Monday morning.",
    "The ferry crew counts passengers at the gangway before casting off.",
    "Storm warnings close the pier until the harbour master reopens it.",
)


def _runtime(db: Path) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _memory_id(said: str) -> str:
    return re.search(r"Memory ID: (engram_[A-Za-z0-9]+)", said).group(1)


def _note_id(said: str) -> str:
    return re.search(r"Continuity note ID: (\S+)", said).group(1)


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _ids(db: Path) -> set[str]:
    """Every memory in the store, in any state."""
    return {row[0] for row in _read(db, "SELECT id FROM engrams")}


def _written(db: Path, ids: set[str]) -> list[tuple]:
    """What the memories named say: author, words, meaning and its source."""
    marks = ",".join("?" * len(ids))
    return _read(db, f"SELECT author_kind, content, impact, impact_source FROM engrams "
                     f"WHERE id IN ({marks}) ORDER BY content", tuple(sorted(ids)))


def _mnemos_wrote(db: Path) -> list[tuple]:
    """Every memory Mnemos wrote itself."""
    return _read(db, "SELECT id, content FROM engrams WHERE author_kind = 'system'")


def _placeholder_meanings(db: Path) -> list[tuple]:
    """Every memory whose meaning is one of Mnemos's own phrases."""
    return [
        row for row in _read(db, "SELECT id, impact, impact_source FROM engrams")
        if row[2] == "template" or (row[1] or "").strip() in TEMPLATED_IMPACTS
    ]


def _asked(db: Path, memory: str) -> list[tuple[str, bool]]:
    """Each question asked about ``memory``: its kind, and whether it ended."""
    return [(kind, bool(answered)) for kind, answered in _read(db, """
        SELECT kind, answered_at FROM reflection_queue
        WHERE target_id = ? AND agent_id = ? AND person_id = ? AND project_scope = ?
        ORDER BY created_at
    """, (memory, *SCOPE.values()))]


def _note(db: Path, note_id: str) -> str:
    return _read(db, "SELECT content FROM hypomnema_entries WHERE id = ?", (note_id,))[0][0]


def _pair_of(db: Path, note_id: str) -> str | None:
    return _read(db, "SELECT graduated_to_engram_id FROM hypomnema_entries WHERE id = ?",
                 (note_id,))[0][0]


def _captured(db: Path, *texts: str) -> list[str]:
    rt = _runtime(db)
    try:
        return [_memory_id(rt.capture(text)) for text in texts]
    finally:
        rt.close()


def _stable_note(store, content: str, **fields) -> str:
    """A continuity note stable enough to promote, written as the advanced
    tools write one: no memory of its own, and none it names."""
    fields.setdefault("foundational", True)
    return store.write_hypomnema_entry(
        content, **SCOPE, confidence=fields.pop("confidence", 0.9),
        salience=fields.pop("salience", 0.8), **fields,
    )


def _closed_session_summary(store, n: int, confidence: float = 0.9) -> str:
    """A closed session's summary, as the store writes one when a session
    closes with no synthesis: Mnemos's words around the session's memories
    (``authored_by`` 'system'). Revised once, it is stable enough to promote
    by every measure but who wrote it."""
    session = f"harbour-watch-{n}"
    store.start_memory_session(session_id=session, **SCOPE, title=f"Harbour watch {n}")
    store.write_functional_memory(
        f"Watch {n}: the pier gate sticks when the frost comes in.", session_id=session,
        **SCOPE, memory_type="decision", confidence=confidence, salience=0.85,
    )
    note = store.close_session_to_hypomnema(session, **SCOPE)["hypomnema_id"]
    content = store.get_hypomnema_entry(note, **SCOPE)["content"]
    store.revise_hypomnema_entry(
        note, f"{content} Checked again at dawn.", reason="checked again", **SCOPE,
        confidence=confidence,
    )
    return note


class _Provider:
    """A configured model, as a deep cycle is given one. It would answer a
    request for thoughts with two of them."""

    _model = "stub-provider-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return (
            "The ferries and the pier share one timetable, and the storms keep it.\n"
            "The harbour office decides more of the day than the crews do."
        )


def _reflect(rt: MnemosRuntime, llm_client) -> dict:
    from mnemos.consolidation.reflection import run_reflection_pass

    rt._ensure_init()
    identity = AgentIdentity()
    identity.memory_profile.agent_id = SCOPE["agent_id"]
    return run_reflection_pass(
        rt._store, identity, EmotionalState(), llm_client,
        person_id=SCOPE["person_id"], project_scope=SCOPE["project_scope"],
    )


# ── 1. The reflection pass writes no memories ──


def test_the_reflection_pass_writes_no_memory_without_a_model(tmp_path):
    db = tmp_path / "memory.db"
    _captured(db, *HARBOUR)
    before = _ids(db)

    rt = _runtime(db)
    try:
        stats = _reflect(rt, llm_client=None)
    finally:
        rt.close()

    assert stats["engrams_reviewed"] == len(HARBOUR), stats  # it had the agent's words to look at
    assert _ids(db) == before, (
        f"the reflection pass wrote Mnemos's own words: {_written(db, _ids(db) - before)}"
    )
    assert stats["thoughts_generated"] == 0, stats


def test_the_reflection_pass_writes_no_memory_with_a_model_and_sends_it_nothing(tmp_path):
    db = tmp_path / "memory.db"
    _captured(db, *HARBOUR)
    before = _ids(db)
    provider = _Provider()

    rt = _runtime(db)
    try:
        stats = _reflect(rt, llm_client=provider)
    finally:
        rt.close()

    assert _ids(db) == before, (
        f"the reflection pass wrote a model's words: {_written(db, _ids(db) - before)}"
    )
    assert provider.prompts == [], "the agent's memories were sent out for thoughts kept nowhere"
    assert stats["thoughts_generated"] == 0, stats


def test_a_deep_consolidate_writes_no_recurring_theme(tmp_path, monkeypatch, capsys):
    """The command that wrote three "Recurring theme" memories into the live
    store on 2026-09-30, run on a store like it."""
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _captured(db, *HARBOUR)
    before = _ids(db)

    assert main(["--db-path", str(db), *SCOPE_ARGS, "consolidate", "--deep"]) == 0

    out = capsys.readouterr().out
    passes = next(line for line in out.splitlines() if line.startswith("Passes:"))
    assert "reflection" in passes, out  # the pass ran
    assert _ids(db) == before, f"a deep cycle wrote: {_written(db, _ids(db) - before)}"
    assert "Reflection: 0 thoughts" in out, out


# ── 2. Promotion leaves the meaning as the agent left it ──


def test_a_promoted_memory_keeps_the_notes_meaning_and_asks_for_its_own(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt._ensure_init()
        note = _stable_note(
            rt._store, "Riley keeps the ferry timetable pinned above the chart table.",
            authored_by="agent", author_id="nova", author_model=MODEL,
        )
        said = rt.maintain()
        memory = _pair_of(db, note)
        assert "Promoted continuity notes: 1" in said and memory, said
        [(author, impact, source)] = _read(
            db, "SELECT author_kind, impact, impact_source FROM engrams WHERE id = ?", (memory,)
        )
        assert (impact, source) == ("", ""), (
            f"promotion wrote a meaning no one gave: {impact!r} ({source})"
        )
        assert author == "agent"
        # An empty meaning is asked for, as any memory without one is.
        assert _asked(db, memory) == [("impact", False)], _asked(db, memory)
        answered = rt.reflect(memory, "A pinned timetable keeps the crossing in view.",
                              signed_as=MODEL)
    finally:
        rt.close()

    assert answered.startswith("Reflection recorded."), answered
    assert _read(db, "SELECT impact, impact_source FROM engrams WHERE id = ?", (memory,)) == [
        ("A pinned timetable keeps the crossing in view.", "agent")
    ]
    # And the answer lands in the note the memory was promoted from.
    assert "What this changed:" in _note(db, note), _note(db, note)
    assert "A pinned timetable keeps the crossing in view." in _note(db, note)


def test_a_note_mnemos_wrote_is_never_promoted_and_takes_no_place(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt._ensure_init()
        store = rt._store
        # Three of Mnemos's summaries, each ranked above the agent's note.
        summaries = [_closed_session_summary(store, n, confidence=0.95) for n in range(3)]
        mine = _stable_note(
            store, "The harbour master answers the radio before the telephone.",
            authored_by="agent", author_id="nova", foundational=False, confidence=0.85,
        )
        store.revise_hypomnema_entry(
            mine, "The harbour master answers the radio before the telephone, always.",
            reason="said again", **SCOPE,
        )
        assert _read(db, f"""
            SELECT authored_by, entry_kind, confidence >= 0.82, salience >= 0.65,
                   revision_count >= 1, related_engram_id
            FROM hypomnema_entries WHERE id IN ({','.join('?' * 3)})
        """, tuple(summaries)) == [("system", "continuity", 1, 1, 1, None)] * 3, "premise"
        counted = store.get_hypomnema_stats(**SCOPE)["hypomnema_promotion_candidates"]
        said = rt.maintain()
    finally:
        rt.close()

    assert "Promoted continuity notes: 1" in said, said
    assert _pair_of(db, mine), "the agent's note lost its place to Mnemos's"
    assert [_pair_of(db, summary) for summary in summaries] == [None] * 3, (
        "a note Mnemos wrote became a memory"
    )
    assert _mnemos_wrote(db) == []
    # Health counts what promotion would take, and no more.
    assert counted == 1, counted


# ── 3. The impact pass matches on the pair ──


def _indexed_memory(rt: MnemosRuntime) -> str:
    """A memory the transcript indexer wrote: a tool's words, with no note."""
    return rt._encoder.encode(
        content="Session excerpt: the evening ferry left late because the pier gate jammed.",
        kind="episodic", tags=["session-indexed"], author_kind="tool", **SCOPE,
    ).id


def _interpreting_note(rt: MnemosRuntime, memory: str) -> str:
    """A note that interprets a memory, as mnemos_hypomnema_write writes one:
    it names the memory and is not its pair."""
    return rt._store.write_hypomnema_entry(
        "Reading of the late ferry: the gate, not the crew, decided the evening.",
        **SCOPE, source="synthesized", related_engram_id=memory,
    )


def test_the_impact_pass_never_asks_about_a_memory_a_note_only_names(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt._ensure_init()
        indexed = _indexed_memory(rt)
        _interpreting_note(rt, indexed)
        said = rt.maintain()
    finally:
        rt.close()

    assert "Passes:" in said, said
    assert _asked(db, indexed) == [], (
        "asked what a memory meant through a note that only names it; the answer "
        "has no note to land in"
    )


def test_the_impact_pass_asks_about_the_memory_a_correction_wrote(tmp_path):
    """A note that only named a memory, corrected, becomes a pair of its own
    whose note still names the memory it named. Its own memory has no meaning
    yet, and is the one to ask about."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        rt._ensure_init()
        indexed = _indexed_memory(rt)
        interpreting = _interpreting_note(rt, indexed)
        corrected = rt.correct(
            correction="Reading of the late ferry: the harbour master's delay decided the evening.",
            target_id=interpreting, signed_as=MODEL,
        )
        replacement, replacement_note = _memory_id(corrected), _note_id(corrected)
        assert _read(db, "SELECT related_engram_id, graduated_to_engram_id FROM hypomnema_entries "
                         "WHERE id = ?", (replacement_note,)) == [(indexed, replacement)], "premise"
        assert _read(db, "SELECT impact FROM engrams WHERE id = ?", (replacement,)) == [("",)]
        rt.maintain()
    finally:
        rt.close()

    assert _asked(db, replacement) == [("impact", False)], (
        "the memory a correction wrote was never asked what it changed"
    )
    assert _asked(db, indexed) == [], "asked about the memory its note only names"


# ── 4. No path writes engine words ──


def test_no_cycle_or_promotion_writes_engine_words_on_a_store_full_of_candidates(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _captured(db, *HARBOUR)
    rt = _runtime(db)
    try:
        rt._ensure_init()
        store = rt._store
        candidates = [
            _stable_note(store, "Riley keeps the ferry timetable pinned above the chart table.",
                         authored_by="agent", author_id="nova", author_model=MODEL),
            _stable_note(store, "The lighthouse keeper logs every ship that passes the point.",
                         source="co-formed"),
            _stable_note(store, "The chandlery on the quay sells rope by the fathom.",
                         source="observed"),
        ]
        revised = _stable_note(store, "The tide turns an hour later at the outer buoy.",
                               authored_by="agent", author_id="nova", foundational=False)
        store.revise_hypomnema_entry(revised, "The tide turns an hour later at the outer buoy, "
                                     "and later still in spring.", reason="learned more", **SCOPE)
        candidates.append(revised)
        summary = _closed_session_summary(store, 1)
    finally:
        rt.close()
    authors = dict(_read(db, "SELECT id, authored_by FROM hypomnema_entries"))
    assert [authors[c] for c in candidates] == ["agent", "coauthored", "unknown", "agent"], "premise"
    assert authors[summary] == "system", "premise"
    before = _ids(db)

    # A session's maintenance: a shallow cycle and promotions, three at a time.
    rt = _runtime(db)
    try:
        first, second = rt.maintain(), rt.maintain()
    finally:
        rt.close()
    assert "Cycle: shallow" in first and "Promoted continuity notes: 3" in first, first
    # A deep cycle, as `mnemos consolidate --deep` runs one.
    assert main(["--db-path", str(db), *SCOPE_ARGS, "consolidate", "--deep"]) == 0
    assert "reflection" in capsys.readouterr().out

    assert _mnemos_wrote(db) == [], "a memory Mnemos wrote"
    assert _placeholder_meanings(db) == [], "a meaning from TEMPLATED_IMPACTS"
    # The only memories written are the notes' own words, one per note.
    promoted = {_pair_of(db, c) for c in candidates}
    assert None not in promoted, second
    assert _ids(db) - before == promoted, _written(db, _ids(db) - before - promoted)
    assert _pair_of(db, summary) is None


# ── 5. The code before this change stands down ──


def test_the_code_before_this_change_stands_down_once_this_code_opens_the_store(
    tmp_path, monkeypatch, capsys,
):
    """Servers started before this change still write engine words when they
    maintain: themes, a placeholder meaning, Mnemos's notes as memories. They
    must stop maintaining once this code has opened the store."""
    assert MAINTENANCE_CODE_VERSION >= 11, (
        "what maintenance writes changed, and the code version was not raised"
    )
    monkeypatch.setattr(ei, "_check_local_deps", lambda: False)
    db = tmp_path / "memory.db"
    _captured(db, *HARBOUR)
    rt = _runtime(db)
    try:
        rt._ensure_init()
        note = _stable_note(rt._store, "The harbour bell rings twice for fog.",
                            authored_by="agent", author_id="nova")
    finally:
        rt.close()
    assert _read(db, "SELECT value FROM meta WHERE key = 'min_code_version'") == [
        (str(MAINTENANCE_CODE_VERSION),)
    ]
    before = _ids(db)

    for module in ("mnemos.code_version", "mnemos.simple_runtime", "mnemos.store.sqlite_store",
                   "mnemos.retrieval.reactive"):
        monkeypatch.setattr(f"{module}.MAINTENANCE_CODE_VERSION", MAINTENANCE_CODE_VERSION - 1,
                            raising=False)
    rt = _runtime(db)
    try:
        said = rt.maintain(deep=True)
    finally:
        rt.close()
    assert "Cycle: skipped" in said, said
    assert main(["--db-path", str(db), *SCOPE_ARGS, "consolidate", "--deep"]) == 0
    assert "Consolidation skipped: no passes ran." in capsys.readouterr().out

    assert _ids(db) == before, _written(db, _ids(db) - before)
    assert _pair_of(db, note) is None
