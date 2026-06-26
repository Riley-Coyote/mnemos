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

## PAI Importer And Dual-Life Watcher

- `mnemos pai-import preview` and `watch-preview` open the SQLite DB read-only
  (`EngramStore(read_only=True)` → `file:…?mode=ro`) and never mutate state.
- `mnemos pai-import apply` and `watch-apply` take an integrity-checked
  SQLite backup before any write, into the configured `--backup-dir` (or
  `<db>/pai-import-backups/` by default). Backup retention rotation is the
  operator's responsibility.
- Every PAI import subcommand refuses the default live database
  (`~/.mnemos/memory.db`) unless the operator passes `--allow-live-db`. The
  guard uses inode equality so case-insensitive paths like
  `~/.MNEMOS/memory.db` cannot bypass it.
- Manifest source paths must stay inside the manifest directory (path
  resolution + `relative_to` guard); absolute or `..`-escaping paths are
  rejected at load.
- `watch-once` advances state only after a successful apply. Preview mode
  leaves state untouched so an operator can inspect a change and still apply
  it later.
- `watch-plist` writes the launchd plist atomically (temp file + `rename`),
  resolves the configured Python interpreter on `PATH`, asserts the
  interpreter can `import mnemos.cli` against the repo, and bakes absolute
  paths for manifest, DB, state, artifact, and backup directories into the
  generated `ProgramArguments`. Loading the plist with `launchctl` remains
  an explicit operator action; `watch-plist` only writes the file.
- Error messages that mention recovery actions are probed against actual
  behavior. Recovery steps that would destroy operator hand-edits or in-flight
  state must be removed from user-facing text, even when the underlying code
  path is harmless.
- `IdentityProfile` excludes PAI routing tags (`pai-import`,
  `identity-kernel`, `david-context`, `growth-substrate`, `belief`,
  `hypomnema`) from persistent-concern counts so an import does not surface
  as the agent's persistent concern.

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
