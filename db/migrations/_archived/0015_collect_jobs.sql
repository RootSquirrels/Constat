-- 0015_collect_jobs.sql
-- Roadmap 1.1 + 1.2: asynchronous AWS collection at ICP scale.
--
-- POST /collect/aws no longer scans inside the HTTP request. It creates
-- one `collect_jobs` row and enqueues one work item per (target x region);
-- a worker (in-process thread in `inline` mode, external ECS task in `sqs`
-- mode) drains the queue and writes source_runs as before. The job row is
-- the operator-visible handle: "I asked for a scan of N account-regions,
-- here is how far it got" (GET /collect/aws/jobs/{job_id}).
--
-- What this migration does:
--   - collect_jobs: one row per accepted POST /collect/aws. `summary` is
--     counts only (accounts / regions / resource_types) — never account
--     ids, ARNs, or external ids, same non-PII discipline as audit_events.
--   - source_runs.job_id: nullable back-pointer so the job status endpoint
--     can group runs per job. NOT a foreign key: a work item may be
--     enqueued before its job row commits (queue-first failure mode), and
--     a dangling job_id must not block the scan — it only degrades the
--     status endpoint to "pending".
--   - RLS on collect_jobs: the AGENTS.md invariant — new tenant-scoped
--     table gets ENABLE + FORCE + the standard GUC policy in the same
--     migration, identical in shape to 0007/0011. tests/test_rls.py
--     (Postgres CI job) pins the table list.
--
-- Grants for constat_app come from 0012's ALTER DEFAULT PRIVILEGES
-- (the owner runs this migration), so no explicit GRANT is needed.

CREATE TABLE collect_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor TEXT NOT NULL,                     -- API key name that triggered the collect
    total_items INT NOT NULL,                -- work items enqueued (target x region)
    summary JSONB NOT NULL DEFAULT '{}'::jsonb  -- counts only, no PII
);

CREATE INDEX idx_collect_jobs_tenant ON collect_jobs(tenant_id);

ALTER TABLE source_runs ADD COLUMN job_id UUID;
-- (tenant_id, job_id): the status endpoint always filters by tenant first
-- (RLS), so the index leads with it.
CREATE INDEX idx_source_runs_tenant_job ON source_runs(tenant_id, job_id);

ALTER TABLE collect_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE collect_jobs FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_collect_jobs ON collect_jobs
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
