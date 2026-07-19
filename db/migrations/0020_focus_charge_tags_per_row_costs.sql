-- 0020_focus_charge_tags_per_row_costs.sql
-- The audit committee (FinOps re-audit) flagged the chargeback resolver
-- for weighting tag attribution by row count, not cost. A focus_charge
-- representing 2 input FOCUS rows (3 EUR web + 97 EUR api) gave 50/50
-- under the row-count weighting; the committee's deal-breaker was
-- "an attribution by tag that follows the number of lines rather
-- than their cost".
--
-- The V2 even-split in migration 0009 was a step forward (1/N per
-- unique value -> proportional to per-row count), but it still did
-- not weight by cost. A row of 3 EUR counted the same as a row of
-- 97 EUR.
--
-- This migration adds the per-input-row cost columns to
-- focus_charge_tags, plus an input_row_index to group tags that
-- came from the same input row. The runner reads per-row (cost, tag)
-- pairs and the resolver attributes each input row's own cost to
-- its tag value. 3 EUR web + 97 EUR api -> 3 EUR web / 97 EUR api
-- (3% / 97%), not 50 EUR / 50 EUR.
--
-- Defaults for pre-0020 rows: input_row_index=0, billed_cost=0,
-- amortized_cost=0. The resolver falls back to V2 row-count
-- weighting for any row with billed_cost=0 (the per-row cost is
-- missing). The full FocusCharge.billed_cost is attributed to
-- UNTAGGED for charges with no per-row cost data. This is
-- best-effort, not silent: the migration writes an audit_events
-- row, and the fallback is documented in the resolver.

ALTER TABLE focus_charge_tags
    ADD COLUMN input_row_index INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN billed_cost NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD COLUMN amortized_cost NUMERIC(20, 6) NOT NULL DEFAULT 0;

COMMENT ON COLUMN focus_charge_tags.input_row_index IS
    '0-based index of the input FOCUS row within the focus_charge. '
    'All focus_charge_tags rows for the same input row share this '
    'index, which is how the resolver reconstructs per-row data.';

COMMENT ON COLUMN focus_charge_tags.billed_cost IS
    'Per-input-row BilledCost, preserved from the raw FOCUS line. '
    'Drives cost-weighted tag attribution in the chargeback resolver.';

COMMENT ON COLUMN focus_charge_tags.amortized_cost IS
    'Per-input-row EffectiveCost (FOCUS 1.0 amortized cost), preserved '
    'from the raw FOCUS line. Parallel to billed_cost.';

CREATE INDEX idx_focus_charge_tags_row_idx ON focus_charge_tags(focus_charge_id, input_row_index);

-- Audit trail for the migration: operators must know that pre-0020
-- focus_charge_tags rows have input_row_index=0, billed_cost=0,
-- amortized_cost=0 until the original FOCUS file is re-ingested. The
-- resolver falls back to row-count attribution (V2 weighting) for
-- any row with billed_cost=0; the full FocusCharge.billed_cost is
-- attributed to UNTAGGED. This is best-effort, not silent.
INSERT INTO audit_events (tenant_id, action, actor, target_type, target_id, metadata)
SELECT
    '00000000-0000-0000-0000-000000000001'::UUID,
    'focus_charge_tags_backfill_per_row_costs',
    'system:migration_0020',
    'table',
    'focus_charge_tags',
    jsonb_build_object(
        'backfilled_rows', COUNT(*),
        'assumed_billed_cost', 0,
        'assumed_amortized_cost', 0,
        'assumed_input_row_index', 0,
        'rationale', 'migration 0020: per-row costs were not stored '
                      'before this migration. The resolver falls back to '
                      'row-count attribution (V2 weighting) for any row '
                      'with billed_cost=0; the full FocusCharge.billed_cost '
                      'is attributed to UNTAGGED. Re-ingest the FOCUS file '
                      'to recover per-row cost data.',
        'migration_date', CURRENT_DATE
    )
FROM focus_charge_tags;
