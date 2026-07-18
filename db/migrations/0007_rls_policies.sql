-- 0007_rls_policies.sql
-- Multi-tenant Row Level Security. V1 is single-tenant, but we add the
-- policies now so V2 (tenant #2) is a config change, not a schema change.
--
-- The application sets `app.current_tenant_id` per request via
-- `SELECT set_config('app.current_tenant_id', '<uuid>', true)` (see
-- `apps/api/src/constat_api/tenant.py`). Every tenant-scoped table has
-- a policy that filters by that GUC.
--
-- When the GUC is unset, `current_setting('app.current_tenant_id', true)`
-- returns NULL, so `tenant_id = NULL` is always false, and the policy
-- hides every row. This is the safe default: a session without a tenant
-- context sees nothing, even on the same Postgres instance.
--
-- RLS is Postgres-only. Sqlite tests are unaffected (no policies exist
-- on the in-memory engine).

-- ============================================================================
-- Enable RLS on every tenant-scoped table.
-- ALTER TABLE ... ENABLE ROW LEVEL SECURITY turns on RLS for non-owners.
-- ALTER TABLE ... FORCE ROW LEVEL SECURITY applies RLS even to the table
-- owner, which is what we want: the application role that runs the API
-- is the same role that owns these tables (single-role V1). Without
-- FORCE, the API would bypass RLS entirely. With FORCE, the application
-- is just as constrained as any other role — no accidental backdoor.
-- ============================================================================

ALTER TABLE accounts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts        FORCE  ROW LEVEL SECURITY;
ALTER TABLE resources       ENABLE ROW LEVEL SECURITY;
ALTER TABLE resources       FORCE  ROW LEVEL SECURITY;
ALTER TABLE observations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE observations    FORCE  ROW LEVEL SECURITY;
ALTER TABLE facts           ENABLE ROW LEVEL SECURITY;
ALTER TABLE facts           FORCE  ROW LEVEL SECURITY;
ALTER TABLE focus_charges   ENABLE ROW LEVEL SECURITY;
ALTER TABLE focus_charges   FORCE  ROW LEVEL SECURITY;
ALTER TABLE insights        ENABLE ROW LEVEL SECURITY;
ALTER TABLE insights        FORCE  ROW LEVEL SECURITY;
ALTER TABLE inconclusive    ENABLE ROW LEVEL SECURITY;
ALTER TABLE inconclusive    FORCE  ROW LEVEL SECURITY;
ALTER TABLE source_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_runs     FORCE  ROW LEVEL SECURITY;
ALTER TABLE insight_runs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE insight_runs    FORCE  ROW LEVEL SECURITY;

-- ============================================================================
-- Policies: one per table, identical shape.
-- USING  (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
--   — read-side filter. Applies to SELECT, UPDATE, DELETE.
-- WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
--   — write-side filter. Applies to INSERT, UPDATE.
--
-- We include WITH CHECK so a session can't insert/update a row under
-- someone else's tenant id even if it knows the row id. Defense in depth:
-- the application never sets tenant_id from a request field, but if a
-- future bug lets one through, the policy catches it.
--
-- `current_setting(name, true)` is the missing_ok variant: returns NULL
-- if the GUC was never set, instead of erroring. We rely on this for the
-- safe default (no tenant context = no rows visible).
-- ============================================================================

CREATE POLICY tenant_isolation_accounts ON accounts
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_resources ON resources
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_observations ON observations
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_facts ON facts
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_focus_charges ON focus_charges
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_insights ON insights
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_inconclusive ON inconclusive
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_source_runs ON source_runs
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation_insight_runs ON insight_runs
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- ============================================================================
-- Bypass for the migrator role (optional, V2).
--
-- When V2 lands, you'll likely want a separate "admin" role that can
-- query across tenants (cross-tenant reporting, support escalations).
-- The standard Postgres way is `BYPASSRLS` on that role. We don't
-- create that role here — V1 has no cross-tenant need — but this is
-- the seam where it plugs in:
--
--   CREATE ROLE constat_admin BYPASSRLS;
--   GRANT USAGE ON SCHEMA public TO constat_admin;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
--       TO constat_admin;
--
-- A BYPASSRLS role skips the policies entirely. The application
-- connection should NOT be this role; the application should always
-- be the constrained role so RLS does its job.
-- ============================================================================
