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
- **`mnemos/store/migrations.py`** — Frozen Python migration history through schema v10.
- **`mnemos/store/migration_runner.py`** — Additive-only SQL-file migration runner for schema v11+.
- **`mnemos/store/migrations/NNNN_name.sql`** — New schema migrations. Use four-digit versions above the Python schema max, include `-- additive-only: yes`, and keep files schema-only.
- **`mnemos/instrumentation/`** — Record-only Step 1 receipts, drift-eval registry, origin-stamp validation, and producer failure accounting.

## Schema Migration Changes

New schema work should use SQL-file migrations, not new Python migration
functions. The runner lints each file against the additive-only contract: new
tables, `ALTER TABLE ... ADD COLUMN` with nullable or constant-default columns,
indexes, and views. It refuses DML, destructive DDL, triggers, PRAGMA writes,
`VACUUM`, `ATTACH`/`DETACH`, `CREATE TABLE AS SELECT`, and direct writes to
`schema_migrations`.

Before applying a SQL-file migration, run:

```bash
mnemos migrate plan
```

The CLI resolves the canonical store from `MNEMOS_DB_PATH` or `store.db_path`
config (including `MNEMOS_STORE_DB_PATH`) and refuses `--db-path`. Tests that
need representative stores should use `MNEMOS_DB_PATH` or instantiate
`MigrationRunner` directly with an isolated temporary database and migrations
directory.

Schema v12 is Step 1 instrumentation only: runtime receipts, retrieval events,
retrieval citations, drift-eval rows, producer failure counts, and nullable
`engrams.origin_stamp`. Keep these migrations schema-only and verify behavior
against temporary or representative copy databases, not live `~/.mnemos`.

## Feature Archive

- **`wip-continuity`** branch — A snapshot of the full workshop layer. Mine it for features; don't push it wholesale.
- **`feat/introspection`** branch — A clean extraction of the introspection engine (opt-in self-audit). Ready for review.

## Cleanup

- The `memory-concepts` repo is deprecated; its content is preserved in `wip-continuity`.
- Feature branches are deleted after merge; `main` is the canonical history.
