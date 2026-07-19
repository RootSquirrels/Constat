-- 0017_insight_events.sql
-- Roadmap 2.4: appeared/resolved history of insights.
--
-- The runner's delete-and-replace (audit F-03) wipes the insights table on
-- every run, so "when did this gap appear?" and "how much did we recover
-- when it closed?" were unanswerable. insight_events is the append-only
-- lifecycle log: the runner diffs fingerprints (sha256 of
-- rule_name|resource_id|title) before/after each run and writes one row
-- per appearance and one per resolution (with the last known monthly
-- amount = the money recovered).
--
-- What this migration does:
--   - insight_events: one row per lifecycle event. resource_id /
--     insight_run_id are ON DELETE SET NULL — history must survive the
--     retirement of the resource or the purge of old runs. account_id is
--     TEXT (not the accounts FK) for the same reason, and because
--     chargeback insights are account-scoped.
--   - Index (tenant_id, rule_name, occurred_at): the history endpoint
--     always filters by tenant first (RLS), then optionally by rule.
--   - RLS: the AGENTS.md invariant — new tenant-scoped table gets
--     ENABLE + FORCE + the standard GUC policy in the same migration,
--     identical in shape to 0007/0011/0015/0016. tests/test_rls.py
--     (Postgres CI job) pins the table list.
--
-- Grants for constat_app come from 0012's ALTER DEFAULT PRIVILEGES
-- (the owner runs this migration), so no explicit GRANT is needed.

CREATE TABLE insight_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    fingerprint TEXT NOT NULL,               -- sha256(rule_name|resource_id|title), hex
    rule_name TEXT NOT NULL,
    resource_id UUID REFERENCES resources(id) ON DELETE SET NULL,
    account_id TEXT,                         -- internal account id, as text (no FK: history)
    title TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('appeared', 'resolved')),
    monthly_usd DOUBLE PRECISION,            -- NULL when the insight carries no amount
    insight_run_id UUID REFERENCES insight_runs(id) ON DELETE SET NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_insight_events_tenant_rule_time
    ON insight_events(tenant_id, rule_name, occurred_at);

ALTER TABLE insight_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE insight_events FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_insight_events ON insight_events
    FOR ALL
    TO PUBLIC
    USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
