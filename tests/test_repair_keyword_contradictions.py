"""`mnemos repair keyword-contradictions` undoes what the removed check wrote.

Without a model, encoding lowered a belief by 0.05 and linked the new memory
as contradicting the belief's evidence whenever the two shared a word of four
or more characters and the memory held a negation anywhere ("not" also
matched "note"). The check is gone; what it wrote is still in every store it
ran on. These tests write exactly what it wrote, then repair it.

The model path writes links of exactly the same shape, so a link is taken as
the check's only when its note shows it was saved without a model.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mnemos.cli import main
from mnemos.core.belief import Belief
from mnemos.core.engram import Connection
from mnemos.simple_runtime import MnemosRuntime

SCOPE = {"agent_id": "nova", "person_id": "riley", "project_scope": "demo"}
ARGS = ["--agent-id", "nova", "--person-id", "riley", "--project-scope", "demo"]
RESTORED = "Restored by mnemos repair keyword-contradictions: "


def _runtime(db) -> MnemosRuntime:
    return MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)


def _all(db, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _settled_sha256(path: Path) -> str:
    """Hash the store with everything written so far folded into the file."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _contradictions(db) -> list[tuple]:
    return _all(
        db,
        "SELECT source_id, target_id, strength, formed_by FROM connections "
        "WHERE relation = 'contradicts' ORDER BY source_id, target_id",
    )


def _links_of(db, source: str) -> list[tuple]:
    return _all(
        db,
        "SELECT target_id, relation, formed_by FROM connections WHERE source_id = ? "
        "ORDER BY target_id, relation",
        (source,),
    )


def _belief_row(db, belief_id: str) -> tuple[float, list[dict]]:
    [(confidence, history)] = _all(
        db, "SELECT confidence, revision_history FROM beliefs WHERE id = ?", (belief_id,)
    )
    return confidence, json.loads(history)


def _keyword_check(store, capture, belief, *, revise: bool = True) -> None:
    """Write what the removed check wrote for one capture and one belief.

    A CONTRADICTS link from the capture to each of the belief's first three
    supporting memories, formed at encoding at strength 0.7, and, outside its
    six-hour cooldown, a revision lowering the belief by 0.05 whose reason is
    "Contradicted by new evidence: " and the capture's first 50 characters.
    """
    for supporting_id in belief.supporting_engram_ids[:3]:
        store.save_connection(
            capture.id,
            Connection(
                target_id=supporting_id, relation="contradicts", strength=0.7,
                formed_by="encoding",
            ),
        )
    if revise:
        belief.revise(
            belief.confidence - 0.05,
            f"Contradicted by new evidence: {capture.content[:50]}...",
            trigger_engram_id=capture.id,
        )
        store.save_belief(belief)


def _no_model_link(store, capture, target_id: str) -> None:
    """What encoding without a model writes when two notes share enough words."""
    store.save_connection(
        capture.id,
        Connection(
            target_id=target_id, relation="co_activated", strength=0.3,
            formed_by="encoding_no_llm",
        ),
    )


class _ModelSaysItContradicts:
    """A configured model that judges every belief contradicted."""

    def structured_complete(self, *, system, user, temperature, max_tokens):
        if "## Active Beliefs" not in user:
            return "[]"
        ids = [line.split(":")[0].split()[-1] for line in user.splitlines()
               if line.startswith("### Belief ")]
        return json.dumps([
            {"belief_id": belief_id, "relation": "CONTRADICTS", "impact": 0.6,
             "reasoning": "the model weighed it"}
            for belief_id in ids
        ])


