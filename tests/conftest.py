"""Shared test fixtures for Mnemos."""
import os
import tempfile
import pytest

# mcp's stdio_client takes the server's stderr as ``errlog=sys.stderr``, a
# default bound when mcp is first imported. Imported first inside a test that
# uses capsys, it bound pytest's in-memory capture, which has no file
# descriptor, and every later test that starts a server over stdio failed on
# fileno() (test_watchdog.py before test_min_code_version.py). Imported here,
# while pytest's own capture is in place, it binds the same stream whatever
# order the tests run in.
try:
    import mcp.client.stdio  # noqa: F401
except ImportError:  # pragma: no cover - mcp is a core dependency
    pass


@pytest.fixture(autouse=True)
def _isolate_mnemos_env(monkeypatch, tmp_path_factory):
    """No developer's real environment bleeds into tests.

    MNEMOS_DISABLE_DOTENV stops llm._load_env_key (and the OpenClaw key
    lookup) from reading workspace .env files; the MNEMOS_*/provider
    variables are cleared so every test starts from a clean slate and
    sets exactly what it needs via monkeypatch.setenv.

    And every test has a home of its own. With the developer's, whatever
    resolved a default path wrote into the real ~/.mnemos: a store named
    audit.db (bootstrap with no store path), the scheduler's log folder, and
    the cue's shown files. A test that needs a particular home sets its own.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # where Windows looks
    for var in (
        "MNEMOS_LLM_PROVIDER", "MNEMOS_MODEL", "MNEMOS_AGENT_MODEL",
        "MNEMOS_SUBSTRATE_AFFINITY", "MNEMOS_AGENT_ID", "MNEMOS_PERSON_ID",
        "MNEMOS_PROJECT_SCOPE", "MNEMOS_DB_PATH", "MNEMOS_ENV_PATHS",
        "MNEMOS_WORKSPACE", "MNEMOS_MODE", "MNEMOS_PERSON_NAME",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
        # A real key would send every captured test memory to the Gemini API.
        "GEMINI_API_KEY", "MNEMOS_EMBEDDING_MODEL",
        # A test run inside a Claude Code session inherits its session id, and
        # signing would then read that real transcript: notes written in the
        # suite would be signed by whatever model is running the developer's
        # session, while CI left them unsigned.
        "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR",
        # Claude Code's own pid, which the prompt hook uses to find its
        # session's answerer: a test run inside a session would otherwise
        # look for that session's server.
        "CLAUDE_PID",
        # The cue's judge: a developer who switched it on would otherwise
        # send test messages to Jev.
        "MNEMOS_CUE_JUDGE",
        # A suite run from inside one of the agent's quiet hours would
        # otherwise write every journal entry as written between sessions, and
        # refuse every reply (R22): the hour belongs to the process the hour's
        # runner woke, not to the tests.
        "MNEMOS_HOUR_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_DOTENV", "1")
    # And no test reaches a real Jev key (~/.config/jev/api_key): an empty
    # file is no key. A test that needs one names its own.
    monkeypatch.setenv("MNEMOS_JEV_KEY_FILE", os.devnull)


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
