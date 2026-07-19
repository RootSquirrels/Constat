-- 0019_billing_currency.sql
-- The audit committee (FinOps / Cloud Cost re-audit) flagged the
-- product for treating all FOCUS amounts as USD regardless of
-- BillingCurrency. An export billed in EUR was labeled USD, then
-- "converted" to EUR in the restitution — a double-translation
-- error of ~10-20% on every line. The deal-breaker was: "any amount
-- presented as ACTUAL, 'confirmed by invoice' or 'avoidable'
-- without a traceable link to the FOCUS line, the currency, and
-- the exact cost component."
--
-- This migration makes BillingCurrency a first-class column on
-- focus_charges. The loader refuses any row where BillingCurrency
-- is missing (the FOCUS 1.0 spec REQUIRES it; if a prospect's
-- export drops it, the export is non-conformant and ingest fails
-- loud). Currency is preserved as-written (USD, EUR, GBP, ...);
-- conversion to a presentation currency is a display concern that
-- happens at restitution time, not at ingest time. That way the
-- displayed amount is always traceable back to a single FOCUS line
-- with a single currency.
--
-- Default for existing rows: a backfill cannot know the original
-- currency. We backfill to 'USD' (the most common FOCUS default
-- in 2026 exports) AND log a row in audit_events for the backfill
-- so an operator can later identify which rows were migrated
-- versus which were ingested post-deploy. The risk of a wrong
-- default is bounded: the backfill is visible, and re-ingesting
-- the same FOCUS file (which most prospects can regenerate) would
-- overwrite these rows with the real currency.

ALTER TABLE focus_charges
    ADD COLUMN billing_currency CHAR(3) NOT NULL DEFAULT 'USD';

COMMENT ON COLUMN focus_charges.billing_currency IS
    'ISO 4217 3-letter code. FOCUS 1.0 BillingCurrency column, '
    'preserved as-written. Conversion to a display currency happens '
    'at restitution time only.';

-- Audit trail for the backfill. Every pre-deploy row was forced
-- to 'USD' — operators must know that pre-0019 data may be
-- misclassified if the original export was non-USD.
INSERT INTO audit_events (tenant_id, action, actor, target_type, target_id, metadata)
SELECT
    '00000000-0000-0000-0000-000000000001'::UUID,
    'focus_charges_backfill_billing_currency',
    'system:migration_0019',
    'table',
    'focus_charges',
    jsonb_build_object(
        'backfilled_rows', COUNT(*),
        'assumed_currency', 'USD',
        'rationale', 'migration 0019: BillingCurrency was not in the schema; '
                      'existing rows were not labeled with the source currency. '
                      'Re-ingest the FOCUS file when possible to recover the real '
                      'currency. Pre-0019 USD labels on a non-USD export are '
                      'a known data-quality debt, not a silent assumption.',
        'migration_date', CURRENT_DATE
    )
FROM focus_charges;
