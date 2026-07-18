-- 0001_init.sql — Constat V1 schema (6 tables)
--
-- Conventions:
--   * All timestamps TIMESTAMPTZ in UTC.
--   * UUIDs as primary keys (gen_random_uuid from pgcrypto).
--   * tenant_id reserved for V2 (RLS); not used in V1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- accounts: prospect AWS accounts we monitor
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id TEXT NOT NULL UNIQUE,  -- AWS account ID (12 digits)
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- resources: stable identity of a cloud resource
CREATE TABLE resources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    resource_type TEXT NOT NULL,   -- e.g. "AWS::RDS::DBInstance"
    native_id TEXT NOT NULL,       -- e.g. ARN
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,        -- null = active; only set when a complete scan proves it
    UNIQUE (account_id, region, resource_type, native_id)
);

CREATE INDEX idx_resources_account_type ON resources(account_id, resource_type);
CREATE INDEX idx_resources_active ON resources(account_id) WHERE retired_at IS NULL;

-- observations: immutable source data, replayable from S3/Parquet
CREATE TABLE observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    source TEXT NOT NULL,  -- e.g. "aws_rds", "focus"
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_observations_resource ON observations(resource_id, observed_at DESC);
CREATE INDEX idx_observations_source ON observations(source, observed_at DESC);

-- facts: current values, namespaced
CREATE TABLE facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id UUID REFERENCES resources(id) ON DELETE CASCADE,
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,  -- aws.* / catalog.* / cost.* / derived.*
    key TEXT NOT NULL,
    value JSONB,
    value_state TEXT NOT NULL CHECK (value_state IN ('KNOWN', 'UNKNOWN', 'STALE', 'ERROR')),
    source TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fact_scope_present CHECK (resource_id IS NOT NULL OR account_id IS NOT NULL)
);

CREATE INDEX idx_facts_resource ON facts(resource_id, namespace, key) WHERE resource_id IS NOT NULL;
CREATE INDEX idx_facts_account ON facts(account_id, namespace, key) WHERE account_id IS NOT NULL;
CREATE INDEX idx_facts_observed ON facts(observed_at DESC);

-- focus_charges: FOCUS billing data (one row per account × service × period bucket)
CREATE TABLE focus_charges (
    id BIGSERIAL PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    service TEXT NOT NULL,
    region TEXT,
    pricing_category TEXT,
    billed_cost NUMERIC(18, 6) NOT NULL DEFAULT 0,
    amortized_cost NUMERIC(18, 6) NOT NULL DEFAULT 0,
    effective_cost NUMERIC(18, 6) NOT NULL DEFAULT 0,
    charge_count INT NOT NULL DEFAULT 1,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_focus_charges_account_period ON focus_charges(account_id, period_start);
CREATE INDEX idx_focus_charges_service ON focus_charges(service, period_start);

-- insights: computed gaps
CREATE TABLE insights (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name TEXT NOT NULL,
    resource_id UUID REFERENCES resources(id) ON DELETE CASCADE,
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title TEXT NOT NULL,
    payload JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT insight_scope_present CHECK (resource_id IS NOT NULL OR account_id IS NOT NULL)
);

CREATE INDEX idx_insights_rule ON insights(rule_name, computed_at DESC);
CREATE INDEX idx_insights_severity ON insights(severity, computed_at DESC);
CREATE INDEX idx_insights_account ON insights(account_id, computed_at DESC) WHERE account_id IS NOT NULL;

-- insight_runs: trace of rule execution
CREATE TABLE insight_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    resources_scanned INT,
    insights_emitted INT,
    error TEXT
);

CREATE INDEX idx_insight_runs_rule ON insight_runs(rule_name, started_at DESC);
