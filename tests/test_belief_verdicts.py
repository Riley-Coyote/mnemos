"""Beliefs change only for real reasons.

A belief is something the agent said yes to. It should fall when the agent
retires it, rise when the agent holds to it, and a contradiction should be
recorded when the agent judges one. The server guessed instead:

- any answer to a belief question formed a belief, a declined one included;
- "Now more than ever" retired a belief, because it begins with "no";
- "No, they don't contradict" recorded a contradiction, because it contains
  "contradict", and a "no" deleted every link from one memory to the other;
- a capture sharing one word with a belief and containing "not" (or "note")
  lowered the belief and linked the capture as contradicting it;
- nothing could raise a belief: a reaffirmation could never be asked, because
  the answered question that formed the belief held its place in the queue.

The agent now says what it decided (mnemos_reflect's verdict), and only that
decides. Its words are kept as written and never read for a yes or a no.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
OLDER = (
    "This session runs older Mnemos code than the store expects. "
    "Restart the session."
)
THEME_ASK = (
    'You keep returning to "ferry" (5 memories). Is that a belief you now hold? '
    "[theme:ferry]"
)


# ── Helpers ──


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _memory(rt: MnemosRuntime, content: str) -> str:
    """Capture a memory that carries its own meaning, so nothing asks about it."""
    said = rt.capture(content, impact="Kept for the test.")
    return re.search(r"Memory ID: (engram_\w+)", said).group(1)


def _clear_asks(rt: MnemosRuntime) -> None:
    rt._ensure_init()
    rt._store._get_conn().execute("DELETE FROM reflection_queue")
    rt._store._get_conn().commit()


def _ask(rt: MnemosRuntime, kind: str, target: str, prompt: str) -> str:
    ask_id = rt._store.enqueue_reflection(kind, target, prompt, **SCOPE)
    assert ask_id, f"premise: the {kind} question was queued"
    return ask_id


def _belief(rt: MnemosRuntime, target: str, content: str, *, confidence=0.4, days_ago=0) -> str:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    belief = Belief(
        agent_id="nova", content=content, confidence=confidence, domain="ferry",
        supporting_engram_ids=[target], source="agent",
        created_at=when, last_revised=when, last_challenged=when,
    )
    rt._store.save_belief(belief)
    return belief.id


def _formed_belief(rt: MnemosRuntime, target: str, content: str, *, days_ago: int) -> str:
    """A belief as an answered belief question formed it, days_ago old.

    Both live beliefs are in exactly this state: the question that formed them
    is answered and stays in the queue, on the memory the belief rests on.
    """
    _ask(rt, "belief", target, THEME_ASK)
    rt._store.answer_reflection(target, content, **SCOPE)
    return _belief(rt, target, content, days_ago=days_ago)


def _beliefs(db) -> list[tuple]:
    return _all(
        db,
        "SELECT id, content, confidence, superseded_by, revision_history, last_challenged "
        "FROM beliefs ORDER BY id",
    )


def _confidence(db, belief_id: str) -> float:
    return _all(db, "SELECT confidence FROM beliefs WHERE id = ?", (belief_id,))[0][0]


def _asks(db) -> list[tuple]:
    return _all(
        db,
        "SELECT id, kind, target_id, prompt, surfaced_count, answered_at, answer "
        "FROM reflection_queue ORDER BY created_at, id",
    )


def _ask_row(db, ask_id: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM reflection_queue WHERE id = ?", (ask_id,)).fetchone())
    finally:
        conn.close()


def _graph(db) -> dict[str, list[tuple]]:
    """Everything a verdict could change in the graph: links and weights."""
    return {
        "connections": _all(
            db,
            "SELECT source_id, target_id, relation, strength, formed_by FROM connections "
            "ORDER BY source_id, target_id, relation",
        ),
        "strengths": _all(db, "SELECT id, strength FROM engrams ORDER BY id"),
    }


def _between(db, a: str, b: str) -> list[tuple]:
    return _all(
        db,
        "SELECT source_id, target_id, relation, formed_by FROM connections "
        "WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?) "
        "ORDER BY source_id, relation",
        (a, b, b, a),
    )


def _link(rt: MnemosRuntime, source: str, target: str, relation: str, formed_by: str) -> None:
    from mnemos.core.engram import Connection

    rt._store.save_connection(
        source, Connection(target_id=target, relation=relation, strength=0.5, formed_by=formed_by)
    )


def _claim_for_newer_code(db, version: int = 999) -> None:
    """What a newer server's startup does to a store: raise its minimum."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', ?)",
            (str(version),),
        )
        conn.commit()
    finally:
        conn.close()


def _age(db, sql: str, params: tuple) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── The three cases reproduced on a copy of the live store ──


DECLINED = (
    "Less a belief than a way of seeing: I keep noticing that things show only "
    "where something catches them. I'll hold it as a habit of attention, not a claim."
)


