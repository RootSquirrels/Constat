-- 0004_tenant_id.sql
-- Add tenant_id to all tenant-scoped tables. V1 is single-tenant; we add
-- the column now (cheap) so we don't pay for a 6-table migration later
-- (the doc's §3.5 promise: "multi-tenant des la fondation, jamais bolt-on").
--
-- Also add UNIQUE constraint on facts: one observation per
-- (tenant, resource, namespace, key, source, observed_at). Prevents
-- duplicate accumulation across scans.

DO $$
DECLARE
    default_tenant UUID := '00000000-0000-0000-0000-000000000001';
BEGIN
    -- Add tenant_id columns (nullable first, so backfill can run)
    ALTER TABLE accounts ADD COLUMN tenant_id UUID;
    ALTER TABLE resources ADD COLUMN tenant_id UUID;
    ALTER TABLE facts ADD COLUMN tenant_id UUID;
    ALTER TABLE insights ADD COLUMN tenant_id UUID;
    ALTER TABLE inconclusive ADD COLUMN tenant_id UUID;
    ALTER TABLE observations ADD COLUMN tenant_id UUID;
    ALTER TABLE focus_charges ADD COLUMN tenant_id UUID;
    -- insight_runs was missed here originally; 0007's RLS policy on it
    -- references tenant_id, so the fresh-database chain (0001 -> 0007)
    -- failed at policy creation. Adding it where it belongs.
    ALTER TABLE insight_runs ADD COLUMN tenant_id UUID;

    -- Backfill with default tenant
    UPDATE accounts SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE resources SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE facts SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE insights SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE inconclusive SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE observations SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE focus_charges SET tenant_id = default_tenant WHERE tenant_id IS NULL;
    UPDATE insight_runs SET tenant_id = default_tenant WHERE tenant_id IS NULL;

    -- Make NOT NULL
    ALTER TABLE accounts ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE resources ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE facts ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE insights ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE inconclusive ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE observations ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE focus_charges ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE insight_runs ALTER COLUMN tenant_id SET NOT NULL;
END $$;

-- Tenant indexes (fast lookups for V2 multi-tenant queries)
CREATE INDEX idx_accounts_tenant ON accounts(tenant_id);
CREATE INDEX idx_resources_tenant ON resources(tenant_id);
CREATE INDEX idx_facts_tenant ON facts(tenant_id);
CREATE INDEX idx_insights_tenant ON insights(tenant_id);
CREATE INDEX idx_inconclusive_tenant ON inconclusive(tenant_id);
CREATE INDEX idx_observations_tenant ON observations(tenant_id);
CREATE INDEX idx_focus_charges_tenant ON focus_charges(tenant_id);
CREATE INDEX idx_insight_runs_tenant ON insight_runs(tenant_id);

-- UNIQUE on facts: prevents duplicate snapshots of the same fact at the
-- same observation time. Allows multiple scans to coexist (different
-- observed_at) without piling up duplicates within a single scan.
ALTER TABLE facts ADD CONSTRAINT uq_fact_snapshot UNIQUE
    (tenant_id, resource_id, namespace, key, source, observed_at);
