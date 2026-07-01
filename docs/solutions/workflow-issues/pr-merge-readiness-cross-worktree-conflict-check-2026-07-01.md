---
title: PR Merge Readiness Requires Cross-Worktree Conflict Checks
date: 2026-07-01
category: workflow-issues
module: Mnemos PR merge readiness
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "David asks to ensure a PR, branch, or worktree will not conflict with other active work"
  - "A PR merges cleanly into origin/main but other local Mnemos worktrees or feature branches are active"
  - "A branch touches shared migration, store, substrate handler, CLI, or schema-test surfaces"
symptoms:
  - "PR merge readiness is treated as satisfied after checking only origin/main"
  - "`git merge-tree` against another active local branch reports conflicts after the PR is clean against main"
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components:
  - "assistant"
  - "tooling"
tags: [mnemos, pr-merge-readiness, cross-worktree, merge-conflicts, treehouse, git-merge-tree, parallel-branches]
---

# PR Merge Readiness Requires Cross-Worktree Conflict Checks

## Context

PR #4 on `codex/afferent-membrane-v1-ledger` was locally proven and pushed at `9febd4109cdb597ff8b73c91a9f06347e3e49840`. The stale No Mistakes run had been aborted, the full local suite passed, and GitHub showed the PR open with no CI checks configured.

David then asked to merge it, but with the extra constraint: make sure the other work does not conflict with it. The first mergeability check answered only the narrow GitHub question: PR #4 merges cleanly into `origin/main`. That was true, but incomplete.

The active-work check found another Mnemos worktree:

```text
/Users/davidef/Projects/mnemos-install
branch feat/gated-inner-life-soak
head 375cdce
dirty working tree
```

`git merge-tree --write-tree origin/main origin/pr/4` succeeded. `git merge-tree --write-tree feat/gated-inner-life-soak origin/pr/4` failed with conflicts in:

```text
mnemos/store/migrations.py
mnemos/store/sqlite_store.py
mnemos/substrate/handlers/dreaming.py
mnemos/substrate/handlers/wandering.py
tests/test_cli_simple.py
tests/test_u3a_schema_migrations.py
```

The correct conclusion was not "PR #4 is unsafe to merge into main." It was sharper: PR #4 is clean into `main`, but not integration-safe relative to David's active local soak work.

## Guidance

When David asks to merge a PR and says to make sure other work does not conflict, treat the request as an integration-safety check, not just a base-branch mergeability check.

Run four checks before merging.

First, inspect the PR and its check surface:

```bash
gh-axi pr view 4 --repo davidefitz/mnemos
gh-axi pr checks 4 --repo davidefitz/mnemos
gh-axi pr diff 4 --repo davidefitz/mnemos --full
```

Second, prove base-branch mergeability:

```bash
git fetch origin main pull/4/head:refs/remotes/origin/pr/4
git merge-tree --write-tree origin/main origin/pr/4
```

Third, inspect active local work and worktrees:

```bash
git worktree list --porcelain
git status --short --branch
```

For each related active worktree, inspect its own branch and dirty state from that worktree:

```bash
git -C /Users/davidef/Projects/mnemos-install status --short --branch
git -C /Users/davidef/Projects/mnemos-install rev-parse --abbrev-ref HEAD
git -C /Users/davidef/Projects/mnemos-install rev-parse --short HEAD
```

Fourth, test the PR against active parallel branches, not only against `main`:

```bash
git merge-tree --write-tree feat/gated-inner-life-soak origin/pr/4
```

If that conflicts, do not describe the PR as safe to merge without qualification. Say the exact state:

```text
PR #4 merges cleanly into origin/main, but conflicts with active local branch feat/gated-inner-life-soak. Merging now may be technically valid for main, but downstream integration with David's active soak work is unresolved.
```

Use `no-mistakes axi status` for gate state and active-run visibility, but do not let No Mistakes or GitHub checks substitute for the active-worktree conflict check. A gate can prove the PR branch; it does not know every local worktree David has in flight.

## Why This Matters

GitHub mergeability answers one narrow question: can this PR apply to the configured base branch right now?

David's preservation constraint asks a different question: will this merge preserve or at least not blindside the work already in flight?

Those are different safety domains. A PR can be perfectly clean against `origin/main` and still create painful downstream conflicts for a dirty local worktree, a long-running feature branch, or another branch outside GitHub's visible dependency graph. The failure mode is collapsing a proven fact into an unproven judgment:

- Fact: `merge-tree` against `origin/main` succeeds.
- Unproven judgment: the PR is safe relative to other active work.

For Mnemos, this matters most when branches touch shared foundations such as migrations, store code, substrate handlers, CLI surfaces, MCP/context surfaces, and schema tests. Those files create real integration pressure even when the PR base merge is clean.

## When to Apply

- David asks to merge, land, ship, approve, or declare a PR safe.
- David mentions "other work," "parallel work," "doesn't conflict," "active branch," "Treehouse," or "worktree."
- The repo has multiple local worktrees.
- A related checkout has a dirty working tree.
- A PR touches substrate, migrations, store code, schema code, tests, or other shared foundations.
- No Mistakes or GitHub checks are green, but local integration context may be broader than GitHub knows.

Do not require this full check for a trivial PR status question. Do require it before acting on a merge request with an explicit preservation constraint.

## Examples

Bad conclusion:

```text
PR #4 is safe to merge. merge-tree against origin/main succeeds and checks pass.
```

Better conclusion:

```text
PR #4 is clean against origin/main, but not integration-safe yet. The active soak branch conflicts with it in store migrations, sqlite store, dreaming/wandering handlers, and tests.
```

Safe merge flow when no active conflicts exist:

```bash
gh-axi pr view 4 --repo davidefitz/mnemos
gh-axi pr checks 4 --repo davidefitz/mnemos
git fetch origin main pull/4/head:refs/remotes/origin/pr/4
git merge-tree --write-tree origin/main origin/pr/4
git worktree list --porcelain
git merge-tree --write-tree feat/active-branch origin/pr/4
no-mistakes axi status
gh-axi pr merge 4 --repo davidefitz/mnemos --method squash --delete-branch
```

Blocked merge flow when active conflicts exist:

```bash
git merge-tree --write-tree origin/main origin/pr/4
# succeeds

git worktree list --porcelain
git merge-tree --write-tree feat/gated-inner-life-soak origin/pr/4
# fails with content conflicts
```

Correct response:

```text
I am not merging this yet. It is clean into main, but it conflicts with the active soak worktree. The next decision is whether to merge PR #4 anyway and resolve soak later, or pause PR #4 until the soak branch is rebased or integrated.
```

## Related

- [Afferent Membrane Safety Ledger Repair Workflow](/Users/davidef/.treehouse/mnemos-install-7d822b/1/mnemos-install/docs/solutions/workflow-issues/afferent-membrane-safety-ledger-repair-workflow-2026-07-01.md) - adjacent false-readiness lesson: same-head validation and stale No Mistakes runs are a different proof-surface trap.
