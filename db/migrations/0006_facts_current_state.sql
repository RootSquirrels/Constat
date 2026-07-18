-- 0006_facts_current_state_and_source_run_chain.sql
--
-- Two changes, both small, both cheap now (V1, no production data):
--
-- 1. facts: append-log -> current-state. The previous UNIQUE included
--    observed_at, which made every scan append a new row. The doc's
--    ResourceFactCurrent design is current-state: one row per (tenant,
--    resource, namespace, key, source). observed_at becomes the timestamp
--    of the most recent observation (semantic change, no rename).
--
-- 2. source_run_id FK on observations and facts. Without this link,
--    source_runs is a forensic table with no join path to the data it
--    proves. The chain is now: fact -> (its) source_run -> (run's) status.

-- 1. Drop the append-log UNIQUE
ALTER TABLE facts DROP CONSTRAINT uq_fact_snapshot;

-- 2. Add current-state UNIQUE (no observed_at)
ALTER TABLE facts ADD CONSTRAINT uq_fact_current UNIQUE
    (tenant_id, resource_id, namespace, key, source);

-- 3. Link facts to the most recent source run that observed them
ALTER TABLE facts ADD COLUMN last_source_run_id UUID
    REFERENCES source_runs(id) ON DELETE SET NULL;

-- 4. Link observations to their source run
ALTER TABLE observations ADD COLUMN source_run_id UUID
    REFERENCES source_runs(id) ON DELETE SET NULL;

-- 5. Indexes for joins
CREATE INDEX idx_observations_source_run ON observations(source_run_id)
    WHERE source_run_id IS NOT NULL;
CREATE INDEX idx_facts_last_source_run ON facts(last_source_run_id)
    WHERE last_source_run_id IS NOT NULL;
