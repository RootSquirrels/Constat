"""Benchmark: run_rds_eol over N seeded RDS resources.

Roadmap scalability item: "Bench documenté à 10 k ressources — pas
d'optimisation avant la mesure". This script is the measurement; the
write-up lives in docs/operations/benchmarks.md.

Method (see the doc for the honest caveats):
- sqlite in-memory via StaticPool (same setup as tests/conftest.py),
  single process, single session.
- Seed: 1 account, 1 successful source_run (so scopes are proven),
  N resources (AWS::RDS::DBInstance, eu-west-1), 4 facts each
  (aws.rds.engine / engine_version / instance_class / vcpu) mirroring
  the test bootstrap in tests/test_runner.py.
- Measure: wall time and peak Python memory (tracemalloc) around the
  `run_rds_eol` call only — seeding time is reported separately.

Time series (V):
- Each run appends a JSONL row to `scripts/.bench_history.jsonl`
  (gitignored). The file is the source of truth for the regression
  alert; no external DB, no Prometheus.
- The `--check` flag compares the latest row against the median of
  the previous 5 rows. A >20% regression on `wall_time_s` OR
  `peak_mib` exits non-zero. The first run is the baseline, no
  comparison possible.
- CI calls `bench_runner.py --resources 1000 --check` so the
  alert runs on every PR. 1k resources is fast (~1-2s) — enough
  to catch a >20% regression without making the CI gate slow.

Usage:
    python scripts/bench_runner.py --resources 10000
    python scripts/bench_runner.py --resources 10000 --check
    python scripts/bench_runner.py --resources 1000 --check \
        --history /tmp/bench.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

# Wire the workspace src/ paths the same way tests/conftest.py does, so
# the script runs from a bare checkout without an editable install.
ROOT = Path(__file__).resolve().parent.parent
SRC_PATHS = [
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "connectors" / "aws_rds" / "src",
    ROOT / "packages" / "connectors" / "focus" / "src",
    ROOT / "packages" / "insights" / "rds_eol" / "src",
    ROOT / "packages" / "insights" / "aurora_eol" / "src",
    ROOT / "packages" / "insights" / "mysql_eol" / "src",
    ROOT / "packages" / "insights" / "chargeback" / "src",
    ROOT / "apps" / "api" / "src",
]
for p in SRC_PATHS:
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)

from constat_api.insights.runner import DEFAULT_SOURCE, run_rds_eol  # noqa: E402
from constat_api.orm import Base, ResourceORM  # noqa: E402
from constat_api.repositories import accounts as accounts_repo  # noqa: E402
from constat_api.repositories import facts as facts_repo  # noqa: E402
from constat_api.repositories import source_runs as source_runs_repo  # noqa: E402
from constat_api.settings import DEFAULT_TENANT_ID  # noqa: E402
from constat_core.models import Fact, ValueState  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

REGION = "eu-west-1"
RESOURCE_TYPE = "AWS::RDS::DBInstance"
OBSERVED_AT = datetime(2026, 7, 18, tzinfo=UTC)

# Version mix across the fleet: PG11 + PG12 are past EOL (emit an
# insight), PG15 is far from EOL (no insight). Roughly 2/3 insights.
_VERSION_MIX = ["11.22", "11.22", "12.13", "12.13", "15.4", "15.4"]

# Default history file. Local to the repo (not the user's home
# dir) so `git clean` doesn't reach it and so CI can read it
# across runs in the same checkout. Gitignored (V).
DEFAULT_HISTORY = Path(__file__).resolve().parent / ".bench_history.jsonl"

# Regression threshold. The roadmap's V calls for ">20%". Higher
# means more tolerance (fewer false alarms from a single cold
# run); lower means earlier warning. 20% is a balance — large
# enough to absorb noise, small enough to catch a real
# algorithmic regression (e.g., N+1 query introduced).
REGRESSION_THRESHOLD_PCT = 20.0

# How many historical rows to use for the baseline. 5 gives a
# stable median; 1 would be the immediate previous run (too
# noisy); 20+ would over-smooth.
BASELINE_WINDOW = 5


def seed(session: Session, n_resources: int) -> None:
    """Seed account + proven scope + N resources with their 4 facts each."""
    acc = accounts_repo.get_or_create(session, "111111111111")

    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region=REGION,
        resource_type=RESOURCE_TYPE,
        source=DEFAULT_SOURCE,
    )
    assert run is not None
    source_runs_repo.finish_run(session, run, status="success", resources_found=n_resources)
    session.commit()

    # Chunked so the unit-of-work flush stays bounded.
    chunk = 1000
    for start in range(0, n_resources, chunk):
        stop = min(start + chunk, n_resources)
        resources = [
            ResourceORM(
                tenant_id=DEFAULT_TENANT_ID,
                account_id=acc.id,
                region=REGION,
                resource_type=RESOURCE_TYPE,
                native_id=f"arn:aws:rds:{REGION}:111111111111:db:bench-{i:06d}",
            )
            for i in range(start, stop)
        ]
        session.add_all(resources)
        session.flush()  # assign resource IDs before building facts

        facts: list[Fact] = []
        for i, resource in zip(range(start, stop), resources, strict=True):
            version = _VERSION_MIX[i % len(_VERSION_MIX)]
            for key, value in (
                ("engine", "postgres"),
                ("engine_version", version),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 4),
            ):
                facts.append(
                    Fact(
                        resource_id=resource.id,
                        account_id=str(acc.id),
                        namespace="aws.rds",
                        key=key,
                        value=value,
                        value_state=ValueState.KNOWN,
                        source=DEFAULT_SOURCE,
                        observed_at=OBSERVED_AT,
                    )
                )
        facts_repo.insert_facts(session, facts)
        session.commit()


def _append_history(
    path: Path,
    *,
    resources: int,
    wall_s: float,
    peak_mib: float,
    rate: float,
    insights_emitted: int,
) -> None:
    """Append one JSONL row to the history file.

    The row is the canonical "one bench run" record. Fields are
    stable (no nested objects) so a future Prometheus exporter or
    a Jupyter analysis can read the file as a CSV-friendly
    stream. `seed_s` is the time spent seeding (orthogonal to
    the rule's perf); we record it but the alert only fires on
    `wall_s` and `peak_mib`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "resources": resources,
        "wall_s": round(wall_s, 4),
        "peak_mib": round(peak_mib, 2),
        "rate": round(rate, 2),
        "insights_emitted": insights_emitted,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _load_history(path: Path) -> list[dict]:
    """Read all rows from the JSONL history. Returns an empty
    list if the file doesn't exist yet (first run, no baseline)."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip malformed lines (e.g., a row written by a
            # future schema that this older bench doesn't know
            # about — forward-compat for the history file).
            continue
    return rows


def _check_regression(
    latest: dict,
    history: list[dict],
    *,
    threshold_pct: float = REGRESSION_THRESHOLD_PCT,
    window: int = BASELINE_WINDOW,
) -> tuple[int, str]:
    """Compare the latest run to the median of the last N runs.

    Returns (exit_code, message):
    - exit_code 0: no alert (first run, or no regression)
    - exit_code 1: a regression > threshold_pct on wall_s or
      peak_mib

    The message is human-readable (CI logs read it). The math
    uses the median (not the mean) to absorb a single cold-start
    outlier without alerting on it; 5 rows is enough to stabilize
    the median for the typical run-to-run noise of the bench.
    """
    if not history:
        return 0, (
            f"first run recorded: {latest['wall_s']:.2f}s, "
            f"{latest['peak_mib']:.1f} MiB "
            f"(baseline will be the median of the next {window} runs)"
        )

    # The last `window` rows BEFORE the latest. We don't include
    # the latest itself in the baseline (we're comparing to it).
    baseline_rows = history[-window:] if len(history) >= window else history
    if len(baseline_rows) < 2:
        # Not enough history to compute a stable median.
        return 0, (
            f"only {len(baseline_rows)} historical row(s); "
            f"need at least 2 for a baseline. Latest: "
            f"{latest['wall_s']:.2f}s, {latest['peak_mib']:.1f} MiB"
        )

    base_wall = statistics.median(r["wall_s"] for r in baseline_rows)
    base_mem = statistics.median(r["peak_mib"] for r in baseline_rows)
    latest_wall = latest["wall_s"]
    latest_mem = latest["peak_mib"]

    # A regression is `latest > baseline * (1 + threshold/100)`.
    # Improvements (latest < baseline) are reported but never alert.
    regressions: list[str] = []
    if base_wall > 0:
        wall_pct = (latest_wall - base_wall) / base_wall * 100
        if wall_pct > threshold_pct:
            regressions.append(
                f"wall_time_s regressed {wall_pct:+.1f}% "
                f"(latest={latest_wall:.2f}s, baseline_median={base_wall:.2f}s)"
            )
    if base_mem > 0:
        mem_pct = (latest_mem - base_mem) / base_mem * 100
        if mem_pct > threshold_pct:
            regressions.append(
                f"peak_mib regressed {mem_pct:+.1f}% "
                f"(latest={latest_mem:.1f} MiB, baseline_median={base_mem:.1f} MiB)"
            )

    if regressions:
        return 1, (
            f"REGRESSION > {threshold_pct}% on {' AND '.join(regressions)}\n"
            f"history: {len(baseline_rows)} baseline row(s) "
            f"(window={window})"
        )
    return 0, (
        f"OK: latest wall_s={latest_wall:.2f} (baseline median {base_wall:.2f}), "
        f"peak_mib={latest_mem:.1f} (baseline median {base_mem:.1f}); "
        f"no metric regressed > {threshold_pct}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=int, default=10_000)
    parser.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY,
        help="JSONL history file (default: scripts/.bench_history.jsonl).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="After measuring, compare the latest row to the median of "
        "the previous N rows and exit non-zero on >20%% regression. "
        "No-op on the first run (no baseline yet).",
    )
    args = parser.parse_args()

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with session_factory() as session:
        seed_started = time.perf_counter()
        seed(session, args.resources)
        seed_s = time.perf_counter() - seed_started
        print(f"seed: {args.resources} resources, {4 * args.resources} facts in {seed_s:.2f}s")

        tracemalloc.start()
        started = time.perf_counter()
        result = run_rds_eol(session)
        wall_s = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    peak_mib = peak_bytes / (1024 * 1024)
    rate = args.resources / wall_s if wall_s > 0 else float("inf")

    print(f"resources_scanned : {result.resources_scanned}")
    print(f"insights_emitted  : {result.insights_emitted}")
    print(f"inconclusive      : {result.inconclusive_emitted}")
    print(f"errors            : {len(result.errors)}")
    print(f"wall_time_s       : {wall_s:.2f}")
    print(f"peak_tracemalloc  : {peak_mib:.1f} MiB")
    print(f"resources_per_s   : {rate:.0f}")

    if result.errors:
        return 1

    # Persist the row to the history. We do this AFTER the
    # human-readable print so the operator sees the same numbers
    # in the terminal and the file (no drift between the two).
    latest_row = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "resources": args.resources,
        "wall_s": round(wall_s, 4),
        "peak_mib": round(peak_mib, 2),
        "rate": round(rate, 2),
        "insights_emitted": result.insights_emitted,
    }
    _append_history(
        args.history,
        resources=latest_row["resources"],
        wall_s=latest_row["wall_s"],
        peak_mib=latest_row["peak_mib"],
        rate=latest_row["rate"],
        insights_emitted=latest_row["insights_emitted"],
    )
    print(f"history           : appended to {args.history}")

    if args.check:
        # We exclude the row we just wrote (the latest) from the
        # baseline — the baseline is the history BEFORE this run.
        history = _load_history(args.history)
        # The last row is the one we just appended.
        baseline_history = history[:-1]
        exit_code, message = _check_regression(latest_row, baseline_history)
        print(f"check             : {message}")
        return exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())
