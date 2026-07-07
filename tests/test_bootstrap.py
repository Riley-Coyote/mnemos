"""Tests for turnkey bootstrap setup."""

from mnemos.setup.bootstrap import bootstrap
from mnemos.store.sqlite_store import EngramStore


def test_bootstrap_seeds_foundational_hypomnema_for_review(tmp_path):
    db_path = tmp_path / "memory.db"

    bootstrap(
        agent_name="Nova",
        workspace=str(tmp_path / "workspace"),
        user_name="Riley",
        db_path=str(db_path),
        agent_id="nova",
    )

    store = EngramStore(db_path)
    try:
        operational = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova",
            person_id="riley",
            project_scope="global",
            read_visibility="operational_context",
        )
        review_only = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova",
            person_id="riley",
            project_scope="global",
            read_visibility="review_only",
        )
    finally:
        store.close()

    assert operational == []
    assert review_only


def test_bootstrap_default_resolves_configured_canonical_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    configured_db = tmp_path / "canonical" / "memory.db"
    default_db = home / ".mnemos" / "memory.db"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured_db))
    monkeypatch.delenv("MNEMOS_DB_PATH", raising=False)

    result = bootstrap(
        agent_name="Nova",
        workspace=str(workspace),
        user_name="Riley",
        agent_id="nova",
    )

    assert result["errors"] == []
    assert result["db_path"] == str(configured_db)
    assert configured_db.exists()
    assert not default_db.exists()
    assert f"MNEMOS_DB_PATH={configured_db}" in (workspace / ".env").read_text()

    store = EngramStore(configured_db)
    try:
        review_only = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova",
            person_id="riley",
            project_scope="global",
            read_visibility="review_only",
        )
    finally:
        store.close()

    assert review_only


def test_bootstrap_cli_forwards_global_db_path_and_agent_id(
    tmp_path, monkeypatch, capsys
):
    from mnemos.cli import main

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    explicit_db = tmp_path / "explicit" / "memory.db"
    configured_db = tmp_path / "configured" / "memory.db"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MNEMOS_STORE_DB_PATH", str(configured_db))

    result = main(
        [
            "--db-path",
            str(explicit_db),
            "--agent-id",
            "nova-cli",
            "bootstrap",
            "--agent-name",
            "Nova CLI",
            "--workspace",
            str(workspace),
            "--user-name",
            "Riley",
        ]
    )
    capsys.readouterr()

    assert result == 0
    assert explicit_db.exists()
    assert not configured_db.exists()
    assert f"MNEMOS_DB_PATH={explicit_db}" in (workspace / ".env").read_text()
    assert "MNEMOS_AGENT_ID=nova-cli" in (workspace / ".env").read_text()

    store = EngramStore(explicit_db)
    try:
        review_only = store.search_hypomnema(
            "bootstrapped primary memory-bearing agent",
            agent_id="nova-cli",
            person_id="riley",
            project_scope="global",
            read_visibility="review_only",
        )
    finally:
        store.close()

    assert review_only
