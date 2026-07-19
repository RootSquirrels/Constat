-- 0016_collect_targets.sql
-- Roadmap 1.3: batch onboarding — persist the collect targets so 35 AWS
-- accounts are onboarded with ONE CSV import, not 35 collect forms.
--
-- Until now every POST /collect/aws re-sent its full target list, and the
-- ECS scheduler read a `scan-targets` JSON secret. With this table the
-- targets live in the DB (tenant-scoped, RLS-bound): the scheduler calls
-- POST /collect/aws with an empty body and the API collects every
-- persisted target.
--
-- SECURITY: external_id is a SHARED SECRET (F-06 confused-deputy
-- mitigation — without it, anyone who learns the role ARN can ride our
-- trust policy). It is write-only over the API: POST
-- /collect/targets/import accepts it, but no GET endpoint ever returns
-- it (GET /collect/targets returns `external_id_set: true` instead).
-- Rotation = re-import the row (upsert semantics).
--
-- What this migration does:
--   - collect_targets: one row per (tenant, AWS account) to scan.
--     regions/resource_types NULL = the collector defaults (RDS-only over
--     the default region set), same semantics as an absent field in a
--     POST /collect/aws body.
--   - RLS on collect_targets: the AGENTS.md invariant — new tenant-scoped
--     table gets ENABLE + FORCE + the standard GUC policy in the same
--     migration, identical in shape to 0007/0011/0015. tests/test_rls.py
--     (Postgres CI job) pins the table list.
--
-- Grants for constat_app come from 0012's ALTER DEFAULT PRIVILEGES
-- (the owner runs this migration), so no explicit GRANT is needed.

CREATE TABLE collect_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    aws_account_id TEXT NOT NULL
        CHECK (aws_account_id ~ '^\d{12}$'),
    role_arn TEXT NOT NULL,
    external_id TEXT NOT NULL,               -- shared secret, write-only over the API
    name TEXT,                               -- human label (e.g. "prod", "customer-acme")
    regions JSONB,                           -- NULL = default set; else e.g. ["eu-west-1"]
    resource_types JSONB,                    -- NULL = RDS only (V1 default)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One row per AWS account per tenant. Two tenants may monitor the
    -- same AWS account (MSP case, same discipline as 0011/audit F-12).
    UNIQUE (tenant_id, aws_account_id)
);

-- The UNIQUE above already leads with tenant_id, so no extra index.

ALTER TABLE collect_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE collect_targets FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_collect_targets ON collect_targets
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
