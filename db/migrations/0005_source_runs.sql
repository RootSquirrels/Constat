-- 0005_source_runs.sql
-- SourceRun models the proof-of-completeness for a (account, region, type) scan.
-- When status='success', absence of a resource in that scope is PROVEN, not guessed.
-- Without this, the inventory-first promise is unverifiable: we don't know if
-- "0 RDS in eu-west-1" means "we scanned and found none" or "we never scanned".
--
-- Partial unique index: only one running scan per (tenant, account, region, type, source).
-- Multiple completed scans coexist; only the active one is exclusive.

CREATE TABLE source_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    resource_type TEXT NOT NULL,             -- e.g. "AWS::RDS::DBInstance"
    source TEXT NOT NULL,                    -- e.g. "aws_rds"
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'partial')),
    resources_found INT,                     -- count of resources seen in this run
    error TEXT,
    CONSTRAINT source_runs_scope_present CHECK (
        account_id IS NOT NULL
    )
);

CREATE INDEX idx_source_runs_account ON source_runs(account_id, region, resource_type, started_at DESC);
CREATE INDEX idx_source_runs_tenant ON source_runs(tenant_id);

-- Only one running scan per scope at a time. Multiple completed runs are OK.
CREATE UNIQUE INDEX uq_source_run_active ON source_runs(account_id, region, resource_type, source)
    WHERE status = 'running';
