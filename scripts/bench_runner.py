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

Usage:
    python scripts/bench_runner.py --resources 10000
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=int, default=10_000)
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
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
