-- 0011_rls_remaining_tables.sql
-- Audit F-04 + F-12 remediation.
--
-- F-04: four tenant-scoped tables were created after 0007_rls_policies.sql
-- and never got RLS: focus_charge_tags (0009), audit_events,
-- retention_policies, pii_classifications (0010). Without policies, tenant
-- #2 would see tenant #1's rows on these tables — the exact cross-tenant
-- leak the rest of the schema is protected against. All four already have
-- `tenant_id UUID NOT NULL`, so this migration only adds ENABLE + FORCE +
-- the standard tenant GUC policy, identical in shape to 0007.
--
-- F-12: accounts.external_id had a global UNIQUE (from 0001) with no
-- tenant_id in the key. Two tenants could never reference the same AWS
-- account id — which breaks the MSP case (one AWS account, several
-- customers). Fix: drop the global unique, add UNIQUE(tenant_id,
-- external_id).
--
-- resources needs no equivalent change: its UNIQUE is (account_id, region,
-- resource_type, native_id) and account_id is a per-tenant UUID, so the
-- constraint is already tenant-scoped by construction.

-- ============================================================================
-- F-12: tenant-scope the accounts external id
-- ============================================================================
--
-- 0001 declared `external_id TEXT NOT NULL UNIQUE` inline, so Postgres
-- auto-named the constraint `accounts_external_id_key`. Dropping it also
-- drops its implicit index; the new composite constraint's index covers
-- (tenant_id, external_id) lookups.

ALTER TABLE accounts DROP CONSTRAINT accounts_external_id_key;
ALTER TABLE accounts ADD CONSTRAINT uq_accounts_tenant_external
    UNIQUE (tenant_id, external_id);

-- ============================================================================
-- F-04: RLS on the four tables missed by 0007
-- ============================================================================
--
-- Same pattern as 0007_rls_policies.sql — read the header there for the
-- full rationale. Short version:
--   ENABLE ROW LEVEL SECURITY  — turn RLS on for non-owners.
--   FORCE  ROW LEVEL SECURITY  — apply RLS to the table owner too (the
--                                application role owns these tables;
--                                without FORCE it would bypass RLS).
--   USING / WITH CHECK         — read-side and write-side filter on the
--                                `app.current_tenant_id` GUC. Unset GUC =>
--                                current_setting(..., true) is NULL => no
--                                rows visible, no writes allowed.
--
-- Every new tenant-scoped table MUST get this treatment. If you add a
-- table with a tenant_id column and no policy here, tests/test_rls.py
-- (the Postgres-marked tests) will fail in CI.

ALTER TABLE focus_charge_tags   ENABLE ROW LEVEL SECURITY;
ALTER TABLE focus_charge_tags   FORCE  ROW LEVEL SECURITY;
ALTER TABLE audit_events        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events        FORCE  ROW LEVEL SECURITY;
ALTER TABLE retention_policies  ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_policies  FORCE  ROW LEVEL SECURITY;
ALTER TABLE pii_classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_classifications FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_focus_charge_tags ON focus_charge_tags
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_audit_events ON audit_events
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_retention_policies ON retention_policies
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_pii_classifications ON pii_classifications
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- focus_charge_tags had no tenant_id index (0010 added them for its own
-- tables). The RLS policy filters every query on tenant_id; without an
-- index that is a seq scan per query.
CREATE INDEX idx_focus_charge_tags_tenant ON focus_charge_tags(tenant_id);