def _damaged_store(tmp_path) -> dict:
    """A store the check ran on, beside writes it must leave alone.

    - "keyword": saved without a model (a no-model link), checked and revised;
    - "cooled": saved without a model, checked inside the cooldown, so no
      revision;
    - "revised": no link of its own, but a revision of the check names it;
    - "unclear": the check's shape, and nothing to say how it was saved;
    - "model": weighed by a configured model, through the real model path.
    """
    db = tmp_path / "memory.db"
    rt = _runtime(db)
    rt._ensure_init()
    store = rt._store
    from mnemos.encoding.encoder import Encoder

    ids = {}
    for name, content in (
        ("anchor", "Riley reviews every deploy checklist before shipping."),
        ("neighbour", "The deploy checklist lives in the release wiki."),
        ("keyword", "Riley did not review the deploy checklist this week."),
        ("cooled", "A note on the deploy checklist Riley reviews on Fridays."),
    ):
        said = rt.capture(content, impact="Kept for the test.")
        ids[name] = said.split("Memory ID: ")[1].split()[0]
    # Saved with no links of their own, so nothing but what each case adds
    # says how they were saved.
    bare = Encoder(store, llm_client=None)
    for name, content in (
        ("revised", "Riley never skipped the deploy checklist review."),
        ("unclear", "The deploy checklist was not reviewed before the hotfix."),
    ):
        ids[name] = bare.encode(
            content=content, agent_id="nova", person_id="riley", project_scope="demo",
            discover_connections=False, skip_surprise_detection=True,
        ).id
    belief = Belief(
        agent_id="nova", content="Riley reviews every deploy checklist", confidence=0.4,
        source="agent", supporting_engram_ids=[ids["anchor"]],
        created_at=_days_ago(3), last_revised=_days_ago(3), last_challenged=_days_ago(3),
    )
    store.save_belief(belief)
    engram = {name: store.get_engram(engram_id) for name, engram_id in ids.items()}

    _no_model_link(store, engram["keyword"], ids["neighbour"])
    _keyword_check(store, engram["keyword"], belief)
    _no_model_link(store, engram["cooled"], ids["neighbour"])
    _keyword_check(store, engram["cooled"], belief, revise=False)
    _keyword_check(store, engram["revised"], belief)
    _keyword_check(store, engram["unclear"], belief, revise=False)

    # Other reasons stand: an agent held to it, and a model weighed a note.
    belief.revise(belief.confidence + 0.05, "reaffirmed by the agent: still true")
    belief.last_revised = _days_ago(1)  # past the model path's cooldown
    store.save_belief(belief)
    weighed = Encoder(store, llm_client=_ModelSaysItContradicts()).encode(
        content="The deploy checklist was skipped for the demo build.",
        agent_id="nova", person_id="riley", project_scope="demo",
    )
    ids["model"] = weighed.id
    # A link a model classified, not of the check's shape.
    store.save_connection(
        weighed.id,
        Connection(target_id=ids["neighbour"], relation="contradicts", strength=0.95,
                   formed_by="encoding"),
    )
    rt.close()
    return {"db": db, "ids": ids, "belief_id": belief.id}


def test_the_dry_run_finds_the_checks_writes_and_changes_nothing(tmp_path, capsys):
    case = _damaged_store(tmp_path)
    db, ids = case["db"], case["ids"]
    before = _settled_sha256(db)

    code = main(["repair", "keyword-contradictions", "--db-path", str(db), *ARGS])
    out = capsys.readouterr().out
    rt = _runtime(db)
    try:
        plan = rt.repair_keyword_contradictions()
    finally:
        rt.close()

    assert code == 0
    assert _settled_sha256(db) == before, "the dry run changed the store"
    found = plan["found"]
    assert sorted(link["source_id"] for link in found["links"]) == sorted(
        [ids["keyword"], ids["cooled"], ids["revised"]]
    )
    assert [link["source_id"] for link in found["model_links"]] == [ids["model"]]
    assert [link["source_id"] for link in found["ambiguous_links"]] == [ids["unclear"]]
    assert found["other_links"] == 1
    [belief] = found["beliefs"]
    # 0.40, two of the check's -0.05, the agent's +0.05, the model's -0.024.
    assert belief["revisions"] == 2
    assert belief["before"] == pytest.approx(0.326)
    assert belief["after"] == pytest.approx(0.426)
    assert "Dry run: nothing changed." in out
    assert "0.33 -> 0.43" in out


