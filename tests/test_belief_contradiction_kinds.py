"""The agent forms and judges its own beliefs and contradictions, keyless.

Two reflection kinds were legal in the schema but never produced or applied.
Wiring them completes the inversion: a keyless install now does the last two
judgment tasks — belief formation and contradiction detection — through the
agent's own turns, no provider. The server only ever *proposes*; the agent's
answer is what becomes a belief or a contradiction edge.
"""

from __future__ import annotations

import re

import pytest

from mnemos.core.belief import Belief
from mnemos.simple_runtime import MnemosRuntime


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "kinds.db")


def _runtime(db):
    return MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")


def _pending(rt, kind):
    return [
        i for i in rt.pending_reflections(limit=10) if i["kind"] == kind
    ]


def _asked_themes(rt):
    """Every theme ever put to the agent — answered or not, shown or not."""
    rows = rt._store._get_conn().execute(
        "SELECT prompt FROM reflection_queue WHERE kind = 'belief' ORDER BY created_at"
    ).fetchall()
    themes = []
    for row in rows:
        m = re.search(r"\[theme:([^\]]+)\]", row[0])
        if m:
            themes.append(m.group(1))
    return themes


# One word recurs across these; nothing else does.
_VEKTOR_NOTES = [
    "vektor render queue stalled overnight",
    "vektor shader cache rebuilt following a crash",
    "vektor timeline scrubbing feels sluggish",
    "vektor export finally matches preview",
    "vektor audio drifted during playback",
    "vektor autosave corrupted twice",
    "vektor installer signed properly",
    "vektor plugin loading improved",
]
_TESSERA_NOTES = [
    "tessera mosaic grout cured",
    "tessera glaze samples arrived cracked",
    "tessera kiln schedule moved earlier",
    "tessera commission deposit cleared",
]
_ELSEWHERE_NOTES = [
    "groceries restocked and the kitchen tidied",
    "walked along the river at sunset",
]


class TestBeliefFormation:
    def test_a_recurring_theme_is_offered_as_a_belief(self, db):
        rt = _runtime(db)
        # Five captures sharing a real (non-structural) theme tag.
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}",
                       importance="high")
        rt.maintain()
        beliefs = _pending(rt, "belief")
        assert beliefs, "no belief candidate surfaced for a strongly recurring theme"
        assert "belief you now hold" in beliefs[0]["prompt"].lower()

    def test_answering_forms_an_agent_belief(self, db):
        rt = _runtime(db)
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}")
        rt.maintain()
        item = _pending(rt, "belief")[0]
        out = rt.reflect(item["target_id"], "Vektor's performance is never quite finished.")
        assert "Belief recorded" in out, out
        beliefs = rt._store.get_beliefs("t", active_only=True)
        mine = [b for b in beliefs if b.source == "agent"]
        assert any("never quite finished" in b.content for b in mine)

    def test_an_empty_answer_forms_nothing(self, db):
        rt = _runtime(db)
        for i in range(5):
            rt.capture(content=f"The vektor project needs another perf pass, note {i}")
        rt.maintain()
        item = _pending(rt, "belief")[0]
        before = len(rt._store.get_beliefs("t"))
        rt.reflect(item["target_id"], "   ")
        assert len(rt._store.get_beliefs("t")) == before


class TestAThemeIsAskedOnce:
    """A live store held 103 unanswered copies of one question:
    'You keep returning to "2026"'. Nearly every note starts with a date,
    so the year outranked every real theme, and a theme the agent left
    unanswered was asked again the next cycle against a fresh memory.
    Leaving it is how the packet tells the agent to decline, so silence
    has to be what lets a theme fade.
    """

    @pytest.mark.parametrize("stamp", [
        "2026-09-2{i}: ",
        "2026-09-24T22:1{i}:07Z ",
        "11pm: ",
        "the 24th: ",
    ], ids=["iso-date", "iso-timestamp", "clock-time", "day-ordinal"])
    def test_a_date_on_every_note_is_not_a_theme(self, db, stamp):
        rt = _runtime(db)
        for i, note in enumerate(_VEKTOR_NOTES[:4] + _ELSEWHERE_NOTES):
            rt.capture(content=stamp.format(i=i) + note)
        rt.maintain()

        themes = _asked_themes(rt)
        assert not [t for t in themes if any(c.isdigit() for c in t)], (
            f"a date or time was offered as a belief: {themes}"
        )
        assert "vektor" in themes, "the real theme under the dates was never offered"

    def test_a_theme_already_waiting_is_not_asked_again(self, db):
        rt = _runtime(db)
        notes = iter(_VEKTOR_NOTES)
        for _ in range(4):
            rt.capture(content=next(notes), impact="kept for the test")
        rt.maintain()
        assert _asked_themes(rt) == ["vektor"]

        for _ in range(3):  # later sessions keep mentioning it
            rt.capture(content=next(notes), impact="kept for the test")
            rt.maintain()

        assert _asked_themes(rt) == ["vektor"], "the same theme was put to the agent again"

    @pytest.mark.parametrize("lapse", ["left unanswered", "expired"])
    def test_a_theme_left_to_fade_does_not_come_back(self, db, lapse):
        rt = _runtime(db)
        notes = iter(_VEKTOR_NOTES)
        for _ in range(4):
            rt.capture(content=next(notes), impact="kept for the test")
        rt.maintain()
        assert _asked_themes(rt) == ["vektor"]

        if lapse == "left unanswered":
            # Shown in every packet it was allowed, and never answered.
            for _ in range(rt._store.MAX_SURFACINGS):
                rt._reflection_block(limit=10)
        else:
            rt._store._get_conn().execute(
                "UPDATE reflection_queue SET expires_at = '2000-01-01T00:00:00+00:00'"
            )
            rt._store._commit()
        assert not _pending(rt, "belief"), "the ask never faded"

        for _ in range(3):
            rt.capture(content=next(notes), impact="kept for the test")
            rt.maintain()

        assert _asked_themes(rt) == ["vektor"], "a declined theme came back"
        assert not _pending(rt, "belief")

    def test_a_new_theme_is_still_offered(self, db):
        rt = _runtime(db)
        for note in _VEKTOR_NOTES[:4]:
            rt.capture(content=note, impact="kept for the test")
        rt.maintain()
        for note in _TESSERA_NOTES:
            rt.capture(content=note, impact="kept for the test")
        rt.maintain()

        assert _asked_themes(rt) == ["vektor", "tessera"]