def test_decline_forms_no_belief(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        ask_id = _ask(rt, "belief", target, THEME_ASK)

        # Without a verdict the words are not read for a no, and no belief is
        # formed from them either.
        unsure = rt.reflect(target, DECLINED)
        assert _beliefs(db) == [], "a belief was formed from an answer that declined"
        assert _ask_row(db, ask_id)["answered_at"] is None, "the question was spent"
        assert "stays open" in unsure

        said = rt.reflect(target, DECLINED, verdict="decline")
    finally:
        rt.close()

    assert "No belief was formed" in said
    assert _beliefs(db) == [], "decline formed a belief"
    row = _ask_row(db, ask_id)
    assert row["answered_at"] is not None
    assert row["answer"] == DECLINED, "the words are kept as written"


@pytest.mark.parametrize("filed_as", ["belief", "reaffirm"])
def test_now_more_than_ever_does_not_retire(tmp_path, filed_as):
    """A reaffirmation was filed as a belief question until it had its own kind."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley checks the live render before calling a fix done.")
        belief_id = _belief(rt, target, "I trust what is checked against the real thing.")
        _clear_asks(rt)
        _ask(rt, filed_as, target, f'You hold this belief. Still true? [belief:{belief_id}]')
        before = _beliefs(db)

        unsure = rt.reflect(target, "Now more than ever.")
        assert _beliefs(db) == before, "an answer beginning with 'no' moved the belief"
        assert "stays open" in unsure

        said = rt.reflect(target, "Now more than ever.", verdict="hold")
    finally:
        rt.close()

    assert "Kept." in said
    [(_, _, confidence, superseded_by, history, _)] = _beliefs(db)
    assert superseded_by is None, "the belief was retired"
    assert confidence == pytest.approx(0.45)
    assert json.loads(history)[-1]["reason"] == "reaffirmed by the agent: Now more than ever."


def test_no_they_dont_contradict_records_no_contradiction(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        a = _memory(rt, "Riley ships every change through a pull request.")
        b = _memory(rt, "Riley pushed a one-line fix straight to main last night.")
        _link(rt, a, b, "co_activated", "encoding_no_llm")
        _clear_asks(rt)
        ask_id = _ask(rt, "contradiction", a, f"Do these contradict? [ref:{b}]")
        graph = _graph(db)

        unsure = rt.reflect(a, "No, they don't contradict: the fix was an emergency.")
        assert _graph(db) == graph, "an answer containing 'contradict' changed the graph"
        assert "stays open" in unsure

        said = rt.reflect(
            a, "No, they don't contradict: the fix was an emergency.", verdict="compatible",
        )
    finally:
        rt.close()

    assert "not a contradiction" in said
    assert _graph(db) == graph, "compatible changed a graph with no contradiction in it"
    assert _ask_row(db, ask_id)["answered_at"] is not None


# ── No verdict: the words are kept, the question waits, nothing changes ──


@pytest.mark.parametrize("kind,words", [
    ("belief", "Yes. Riley builds every trip around the ferry."),
    ("reaffirm", "no"),
    ("contradiction", "yes"),
])
def test_no_verdict_changes_nothing(tmp_path, kind, words):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        other = _memory(rt, "Riley now avoids the ferry in winter.")
        belief_id = _belief(rt, target, "Riley loves the ferry.", confidence=0.6)
        _link(rt, target, other, "co_activated", "encoding_no_llm")
        _link(rt, target, other, "temporal_after", "encoding")
        _clear_asks(rt)
        prompt = {
            "belief": THEME_ASK,
            "reaffirm": f"Still true? [belief:{belief_id}]",
            "contradiction": f"Do these contradict? [ref:{other}]",
        }[kind]
        ask_id = _ask(rt, "belief" if kind == "reaffirm" else kind, target, prompt)
        beliefs, graph, asks = _beliefs(db), _graph(db), _asks(db)

        said = rt.reflect(target, words)
    finally:
        rt.close()

    assert _beliefs(db) == beliefs, "a belief was formed, revised or retired"
    assert _graph(db) == graph, "a link or a weight changed"
    row = _ask_row(db, ask_id)
    assert row["answered_at"] is None and row["surfaced_count"] == 0, "the question was spent"
    assert row["answer"] == words, "the words were not kept on the question"
    assert [a for a in _asks(db) if a[0] != ask_id] == [a for a in asks if a[0] != ask_id]
    assert "answer again with a verdict" in said


@pytest.mark.parametrize("builder", ["runtime", "hook"])
@pytest.mark.parametrize("kind,verdicts", [
    ("belief", "hold, decline or not_now"),
    ("reaffirm", "hold, decline, retire or not_now"),
    ("contradiction", "contradicts, compatible or unsure"),
    ("impact", None),
])
def test_the_packet_shows_the_verdict_a_question_takes(tmp_path, builder, kind, verdicts):
    """An agent copies the call the packet shows. Without a verdict a belief
    or contradiction answer forms nothing, so both builders (the runtime's
    and the session-start hook's) show it, with the verdicts that kind takes.
    An impact question's words are its answer, and its call stays as it was."""
    from mnemos.interface.context_packet import build_context_packet

    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        other = _memory(rt, "Riley now avoids the ferry in winter.")
        belief_id = _belief(rt, target, "Riley's trips bend to the ferry.")
        _clear_asks(rt)
        _ask(rt, kind, target, {
            "belief": THEME_ASK,
            "reaffirm": f"Still true? [belief:{belief_id}]",
            "contradiction": f"Do these contradict? [ref:{other}]",
            "impact": "What did this change in how you understand things? One sentence.",
        }[kind])
        if builder == "runtime":
            shown = rt._reflection_block()
        else:
            shown = build_context_packet(rt._store, "", include_engrams=False, **SCOPE)["prompt"]
    finally:
        rt.close()

    lines = [line.strip() for line in shown.splitlines()]
    call = f'mnemos_reflect(target_id="{target}", text="…", verdict="…")'
    if verdicts is None:
        assert call not in lines
        assert [line for line in lines if line.startswith("mnemos_reflect(")] == [
            f'mnemos_reflect(target_id="{target}", ...)' if builder == "runtime"
            else f'mnemos_reflect(target_id="{target}", text="…")'
        ]
        assert not [line for line in lines if line.startswith("verdict:")]
        return
    assert call in lines, f"the {builder} packet shows no verdict:\n{shown}"
    assert lines[lines.index(call) + 1] == f"verdict: {verdicts}"


def test_a_verdict_that_does_not_fit_the_question_changes_nothing(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _ask(rt, "belief", target, THEME_ASK)
        asks = _asks(db)
        not_one = rt.reflect(target, "Yes.", verdict="yes")
        no_belief_yet = rt.reflect(target, "Not any more.", verdict="retire")
    finally:
        rt.close()

    assert "'yes' is not a verdict" in not_one
    assert "retire does not answer this question" in no_belief_yet
    assert _asks(db) == asks and _beliefs(db) == []


# ── Belief questions: hold, decline, retire, not_now ──


def test_hold_forms_the_belief_in_the_agents_words(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _ask(rt, "belief", target, THEME_ASK)
        said = rt.reflect(target, "  No trip of Riley's starts without the ferry.  ", verdict="hold")
    finally:
        rt.close()

    assert "Belief recorded" in said
    [row] = _all(db, "SELECT content, confidence, domain, supporting_engram_ids, source FROM beliefs")
    assert row == ("No trip of Riley's starts without the ferry.", 0.4, "ferry", json.dumps([target]), "agent")


def test_retire_sets_confidence_to_zero_and_deletes_nothing(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley loved the old ferry route.")
        belief_id = _belief(rt, target, "Riley loves the ferry.", confidence=0.6)
        _clear_asks(rt)
        _ask(rt, "reaffirm", target, f"Still true? [belief:{belief_id}]")
        graph = _graph(db)
        memories = _all(db, "SELECT id, state FROM engrams ORDER BY id")
        said = rt.reflect(target, "Riley sold the boat and takes the bridge now.", verdict="retire")
        active = rt._store.get_beliefs("nova", active_only=True)
    finally:
        rt.close()

    assert "Retired" in said
    assert active == [], "a retired belief still shapes context"
    [(_, content, confidence, superseded_by, history, _)] = _beliefs(db)
    assert content == "Riley loves the ferry." and confidence == 0.0
    assert superseded_by == "retired"
    revision = json.loads(history)[-1]
    assert (revision["old_confidence"], revision["new_confidence"]) == (0.6, 0.0)
    assert revision["reason"] == "retired by the agent: Riley sold the boat and takes the bridge now."
    assert _graph(db) == graph and _all(db, "SELECT id, state FROM engrams ORDER BY id") == memories


def test_not_now_leaves_the_question_waiting(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley loved the old ferry route.")
        belief_id = _belief(rt, target, "Riley loves the ferry.", confidence=0.6)
        _clear_asks(rt)
        ask_id = _ask(rt, "reaffirm", target, f"Still true? [belief:{belief_id}]")
        rt._reflection_block()  # shown once
        beliefs = _beliefs(db)
        said = rt.reflect(target, "I want to see a winter first.", verdict="not_now")
        shown_again = rt._reflection_block()
    finally:
        rt.close()

    assert "Left open for later" in said
    assert _beliefs(db) == beliefs
    row = _ask_row(db, ask_id)
    assert row["answered_at"] is None
    assert row["answer"] == "I want to see a winter first."
    assert row["surfaced_count"] == 2, "not_now spent or reset a showing"
    assert shown_again and "Still true?" in shown_again


# ── Contradiction questions: contradicts, compatible, unsure ──


def test_compatible_keeps_other_edges(tmp_path):
    """Only the edge the question proposes may go: a contradiction from this
    memory to the other. A "no" used to delete every link between them."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        a = _memory(rt, "Riley ships every change through a pull request.")
        b = _memory(rt, "Riley pushed a one-line fix straight to main last night.")
        _link(rt, a, b, "co_activated", "encoding_no_llm")
        _link(rt, a, b, "temporal_after", "encoding")
        _link(rt, b, a, "co_activated", "consolidation_no_llm")
        _link(rt, b, a, "contradicts", "encoding")
        _link(rt, a, b, "contradicts", "agent_reflection")
        _clear_asks(rt)
        _ask(rt, "contradiction", a, f"Do these contradict? [ref:{b}]")
        before = _between(db, a, b)

        rt.reflect(a, "no")  # the old way to say it
        assert _between(db, a, b) == before, "an answer without a verdict removed a link"

        said = rt.reflect(a, "They fit: the fix was an emergency.", verdict="compatible")
    finally:
        rt.close()

    assert "removed" in said
    assert _between(db, a, b) == [
        edge for edge in before if edge != (a, b, "contradicts", "agent_reflection")
    ]


@pytest.mark.parametrize("already", [None, "contradicts-back"])
def test_contradicts_leaves_exactly_one_contradiction_between_the_pair(tmp_path, already):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        a = _memory(rt, "Riley ships every change through a pull request.")
        b = _memory(rt, "Riley pushed a one-line fix straight to main last night.")
        _link(rt, a, b, "co_activated", "encoding_no_llm")
        if already:
            _link(rt, b, a, "contradicts", "encoding")
        _clear_asks(rt)
        _ask(rt, "contradiction", b, f"Do these contradict? [ref:{a}]")
        said = rt.reflect(b, "Yes: he changed how he ships.", verdict="contradicts")
    finally:
        rt.close()

    assert "Contradiction recorded" in said
    contradictions = [edge for edge in _between(db, a, b) if edge[2] == "contradicts"]
    expected = (b, a, "contradicts", "encoding" if already else "agent_reflection")
    assert contradictions == [expected]
    assert (a, b, "co_activated", "encoding_no_llm") in _between(db, a, b)


def test_contradicts_changes_nothing_but_the_link(tmp_path):
    """The verdict says the two conflict, not which one is wrong. Lowering
    the earlier memory's strength assumed the newer one wins."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        earlier = _memory(rt, "Riley ships every change through a pull request.")
        later = _memory(rt, "Riley pushed a one-line fix straight to main last night.")
        _link(rt, later, earlier, "co_activated", "encoding_no_llm")
        _clear_asks(rt)
        _ask(rt, "contradiction", later, f"Do these contradict? [ref:{earlier}]")
        graph, beliefs = _graph(db), _beliefs(db)
        memories = _all(db, "SELECT * FROM engrams ORDER BY id")
        said = rt.reflect(later, "Yes: he changed how he ships.", verdict="contradicts")
    finally:
        rt.close()

    after = _graph(db)
    assert after["strengths"] == graph["strengths"], "a memory was weakened"
    assert _all(db, "SELECT * FROM engrams ORDER BY id") == memories
    assert "neither is weakened" in said
    assert _beliefs(db) == beliefs
    added = [edge for edge in after["connections"] if edge not in graph["connections"]]
    assert added == [(later, earlier, "contradicts", 0.7, "agent_reflection")]
    assert [edge for edge in graph["connections"] if edge not in after["connections"]] == []


def test_unsure_changes_nothing(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        a = _memory(rt, "Riley ships every change through a pull request.")
        b = _memory(rt, "Riley pushed a one-line fix straight to main last night.")
        _link(rt, a, b, "contradicts", "agent_reflection")
        _clear_asks(rt)
        ask_id = _ask(rt, "contradiction", a, f"Do these contradict? [ref:{b}]")
        graph = _graph(db)
        said = rt.reflect(a, "I can't tell yet.", verdict="unsure")
    finally:
        rt.close()

    assert "Nothing was changed" in said
    assert _graph(db) == graph
    assert _ask_row(db, ask_id)["answer"] == "I can't tell yet."


# ── A capture never moves a belief without a model ──


@pytest.mark.parametrize("capture", [
    "Riley did not review the deploy checklist this week.",
    "A note on the deploy checklist Riley reviews on Fridays.",  # "note" held "not"
])
def test_no_keyword_contradictions(tmp_path, capture):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        anchor = _memory(rt, "Riley reviews every deploy checklist before shipping.")
        belief = Belief(
            agent_id="nova", content="Riley reviews every deploy checklist",
            confidence=0.6, source="agent", supporting_engram_ids=[anchor],
            # Past the six-hour cooldown between revisions.
            last_revised=_days_ago(2),
        )
        rt._store.save_belief(belief)
        beliefs = _beliefs(db)
        said = rt.capture(capture)
    finally:
        rt.close()

    assert "Captured continuity." in said
    assert _beliefs(db) == beliefs, "a capture sharing words with a belief moved it"
    assert _all(db, "SELECT COUNT(*) FROM connections WHERE relation = 'contradicts'") == [(0,)]


# ── Reaffirmation: its own kind, a month apart, a little firmer on hold ──


def test_reaffirm_can_be_asked(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        belief_id = _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
        rt.maintain()
        waiting = rt.pending_reflections(limit=5)
        packet = rt._reflection_block()
    finally:
        rt.close()

    assert [(item["kind"], item["target_id"]) for item in waiting] == [("reaffirm", target)], (
        "a belief not held to for a month was not put to the agent again"
    )
    assert f"[belief:{belief_id}]" in waiting[0]["prompt"]
    assert "Still true?" in packet and "Riley's trips bend to the ferry." in packet


def test_new_themes_do_not_crowd_out_a_reaffirmation(tmp_path):
    """On a real store every maintenance cycle found a new theme to ask about
    (14 cycles, 14 theme questions in a day). A reaffirmation that waited for a
    cycle with none was never asked."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        for note in (
            "vektor render queue stalled overnight",
            "vektor shader cache rebuilt following a crash",
            "vektor timeline scrubbing feels sluggish",
            "vektor export finally matches preview",
        ):
            _memory(rt, note)
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
        rt.maintain()
        waiting = rt.pending_reflections(limit=5)
    finally:
        rt.close()

    assert sorted(i["kind"] for i in waiting) == ["belief", "reaffirm"], waiting
    assert "[theme:vektor]" in next(i for i in waiting if i["kind"] == "belief")["prompt"]
    assert next(i for i in waiting if i["kind"] == "reaffirm")["target_id"] == target


def test_a_belief_is_asked_about_only_where_its_memory_lives(tmp_path):
    """Beliefs belong to the agent, not to a scope. Another project's packet
    must not be shown this project's memory to ask about it."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
    finally:
        rt.close()

    elsewhere = MnemosRuntime(
        db_path=str(db), use_dedicated_model=False,
        agent_id="nova", person_id="riley", project_scope="other-project",
    )
    try:
        elsewhere.capture("Unrelated work in another project.", impact="Kept for the test.")
        elsewhere.maintain()
        waiting = elsewhere.pending_reflections(limit=5)
    finally:
        elsewhere.close()
    assert [i for i in waiting if i["kind"] == "reaffirm"] == []

    rt = _runtime(db)
    try:
        rt.maintain()
        home = rt.pending_reflections(limit=5)
    finally:
        rt.close()
    assert [(i["kind"], i["target_id"]) for i in home] == [("reaffirm", target)]


def test_a_belief_held_to_this_month_is_not_asked_about(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        recent = _memory(rt, "Riley plans every trip around the ferry timetable.")
        old = _memory(rt, "Riley keeps the harbour tide table by the door.")
        _clear_asks(rt)
        _formed_belief(rt, recent, "Riley's trips bend to the ferry.", days_ago=29)
        _formed_belief(rt, old, "Riley reads the tides before any plan.", days_ago=31)
        for _ in range(2):
            rt.maintain()
        waiting = rt.pending_reflections(limit=5)
    finally:
        rt.close()
    # The month-old belief is asked about (the premise); the other is not.
    assert [(i["kind"], i["target_id"]) for i in waiting] == [("reaffirm", old)]


def test_hold_raises_confidence(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        belief_id = _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
        rt.maintain()
        [item] = [i for i in rt.pending_reflections(limit=5) if i["kind"] == "reaffirm"]
        said = rt.reflect(item["target_id"], "Still true: the timetable decides.", verdict="hold")
        rt.maintain()  # held to now: not asked again this month
        waiting = rt.pending_reflections(limit=5)
    finally:
        rt.close()

    assert "Kept." in said
    assert _confidence(db, belief_id) == pytest.approx(0.45)
    [(_, _, _, _, history, last_challenged)] = _beliefs(db)
    revision = json.loads(history)[-1]
    assert revision["new_confidence"] == pytest.approx(0.45)
    assert revision["reason"] == "reaffirmed by the agent: Still true: the timetable decides."
    assert datetime.fromisoformat(last_challenged) > datetime.now(timezone.utc) - timedelta(minutes=5)
    assert waiting == []


def test_hold_never_raises_a_belief_past_099(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        belief_id = _belief(rt, target, "Riley's trips bend to the ferry.", confidence=0.97)
        for _ in range(2):
            _clear_asks(rt)
            _ask(rt, "reaffirm", target, f"Still true? [belief:{belief_id}]")
            rt.reflect(target, "Yes.", verdict="hold")
    finally:
        rt.close()
    assert _confidence(db, belief_id) == pytest.approx(0.99)


def test_a_belief_is_asked_about_again_a_month_after_its_last_answer(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
        rt.maintain()
        [first] = [i for i in rt.pending_reflections(limit=5) if i["kind"] == "reaffirm"]
        rt.reflect(target, "Leave it as it is for now.", verdict="decline")
        rt.maintain()
        assert rt.pending_reflections(limit=5) == [], "asked again the same month"

        # A month on, still not held to: asked again, beside the answered one.
        _age(db, "UPDATE reflection_queue SET created_at = ?, answered_at = ? WHERE id = ?",
             (_days_ago(31), _days_ago(31), first["id"]))
        rt.maintain()
        waiting = rt.pending_reflections(limit=5)
    finally:
        rt.close()

    assert [(i["kind"], i["target_id"]) for i in waiting] == [("reaffirm", target)]
    assert waiting[0]["id"] != first["id"]
    assert _ask_row(db, first["id"])["answer"] == "Leave it as it is for now."


def test_an_unanswered_reaffirmation_is_put_again_once_it_expires(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
        rt.maintain()
        [ask] = [i for i in rt.pending_reflections(limit=5) if i["kind"] == "reaffirm"]
        for _ in range(rt._store.MAX_SURFACINGS):
            rt._reflection_block()
        rt.maintain()
        assert rt.pending_reflections(limit=5) == [], "shown out, and put again before it expired"

        _age(db, "UPDATE reflection_queue SET expires_at = ? WHERE id = ?", (_days_ago(1), ask["id"]))
        rt.maintain()
        waiting = rt.pending_reflections(limit=5)
    finally:
        rt.close()

    assert [(i["id"], i["surfaced_count"]) for i in waiting] == [(ask["id"], 0)]
    assert len(_all(db, "SELECT id FROM reflection_queue WHERE kind = 'reaffirm'")) == 1


def test_reaffirmations_share_the_two_question_cap(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        targets = [
            _memory(rt, f"Riley plans trip {n} around the ferry timetable.") for n in range(3)
        ]
        _clear_asks(rt)
        belief_id = _belief(rt, targets[0], "Riley's trips bend to the ferry.")
        _ask(rt, "reaffirm", targets[0], f"Still true? [belief:{belief_id}]")
        _ask(rt, "impact", targets[1], "What did this change?")
        _ask(rt, "belief", targets[2], THEME_ASK)
        block = rt._reflection_block()
    finally:
        rt.close()
    assert block.count("mnemos_reflect(") == 2


def test_a_declined_theme_is_not_asked_again(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        notes = iter([
            "vektor render queue stalled overnight",
            "vektor shader cache rebuilt following a crash",
            "vektor timeline scrubbing feels sluggish",
            "vektor export finally matches preview",
            "vektor audio drifted during playback",
            "vektor autosave corrupted twice",
            "vektor installer signed properly",
        ])
        for _ in range(4):
            _memory(rt, next(notes))
        rt.maintain()
        [ask] = rt.pending_reflections(limit=5)
        rt.reflect(ask["target_id"], "It is a project, not a belief.", verdict="decline")
        for _ in range(3):
            _memory(rt, next(notes))
            rt.maintain()
    finally:
        rt.close()

    themes = [p for (p,) in _all(db, "SELECT prompt FROM reflection_queue WHERE kind = 'belief'")]
    assert len(themes) == 1 and "[theme:vektor]" in themes[0], themes
    assert _beliefs(db) == []


# ── Older code: every verdict waits, and no reaffirmation is asked ──


def test_older_code_applies_no_verdict(tmp_path):
    """Code older than the store keeps the words as a signed note naming the
    question and the verdict, and forms, retires, links and removes nothing."""
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        formation = _memory(rt, "Riley plans every trip around the ferry timetable.")
        held = _memory(rt, "Riley loved the old ferry route.")
        surprising = _memory(rt, "Riley now avoids the ferry in winter.")
        earlier = _memory(rt, "Riley takes the ferry every winter weekend.")
        belief_id = _belief(rt, held, "Riley loves the ferry.", confidence=0.6, days_ago=40)
        _link(rt, surprising, earlier, "contradicts", "agent_reflection")
        _clear_asks(rt)
        _ask(rt, "belief", formation, THEME_ASK)
        _ask(rt, "reaffirm", held, f"Still true? [belief:{belief_id}]")
        _ask(rt, "contradiction", surprising, f"Do these contradict? [ref:{earlier}]")
        _ask(rt, "contradiction", earlier, f"Do these contradict? [ref:{surprising}]")
    finally:
        rt.close()

    _claim_for_newer_code(db)
    beliefs, graph, asks = _beliefs(db), _graph(db), _asks(db)
    answers = [
        (formation, "Riley's trips bend to the ferry.", "hold"),
        (held, "Riley sold the boat.", "retire"),
        (surprising, "It fits: winter is different.", "compatible"),
        (earlier, "Yes, they conflict.", "contradicts"),
    ]
    rt = _runtime(db)
    try:
        rt.introduce("claude-opus-5-5")
        said = [rt.reflect(target, words, verdict=verdict) for target, words, verdict in answers]
        rt.maintain()
    finally:
        rt.close()

    assert _asks(db) == asks, "older code answered a question or spent a showing"
    assert _beliefs(db) == beliefs, "older code formed, raised or retired a belief"
    assert _graph(db) == graph, "older code linked, unlinked or weakened a memory"
    notes = [c for (c,) in _all(db, "SELECT content FROM hypomnema_entries WHERE content LIKE '%open question%'")]
    for (_, words, verdict), result in zip(answers, said):
        assert "the question stays open for a current session" in result
        assert result.splitlines()[-1] == OLDER
        kept = [note for note in notes if note.startswith(words)]
        assert len(kept) == 1 and kept[0].endswith(f"Verdict: {verdict}")


def test_older_code_asks_no_reaffirmation(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _formed_belief(rt, target, "Riley's trips bend to the ferry.", days_ago=31)
    finally:
        rt.close()
    current = tmp_path / "current.db"
    shutil.copyfile(db, current)

    rt = _runtime(current)
    try:
        rt.maintain()
    finally:
        rt.close()
    assert _all(current, "SELECT kind FROM reflection_queue WHERE answered_at IS NULL") == [
        ("reaffirm",)
    ], "premise: current code asks"

    _claim_for_newer_code(db)
    asks = _asks(db)
    rt = _runtime(db)
    try:
        rt.maintain()
        rt.context()
    finally:
        rt.close()
    assert _asks(db) == asks, "older code put a reaffirmation to the agent"


# ── The queue takes the new kind: schema v11 ──


V10_QUEUE = """CREATE TABLE reflection_queue (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT 'default',
    person_id TEXT NOT NULL DEFAULT 'user',
    project_scope TEXT NOT NULL DEFAULT 'global',
    kind TEXT NOT NULL
        CHECK (kind IN ('impact', 'lesson', 'belief', 'contradiction')),
    target_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    surfaced_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    answered_at TEXT,
    answer TEXT
)"""
V10_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_unique "
    "ON reflection_queue(agent_id, person_id, project_scope, kind, target_id)"
)
V10_PENDING = (
    "CREATE INDEX IF NOT EXISTS idx_reflection_pending "
    "ON reflection_queue(agent_id, person_id, project_scope, surfaced_count, created_at) "
    "WHERE answered_at IS NULL"
)


def _v10_store(tmp_path) -> Path:
    """A store whose queue is as v10 and earlier made it, with asks in it."""
    db = tmp_path / "legacy.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
    finally:
        rt.close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE reflection_queue")
        conn.execute(V10_QUEUE)
        conn.execute(V10_UNIQUE)
        conn.execute(V10_PENDING)
        rows = [
            ("a1", "belief", target, THEME_ASK, 1, _days_ago(40), None, _days_ago(39), "Yes."),
            ("a2", "impact", target, "What did this change?", 0, _days_ago(2), None, None, None),
        ]
        conn.executemany(
            "INSERT INTO reflection_queue (id, agent_id, person_id, project_scope, kind, "
            "target_id, prompt, surfaced_count, created_at, expires_at, answered_at, answer) "
            "VALUES (?, 'nova', 'riley', 'demo', ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    return db


def test_a_v10_queue_is_rebuilt_to_take_reaffirmations(tmp_path):
    from mnemos.store.sqlite_store import SCHEMA_VERSION, EngramStore

    db = _v10_store(tmp_path)
    rows = _all(db, "SELECT * FROM reflection_queue ORDER BY id")
    target = rows[0][5]

    store = EngramStore(db)
    try:
        first = store.enqueue_reflection("reaffirm", target, "Still true? [belief:belief_X]", **SCOPE)
        second = store.enqueue_reflection("reaffirm", target, "Still true? [belief:belief_X]", **SCOPE)
        store.answer_reflection(target, "Yes.", reflection_id=first, **SCOPE)
        third = store.enqueue_reflection("reaffirm", target, "Still true? [belief:belief_X]", **SCOPE)
        again = store.enqueue_reflection("belief", target, THEME_ASK, **SCOPE)
    finally:
        store.close()

    assert SCHEMA_VERSION == 11
    assert _all(db, "SELECT value FROM meta WHERE key = 'schema_version'") == [("11",)]
    assert _all(db, "SELECT * FROM reflection_queue WHERE id IN ('a1', 'a2') ORDER BY id") == rows
    assert first and third, "a reaffirmation could not be asked, or asked again once answered"
    assert second is None, "two reaffirmations waited on one memory"
    assert again is None, "an answered belief question no longer counts as asked"
    assert len(list((tmp_path / "backups").glob("legacy.pre-v11-*.db"))) == 1
    assert _all(db, "PRAGMA integrity_check") == [("ok",)]

    # An older Mnemos opening this store runs its own schema script: the same
    # names with IF NOT EXISTS. With two answered reaffirmations on one memory
    # now, rebuilding the old index would fail; it must leave the new one be.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(V10_QUEUE.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        conn.execute(V10_UNIQUE)
        conn.execute(V10_PENDING)
        conn.commit()
    finally:
        conn.close()
    [(index,)] = _all(db, "SELECT sql FROM sqlite_master WHERE name = 'idx_reflection_unique'")
    assert "reaffirm" in index

    reopened = EngramStore(db)  # and current code opens it unchanged
    reopened.close()
    assert _all(db, "SELECT COUNT(*) FROM reflection_queue") == [(4,)]


def test_the_rebuilt_queue_is_never_without_its_indexes(tmp_path):
    """The rebuild commits the queue with its indexes. Left to the schema
    script, which runs after the commit, an older process could slip a
    duplicate into an unindexed queue, and then the unique index could never
    be built again: every later open would fail."""
    from mnemos.store.sqlite_store import EngramStore

    db = _v10_store(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        EngramStore._allow_reaffirm_asks(conn)
    finally:
        conn.close()
    indexes = dict(_all(db, "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'reflection_queue' AND sql IS NOT NULL"))
    assert "WHERE kind != 'reaffirm' OR answered_at IS NULL" in indexes["idx_reflection_unique"]
    assert "WHERE answered_at IS NULL" in indexes["idx_reflection_pending"]
    assert "'reaffirm'" in indexes["reflection_queue"]
    assert [name for (name,) in _all(db, "SELECT name FROM sqlite_master WHERE name LIKE 'reflection_queue_%'")] == []


def test_an_index_an_older_opener_rebuilt_is_rebuilt_again(tmp_path):
    from mnemos.store.sqlite_store import EngramStore

    db = tmp_path / "memory.db"
    EngramStore(db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP INDEX idx_reflection_unique")
        conn.execute(V10_UNIQUE)
        conn.commit()
    finally:
        conn.close()

    EngramStore(db).close()
    [(index,)] = _all(db, "SELECT sql FROM sqlite_master WHERE name = 'idx_reflection_unique'")
    assert "WHERE kind != 'reaffirm' OR answered_at IS NULL" in index


# ── Hosts pass the verdict too ──


def test_a_host_passes_the_verdict_and_a_replay_forms_nothing_more(tmp_path):
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        _clear_asks(rt)
        _ask(rt, "belief", target, THEME_ASK)
        request = {"target_id": target, "text": "The ferry decides every trip.", "verdict": "hold"}
        first = rt.execute_host_mutation(
            "reflect", request, host_namespace="mnemos-hermes/v1", idempotency_key="turn-7",
        )
        replay = rt.execute_host_mutation(
            "reflect", request, host_namespace="mnemos-hermes/v1", idempotency_key="turn-7",
        )
    finally:
        rt.close()

    assert "Belief recorded" in first["result"]
    assert replay == {**first, "replayed": True}
    assert _all(db, "SELECT content, confidence FROM beliefs") == [("The ferry decides every trip.", 0.4)]


# ── Across processes: written in one, read back in another ──


def _cli(*args, home):
    return subprocess.run(
        [sys.executable, "-m", "mnemos.cli", *args],
        capture_output=True, text=True, timeout=180,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )


def _python(code: str, *, home):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=180,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MNEMOS_DISABLE_DOTENV": "1",
            "PYTHONPATH": ":".join(sys.path),
        },
    )


def test_a_capture_in_one_process_leaves_beliefs_alone_and_is_read_back_in_another(tmp_path):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    (home / ".mnemos" / "config.json").write_text(
        json.dumps({"consolidation": {"min_idle_minutes": 0}})
    )
    first = _cli("remember", "Riley reviews every deploy checklist before shipping.", home=home)
    assert first.returncode == 0, first.stderr
    [db] = [p for p in (home / ".mnemos").glob("*.db") if p.name != "audit.db"]
    [(anchor, agent)] = _all(db, "SELECT id, owner_agent_id FROM engrams")
    conn = sqlite3.connect(str(db))
    try:
        belief = Belief(
            agent_id=agent, content="Riley reviews every deploy checklist", confidence=0.6,
            source="agent", supporting_engram_ids=[anchor], last_revised=_days_ago(2),
        ).to_dict()
        conn.execute(
            f"INSERT INTO beliefs ({', '.join(belief)}) VALUES ({', '.join('?' for _ in belief)})",
            list(belief.values()),
        )
        conn.commit()
    finally:
        conn.close()
    beliefs = _beliefs(db)

    token = f"harbour-{uuid.uuid4().hex[:12]}"
    wrote = _cli(
        "remember", f"Riley did not review the deploy checklist this week; a note: {token}.",
        home=home,
    )
    assert wrote.returncode == 0, wrote.stderr

    read = _cli("hook", "session-start", home=home)
    assert read.returncode == 0, read.stderr
    packet = json.loads(read.stdout)["hookSpecificOutput"]["additionalContext"]
    assert token in packet, f"the capture did not come back in another process:\n{packet}"
    assert _beliefs(db) == beliefs, "the capture moved a belief"
    assert _all(db, "SELECT COUNT(*) FROM connections WHERE relation = 'contradicts'") == [(0,)]


def test_a_verdict_given_in_one_process_is_read_back_in_another(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    try:
        target = _memory(rt, "Riley plans every trip around the ferry timetable.")
        belief_id = _belief(rt, target, "Riley's trips bend to the ferry.")
        _clear_asks(rt)
        _ask(rt, "reaffirm", target, f"Still true? [belief:{belief_id}]")
    finally:
        rt.close()

    open_runtime = (
        "from mnemos.simple_runtime import MnemosRuntime; "
        f"rt = MnemosRuntime(db_path={str(db)!r}, use_dedicated_model=False, "
        "agent_id='nova', person_id='riley', project_scope='demo'); "
    )
    wrote = _python(
        open_runtime + f"print(rt.reflect({target!r}, 'Still true.', verdict='hold')); rt.close()",
        home=home,
    )
    assert wrote.returncode == 0, wrote.stderr
    assert "Kept." in wrote.stdout

    read = _python(
        open_runtime
        + "rt._ensure_init(); "
        + "print([(b.id, round(b.confidence, 4)) for b in rt._store.get_beliefs('nova')]); rt.close()",
        home=home,
    )
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == str([(belief_id, 0.45)])
