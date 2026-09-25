"""Lesson links filed wrong can be listed, and removed after a backup.

Softening used to take the first lesson sharing any word with a fading memory's
impact as the lesson it taught, and to file placeholder impacts under
placeholder lessons: on a real store, 451 of 631 distilled_into links. #69 stops
new ones. This is the way to clear the old ones: a dry run by default, a
verified backup before anything goes, and only the misfiled links go.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemos.core.engram import Connection, Engram
from mnemos.core.types import ConnectionRelation
from mnemos.simple_runtime import MnemosRuntime

GREP = ("When I claim something is clean, the verification must match how the thing "
        "actually varies.")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = MnemosRuntime(db_path=str(tmp_path / "r.db"), agent_id="demo", use_dedicated_model=False)
    r._ensure_init()
    yield r
    r.close()


def _save(runtime, content, impact="", impact_source="", tags=(), links=()):
    scope = dict(owner_agent_id="demo", person_id=runtime.scope.person_id,
                 project_scope=runtime.scope.project_scope)
    engram = Engram(content=content, impact=impact, impact_source=impact_source,
                    kind="episodic", tags=list(tags), **scope)
    for target, relation in links:
        engram.connections.append(Connection(target_id=target.id, relation=relation,
                                             strength=0.9, formed_by="consolidation"))
    runtime._store.save_engram(engram)
    return engram


@pytest.fixture
def filed(runtime):
    """One lesson with a memory filed right, one filed on shared words, and one
    placeholder filed under a placeholder lesson."""
    grep = _save(runtime, GREP, impact=GREP, tags=("lesson", "distilled"))
    placeholder = _save(runtime, "Current working context for continuity.",
                        impact="Current working context for continuity.", tags=("lesson", "distilled"))
    right = _save(runtime, "grepped for 'seven tools' and missed 'Seven tools'", impact=GREP,
                  impact_source="agent", links=[(grep, ConnectionRelation.DISTILLED_INTO)])
    wrong = _save(runtime, "a long talk about what I am",
                  impact="When I looked for how I actually exist, what was true was relational.",
                  impact_source="agent",
                  links=[(grep, ConnectionRelation.DISTILLED_INTO), (grep, ConnectionRelation.CO_ACTIVATED)])
    template = _save(runtime, "the working notes for the afternoon",
                     impact="Current working context for continuity.",
                     links=[(placeholder, ConnectionRelation.DISTILLED_INTO)])
    return dict(grep=grep, placeholder=placeholder, right=right, wrong=wrong, template=template)


def _distilled(runtime, source):
    return [c.target_id for c in runtime._store.get_connections(source.id)
            if c.relation == ConnectionRelation.DISTILLED_INTO]


def test_the_dry_run_lists_the_misfiled_links_and_changes_nothing(runtime, filed):
    plan = runtime.repair_lessons()

    assert plan["links"] == 3
    assert {(m["source_id"], m["reason"]) for m in plan["misfiled"]} == {
        (filed["wrong"].id, "unrelated"), (filed["template"].id, "placeholder"),
    }
    assert plan["removed"] == 0 and plan["backup"] is None
    assert _distilled(runtime, filed["wrong"]) == [filed["grep"].id]


def test_write_removes_only_the_misfiled_links_after_a_backup(runtime, filed):
    plan = runtime.repair_lessons(write=True)

    assert plan["removed"] == 2
    assert Path(plan["backup"]).is_file()
    assert _distilled(runtime, filed["wrong"]) == []
    assert _distilled(runtime, filed["template"]) == []
    assert _distilled(runtime, filed["right"]) == [filed["grep"].id], "a link that holds must stay"
    others = [c.relation for c in runtime._store.get_connections(filed["wrong"].id)]
    assert others == [ConnectionRelation.CO_ACTIVATED], "only the distilled_into link goes"


def test_a_repaired_store_has_nothing_left_to_repair(runtime, filed):
    runtime.repair_lessons(write=True)
    assert runtime.repair_lessons()["misfiled"] == []


def test_an_older_placeholder_with_no_source_recorded_is_not_a_lesson(runtime):
    """Rows written before impact_source existed carry the phrase alone."""
    from mnemos.consolidation.softening import _create_or_reinforce_lesson

    older = _save(runtime, "the working notes for the afternoon",
                  impact="Current working context for continuity.")
    assert _create_or_reinforce_lesson(runtime._store.get_engram(older.id), runtime._store, {}) is None
