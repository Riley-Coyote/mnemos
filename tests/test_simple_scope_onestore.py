"""One-store scope resolution: resolve_scope + detect_sibling_stores.

Regression suite for the shadow-store routing bug (2026-07-06/07): the
resolver treated the canonical default path as a "not configured" sentinel
and fell through to ~/.mnemos/{agent}.db, silently forking a sibling store
that nothing reads. Identity writes (the David-bond first mint) landed there.

Every assertion class carries its mutation: the sentinel tests below go RED
under the pre-fix resolver, and the sibling detector is tested both firing
and clean.
"""

import argparse
import json

import pytest

from mnemos.simple_scope import MnemosScope, detect_sibling_stores, resolve_scope


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate HOME so load_config reads a controlled ~/.mnemos/config.json."""
    home = tmp_path / "home"
    (home / ".mnemos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Never let the real session's env leak into resolution.
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_STORE_DB_PATH", raising=False)
    monkeypatch.delenv("MNEMOS_AGENT_ID", raising=False)
    return home


def _write_config(home, config: dict) -> None:
    (home / ".mnemos" / "config.json").write_text(json.dumps(config))


# ── resolve_scope: one store, no sentinel ──


def test_default_resolves_canonical_not_per_agent(fake_home):
    """No config file at all → canonical memory.db, never {agent}.db.

    Mutation proof: the pre-fix resolver returned ~/.mnemos/oliver.db here.
    """
    scope = resolve_scope(agent_id="oliver")
    assert scope.db_path == "~/.mnemos/memory.db"


def test_configured_canonical_path_is_honored_not_treated_as_sentinel(fake_home):
    """Explicitly configuring the canonical path must be honored.

    This is THE bug: the pre-fix resolver compared the configured value
    against the literal "~/.mnemos/memory.db" and, on match, REPLACED it
    with ~/.mnemos/{agent}.db — configuring the canonical store was
    indistinguishable from not configuring at all.
    """
    _write_config(fake_home, {"agent_id": "oliver", "store": {"db_path": "~/.mnemos/memory.db"}})
    scope = resolve_scope()
    assert scope.agent_id == "oliver"
    assert scope.db_path == "~/.mnemos/memory.db"


def test_configured_custom_path_is_honored(fake_home):
    """A deliberately configured per-agent (or any) path still wins."""
    _write_config(fake_home, {"store": {"db_path": "/tmp/elsewhere/custom.db"}})
    scope = resolve_scope(agent_id="oliver")
    assert scope.db_path == "/tmp/elsewhere/custom.db"


def test_explicit_arg_beats_env_and_config(fake_home, monkeypatch):
    _write_config(fake_home, {"store": {"db_path": "/tmp/config.db"}})
    monkeypatch.setenv("MNEMOS_DB_PATH", "/tmp/env.db")
    scope = resolve_scope(db_path="/tmp/explicit.db", agent_id="oliver")
    assert scope.db_path == "/tmp/explicit.db"


def test_env_beats_config(fake_home, monkeypatch):
    _write_config(fake_home, {"store": {"db_path": "/tmp/config.db"}})
    monkeypatch.setenv("MNEMOS_DB_PATH", "/tmp/env.db")
    scope = resolve_scope(agent_id="oliver")
    assert scope.db_path == "/tmp/env.db"


def test_cli_store_helper_resolves_store_db_path_env(fake_home, monkeypatch):
    configured = fake_home / ".mnemos" / "configured.db"
    default = fake_home / ".mnemos" / "memory.db"
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured))

    from mnemos.cli import _get_store, _resolve_db_path

    args = argparse.Namespace(
        db_path=None,
        agent_id="nova",
        person_id=None,
        project_scope=None,
    )
    assert _resolve_db_path(args) == str(configured)

    store = _get_store(args)
    store.close()

    assert configured.exists()
    assert not default.exists()


def test_index_cli_resolves_store_db_path_env(fake_home, monkeypatch, capsys):
    configured = fake_home / ".mnemos" / "index.db"
    seen = {}
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured))

    class StubIndexer:
        def __init__(self, *, agent_id, db_path):
            seen["agent_id"] = agent_id
            seen["db_path"] = db_path

        def run(self):
            return {"sessions_processed": 0, "memories_created": 0}

    import mnemos.indexer.session_indexer as session_indexer
    from mnemos.cli import main

    monkeypatch.setattr(session_indexer, "SessionIndexer", StubIndexer)

    result = main(["index"])
    out = capsys.readouterr().out

    assert result == 0
    assert seen == {"agent_id": "default", "db_path": str(configured)}
    assert "Indexed 0 sessions, 0 memories created" in out


def test_agent_id_never_reaches_db_path(fake_home):
    """No agent id, under any spelling, may leak into the resolved path."""
    for agent in ("oliver", "nova", "some-other-agent"):
        scope = resolve_scope(agent_id=agent)
        assert agent not in scope.db_path


# ── detect_sibling_stores: the doctor's one-store check ──


def test_sibling_store_detected(fake_home):
    """A per-agent DB beside the canonical store is flagged."""
    canonical = fake_home / ".mnemos" / "memory.db"
    canonical.write_bytes(b"")
    sibling = fake_home / ".mnemos" / "oliver.db"
    sibling.write_bytes(b"")

    scope = MnemosScope(
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        db_path="~/.mnemos/memory.db",
    )
    found = detect_sibling_stores(scope)
    assert found == [str(sibling)]


def test_no_sibling_is_clean(fake_home):
    """Mutation twin of the detection test: absent sibling → empty result."""
    (fake_home / ".mnemos" / "memory.db").write_bytes(b"")
    scope = MnemosScope(
        agent_id="oliver",
        person_id="david",
        project_scope="pai",
        db_path="~/.mnemos/memory.db",
    )
    assert detect_sibling_stores(scope) == []


def test_deliberate_per_agent_store_is_not_its_own_sibling(fake_home):
    """A configured per-agent store resolves TO {agent}.db — no false alarm."""
    per_agent = fake_home / ".mnemos" / "nova.db"
    per_agent.write_bytes(b"")
    scope = MnemosScope(
        agent_id="nova",
        person_id="riley",
        project_scope="demo",
        db_path=str(per_agent),
    )
    assert detect_sibling_stores(scope) == []


# ── doctor wiring: the FAIL path at the CLI ──


def test_doctor_fails_on_sibling_store(fake_home, capsys):
    """Doctor exits 1 and names the sibling when a shadow store exists."""
    from mnemos.cli import main

    canonical = fake_home / ".mnemos" / "memory.db"
    sibling = fake_home / ".mnemos" / "shadowed.db"
    sibling.write_bytes(b"")

    result = main(
        [
            "doctor",
            "--db-path",
            str(canonical),
            "--agent-id",
            "shadowed",
            "--person-id",
            "riley",
            "--project-scope",
            "demo",
        ]
    )
    out = capsys.readouterr().out
    assert result == 1
    assert f"FAIL: sibling store detected: {sibling}" in out


def test_doctor_clean_reports_one_store_ok(fake_home, capsys):
    """Mutation twin: no sibling → doctor exits 0 and says so."""
    from mnemos.cli import main

    result = main(
        [
            "doctor",
            "--db-path",
            str(fake_home / ".mnemos" / "memory.db"),
            "--agent-id",
            "nova",
            "--person-id",
            "riley",
            "--project-scope",
            "demo",
        ]
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "One store:    ok" in out
