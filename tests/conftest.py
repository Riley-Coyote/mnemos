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
def _isolate_mnemos_env(monkeypatch):
    """No developer's real environment bleeds into tests.

    MNEMOS_DISABLE_DOTENV stops llm._load_env_key (and the OpenClaw key
    lookup) from reading workspace .env files; the MNEMOS_*/provider
    variables are cleared so every test starts from a clean slate and
    sets exactly what it needs via monkeypatch.setenv.
    """
    for var in (
        "MNEMOS_LLM_PROVIDER", "MNEMOS_MODEL", "MNEMOS_AGENT_MODEL",
        "MNEMOS_SUBSTRATE_AFFINITY", "MNEMOS_AGENT_ID", "MNEMOS_PERSON_ID",
        "MNEMOS_PROJECT_SCOPE", "MNEMOS_DB_PATH", "MNEMOS_ENV_PATHS",
        "MNEMOS_WORKSPACE", "MNEMOS_MODE", "MNEMOS_VAULT_JOURNAL",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    # T4 vault (008r-review): the resolver reads NO env — default every test
    # store INERT by pointing the resolution seam's dir at a path that does not
    # exist, so no test arms against a post-ceremony system vault by accident.
    # Tests that need the gate armed use _arm_vault() or pass vault_active=True.
    from mnemos.store import sqlite_store
    monkeypatch.setattr(
        sqlite_store, "_VAULT_DIR_FOR_RESOLUTION",
        "/nonexistent/mnemos-vault-test-inert",
    )


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
