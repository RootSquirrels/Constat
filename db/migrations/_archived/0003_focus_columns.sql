-- 0003_focus_columns.sql
-- FOCUS 1.0 conformance: drop effective_cost (redundant with amortized_cost
-- in FOCUS 1.0, which maps to FOCUS EffectiveCost), add resource_id and
-- sub_account_id for cost-to-resource attribution.
--
-- Source: https://focus.finops.org/focus-specification/v1-0/
-- FOCUS 1.0 has EffectiveCost (amortized), no AmortizedCost column.
-- FOCUS 1.0 has ResourceId and SubAccountId as required-by-spec columns
-- for cost-to-resource attribution.

ALTER TABLE focus_charges ADD COLUMN resource_id TEXT;
ALTER TABLE focus_charges ADD COLUMN sub_account_id TEXT;
ALTER TABLE focus_charges DROP COLUMN IF EXISTS effective_cost;

CREATE INDEX idx_focus_charges_resource ON focus_charges(resource_id)
    WHERE resource_id IS NOT NULL;
CREATE INDEX idx_focus_charges_sub_account ON focus_charges(sub_account_id)
    WHERE sub_account_id IS NOT NULL;
