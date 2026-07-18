-- 0013_insight_acks.sql
-- P1 item 1: minimal operator acknowledgment for insights.
--
-- The pilot needs a way for the operator to triage the daily
-- "12 critical" list: which ones are in flight, which are resolved,
-- which were dismissed as false positives. Without this, the
-- same insights regenerate every scan and the customer can't
-- separate signal from noise.
--
-- Three columns on `insights`, no new table:
--   - ack_status  : text, one of NULL | 'acknowledged' | 'in_progress'
--                   | 'resolved' | 'dismissed'. NULL = "open / not yet
--                   triaged" (the default).
--   - ack_at      : timestamptz, when the latest transition happened.
--                   Server-set on PATCH; the client never sends it.
--   - ack_by      : text, who acked (free-form string in V1 — no
--                   users table yet). Examples:
--                   'ops@prospect.com', 'jira-bot', 'platform-team'.
--
-- State transitions (the operator's mental model):
--   NULL              -> acknowledged   (operator saw it)
--   acknowledged      -> in_progress    (someone is working it)
--   in_progress       -> resolved       (fix is in production)
--   any state         -> dismissed       (false positive, won't fix)
--   any state         -> NULL            (un-ack, treat as fresh)
-- We don't enforce this in the schema — the API does. Keeping the
-- schema dumb keeps the migration reversible.
--
-- Audit: the latest (ack_status, ack_at, ack_by) is the current
-- state. We do NOT keep history in V1 ("minimal"). If a customer
-- needs history, V2 adds an `insight_acks` table (one row per
-- transition). Until then, last write wins.
--
-- Cardinality: ack_status is a small enum. ack_by is unbounded in
-- principle (a free-form string the operator enters), but in
-- practice it's a few distinct values per customer (email addresses,
-- team names). No index on it in V1; the pilot volume is small
-- enough to scan.

BEGIN;

-- ack_status: NULL means "open". The CHECK explicitly allows NULL
-- (NULL is the default).
ALTER TABLE insights
    ADD COLUMN IF NOT EXISTS ack_status TEXT
        CHECK (ack_status IN ('acknowledged', 'in_progress', 'resolved', 'dismissed')),
    ADD COLUMN IF NOT EXISTS ack_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ack_by TEXT;

-- Index for the inbox query ("show me open critical insights").
-- Partial: only the rows that still need triage are in the index.
-- Replaces the "scan all critical" pattern with a focused one.
CREATE INDEX IF NOT EXISTS idx_insights_open_critical
    ON insights (severity, computed_at DESC)
    WHERE ack_status IS NULL;

COMMENT ON COLUMN insights.ack_status IS
    'Operator triage state: NULL=open, acknowledged/in_progress/resolved/dismissed. PATCH /insights/{id}.';
COMMENT ON COLUMN insights.ack_at IS
    'When ack_status was last set. Server-set; client never sends it.';
COMMENT ON COLUMN insights.ack_by IS
    'Free-form operator identifier (email, team, bot). No users table in V1.';

COMMIT;
