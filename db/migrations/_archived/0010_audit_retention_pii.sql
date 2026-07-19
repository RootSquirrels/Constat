-- 0010_audit_retention_pii.sql
-- V1 security & compliance primitives. DORA / ISO 27001 / GDPR-friendly
-- foundations for talking to prospects. Three tables:
--
-- 1. audit_events: append-only log of "who did what when". The first
--    thing a security questionnaire asks. Every privileged action
--    (AWS scan, insights run, retention cleanup, login) records
--    here with no PII in the metadata.
--
-- 2. retention_policies: declarative "delete N days after creation"
--    per table. The GDPR / SOC2 question is not "do you delete data?"
--    but "do you delete it automatically, on a schedule, with proof?"
--    This table is the proof.
--
-- 3. pii_classifications: per-field sensitivity label (public /
--    internal / confidential / restricted) + SHA-256 of the value
--    so we can detect duplicates without storing PII. Wired into
--    the AWS collector at ingest time. The first thing a privacy
--    questionnaire asks: "where does customer PII live and how is
--    it classified?".

-- ============================================================================
-- audit_events
-- ============================================================================
--
-- Append-only by convention. No UPDATE/DELETE in the application code.
-- (V2: enforce via Postgres trigger if we want belt-and-suspenders.)
--
-- `actor` is the WHO: "api_key:<id_hash>" or "system:<job_name>". Never
-- the raw API key value. Hashing the API key id at log time means we
-- can answer "which key accessed X" without storing the secret.
--
-- `metadata` is JSONB but with a strict contract: counts, durations,
-- rule names, region names. Never raw account_id, ARN, tag values, or
-- any other customer-identifying field. The AuditLogger class enforces
-- this on the Python side; we should also add a CHECK constraint
-- banning known-PII field names if the questionnaire pushes for it.

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_audit_events_tenant_occurred ON audit_events(tenant_id, occurred_at DESC);
CREATE INDEX idx_audit_events_actor ON audit_events(actor);
CREATE INDEX idx_audit_events_action ON audit_events(action);

-- ============================================================================
-- retention_policies
-- ============================================================================
--
-- One row per (tenant_id, table_name). Default retention days are
-- seeded by the application on first boot (the RetentionPolicy
-- repository bootstraps the table with sensible defaults). Operators
-- can override per-tenant in V2.
--
-- `table_name` is intentionally a free string (not a Postgres enum)
-- so we can add new auditable tables without a migration. The
-- RetentionRunner validates the table_name against an allow-list
-- before issuing the DELETE.
--
-- We do NOT cascade delete from the application — the
-- `retention_applies_at` column lets us plan deletions and verify
-- after the fact.

CREATE TABLE retention_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    table_name TEXT NOT NULL,
    retention_days INT NOT NULL CHECK (retention_days >= 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_applied_at TIMESTAMPTZ,
    last_deleted_count INT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, table_name)
);

CREATE INDEX idx_retention_policies_tenant ON retention_policies(tenant_id);

-- ============================================================================
-- pii_classifications
-- ============================================================================
--
-- Per (resource_type, resource_id, field_name) row. Records:
--   - the field's sensitivity level
--   - SHA-256 of the value (so we can detect duplicates without
--     storing the PII itself)
--   - the original value's hash bucket (e.g. SHA-256[0:8] for
--     quick uniqueness checks across the table)
--
-- Inserted by the AWS collector at ingest time. V1 doesn't enforce
-- row-level encryption or tokenization — that's V2. V1 just labels
-- + hashes so we have an answer to "what's the sensitivity of
-- account 111111111111's account_id field?".

CREATE TABLE pii_classifications (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    resource_type TEXT NOT NULL,  -- 'account', 'resource', 'focus_charge', 'tag'
    resource_id TEXT NOT NULL,
    field_name TEXT NOT NULL,     -- 'account_id', 'arn', 'tag:Application', etc.
    sensitivity TEXT NOT NULL CHECK (
        sensitivity IN ('public', 'internal', 'confidential', 'restricted')
    ),
    value_hash TEXT NOT NULL,     -- SHA-256 hex
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pii_classifications_resource
    ON pii_classifications(tenant_id, resource_type, resource_id);
CREATE INDEX idx_pii_classifications_sensitivity
    ON pii_classifications(tenant_id, sensitivity);
