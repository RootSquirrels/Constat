# Benchmarks

Two different measurements live here:

- **Insight runner (local approximation)** — `scripts/bench_runner.py`,
  sqlite in-memory, synthetic dataset. Measures the runner's algorithmic
  shape, not production latency.
- **Réel (staging, 35 comptes)** — `scripts/bench_real.py`, the real
  async collection path (API → SQS → worker → AWS APIs → Postgres) on
  the staging environment. This is the number that decides whether the
  worker needs scaling (see the SCALING NOTE in `infra/ecs.tf`).

## Réel (staging, 35 comptes) — chantier 1.5

### Methodology

- Script: `scripts/bench_real.py`. It submits one `POST /collect/aws`
  job **per target account** (so per-account wall time is visible and a
  poison account does not hide the others), polls
  `GET /collect/aws/jobs/{job_id}` until every job is terminal, and
  prints:
  - wall time per job (account) and **total** wall time,
  - **per-region durations** taken from the server-side job detail,
  - the HTTP status codes seen (a flood of 5xx during the run would
    invalidate the timing).
- The run exercises the full deployed path: ALB → API (enqueue) → SQS →
  worker (`CONSTAT_WORKER_CONCURRENCY=4`) → cross-account AssumeRole →
  AWS APIs → Postgres writes. Exactly what an ICP-scale scan costs in
  production shape.
- What it **cannot** measure: AWS-side throttling detail — which API
  call was throttled, how long the adaptive retry absorbed before
  succeeding. That stays in the worker logs / CloudWatch
  (`constat_source_run_duration_seconds{status=...}`), not in the job
  detail. If a region is slow, the logs say why; this script says *that*
  it was slow.
- Requires an **operator** API key (`POST /collect/aws` is
  `require_operator`; a reader key gets 403).

### Command

```bash
export CONSTAT_API_KEY=<operator-key>
python scripts/bench_real.py \
  --base-url https://<staging-alb-dns> \
  --targets staging-targets.json \
  --poll-interval 5 --timeout 3600
```

(`staging-targets.json`: same shape as the `scan_targets_json` secret —
one entry per staged account with its `regions`.)

### Results

> **PENDING EXECUTION — chantier 0 staging gate.**
> The staging environment does not exist yet (the infra in `infra/` is
> unapplied as of 2026-07-18), so there are deliberately **no numbers
> here**. Do not fill this table with estimates; run the command above
> once staging is live and paste what the script printed.

| Date | Accounts | Regions | Jobs terminal | Total wall time | Slowest region | HTTP statuses |
|---|---|---|---|---|---|---|
| _pending_ | 35 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |

## Insight runner (local approximation)

Roadmap scalability item: *"Bench documenté à 10 k ressources — pas
d'optimisation avant la mesure."* This page is the measurement. No
optimization was done before or as part of it.

## 2026-07-18 — `run_rds_eol` at 1k and 10k resources

### Method

- Script: `scripts/bench_runner.py` (`python scripts/bench_runner.py --resources N`).
- Dataset: 1 account, 1 successful `source_run` (scopes proven), N
  `AWS::RDS::DBInstance` resources in `eu-west-1`, 4 facts each
  (`aws.rds.engine`, `engine_version`, `instance_class`, `vcpu`) —
  same shape as the test bootstrap in `tests/test_runner.py`.
  Version mix: 2/3 past-EOL (PG11/PG12 → insight emitted), 1/3 PG15
  (no insight).
- Wall time and peak memory (tracemalloc) are measured **around the
  `run_rds_eol` call only**; seeding time is reported separately.
- Storage: **sqlite in-memory** (StaticPool), single process, single
  session. Production runs on Postgres over a network — absolute
  numbers **will differ** (worse on latency-bound per-row statements).
  This measures the runner's algorithmic shape, not production latency.
- All 10k resources share one (account, region, type) scope, so the
  per-resource scope-proof query always hits the same successful run.

### Machine

- CPU: AMD Ryzen 5 3600 6-Core (12 logical processors)
- RAM: 31.9 GiB
- OS: Windows, Python 3.13 (project `.venv`)

### Measured numbers

| Resources | Facts | Run wall time | Peak mem (tracemalloc) | Resources/s | Insights emitted |
|---|---|---|---|---|---|
| 1,000 | 4,000 | 3.04 s | 16.6 MiB | 329 | 668 |
| 1,000 (re-run, after runner refactor) | 4,000 | 3.26 s | 16.6 MiB | 307 | 668 |
| 10,000 (run 1) | 40,000 | 36.03 s | 162.5 MiB | 278 | 6,668 |
| 10,000 (run 2) | 40,000 | 32.23 s | 162.5 MiB | 310 | 6,668 |
| 10,000 (run 3, after runner refactor) | 40,000 | 31.29 s | 162.5 MiB | 320 | 6,668 |

Runs 1–2 were measured against the pre-refactor `run_rds_eol`; run 3 was
measured the same day after `runner.py` gained the aurora_eol / mysql_eol
rule imports. Numbers are consistent within noise — the refactor did not
measurably change rds_eol throughput on this dataset.

Seeding (not counted above): 0.45 s at 1k, ~4.4–4.6 s at 10k.
Zero errors, zero inconclusive in all runs (scope proven, facts complete).

### Conclusion vs the roadmap threshold (50k)

- Throughput at 10k is **~280–330 resources/s** and roughly linear
  (10× resources → ~11× time), memory linear (~16 MiB → ~163 MiB
  traced).
- Extrapolating linearly, **50k resources ≈ 3 minutes** on this
  hardware with sqlite — comfortably inside any cron/Fargate window.
  **No optimization is justified by this measurement.** The known
  per-resource scope-proof query (one SELECT per resource) and
  per-insight INSERT are the obvious candidates *if* Postgres
  round-trips change the picture, but per the roadmap rule we do not
  touch them without a Postgres-backed measurement.
- Next action point at 50k: re-run this script against a real
  Postgres (docker-compose) before deciding anything.
