# Inconclusive cleanup (UX/ops P2 item 8)

> The `inconclusive` table grows without bound. A "missing fact"
> listed 6 months ago is no longer actionable. This document is
> the runbook for scheduled cleanup.

## What gets deleted

Records in the `inconclusive` table whose `computed_at` is older
than the threshold. Default: 30 days.

The cutoff is strict (`<`): a record whose `computed_at` is
exactly at the threshold survives. The first re-evaluation of the
same `(rule_name, resource_id, missing_facts)` after deletion
re-emits a fresh record with `computed_at = now`.

## How to invoke

### CLI

```bash
# Audit only — count without deleting
python -m constat_api.cli.cleanup_inconclusives --older-than 30 --dry-run

# Actually delete
python -m constat_api.cli.cleanup_inconclusives --older-than 30

# Different threshold
python -m constat_api.cli.cleanup_inconclusives --older-than 7
```

The CLI returns 0 on success and prints the count of deleted (or
eligible) records. Returns 1 on a missing threshold, 2 on a runtime
error.

### HTTP

```bash
curl -X POST 'http://localhost:8000/admin/cleanup-inconclusives?older_than_days=30' \
  -H 'X-API-Key: $CONSTAT_API_KEY'
```

```json
{ "older_than_days": 30, "deleted": 17 }
```

The endpoint is idempotent. Calling twice in the same hour is safe
(the second call returns `deleted: 0`). The endpoint requires the
`X-API-Key` header (same auth as the rest of the API).

## Recommended schedule

V1 default: **once per day, threshold 30 days.** Rationale:

- A `missing_facts` record that hasn't been re-emitted in 30 days
  is almost certainly about a resource that has been retired, a
  scope that was deleted, or a fact that was never going to
  resolve. None of these are actionable past the 30-day window.
- Daily is enough granularity: the table grows by ~N/day where N
  is the rate of new INCONCLUSIVE emissions (typical pilot: 1-10
  per rule per day). A monthly cleanup would still be fine.

Adjust the threshold per tenant if needed: a prospect with high
churn and a tight attention budget might want 7 days. A prospect
with low churn and a long audit history might want 90 days.

## How to schedule

### Linux cron

```cron
# /etc/cron.d/constat-cleanup
0 3 * * * constat curl -fsS -X POST \
  'http://localhost:8000/admin/cleanup-inconclusives?older_than_days=30' \
  -H "X-API-Key: ${CONSTAT_API_KEY}" \
  >> /var/log/constat-cleanup.log 2>&1
```

### Windows Task Scheduler

```powershell
# In Task Scheduler → Create Task
# Program: powershell
# Arguments: -NoProfile -Command "irm -Method POST -Headers @{ 'X-API-Key' = $env:CONSTAT_API_KEY } 'http://localhost:8000/admin/cleanup-inconclusives?older_than_days=30'"
# Trigger: Daily, 03:00
```

### k8s CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: constat-inconclusive-cleanup
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: cleanup
              image: your-registry/constat-api:latest
              command: ["python", "-m", "constat_api.cli.cleanup_inconclusives", "--older-than", "30"]
              env:
                - name: CONSTAT_DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: constat-secrets
                      key: database-url
          restartPolicy: OnFailure
```

## What about the runner? (the alternative)

The user spec mentioned an alternative: add a `valid_until` column
to `inconclusive` and have the runner skip re-emitting records that
are still valid. We chose the cleanup-job approach because:

- **The cleanup is idempotent and simple.** One endpoint, one
  query, no schema change.
- **The runner-skip is a different feature.** It changes the
  runner's emit-or-skip logic, which is a hot path. Cleanup is a
  cold path; cold paths are cheaper to get right.
- **The runner-skip hides stale data longer.** A 30-day-old
  missing fact might be relevant again if the underlying issue
  resurfaces; a 30-day TTL with a fresh emission on next scan
  makes the resurrection visible.

We can add `valid_until` in V2 if the cleanup cadence becomes
operationally painful.

## Monitoring the cleanup

Each call logs an `info`-level access line via the middleware
(method=`POST`, path=`/admin/cleanup-inconclusives`). The number
of deleted rows is in the response body.

Alerts we recommend:

- **No cleanup ran in 24h** (the cron didn't fire). Alert on
  absence of the access log line.
- **`deleted` count > some threshold per run** (something is
  producing INCONCLUSIVE records at a high rate). Suggests a
  upstream issue (broken catalog, broken IAM).

## What is NOT in V1

- **Per-rule cleanup.** The endpoint deletes across all rules.
  Per-rule scoping is `--rule` dry-run only. If you need it for
  real, add a `?rule_name=rds_eol` query param to the endpoint.
- **Soft delete (tombstones).** A deleted record cannot be
  recovered. If the audit history matters, the answer is to ship
  the deletions to S3 before dropping them in Postgres. V2.
- **Cleanup of stale `source_runs` or `insight_runs`.** Same
  problem, different table. Add a separate job when needed.

## See also

- [`../api/endpoints.md`](../api/endpoints.md) — the
  `/admin/cleanup-inconclusives` endpoint
- [`../concepts.md`](../concepts.md) — what `Inconclusive` means
- [`../development/running-the-stack.md`](../development/running-the-stack.md) —
  the demo path
