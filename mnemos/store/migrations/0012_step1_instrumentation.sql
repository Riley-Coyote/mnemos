-- purpose: create Step 1 runtime instrumentation journals.
-- v1 section: §8 receipts, retrieval logging, drift eval plumbing, origin stamps
-- additive-only: yes
--
-- Record-only schema. No data migration, no backfill, no triggers, no DML.
-- Existing engram rows keep origin_stamp NULL. NULL means pre-instrumentation:
-- absence of a measurement, not a measurement.

CREATE TABLE IF NOT EXISTS runtime_receipts (
    receipt_id       TEXT PRIMARY KEY,
    ts               TEXT NOT NULL,
    actor            TEXT NOT NULL,
    runtime          TEXT NOT NULL,
    session_id       TEXT NOT NULL DEFAULT '',
    engram_refs_json TEXT NOT NULL DEFAULT '[]',
    immediacy        TEXT NOT NULL,
    kind             TEXT NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runtime_receipts_kind_ts
    ON runtime_receipts(kind, ts);

CREATE INDEX IF NOT EXISTS idx_runtime_receipts_session_ts
    ON runtime_receipts(session_id, ts);

CREATE TABLE IF NOT EXISTS retrieval_events (
    event_id                  TEXT PRIMARY KEY,
    ts                        TEXT NOT NULL,
    actor                     TEXT NOT NULL,
    runtime                   TEXT NOT NULL,
    session_id                TEXT NOT NULL DEFAULT '',
    agent_id                  TEXT NOT NULL,
    cue                       TEXT NOT NULL,
    read_visibility           TEXT,
    max_results               INTEGER NOT NULL,
    surfaced_engram_ids_json  TEXT NOT NULL DEFAULT '[]',
    result_count              INTEGER NOT NULL DEFAULT 0,
    why_json                  TEXT NOT NULL DEFAULT '{}',
    failure_count             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_agent_ts
    ON retrieval_events(agent_id, ts);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_session_ts
    ON retrieval_events(session_id, ts);

CREATE TABLE IF NOT EXISTS retrieval_citations (
    citation_id   TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL,
    engram_id     TEXT NOT NULL,
    surface       TEXT NOT NULL,
    cited_at      TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_retrieval_citations_event
    ON retrieval_citations(event_id);

CREATE INDEX IF NOT EXISTS idx_retrieval_citations_engram
    ON retrieval_citations(engram_id);

CREATE TABLE IF NOT EXISTS drift_eval_runs (
    run_id          TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    instrument_name TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    status          TEXT NOT NULL,
    metrics_json    TEXT NOT NULL DEFAULT '{}',
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_drift_eval_runs_instrument_ts
    ON drift_eval_runs(instrument_name, ts);

CREATE TABLE IF NOT EXISTS drift_eval_observations (
    observation_id  TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    REAL,
    text_value      TEXT,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    observed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_eval_observations_run
    ON drift_eval_observations(run_id);

CREATE TABLE IF NOT EXISTS instrumentation_failures (
    producer      TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instrumentation_failures_last_seen
    ON instrumentation_failures(last_seen_at);

ALTER TABLE engrams ADD COLUMN origin_stamp TEXT
    CHECK (origin_stamp IN ('user-witnessed', 'inference', 'retrieval', 'import'));
