"""A fading lesson fades; it does not become a copy of itself.

Softening turns a fading memory's impact into a lesson. A lesson's impact is its
own words, so when a lesson faded it was distilled again, into a second lesson
with the same words and a distilled_into link from the first. Lessons used to be
reinforced on every cycle and so never faded; since #69 they can, and each one
would have doubled.
"""

from __future__ import annotations

import pytest

from mnemos.core.engram import Engram
from mnemos.simple_runtime import MnemosRuntime

TEXT = "Verify the rendered result, not the declaration."


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = MnemosRuntime(db_path=str(tmp_path / "t.db"), agent_id="demo", use_dedicated_model=False)
    r._ensure_init()
    yield r
    r.close()


def test_a_fading_lesson_is_not_distilled_into_a_copy_of_itself(runtime):
    lesson = Engram(content=TEXT, impact=TEXT, kind="procedural", tags=["lesson", "distilled"],
                    owner_agent_id="demo", person_id=runtime.scope.person_id,
                    project_scope=runtime.scope.project_scope, accessibility=0.02, resolution=1.0)
    runtime._store.save_engram(lesson)
    conn = runtime._store._get_conn()
    conn.execute("UPDATE engrams SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", lesson.id))
    conn.commit()

    runtime.maintain()
    runtime.maintain()

    copies = conn.execute("SELECT count(*) FROM engrams WHERE content = ?", (TEXT,)).fetchone()[0]
    assert copies == 1
