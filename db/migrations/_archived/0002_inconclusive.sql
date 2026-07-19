-- 0002_inconclusive.sql
-- Add the inconclusive table for the INCONCLUSIVE insight state.
-- Criterion n°15: when an evaluation cannot complete, the gap is surfaced,
-- never silently dropped.

CREATE TABLE inconclusive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name TEXT NOT NULL,
    resource_id UUID REFERENCES resources(id) ON DELETE CASCADE,
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
    missing_facts JSONB NOT NULL,         -- list[str], e.g. ["aws.rds.vcpu"]
    reason TEXT,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT inconclusive_scope_present CHECK (resource_id IS NOT NULL OR account_id IS NOT NULL)
);

CREATE INDEX idx_inconclusive_rule ON inconclusive(rule_name, computed_at DESC);
CREATE INDEX idx_inconclusive_resource ON inconclusive(resource_id, computed_at DESC)
    WHERE resource_id IS NOT NULL;
CREATE INDEX idx_inconclusive_account ON inconclusive(account_id, computed_at DESC)
    WHERE account_id IS NOT NULL;
