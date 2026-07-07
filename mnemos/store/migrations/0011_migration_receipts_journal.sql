-- purpose: create the migration-applied receipts journal (§3 bootstrap organ).
-- v1 section: §14 step 0 (migration runner) — the runner cannot depend on the
--   organ it is building, so this is the first SQL-file migration and its own
--   receipt is the schema_migrations row until the journal exists.
-- additive-only: yes
--
-- The receipts journal is a plain append-only table. Rows are written by the
-- runner AFTER a version commits (or drained from pending_receipts for the
-- version that creates this table). Additive: a new table, no writes to
-- existing rows, no triggers, no data migration.

CREATE TABLE IF NOT EXISTS migration_receipts (
    receipt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    version      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    checksum     TEXT NOT NULL,
    snapshot     TEXT NOT NULL,
    applied_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_version
    ON migration_receipts(version);
