# Benchmarks — insight runner

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
