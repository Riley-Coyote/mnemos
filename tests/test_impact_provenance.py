"""An agent-authored trace must be distinguishable from a generated one.

Mnemos's central claim is that only the agent can say what a memory changed.
Impacts are written by four different origins — the agent via `mnemos_reflect`
and `mnemos_capture`, a configured model during softening, and server
boilerplate — and once a store has data, nothing can tell them apart after the
fact. `_TEMPLATED_IMPACTS` in simple_runtime is exactly that archaeology, done
because provenance was never recorded.

`impact_source` records it at write time. This is why it must land before
0.2.0 ships: a column added later leaves every impact created in between
unlabelled forever.
"""

from __future__ import annotations

import sqlite3

import pytest

from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "prov.db")


def _runtime(db):
    return MnemosRuntime(db_path=db, agent_id="t", person_id="p", project_scope="g")


def _source_of(db, like):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT impact, impact_source FROM engrams WHERE content LIKE ?", (f"%{like}%",)
    ).fetchone()
    conn.close()
    return row


class TestImpactSourceIsRecordedAtWriteTime:
    def test_agent_capture_is_labelled_agent(self, db):
        rt = _runtime(db)
        rt.capture(content="Riley prefers Zed", impact="how I set up his editor")
        impact, source = _source_of(db, "Zed")
        assert impact == "how I set up his editor"
        assert source == "agent", source

    def test_reflection_is_labelled_agent(self, db):
        import re

        rt = _runtime(db)
        rt.capture(content="Riley rejected MCP sampling for maintenance")
        tid = re.search(r"engram_[A-Z0-9]+", rt.context())
        assert tid, "no reflection request surfaced"
        rt.reflect(target_id=tid.group(0), text="the server proposes, I answer")
        _impact, source = _source_of(db, "MCP sampling")
        assert source == "agent", source

    def test_server_correction_impact_is_labelled_template(self, db):
        rt = _runtime(db)
        rt.capture(content="Riley's editor is vim")
        rt.correct(correction="Riley's editor is Zed, not vim", query="editor")
        # The replacement's impact sentence is server boilerplate, whoever
        # supplied the content.
        _impact, source = _source_of(db, "Zed, not vim")
        assert source == "template", source

    def test_empty_impact_has_no_source(self, db):
        """No impact, no author. A source on an empty trace would be a lie."""
        rt = _runtime(db)
        rt.capture(content="A memory with no stated impact at all")
        impact, source = _source_of(db, "no stated impact")
        assert impact == ""
        assert source == "", (impact, source)


class TestProvenanceSurvivesStorage:
    def test_impact_source_round_trips_through_the_store(self, db):
        store = EngramStore(db)
        try:
            from mnemos.core.engram import Engram

            e = Engram(content="x", impact="a trace", impact_source="agent")
            store.save_engram(e)
            loaded = store.get_engram(e.id)
            assert loaded is not None
            assert loaded.impact == "a trace"
            assert loaded.impact_source == "agent"
        finally:
            store.close()

    def test_a_pre_provenance_store_upgrades_and_reads_blank(self, tmp_path):
        """Old stores lack the column. Opening one must add it, defaulting blank.

        Blank is 'unknown' — the honest state of an impact written before
        provenance existed. It must never be back-filled with a guess.
        """
        raw = str(tmp_path / "old.db")
        # Build a store, then physically drop the column to mimic v0.2-pre.
        store = EngramStore(raw)
        from mnemos.core.engram import Engram

        store.save_engram(Engram(content="legacy", impact="was here"))
        store.close()

        conn = sqlite3.connect(raw)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(engrams)")]
        assert "impact_source" in cols  # our migration added it
        # Simulate a genuinely old row: null the provenance.
        conn.execute("UPDATE engrams SET impact_source = ''")
        conn.commit()
        conn.close()

        reopened = EngramStore(raw)
        try:
            row = reopened._get_conn().execute(
                "SELECT impact, impact_source FROM engrams WHERE content = 'legacy'"
            ).fetchone()
            assert row[0] == "was here"
            assert row[1] == ""  # unknown, not fabricated
        finally:
            reopened.close()
