# mnemos Development Workflow

## Repository Structure

- **`~/Documents/Repositories/mnemos`** — The canonical clone, always on `main`. This is the **production checkout** — the live source the agent fleet runs.
- **`~/Documents/Repositories/mnemos-dev`** — The development worktree, on the `development` branch. All feature work happens here.

## Workflow

### Starting a Feature

1. **In the dev worktree**, create a feature branch off `development`:
   ```bash
   cd ~/Documents/Repositories/mnemos-dev
   git checkout -b feat/your-feature
   ```

2. **Develop and test** — your changes are isolated; the production clone is untouched:
   ```bash
   # Make changes, commit as you go
   git add . && git commit -m "..."
   
   # Run the health harness to verify the system is sound
   cd ../mnemos-dev && scripts/health_check.py
   ```

3. **Merge to main only when green** — once `scripts/health_check.py` passes:
   ```bash
   # In mnemos-dev
   git checkout development && git pull
   git rebase main  # Keep development in sync
   
   # Then merge your feature
   git merge feat/your-feature
   
   # In the main clone, bring it forward
   cd ../mnemos && git pull
   ```

## Why This Structure

- **Isolation**: The dev worktree doesn't touch the live source. Feature branches are tested in a separate working directory.
- **Test hygiene**: Pytest autouse fixtures clear ambient Mnemos/provider configuration, disable dotenv reads, refuse the live `~/.mnemos/memory.db`, and redirect vault alerts into per-test temp directories by default.
- **Safety**: The production clone only advances when you explicitly `git pull` after merging to main in the dev tree.
- **The harness as a gate**: `scripts/health_check.py` verifies 52 invariants before you commit to main — storage integrity, recall, the full lifecycle, no hard deletes, schema consistency.

## Key Files

- **`scripts/health_check.py`** — The invariant harness. Run it before every merge to main.
- **`health_baseline.local.json`** — Your local baseline (agent names, DB paths). Gitignored; re-run the harness to generate it.

## Feature Archive

- **`wip-continuity`** branch — A snapshot of the full workshop layer. Mine it for features; don't push it wholesale.
- **`feat/introspection`** branch — A clean extraction of the introspection engine (opt-in self-audit). Ready for review.

## Cleanup

- The `memory-concepts` repo is deprecated; its content is preserved in `wip-continuity`.
- Feature branches are deleted after merge; `main` is the canonical history.
