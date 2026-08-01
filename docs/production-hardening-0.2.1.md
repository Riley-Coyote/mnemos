# Mnemos 0.2.1 Production Hardening

Status: release candidate; trusted pilot pending
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
7. Final release-candidate verification and a 24-hour trusted pilot.

## Release-candidate verification

Completed locally on 2026-08-01:

- 378 tests pass on Python 3.10 with 81.1 percent supported-code coverage.
- The wheel and source archive build successfully and pass metadata checks.
- A fresh Python 3.13 environment installs the wheel and starts Mnemos.
- The real MCP stdio connection exposes exactly eight simple tools.
- Provider and Sidecar Hermes integration tests pass with scope isolation.
- Two real CLI sessions carry continuity forward across a restart.
- A background maintenance cycle completes without a model.
- Backup, inspection, destructive-change, and verified restore rehearsal passes.
- Repeated four-writer concurrency rehearsals preserve every memory.
- Locked dependencies have no known vulnerabilities; lint, workflow syntax,
  and the high-severity code-security gate pass.
- GitHub's Python 3.10-3.13 matrix, CodeQL, and release-hardening jobs pass.

Still required before publication:

- Complete the 24-hour trusted pilot.
- Receive Riley's explicit approval before creating or pushing the `v0.2.1` tag.
