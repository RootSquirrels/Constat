-- 0012_runtime_role.sql
-- Architecture doc §11.2: "rôle runtime non-owner, non-superuser, sans
-- BYPASSRLS". Until now the API connected as `constat`, the same role that
-- owns the tables and runs the migrations — owner FORCE RLS was the only
-- thing binding it, and it could ALTER POLICY or CREATE TABLE at will.
-- This migration creates `constat_app`, the role the API/collector/CLI
-- should connect as in any deployed environment.
--
-- What constat_app is:
--   - LOGIN, non-superuser, NO BYPASSRLS (Postgres default — we do not
--     grant it). It owns nothing: no tables, no policies, no schema.
--   - DML-only: SELECT/INSERT/UPDATE/DELETE on every table in schema
--     public. No DDL: it cannot CREATE/ALTER/DROP tables and cannot
--     ALTER POLICY (only the table owner can), so it cannot weaken or
--     drop the tenant isolation policies from 0007/0011.
--   - Fully bound by RLS: FORCE ROW LEVEL SECURITY (0007/0011) applies
--     to the owner; for a non-owner like constat_app, plain ENABLE
--     already binds it. Either way, every query is filtered by the
--     `app.current_tenant_id` GUC — the runtime role must still set it
--     per transaction, exactly like apps/api/src/constat_api/tenant.py
--     does today.
--
-- What constat (the owner) keeps: migrations, DDL, policy management.
-- The owner should NOT be the runtime connection anymore once this is
-- deployed (see docs/development/known-issues.md §2).
--
-- PASSWORD: 'constat' is for local/dev parity with docker-compose.yml
-- ONLY. Production MUST rotate it at provision time and store it in
-- Secrets Manager — see docs/operations/deployment.md.

-- ============================================================================
-- The role itself.
--
-- Roles are cluster-global, not schema objects: DROP SCHEMA public CASCADE
-- (which tests/test_rls.py runs before re-applying migrations) does NOT
-- drop them. The guard makes this file re-runnable against a cluster
-- where the role already exists.
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'constat_app') THEN
        CREATE ROLE constat_app LOGIN PASSWORD 'constat';
    END IF;
END
$$;

-- ============================================================================
-- Grants: connect, see the schema, DML on everything in it.
-- ============================================================================

GRANT CONNECT ON DATABASE constat TO constat_app;
GRANT USAGE ON SCHEMA public TO constat_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO constat_app;

-- Future tables: the owner (constat) runs the migrations, so any table it
-- creates later must be readable/writable by the runtime role without a
-- follow-up GRANT. Default privileges are per-creator-role — that is why
-- this is FOR ROLE constat, not a bare ALTER DEFAULT PRIVILEGES.
ALTER DEFAULT PRIVILEGES FOR ROLE constat
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO constat_app;
