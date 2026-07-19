-- 0008_focus_charge_tags.sql
-- Tag-based chargeback aggregation. Stores the list of unique FOCUS Tags
-- dicts seen for each (account, service, period) row, so the chargeback
-- runner can re-aggregate by any tag key (Application, CostCenter, ...).
--
-- Why a list, not a single dict? When FOCUS rows for the same (service,
-- period) carry heterogeneous tag values (e.g. 3 rows for Application=web
-- and 1 for Application=api), the runner needs all of them to compute a
-- correct per-tag-value breakdown. Storing only the mode would silently
-- drop the minority.
--
-- Default '[]' (empty list) so existing rows stay valid; the new
-- ingestions overwrite with the real list.

ALTER TABLE focus_charges ADD COLUMN tags JSONB NOT NULL DEFAULT '[]'::jsonb;
