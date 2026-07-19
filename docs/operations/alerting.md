# Alerting

> Roadmap scoreboard "Exploitabilité": 4 alerts wired to the metrics
> described in [`metrics.md`](./metrics.md). The rules live in
> [`deploy/prometheus/alerts.yml`](../../deploy/prometheus/alerts.yml);
> this doc is the runbook they link to. A 5th signal — the SQS DLQ
> CloudWatch alarm (`infra/sqs.tf`) — pages through SNS, not Prometheus,
> and is covered under [ConstatCollectItemsFailed](#constatcollectitemsfailed).

Load the rules into Prometheus with:

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/alerts.yml   # copy of deploy/prometheus/alerts.yml
```

All alerts are `warning` except the 5xx one (`critical`). None of them
pages during a single blip — every rule has a `for:` duration. If an
alert flaps, widen the window before lowering the threshold.

## ConstatSourceRunFailed

**Expr:** `increase(constat_source_run_total{status="failed"}[1h]) > 0`, for 15m.

**What it means.** A per-region AWS scan finished with `status="failed"`.
That region's inventory is now unproven: resources there keep their last
known facts, and after 24h without a successful run the scope goes
INCONCLUSIVE (`scope_stale`) — which is what the second alert catches.

**Threshold rationale.** Scans run on a cron; one failure in an hour is
already abnormal because the adaptive retry mode absorbs transient
throttling before the run can fail. `for: 15m` avoids firing on a run
that is retried and succeeds immediately.

**Operator action — targeted re-scan (chantier 1.4).**

The remediation is a **single API call**. The operator never opens psql
for this.

1. Identify the failed region and account from the alert labels /
   `worker` or `scan` log stream (the region is in the alert summary).
2. Re-scan exactly that region, forcing past any stuck `running` run:

   ```bash
   curl -sS -X POST "$CONSTAT_API_BASE/collect/aws" \
     -H "X-API-Key: $CONSTAT_OPERATOR_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "targets": [{
         "aws_account_id": "<account>",
         "role_arn": "arn:aws:iam::<account>:role/constat-collector",
         "external_id": "<external-id>",
         "regions": ["<failed-region>"]
       }],
       "force": true
     }'
   ```

   `regions` accepts a subset, so healthy regions are not re-scanned.
   `force: true` frees the scope if the previous run is stuck in
   `running` (worker died mid-scan); otherwise the re-scan would be
   rejected as a duplicate.
3. The call returns **202 + `job_id`** (async sqs mode). Follow the job
   to green:

   ```bash
   curl -sS "$CONSTAT_API_BASE/collect/aws/jobs/<job_id>" \
     -H "X-API-Key: $CONSTAT_OPERATOR_KEY"
   ```

   Repeat until the job status is terminal and the failed region shows
   success. (In `inline` mode the POST is synchronous — 200 with the
   per-region results in the body — and there is no job to poll.)
4. If the same region fails again, triage by error class from the job
   detail: `AccessDenied` → fix the prospect's trust policy / ExternalId;
   `Throttling` / `Timeout` → transient, re-run later.

**RBAC:** `POST /collect/aws` requires an **operator** API key
(`require_operator`); a reader key gets 403. Use the operator key from
your secret store, not the dashboard's reader key.

## ConstatScopeStaleIncreasing

**Expr:** `increase(constat_inconclusive_total{reason=~"scope_stale.*"}[6h]) > 0`, for 1h.

**What it means.** The rds_eol rule is emitting INCONCLUSIVE records
with reason `scope_stale`: the latest successful source_run for a scope
is older than the 24h freshness window, so insights degrade from
"proven" to "we don't know". This is the product's differentiator
working as intended — but an *increasing* count means the collection
pipeline is falling behind.

**Threshold rationale.** The freshness window is 24h; a 6h `increase`
window with `for: 1h` means the pipeline has been degrading for at
least an hour before anyone is alerted — a missed single cron tick does
not fire, a broken scheduler does.

**Operator action.**

1. Check whether scans are running at all: `constat_source_run_total`
   should have recent `status="success"` increases for every region.
2. If scans run but fail, this alert is downstream of
   `ConstatSourceRunFailed` — fix that first.
3. If no scans run, check the cron / Fargate task schedule.
4. Note the label caveat: the `reason` label carries the full
   human-readable string, so the rule matches with the `scope_stale.*`
   regex, not an exact label value.

## ConstatHTTP5xxRateHigh

**Expr:** 5xx share of `constat_http_requests_total` above 1% over 5m
(with a minimum-traffic guard), for 10m.

**What it means.** The API is erroring for real traffic. 5xx responses
fail the 99.9% availability SLO in `docs/architecture.md`.

**Threshold rationale.** 1% over 5 minutes is well above the SLO error
budget burn rate; the `sum(rate(...[5m])) > 0.1` guard (≥ ~6 req/min)
keeps the alert silent in quiet pilot windows where a single 500 would
otherwise read as "100% error rate". `for: 10m` filters deploy blips.

**Operator action.**

1. Correlate by `request_id`: every 5xx is logged at `error` level with
   `request.complete` / `request.failed` (see [`logging.md`](./logging.md)).
2. Check `/health` — the most common cause is Postgres unreachable.
3. The `/metrics` and `/health` endpoints are excluded from the counter,
   so scraper noise cannot trigger this alert.

## ConstatCollectItemsFailed

**Expr:** `increase(constat_collect_items_failed_total[15m]) > 0`, for 5m.

**What it means.** A collection WorkItem (account × region) failed at
the worker level. Two alerting paths cover the same failure, by deploy
mode:

- **sqs mode (staging/pilot):** the PRIMARY path is the CloudWatch alarm
  on the DLQ (`infra/sqs.tf`) — a WorkItem that fails 3 receives is
  parked in `constat-pilot-collect-dlq` and pages via SNS. The
  Prometheus rule is the secondary signal.
- **inline mode (local/dev):** there is no queue; failed items surface
  only through this counter.

**Operator action.**

1. Same targeted re-scan as
   [ConstatSourceRunFailed](#constatsourcerunfailed): POST `/collect/aws`
   with the failed account, `regions: ["<failed-region>"]`, `force: true`,
   then follow `GET /collect/aws/jobs/<job_id>` to green. The operator
   never opens psql for this.
2. If the CloudWatch DLQ alarm fired, inspect the DLQ message (console
   or `aws sqs receive-message --queue-url <dlq-url>`) before
   re-scanning: the WorkItem payload names the account and region.
   After a successful re-scan, drain the DLQ entry manually — draining
   is a deliberate operator action, not a code path.

## See also

- [`metrics.md`](./metrics.md) — metric names, labels, cardinality budget
- [`logging.md`](./logging.md) — request_id correlation
- [`backup-restore.md`](./backup-restore.md) — when the fix is "restore the DB"
