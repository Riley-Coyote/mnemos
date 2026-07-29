"""Shift 2: forgetting that teaches.

"Three hours debugging a guard clause" becoming "patience with small things
pays off" is the example the original design used. The loss of detail is the
learning.

Two things had to be true and were not. Softening only ran in deep,
model-gated cycles, so memories never faded at all. And even when a lesson
engram was created, nothing ever linked it back to the experience it came
from — the DISTILLED_INTO edge count was 0 across a 37,000-edge graph.
"""

import pytest

from mnemos.core.types import ConnectionRelation
from mnemos.simple_runtime import MnemosRuntime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = MnemosRuntime(db_path=str(tmp_path / "l.db"), agent_id="demo",
                      use_dedicated_model=False)
    yield r
    r.close()


def _fade(runtime, content):
    """Capture something and let it drop out of easy reach."""
    runtime.capture(content, importance="high")
    engram = runtime._store.get_active_engrams(agent_id="demo", limit=5)[0]
    engram.accessibility = 0.02
    engram.resolution = 1.0
    runtime._store.save_engram(engram)
    # Forgetting takes time; a memory from this session is not fading.
    conn = runtime._store._get_conn()
    conn.execute("UPDATE engrams SET created_at = ? WHERE id = ?",
                 ("2020-01-01T00:00:00+00:00", engram.id))
    conn.commit()
    return engram.id


class TestForgettingTeaches:
    def test_softening_runs_without_a_model(self, runtime):
        """Compression is deterministic; it never needed a provider."""
        _fade(runtime, "Spent three hours on a misplaced guard clause in the handler")

        result = runtime.maintain()

        assert "softening" in result

    def test_a_fading_memory_asks_what_it_taught(self, runtime):
        _fade(runtime, "Spent three hours on a misplaced guard clause in the handler")
        runtime.maintain()

        pending = runtime.pending_reflections()
        assert [p["kind"] for p in pending] == ["lesson"]

    def test_the_lesson_is_linked_back_to_the_experience(self, runtime):
        """The edge is the shift, not the lesson's existence.

        Without it the distillate is an orphan and resonance can never
        travel from a fading memory to what it taught.
        """
        source_id = _fade(runtime, "Spent three hours on a misplaced guard clause")
        runtime.maintain()
        target = runtime.pending_reflections()[0]["target_id"]

        runtime.reflect(target, "Patience with small things pays off.")

        source = runtime._store.get_engram(source_id)
        distilled = [
            c for c in source.connections
            if c.relation == ConnectionRelation.DISTILLED_INTO
        ]
        assert distilled, "the experience was not linked to its lesson"

        lesson = runtime._store.get_engram(distilled[0].target_id)
        assert lesson.content == "Patience with small things pays off."
        assert "lesson" in lesson.tags

    def test_the_server_never_writes_the_lesson_itself(self, runtime):
        """A lesson assembled from keywords is a summary wearing wisdom."""
        _fade(runtime, "Spent three hours on a misplaced guard clause")
        runtime.maintain()

        engram_id = runtime.pending_reflections()[0]["target_id"]
        assert runtime._store.get_engram(engram_id).impact == "", (
            "the server invented a lesson instead of asking for one"
        )


class TestRestraint:
    def test_one_memory_is_not_asked_about_twice(self, runtime):
        """A fading memory also lacks an impact; that is still one question."""
        _fade(runtime, "Spent three hours on a misplaced guard clause")
        runtime.maintain()

        kinds = [p["kind"] for p in runtime.pending_reflections(limit=10)]
        assert len(kinds) == len(set(kinds)) == 1, (
            f"the same memory was queued more than once: {kinds}"
        )
