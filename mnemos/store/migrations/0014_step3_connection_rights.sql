-- purpose: add Step 3 S1 epistemic rights to connection current rows.
-- authority: STEP3-CONNECTIONS-ARC v2 R1
-- additive-only: yes
--
-- Schema only. Existing rows retain their legacy values and receive NULL for
-- every new field. This migration performs no DML, backfill, trigger install,
-- defaulting, lifecycle work, reader/writer change, index creation, or seed.

ALTER TABLE connections ADD COLUMN valid_at TEXT;
ALTER TABLE connections ADD COLUMN invalid_at TEXT;
ALTER TABLE connections ADD COLUMN confidence REAL;
ALTER TABLE connections ADD COLUMN runner_up_label TEXT;
ALTER TABLE connections ADD COLUMN runner_up_confidence REAL;
ALTER TABLE connections ADD COLUMN classifier_version TEXT;
