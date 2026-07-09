-- purpose: add prospective-only status for future-directed engrams.
-- v1 section: §3 / §6.1 prospective substrate
-- additive-only: yes
--
-- No backfill: live origin/main has zero prospective rows. Non-prospective
-- rows remain NULL; prospective rows get status from code-side validation.

ALTER TABLE engrams ADD COLUMN status TEXT
    CHECK (
        status IS NULL
        OR (
            kind = 'prospective'
            AND status IN ('open', 'fulfilled', 'closed_unfulfilled', 'retired')
        )
    );
