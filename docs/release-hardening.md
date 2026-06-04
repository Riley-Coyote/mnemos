# Release Hardening

Use this checklist before publishing Mnemos or opening a release PR.

## Protocol Correctness

- Simple MCP mode exposes exactly:
  - `mnemos_context`
  - `mnemos_capture`
  - `mnemos_recall`
  - `mnemos_correct`
  - `mnemos_maintain`
- Advanced mode preserves the existing admin tools.
- Injected FastMCP context parameters are not exposed in public tool schemas.
- Sampling is optional and occurs only inside an active client request.
- Sampling failures, denials, or unsupported clients fall back cleanly.
- Tool annotations match local side effects.

## Install UX

- `mnemos doctor` works on a fresh machine with no provider key.
- `mnemos mcp install generic` prints a valid JSON snippet.
- `mnemos mcp install claude --write` safely merges the Claude Desktop config.
- `mnemos mcp install codex` prints a usable `codex mcp add` command.
- `mnemos serve` defaults to simple mode.
- `mnemos serve --mode advanced` exposes the admin surface.

## Package Readiness

- The distribution package is `mnemos-memory`.
- The CLI command remains `mnemos`.
- Wheel and sdist build successfully.
- Wheel contains:
  - `mnemos/simple_runtime.py`
  - `mnemos/simple_mcp.py`
  - `templates/SOUL.md`
  - `templates/IDENTITY.md`
- Package metadata passes `twine check`.

## Privacy and Safety

- Baseline simple mode does not require network access.
- Baseline simple mode does not require OpenRouter, Anthropic, OpenAI, or OpenClaw.
- Dedicated providers are used only when explicitly configured.
- Scope isolation is tested across multiple agents.
- Correction/forget behavior is documented.

## Verification Commands

```bash
uv run --extra dev --extra mcp pytest -q
uv run --extra mcp python -m py_compile mnemos/simple_runtime.py mnemos/simple_mcp.py mnemos/mcp_server.py mnemos/cli.py
uv build
uvx twine check dist/*
git diff --check
```

## Dogfood Continuity

Before shipping a meaningful change, use Mnemos itself to capture:

- what changed
- why the product decision matters
- remaining release risks
- client-specific install gotchas

Then verify recall against those notes.
