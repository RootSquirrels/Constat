# Alerting

> Roadmap scoreboard "Exploitabilité": 3 alerts wired to the metrics
> described in [`metrics.md`](./metrics.md). The rules live in
> [`deploy/prometheus/alerts.yml`](../../deploy/prometheus/alerts.yml);
> this doc is the runbook they link to.

Load the rules into Prometheus with:

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/alerts.yml   # copy of deploy/prometheus/alerts.yml
```

All 3 alerts are `warning` except the 5xx one (`critical`). None of them
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

**Operator action.**

1. Find the failing scope:
   ```sql
   SELECT region, error, finished_at FROM source_runs
   WHERE status = 'failed' ORDER BY finished_at DESC LIMIT 5;
   ```
2. Triage by error class (`AccessDenied` → fix the trust policy /
   ExternalId; `Throttling`/`Timeout` → transient, just re-run).
3. Re-scan only the failed region — `TargetAccount.regions` accepts a
   subset, so a targeted re-run does not re-scan healthy regions (see
   `apps/api/src/constat_api/collectors/aws.py`).
4. If a run is stuck in `running` (worker died), use `force=True` or let
   `cleanup_stuck_runs` free the scope.

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

## See also

- [`metrics.md`](./metrics.md) — metric names, labels, cardinality budget
- [`logging.md`](./logging.md) — request_id correlation
- [`backup-restore.md`](./backup-restore.md) — when the fix is "restore the DB"