def test_the_write_undoes_the_check_and_leaves_everything_else(tmp_path, capsys):
    case = _damaged_store(tmp_path)
    db, ids = case["db"], case["ids"]
    _, history_before = _belief_row(db, case["belief_id"])
    keyword_links = _links_of(db, ids["keyword"])

    code = main(["repair", "keyword-contradictions", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Removed 3 links and restored 1 beliefs." in out
    remaining = _contradictions(db)
    assert remaining == sorted([
        (ids["model"], ids["anchor"], 0.7, "encoding"),
        (ids["model"], ids["neighbour"], 0.95, "encoding"),
        (ids["unclear"], ids["anchor"], 0.7, "encoding"),
    ])
    # Only the check's link went from a note it wrote on.
    assert _links_of(db, ids["keyword"]) == [
        link for link in keyword_links if link[1] != "contradicts"
    ]
    confidence, history = _belief_row(db, case["belief_id"])
    assert confidence == pytest.approx(0.426)
    assert history[:-1] == history_before, "history was changed, not added to"
    restored = history[-1]
    assert restored["reason"].startswith(RESTORED)
    assert "undid 2 revisions (-0.10 in all)" in restored["reason"]
    assert (restored["old_confidence"], restored["new_confidence"]) == (
        pytest.approx(0.326), pytest.approx(0.426),
    )
    [backup] = (tmp_path / "backups").glob("memory.pre-repair-keyword-contradictions-*.db")
    assert _all(backup, "PRAGMA integrity_check") == [("ok",)]
    assert len(_all(backup, "SELECT 1 FROM connections WHERE relation = 'contradicts'")) == 6


def test_a_second_run_finds_nothing(tmp_path, capsys):
    case = _damaged_store(tmp_path)
    db = case["db"]
    assert main(["repair", "keyword-contradictions", "--db-path", str(db), *ARGS, "--write"]) == 0
    capsys.readouterr()
    after_first = _settled_sha256(db)

    rt = _runtime(db)
    try:
        again = rt.repair_keyword_contradictions()["found"]
    finally:
        rt.close()
    code = main(["repair", "keyword-contradictions", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert (again["links"], again["beliefs"], again["revisions"]) == ([], [], 0)
    assert code == 0 and "Nothing to repair." in out
    assert _settled_sha256(db) == after_first, "a second run changed the store"
    assert len(list((tmp_path / "backups").glob("memory.pre-repair-keyword-contradictions-*.db"))) == 1


def test_a_model_path_link_is_left_alone(tmp_path):
    case = _damaged_store(tmp_path)
    db, ids = case["db"], case["ids"]
    # Premise: the fake model went through the real model path, which wrote a
    # link of the check's exact shape and a revision of its own.
    assert (ids["model"], ids["anchor"], 0.7, "encoding") in _contradictions(db)
    _, history = _belief_row(db, case["belief_id"])
    assert history[-1]["reason"].startswith("Contradicted by new evidence (impact 0.60)")
    assert history[-1]["trigger_engram_id"] == ids["model"]
    model_links = _links_of(db, ids["model"])
    rt = _runtime(db)
    try:
        plan = rt.repair_keyword_contradictions(write=True)
    finally:
        rt.close()

    assert plan["removed"] == 3
    assert _links_of(db, ids["model"]) == model_links
    _, history = _belief_row(db, case["belief_id"])
    model_revisions = [r for r in history if r["reason"].startswith("Contradicted by new evidence (impact")]
    assert len(model_revisions) == 1, "the model's revision is kept"
    assert model_revisions[0]["new_confidence"] == pytest.approx(0.426 - 0.1)


def test_a_retired_belief_keeps_its_confidence(tmp_path):
    case = _damaged_store(tmp_path)
    db = case["db"]
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE beliefs SET superseded_by = 'retired' WHERE id = ?", (case["belief_id"],))
        conn.commit()
    finally:
        conn.close()
    confidence, history = _belief_row(db, case["belief_id"])
    rt = _runtime(db)
    try:
        plan = rt.repair_keyword_contradictions(write=True)
    finally:
        rt.close()

    assert plan["found"]["retired_revisions"] == 2 and plan["restored"] == 0
    assert _belief_row(db, case["belief_id"]) == (confidence, history)
    assert plan["removed"] == 3, "the check's links go whatever became of the belief"


def test_older_code_changes_nothing(tmp_path, capsys):
    case = _damaged_store(tmp_path)
    db = case["db"]
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('min_code_version', '999')")
        conn.commit()
    finally:
        conn.close()
    before = _settled_sha256(db)

    code = main(["repair", "keyword-contradictions", "--db-path", str(db), *ARGS, "--write"])
    out = capsys.readouterr().out

    assert code == 1
    assert "older than the store expects, so it changes nothing" in out
    assert _settled_sha256(db) == before
    assert not (tmp_path / "backups").exists() or not list(
        (tmp_path / "backups").glob("*keyword-contradictions*")
    )
