-- 0018_inconclusive_workflow.sql
-- Roadmap 2.5: the inconclusive queue becomes an operator work queue.
--
-- "We don't know" records are only useful if someone owns them: an
-- assignee, a due date, and a triage status (open -> acknowledged ->
-- resolved). Written only by PATCH /inconclusives/{id}; the runner never
-- touches them — but note its delete-and-replace recreates the rows, so a
-- re-run resets the workflow fields to defaults (accepted V1 semantic:
-- the underlying "we don't know" must be re-triaged anyway).
--
-- What this migration does:
--   - inconclusive: + owner TEXT, + due_date DATE, + status TEXT
--     (open | acknowledged | resolved, default 'open').
--   - Columns only: inconclusive already has RLS from 0007, so the
--     AGENTS.md invariant (new table => RLS in the same migration) does
--     not apply here.
--
-- Grants for constat_app come from 0012's ALTER DEFAULT PRIVILEGES
-- (the owner runs this migration), so no explicit GRANT is needed.

ALTER TABLE inconclusive ADD COLUMN owner TEXT;
ALTER TABLE inconclusive ADD COLUMN due_date DATE;
ALTER TABLE inconclusive ADD COLUMN status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'acknowledged', 'resolved'));
