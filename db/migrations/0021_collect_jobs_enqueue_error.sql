-- 0021_collect_jobs_enqueue_error.sql
-- SRE review findings 4 + 1b: outbox race reconciliation and the
-- collect -> evaluate chain state on collect_jobs.
--
-- What this migration does:
--   - collect_jobs.enqueue_error: POST /collect/aws now commits the job
--     row BEFORE enqueueing work items (transactional-outbox ordering).
--     If the queue send then fails, the job is KEPT (never rolled back —
--     a partially-sent SQS batch cannot be unsent) and the failure is
--     recorded here so the operator can reconcile: the 503 response
--     carries the job_id, and GET /collect/aws/jobs/{id} surfaces it.
--   - collect_jobs.evaluation_status: when a job's last work item is
--     acked, exactly one worker claims evaluation via an atomic
--     UPDATE ... WHERE evaluation_status IS NULL, runs every registered
--     insight rule, and records 'success' / 'failed' here. NULL means
--     "not claimed yet" (job still in flight or pre-0021 row).
--
-- RLS: none needed — collect_jobs already carries ENABLE + FORCE + the
-- tenant policy from 0015, and ADD COLUMN does not affect policies.
--
-- Note: the ORM model (orm.py) does NOT map these columns yet — a
-- parallel workstream owns that file. All access is via text() SQL in
-- repositories/collect_jobs.py until the ORM catches up.

ALTER TABLE collect_jobs ADD COLUMN enqueue_error TEXT;

ALTER TABLE collect_jobs ADD COLUMN evaluation_status TEXT
    CHECK (evaluation_status IN ('running', 'success', 'failed'));
