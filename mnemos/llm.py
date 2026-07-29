"""
LLM client abstraction for Mnemos.

Provides a simple protocol that any LLM provider can implement.
Mnemos uses LLM calls for:
- Softening (rewriting memories at lower resolution)
- Impact extraction (distilling the lasting insight from an experience)
- Thought generation (synthesizing patterns from recent memories)

Three implementations:
- AnthropicClient: Uses the Anthropic SDK (Claude models)
- OpenRouterClient: Uses OpenRouter API (any model)
- MockClient: Returns canned responses (for testing)

Auto-detection: create_client() checks env vars and returns the
appropriate client, or None if no API keys are found.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger("mnemos.llm")


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM clients.

    complete(prompt) -> str: simple single-prompt call.
    structured_complete(system, user, temperature, max_tokens) -> str:
        system+user prompt with temperature control for classification tasks.
    """

    def complete(self, prompt: str) -> str: ...

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str: ...


class AnthropicClient:
    """LLM client using the Anthropic SDK (Claude).

    Requires: pip install anthropic
    Env var: ANTHROPIC_API_KEY
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 500,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def complete(self, prompt: str) -> str:
        """Send a prompt to Claude and return the response text."""
        client = self._get_client()
        response = client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str:
        """Send a system+user prompt with temperature control.

        Used by the LLM classifier for deterministic, structured output.
        Supports JSON-heavy responses with higher token limits.
        """
        client = self._get_client()
        response = client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class ClaudeCLIClient:
    """LLM client that routes through the local ``claude`` CLI.

    Uses Claude Code subscription auth (no API key, no per-token API billing).
    Each call spawns a one-shot ``claude -p`` process, so it is slower than the
    API and meant for background work (consolidation / reflection), not hot
    paths. Select with ``MNEMOS_LLM_PROVIDER=claude-cli``.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        claude_bin: str | None = None,
        timeout: int = 120,
    ) -> None:
        import shutil

        self._model = model
        self._bin = (
            claude_bin
            or os.environ.get("CLAUDE_BIN")
            or shutil.which("claude")
            or os.path.expanduser("~/.local/bin/claude")
        )
        self._timeout = timeout

    def _run(self, prompt: str) -> str:
        import subprocess

        try:
            result = subprocess.run(
                [
                    self._bin,
                    "--model", self._model,
                    "--dangerously-skip-permissions",
                    "-p", prompt,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            return (result.stdout or "").strip()
        except Exception:
            return ""

    def complete(self, prompt: str) -> str:
        return self._run(prompt)

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str:
        return self._run(f"{system}\n\n{user}")


class OpenRouterClient:
    """LLM client using the OpenRouter API (any model).

    Requires: pip install httpx
    Env var: OPENROUTER_API_KEY
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "anthropic/claude-sonnet-4-6",
        max_tokens: int = 500,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        """Send a prompt via OpenRouter and return the response text."""
        import json
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str:
        """Send a system+user prompt with temperature control."""
        import json
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


class MockClient:
    """Mock LLM client for testing. Returns simple distilled responses."""

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str:
        """Mock structured completion — returns empty JSON array."""
        return "[]"

    def complete(self, prompt: str) -> str:
        """Return a mock response based on the prompt type."""
        prompt_lower = prompt.lower()

        if "lasting insight" in prompt_lower or "one lasting" in prompt_lower:
            # Impact extraction
            return "A lesson learned through experience."

        if "soften" in prompt_lower or "lower resolution" in prompt_lower:
            # Softening
            return "A memory of something that mattered, details softened by time."

        if "emotional essence" in prompt_lower or "impression" in prompt_lower:
            # Deep softening
            return "The feeling of understanding arriving."

        if "thoughts" in prompt_lower or "patterns" in prompt_lower:
            # Thought generation
            return "Patterns emerge when you stop looking for them directly."

        if "self-narrative" in prompt_lower or "who you are" in prompt_lower:
            # Narrative (legacy — graph identity replaces this)
            return "An entity learning through traces of impact."

        if "stress-test" in prompt_lower or "counterargument" in prompt_lower:
            # Belief challenge
            return (
                "CHALLENGE: This assumption may not hold in all contexts.\n"
                "ASSESSMENT: MAINTAIN\n"
                "CONFIDENCE_DELTA: -0.02\n"
                "REASONING: The belief is generally sound but could use nuance."
            )

        return "A considered response."


def _dotenv_disabled() -> bool:
    return os.environ.get("MNEMOS_DISABLE_DOTENV", "").strip().lower() in (
        "1", "true", "yes",
    )


def _env_search_paths() -> list:
    """Filesystem locations to search for .env files.

    MNEMOS_ENV_PATHS (colon-separated paths) takes precedence; otherwise
    the working directory's .env (typically the agent workspace) and the
    shared ~/.mnemos/.env are checked.
    """
    from pathlib import Path

    raw = os.environ.get("MNEMOS_ENV_PATHS", "").strip()
    if raw:
        return [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    return [
        Path.cwd() / ".env",
        Path.home() / ".mnemos" / ".env",
    ]


def _load_env_key(key_name: str) -> str:
    """Try to find an API key from env vars, then from .env files."""
    # 1. Environment variable
    val = os.environ.get(key_name, "").strip()
    if val:
        return val

    # 2. .env files, unless ambient config reads are disabled
    #    (test isolation, hermetic deployments)
    if _dotenv_disabled():
        return ""

    for env_path in _env_search_paths():
        if env_path.exists():
            try:
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith(f"{key_name}="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except OSError:
                continue

    return ""



def _load_openclaw_openrouter_key() -> str:
    """Try to find OpenRouter API key from OpenClaw config."""
    from pathlib import Path
    import json
    if _dotenv_disabled():
        # Same ambient-machine-state class as .env files.
        return ""
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_config.exists():
        try:
            with open(openclaw_config) as f:
                cfg = json.load(f)
            key = (
                cfg.get("tools", {})
                .get("web", {})
                .get("search", {})
                .get("perplexity", {})
                .get("apiKey", "")
            ).strip()
            if key:
                return key
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def create_client(agent_model_hint: str | None = None) -> "LLMClient | None":
    """Create an optional provider client, if one is configured.

    Provider resolution checks env vars and .env files in order:
    1. MNEMOS_LLM_PROVIDER env var (openai|anthropic|openrouter) — forces provider
    2. ANTHROPIC_API_KEY → AnthropicClient
    3. OPENROUTER_API_KEY → OpenRouterClient
    4. OPENAI_API_KEY → OpenAIClient
    5. Neither → None

    None is an ordinary outcome, not a degraded one. Mnemos does not call a
    model to maintain memory: work needing judgement is proposed to the agent
    and answered in its own words. A provider only accelerates the deep
    passes for headless installs.

    This used to be gated by a substrate-affinity check that refused a model
    of the wrong family, on the reasoning that a foreign model should not
    rewrite an agent's memories. That concern was real and is now answered by
    construction rather than by policy — the agent maintains its own memory —
    so the gate and the ~200 lines behind it are gone.

    Args:
        agent_model_hint: Retained for call compatibility; no longer used.
    """
    return _create_client_unchecked()

def _create_client_unchecked() -> "LLMClient | None":
    """Resolve provider/model from environment without the affinity gate."""
    # Check for model override (env var or .env file)
    model_override = os.environ.get("MNEMOS_MODEL", "").strip()
    if not model_override:
        model_override = _load_env_key("MNEMOS_MODEL")

    # Allow forcing a specific provider (e.g., when Anthropic is out of credits)
    forced = os.environ.get("MNEMOS_LLM_PROVIDER", "").lower()
    if not forced:
        forced = _load_env_key("MNEMOS_LLM_PROVIDER").lower()

    if forced in ("claude-cli", "claude_cli", "subscription"):
        return ClaudeCLIClient(model=model_override or "claude-haiku-4-5-20251001")

    if forced == "openai":
        key = _load_env_key("OPENAI_API_KEY")
        if key:
            return OpenAIClient(api_key=key, model=model_override or "gpt-4o-mini")
        logger.warning(
            "MNEMOS_LLM_PROVIDER=openai but OPENAI_API_KEY is not set; "
            "falling back to provider auto-detection."
        )
    elif forced == "openrouter":
        key = _load_env_key("OPENROUTER_API_KEY")
        if not key:
            key = _load_openclaw_openrouter_key()
        if key:
            return OpenRouterClient(
                api_key=key,
                model=model_override or "anthropic/claude-sonnet-4-5",
            )
        logger.warning(
            "MNEMOS_LLM_PROVIDER=openrouter but no OpenRouter key was found; "
            "falling back to provider auto-detection."
        )
    # forced == "anthropic" or empty string → fall through to auto-detect

    anthropic_key = _load_env_key("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            import anthropic  # noqa: F401
            return AnthropicClient(
                api_key=anthropic_key,
                model=model_override or "claude-sonnet-4-6",
            )
        except ImportError:
            # An operator who forced anthropic must not silently get a
            # different provider performing the agent's maintenance.
            if forced == "anthropic":
                logger.warning(
                    "MNEMOS_LLM_PROVIDER=anthropic but the 'anthropic' package "
                    "is not installed (pip install anthropic); falling back to "
                    "another provider."
                )
            else:
                logger.info(
                    "ANTHROPIC_API_KEY is set but the 'anthropic' package is "
                    "not installed; trying other providers."
                )

    openrouter_key = _load_env_key("OPENROUTER_API_KEY")
    if openrouter_key:
        return OpenRouterClient(api_key=openrouter_key)

    openai_key = _load_env_key("OPENAI_API_KEY")
    if openai_key:
        return OpenAIClient(api_key=openai_key, model="gpt-4o-mini")

    return None


class OpenAIClient:
    """LLM client using the OpenAI API directly.

    Requires: pip install httpx
    Env var: OPENAI_API_KEY
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        max_tokens: int = 500,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        """Send a prompt via OpenAI and return the response text."""
        import httpx

        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "max_tokens": self._max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def structured_complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> str:
        """Send a system+user prompt with temperature control."""
        import httpx

        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
