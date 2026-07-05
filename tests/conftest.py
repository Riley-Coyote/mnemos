"""Shared test fixtures for Mnemos."""

import os
import pathlib
import pytest


# The live-memory paths a test must NEVER open. On 2026-07-04 a watchdog test,
# after the vault watchdog stopped honoring MNEMOS_DB_PATH (008m Addition 1),
# opened David's real ~/.mnemos/memory.db — migrating its schema and
# quarantining 19 identity beliefs. This guard makes that class of accident
# impossible: any EngramStore construction against a live-memory path raises.
_LIVE_MNEMOS_PATHS = {
    str(pathlib.Path(os.path.expanduser("~/.mnemos/memory.db")).resolve()),
    "/Users/davidef/.mnemos/memory.db",
}


@pytest.fixture(autouse=True)
def _forbid_live_mnemos_db(monkeypatch):
    """Autouse, session-wide: no test may open a live ~/.mnemos DB.

    Wraps EngramStore.__init__ to reject the canonical live-memory paths.
    Defense-in-depth over per-test path discipline — the guard is inherited
    by EVERY test, so a future script/test that resolves a live path fails
    loudly instead of mutating David's real memory.
    """
    from mnemos.store import sqlite_store

    real_init = sqlite_store.EngramStore.__init__

    def guarded_init(self, db_path, *args, **kwargs):
        try:
            resolved = str(pathlib.Path(db_path).expanduser().resolve())
        except (OSError, ValueError, TypeError):
            resolved = str(db_path)
        if resolved in _LIVE_MNEMOS_PATHS or str(db_path) in _LIVE_MNEMOS_PATHS:
            raise RuntimeError(
                f"TEST GUARD: refusing to open the live Mnemos DB ({db_path}). "
                "Tests must use tmp_path / injected paths only (008m incident)."
            )
        return real_init(self, db_path, *args, **kwargs)

    monkeypatch.setattr(sqlite_store.EngramStore, "__init__", guarded_init)


@pytest.fixture(autouse=True)
def _isolate_mnemos_env(monkeypatch, tmp_path):
    """No developer's real environment bleeds into tests.

    MNEMOS_DISABLE_DOTENV stops llm._load_env_key (and the OpenClaw key
    lookup) from reading workspace .env files; the MNEMOS_*/provider
    variables are cleared so every test starts from a clean slate and
    sets exactly what it needs via monkeypatch.setenv. Vault alert output is
    also redirected to a per-test temp dir by default, so tests cannot write
    session-start/watchdog alerts into a real Oliver Inbox unless they
    explicitly override the alert dir.
    """
    for var in (
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_MODEL",
        "MNEMOS_AGENT_MODEL",
        "MNEMOS_SUBSTRATE_AFFINITY",
        "MNEMOS_AGENT_ID",
        "MNEMOS_PERSON_ID",
        "MNEMOS_PROJECT_SCOPE",
        "MNEMOS_DB_PATH",
        "MNEMOS_ENV_PATHS",
        "MNEMOS_WORKSPACE",
        "MNEMOS_MODE",
        "MNEMOS_VAULT_JOURNAL",
        "MNEMOS_WATCHDOG_ALERT_DIR",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    # The vault's session-start / watchdog alert path (mnemos.mcp_server) defaults
    # to the developer's REAL ~/Oliver Inbox when MNEMOS_WATCHDOG_ALERT_DIR is
    # unset. A test that triggers a session-start alert — reconcile raising, or a
    # stale monkeypatch returning a non-ReconcileReport — would then write a
    # vault-alert file straight into the real inbox: a passing test with an
    # invisible real-filesystem side effect. Redirect every test's alerts to a
    # per-test tmp dir so no test can ever leak. (Tests needing to read the alert
    # still monkeypatch.setenv their own dir; this only changes the default.)
    monkeypatch.setenv("MNEMOS_WATCHDOG_ALERT_DIR", str(tmp_path / "vault-alerts"))
    # T4 vault (008r-review): the resolver reads NO env — default every test
    # store INERT by pointing the resolution seam's dir at a path that does not
    # exist, so no test arms against a post-ceremony system vault by accident.
    # Tests that need the gate armed use _arm_vault() or pass vault_active=True.
    from mnemos.store import sqlite_store

    monkeypatch.setattr(
        sqlite_store,
        "_VAULT_DIR_FOR_RESOLUTION",
        "/nonexistent/mnemos-vault-test-inert",
    )
    # T5 008y R6-1: the journal-file ownership check is DISABLED by default in
    # tests — a test's tmp journal is agent-owned but legitimate, and reconcile
    # against it must NOT false-quarantine ("valid case" per the ruling). The
    # dedicated R6-1 test re-enables it (monkeypatch True) to exercise the real
    # ownership predicate against an agent-owned fixture. Production leaves it
    # True (the shipped journal is root-owned; an agent-owned leaf is the hazard).
    monkeypatch.setattr(sqlite_store, "_JOURNAL_TRUST_CHECK_ENABLED", False)


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def store(tmp_db):
    """Create a temporary EngramStore."""
    from mnemos.store.sqlite_store import EngramStore

    s = EngramStore(tmp_db)
    yield s
    s.close()


@pytest.fixture
def encoder(store):
    """Create an Encoder with no LLM (rule-based fallback)."""
    from mnemos.encoding.encoder import Encoder

    return Encoder(store, llm_client=None)


@pytest.fixture
def retriever(store):
    """Create a ReactiveRetriever."""
    from mnemos.retrieval.reactive import ReactiveRetriever

    return ReactiveRetriever(store)
