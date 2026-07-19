-- 0014_audit_events_immutable.sql
-- CISO review requirement 3.4: make audit_events append-only a
-- TECHNICAL guarantee, not just a convention. Until now "append-only"
-- meant "the application code never issues UPDATE/DELETE" (see 0010)
-- — any compromised or buggy connection with DML rights could rewrite
-- or erase the very log that answers "who did what when".
--
-- What this migration does:
--   - constat_deny_audit_mutation(): trigger function that RAISEs on
--     any mutation attempt. One function serves both triggers.
--   - audit_events_no_update_delete: row-level BEFORE UPDATE OR DELETE
--     trigger. Fires per row, so even `DELETE FROM audit_events WHERE
--     false` is a no-op (no rows, no error) — only real mutation
--     attempts are denied.
--   - audit_events_no_truncate: statement-level BEFORE TRUNCATE
--     trigger. Row triggers never fire on TRUNCATE, so a separate
--     statement-level trigger is required to close that hole.
--
-- What stays allowed:
--   - INSERT. The log is append-only, not read-only: collectors,
--     the read-attribution hook, and system jobs keep appending.
--   - SELECT, obviously.
--
-- Retention contract (this overrides the seeded `audit_events`
-- retention policy from 0010):
--   Retention of audit_events is ARCHIVAL / EXPORT, never deletion.
--   If a future retention policy must purge audit_events rows, it
--   does so via a privileged migration that DROPs these triggers
--   first (as the table owner), purges, and re-creates the triggers
--   — never from the runtime role, and never silently. The runtime
--   role constat_app (0012) owns nothing, so it cannot DROP TRIGGER
--   or ALTER TABLE ... DISABLE TRIGGER; that is the point.
--
-- Rollback: DROP TRIGGER audit_events_no_truncate ON audit_events;
--           DROP TRIGGER audit_events_no_update_delete ON audit_events;
--           DROP FUNCTION constat_deny_audit_mutation();

CREATE OR REPLACE FUNCTION constat_deny_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is denied (migration 0014)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION constat_deny_audit_mutation();

CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION constat_deny_audit_mutation();
