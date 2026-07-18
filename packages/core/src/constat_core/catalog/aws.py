"""AWS reference data. Versioned by date in the module docstring.

Last reviewed: 2026-07-18. Update when AWS publishes changes.
"""

from __future__ import annotations

from datetime import date

# RDS PostgreSQL major version -> end of standard support (= start of Extended Support).
# Source: https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/major-version-upgrade.html
# Major 16+ are LTS: no Extended Support pricing in scope as of 2026-07.
POSTGRES_EOL_DATE: dict[int, date] = {
    11: date(2024, 2, 29),
    12: date(2024, 2, 29),
    13: date(2025, 2, 28),
    14: date(2026, 2, 28),
    15: date(2027, 2, 27),
}

# RDS Extended Support price, per vCPU-hour (us-east-1 list price).
# Source: https://aws.amazon.com/rds/postgresql/pricing/
# Verify monthly.
EXT_SUPPORT_USD_PER_VCPU_HOUR: float = 0.20

# RDS instance class -> vCPU count.
# Subset of common ones; expand as needed.
# Source: https://aws.amazon.com/ec2/instance-types/
RDS_INSTANCE_VCPU: dict[str, int] = {
    "db.t3.micro": 2,
    "db.t3.small": 2,
    "db.t3.medium": 2,
    "db.t3.large": 2,
    "db.t3.xlarge": 4,
    "db.t3.2xlarge": 8,
    "db.m5.large": 2,
    "db.m5.xlarge": 4,
    "db.m5.2xlarge": 8,
    "db.m5.4xlarge": 16,
    "db.m5.8xlarge": 32,
    "db.m5.12xlarge": 48,
    "db.m5.16xlarge": 64,
    "db.m5.24xlarge": 96,
    "db.r5.large": 2,
    "db.r5.xlarge": 4,
    "db.r5.2xlarge": 8,
    "db.r5.4xlarge": 16,
    "db.r5.8xlarge": 32,
    "db.r5.12xlarge": 48,
    "db.r5.16xlarge": 64,
    "db.r5.24xlarge": 96,
    "db.m6i.large": 2,
    "db.m6i.xlarge": 4,
    "db.m6i.2xlarge": 8,
    "db.m6i.4xlarge": 16,
    "db.m6i.8xlarge": 32,
    "db.m6i.12xlarge": 48,
    "db.m6i.16xlarge": 64,
    "db.m6i.24xlarge": 96,
    "db.r6i.large": 2,
    "db.r6i.xlarge": 4,
    "db.r6i.2xlarge": 8,
    "db.r6i.4xlarge": 16,
    "db.r6i.8xlarge": 32,
    "db.r6i.12xlarge": 48,
    "db.r6i.16xlarge": 64,
    "db.r6i.24xlarge": 96,
}


def vcpu_for_instance_class(instance_class: str) -> int | None:
    """Return vCPU count for an RDS instance class, or None if unknown."""
    return RDS_INSTANCE_VCPU.get(instance_class)
