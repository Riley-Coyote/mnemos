"""Shared test fixtures for Mnemos."""
import os
import tempfile
import pytest


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
        "MNEMOS_WORKSPACE", "MNEMOS_MODE",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
        # A real key would send every captured test memory to the Gemini API.
        "GEMINI_API_KEY", "MNEMOS_EMBEDDING_MODEL",
        # A test run inside a Claude Code session inherits its session id, and
        # signing would then read that real transcript: notes written in the
        # suite would be signed by whatever model is running the developer's
        # session, while CI left them unsigned.
        "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")


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
