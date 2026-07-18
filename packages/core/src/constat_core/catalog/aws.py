"""AWS reference data. Versioned by date in the module docstring.

Last reviewed: 2026-07-18. Update when AWS publishes changes.

Sources:
- EOL dates: https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-release-calendar.html
- Extended Support pricing: https://aws.amazon.com/rds/postgresql/pricing/
- vCPU counts: https://aws.amazon.com/ec2/instance-types/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PostgresEOLInfo:
    """Per-major-version EOL + Extended Support pricing tiering.

    Pricing depends on calendar years since RDS end of standard support:
    - year_1_2: 0-2 years past EOL (cheaper tier)
    - year_3_plus: 3+ years past EOL (double the rate)

    `end_of_extended_support` is when AWS force-upgrades the instance; after
    that, the engine is no longer available at all (caller should treat as
    "must migrate now").
    """

    eol_date: date  # RDS end of standard support
    year_1_2_usd_per_vcpu_hour: float
    year_3_plus_usd_per_vcpu_hour: float
    end_of_extended_support: date  # AWS force-upgrades after this


# Source: AWS RDS PostgreSQL release calendar (2026-07-18).
POSTGRES_EOL: dict[int, PostgresEOLInfo] = {
    11: PostgresEOLInfo(
        eol_date=date(2024, 2, 29),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2027, 3, 31),
    ),
    12: PostgresEOLInfo(
        eol_date=date(2025, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2028, 2, 29),
    ),
    13: PostgresEOLInfo(
        eol_date=date(2026, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2029, 2, 28),
    ),
    14: PostgresEOLInfo(
        eol_date=date(2027, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2030, 2, 28),
    ),
    15: PostgresEOLInfo(
        eol_date=date(2028, 2, 29),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2031, 2, 28),
    ),
    # 16+ are LTS as of 2026-07. When AWS publishes EOL for them, add here.
}


# vCPU count per RDS instance class. Includes Graviton (t4g, m6g, m7g, r6g, r7g)
# which dominates recent fleets. Without these, vcpu is UNKNOWN and the
# EOL insight silently disappears.
RDS_INSTANCE_VCPU: dict[str, int] = {
    # T family (burstable)
    "db.t3.micro": 2,
    "db.t3.small": 2,
    "db.t3.medium": 2,
    "db.t3.large": 2,
    "db.t3.xlarge": 4,
    "db.t3.2xlarge": 8,
    "db.t4g.micro": 2,
    "db.t4g.small": 2,
    "db.t4g.medium": 2,
    "db.t4g.large": 2,
    "db.t4g.xlarge": 4,
    "db.t4g.2xlarge": 8,
    # M family (Intel)
    "db.m5.large": 2,
    "db.m5.xlarge": 4,
    "db.m5.2xlarge": 8,
    "db.m5.4xlarge": 16,
    "db.m5.8xlarge": 32,
    "db.m5.12xlarge": 48,
    "db.m5.16xlarge": 64,
    "db.m5.24xlarge": 96,
    "db.m6i.large": 2,
    "db.m6i.xlarge": 4,
    "db.m6i.2xlarge": 8,
    "db.m6i.4xlarge": 16,
    "db.m6i.8xlarge": 32,
    "db.m6i.12xlarge": 48,
    "db.m6i.16xlarge": 64,
    "db.m6i.24xlarge": 96,
    # M family (Graviton)
    "db.m6g.large": 2,
    "db.m6g.xlarge": 4,
    "db.m6g.2xlarge": 8,
    "db.m6g.4xlarge": 16,
    "db.m6g.8xlarge": 32,
    "db.m6g.12xlarge": 48,
    "db.m6g.16xlarge": 64,
    "db.m6g.24xlarge": 96,
    "db.m7g.large": 2,
    "db.m7g.xlarge": 4,
    "db.m7g.2xlarge": 8,
    "db.m7g.4xlarge": 16,
    "db.m7g.8xlarge": 32,
    "db.m7g.12xlarge": 48,
    "db.m7g.16xlarge": 64,
    "db.m7g.24xlarge": 96,
    # R family (Intel, memory-optimized)
    "db.r5.large": 2,
    "db.r5.xlarge": 4,
    "db.r5.2xlarge": 8,
    "db.r5.4xlarge": 16,
    "db.r5.8xlarge": 32,
    "db.r5.12xlarge": 48,
    "db.r5.16xlarge": 64,
    "db.r5.24xlarge": 96,
    "db.r6i.large": 2,
    "db.r6i.xlarge": 4,
    "db.r6i.2xlarge": 8,
    "db.r6i.4xlarge": 16,
    "db.r6i.8xlarge": 32,
    "db.r6i.12xlarge": 48,
    "db.r6i.16xlarge": 64,
    "db.r6i.24xlarge": 96,
    # R family (Graviton)
    "db.r6g.large": 2,
    "db.r6g.xlarge": 4,
    "db.r6g.2xlarge": 8,
    "db.r6g.4xlarge": 16,
    "db.r6g.8xlarge": 32,
    "db.r6g.12xlarge": 48,
    "db.r6g.16xlarge": 64,
    "db.r6g.24xlarge": 96,
    "db.r7g.large": 2,
    "db.r7g.xlarge": 4,
    "db.r7g.2xlarge": 8,
    "db.r7g.4xlarge": 16,
    "db.r7g.8xlarge": 32,
    "db.r7g.12xlarge": 48,
    "db.r7g.16xlarge": 64,
    "db.r7g.24xlarge": 96,
}


def vcpu_for_instance_class(instance_class: str) -> int | None:
    """Return vCPU count for an RDS instance class, or None if unknown."""
    return RDS_INSTANCE_VCPU.get(instance_class)


def postgres_eol_info(major_version: int) -> PostgresEOLInfo | None:
    """Return EOL info for a Postgres major version, or None if LTS / unknown."""
    return POSTGRES_EOL.get(major_version)


def extended_support_tier(eol_date: date, today: date) -> str:
    """Return 'year_1_2' or 'year_3_plus' based on calendar years since EOL.

    The tier transitions on March 1 of the third calendar year after EOL
    (matches AWS billing calendar). For simplicity, we use 730-day windows
    starting from the EOL date.
    """
    days_since = (today - eol_date).days
    if days_since < 730:  # 2 * 365
        return "year_1_2"
    return "year_3_plus"


def price_per_vcpu_hour(eol_info: PostgresEOLInfo, today: date) -> float:
    """Return the current per-vCPU-hour price for this version, tiered."""
    tier = extended_support_tier(eol_info.eol_date, today)
    return (
        eol_info.year_1_2_usd_per_vcpu_hour
        if tier == "year_1_2"
        else eol_info.year_3_plus_usd_per_vcpu_hour
    )
