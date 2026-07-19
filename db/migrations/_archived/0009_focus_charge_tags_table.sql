-- 0009_focus_charge_tags_table.sql
-- V2 per-row tag storage. Replaces the V1 'tags JSONB' column's
-- even-split approximation with a real per-input-row table.
--
-- Background (V1 limitation, P3 item 11): focus_charges.tags stored the
-- list of unique tag dicts seen across the input FOCUS rows. The
-- chargeback_by_tag runner then attributed cost by EVENLY splitting
-- across the unique values (1/N per value), which is wrong when the
-- input is heterogeneous: e.g. 3 rows for Application=web and 1 row
-- for Application=api would give 50/50, not 75/25.
--
-- V2 fix: a per-input-row tag table. For each (focus_charge, input row),
-- we record the tag dict of that row. The runner counts rows per
-- (tag_key, tag_value) and attributes cost proportionally.
--
-- Storage growth: FOCUS exports are typically O(10k) rows/month with
-- ~3 tags/row. That's ~30k rows/month per tenant. Index on
-- (key, value) keeps the runner's GROUP BY fast.
--
-- NO unique constraint on (focus_charge_id, key, value): a single
-- focus_charge representing N input rows will have N rows for the
-- same (key, value) — once per contributing row. The count IS the
-- signal that drives proportional attribution. Adding UNIQUE would
-- collapse duplicates and silently break the V2 fix.

CREATE TABLE focus_charge_tags (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    focus_charge_id BIGINT NOT NULL REFERENCES focus_charges(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE INDEX idx_focus_charge_tags_charge ON focus_charge_tags(focus_charge_id);
CREATE INDEX idx_focus_charge_tags_kv ON focus_charge_tags(key, value);
