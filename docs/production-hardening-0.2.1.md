# Mnemos 0.2.1 Production Hardening

Status: release candidate; independent same-day verification passed
Branch: `hotfix/0.2.1-production-hardening`
Release rule: prepare the release candidate, but do not publish without Riley's approval.

## Release containment

- Leave the unannounced 0.2.0 package available on PyPI; do not yank it.
- Keep the existing `v0.2.0` tag unchanged.
- Publish the repaired package as 0.2.1 only after every gate below passes.

## Required repairs

### Security and privacy

- Remove permission bypass from the Claude CLI provider and disable its tools.
- Remove automatic MCP host-model sampling from capture and maintenance.
- Preserve captured text exactly as supplied.
- Create private storage, settings, backup, log, and API-key files.
- Add a permission repair path for existing installations.

### Memory boundaries

- Store agent, person, and project scope on every new durable memory.
- Enforce the complete scope before recall, graph generation, reconsolidation,
  correction, archive, connection creation, or maintenance.
- Safely migrate existing stores and quarantine ambiguous legacy records.
- Scope maintenance history so one identity cannot delay another.

### Data safety

- Add verified SQLite backup, backup inspection, and restore commands.
- Back up and integrity-check a database before and after migration.
- Finish archive restoration.
- Enforce append-only identity invariants.

### Installation and operations

- Write client and Mnemos settings atomically, preserving recoverable backups.
- Quote paths safely in hooks, systemd units, and cron entries.
- Fail honestly when scheduler installation or activation fails.
- Report actual scheduler state and recent maintenance health.
- Bound MCP input and result sizes with clear errors.

### Supported product boundary

- Support the eight simple MCP tools, CLI, local continuity, backup/recovery,
  and Claude, Codex, generic MCP, and Hermes integrations.
- Keep unfinished advanced modules importable but explicitly experimental;
  they must never report fake success or appear as supported product features.
- Correct public documentation and resolve stale GitHub issues.

### Release safeguards

- Update locked dependencies until dependency auditing is clean.
- Add dependency, code-security, lint, and supported-surface coverage gates.
- Add `SECURITY.md`, Dependabot, CodeQL, pinned Actions, checksums, SBOM, and
  build provenance.
- Create both PyPI and GitHub releases from the verified tag workflow.

## Acceptance gates

- Existing tests and every new regression test pass on Python 3.10-3.13.
- Cross-person and cross-project reads and mutations are impossible.
- Prompt-injected memories cannot make the Claude CLI use tools.
- Baseline tools make no hidden model-sampling requests.
- Private files use restrictive permissions on supported Unix systems.
- A 0.2.0 database upgrades, backs up, restores, and survives interrupted work.
- Installers preserve invalid or unrelated user configuration and report errors.
- Locked and fresh installations have no known dependency vulnerabilities.
- The built wheel exposes exactly eight simple tools over real stdio.
- Hermes Provider and Sidecar modes preserve their intended boundaries.
- Supported modules reach at least 80 percent test coverage.
- A clean install, two real sessions, backup/restore rehearsal, concurrent-write
  test, and one background-maintenance cycle pass.

## Delivery order

1. Release containment and version bump.
2. Security and scope boundaries.
3. Migration, backup, and recovery.
4. Installer and scheduler reliability.
5. Experimental-feature quarantine and documentation.
6. CI, security, and release workflow.
7. Independent installed-package verification and release rehearsal.

## Release-candidate verification

Completed locally on 2026-08-01:

- 379 tests pass with 81.25 percent supported-code coverage.
- The wheel and source archive build successfully and pass metadata checks.
- Clean installed-wheel black-box audits pass on macOS, Windows, and Linux
  Python 3.10-3.13 without importing Mnemos internals.
- The real MCP stdio connection exposes exactly eight simple tools and passes
  capture, recall, context, correction, reflection, maintenance, and health.
- The black-box audit blocks external network access, uses paths containing
  spaces, rejects oversized input, and confirms exact text preservation.
- Agent, person, and project isolation hold across separate server processes
  sharing one database; an out-of-scope agent cannot read or alter memories.
- Forgetting by the MCP-returned memory ID removes both the memory and its
  linked continuity note after restart.
- Provider and Sidecar Hermes integration tests pass with scope isolation.
- Two real CLI sessions carry continuity forward across a restart.
- A background maintenance cycle completes without a model.
- Backup, inspection, destructive-change, and verified restore rehearsal passes.
- A real PyPI 0.2.0 database upgrades to 0.2.1, preserves its memory and scope,
  creates a clean verified backup, and restores successfully.
- A 32-process write test preserves every memory; online backup remains
  consistent during writes, and forced process termination does not corrupt it.
- Installer tests preserve unrelated Claude and MCP configuration, are
  idempotent, back up valid files, and leave invalid JSON untouched.
- Repository and wheel secret scans pass with no findings.
- Locked dependencies have no known vulnerabilities; lint, workflow syntax,
  and the high-severity code-security gate pass.
- GitHub's Python 3.10-3.13 matrix, cross-platform black-box, CodeQL, and
  release-hardening jobs pass.
- The final release workflow rehearsal builds, installs, inventories, checks,
  and attests the artifacts without publishing them.

Still required before publication:

- Receive Riley's explicit approval before creating or pushing the `v0.2.1` tag.

A longer trusted pilot is still useful as post-release monitoring, but is not a
publication blocker after the independent same-day checks above.