class TestBeliefReaffirmation:
    def test_no_retires_a_belief(self, db):
        rt = _runtime(db)
        rt._ensure_init()
        # Capture with an impact so no impact reflection is queued on this
        # engram — the belief reaffirmation must own its target uncontended
        # (the tool answers by target_id alone).
        rt.capture(content="anchor memory for the belief", impact="grounds a belief")
        eid = rt._store.get_active_engrams(agent_id="t", limit=1)[0].id
        rt._store.save_belief(Belief(agent_id="t", content="An outdated belief",
                                     confidence=0.6, source="agent",
                                     supporting_engram_ids=[eid]))
        # Reaffirmation reflection targets the supporting engram with a marker.
        rt._store.enqueue_reflection(
            "belief", eid,
            "You hold this belief: \"An outdated belief\". Still true? "
            f"[belief:{[b.id for b in rt._store.get_beliefs('t')][0]}]",
            agent_id="t", person_id="p", project_scope="g",
        )
        item = _pending(rt, "belief")[0]
        out = rt.reflect(item["target_id"], "no")
        assert "Retired" in out
        assert rt._store.get_beliefs("t", active_only=True) == []


class TestContradiction:
    def _surprising_pair(self, rt):
        """A capture with a surprising sibling, so a candidate can surface."""
        rt._ensure_init()
        rt.capture(content="Riley always ships through pull requests, never to main")
        # A directly conflicting later capture; force a surprise signal so the
        # enqueuer offers it (surprise detection is store-state dependent).
        eng = rt._encoder.encode(
            content="Riley now pushes small fixes straight to main without a PR",
            agent_id="t",
        )
        eng.encoding_context.surprise_level = 0.7
        rt._store.save_engram(eng)
        return eng

    def test_a_surprising_capture_surfaces_a_contradiction_candidate(self, db):
        rt = _runtime(db)
        self._surprising_pair(rt)
        rt.maintain()
        cand = _pending(rt, "contradiction")
        assert cand, "no contradiction candidate surfaced for a surprising conflicting capture"
        assert re.search(r"\[ref:engram_", cand[0]["prompt"])

    def test_yes_writes_a_contradicts_edge_and_downweights(self, db):
        rt = _runtime(db)
        eng = self._surprising_pair(rt)
        rt.maintain()
        item = _pending(rt, "contradiction")[0]
        other_id = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", item["prompt"]).group(1)
        before = rt._store.get_engram(other_id).strength

        out = rt.reflect(item["target_id"], "yes, he changed his workflow")
        assert "Contradiction recorded" in out, out

        edges = rt._store.get_connections(item["target_id"])
        assert any(
            c.target_id == other_id
            and str(getattr(c.relation, "value", c.relation)) == "contradicts"
            and c.formed_by == "agent_reflection"
            for c in edges
        ), "no agent-authored CONTRADICTS edge was written"
        # The older memory was downweighted — the deliberate downward move.
        assert rt._store.get_engram(other_id).strength < before

    def test_no_records_no_conflict(self, db):
        rt = _runtime(db)
        self._surprising_pair(rt)
        rt.maintain()
        item = _pending(rt, "contradiction")[0]
        other_id = re.search(r"\[ref:(engram_[A-Za-z0-9]+)\]", item["prompt"]).group(1)
        out = rt.reflect(item["target_id"], "no, those are about different things")
        assert "not a contradiction" in out.lower()
        edges = rt._store.get_connections(item["target_id"])
        assert not any(c.target_id == other_id for c in edges)


class TestRestraintHolds:
    def test_the_packet_never_shows_more_than_two_reflections(self, db):
        rt = _runtime(db)
        for i in range(6):
            rt.capture(content=f"The vektor project needs another perf pass note {i}",
                       importance="high")
        rt.maintain()
        assert len(rt.pending_reflections(limit=2)) <= 2
