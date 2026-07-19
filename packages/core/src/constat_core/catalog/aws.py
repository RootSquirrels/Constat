"""AWS reference data. Versioned by date in the module docstring.

Last reviewed: 2026-07-19. Update when AWS publishes changes.

Sources:
- RDS PostgreSQL EOL dates: https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-release-calendar.html
- RDS MySQL EOL dates: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.VersionMgmt.html
- Aurora MySQL EOL dates: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/AuroraMySQL.release-calendars.html
- Aurora PostgreSQL EOL dates: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/aurorapostgresql-release-calendar.html
- Extended Support pricing: https://aws.amazon.com/rds/postgresql/pricing/ ,
  https://aws.amazon.com/rds/mysql/pricing/ , https://aws.amazon.com/rds/aurora/pricing/
- vCPU counts: https://aws.amazon.com/ec2/instance-types/

Extended Support pricing is NOT region-uniform (verified 2026-07-19 on
the pricing pages' per-region data — the RDS FAQ itself states the
charge depends on the AWS Region). The per-region grids are catalogued
in ES_PRICE_BY_REGION below (AWS Price List publication 2026-07-17,
same data the pricing pages' region selector serves): us-east-1
$0.100/$0.200 per vCPU-hr (years 1-2 / year 3+), eu-west-1
$0.112/$0.224, eu-west-3 $0.118/$0.235. The RDS insight rules gate on
the aws.rds.region fact (missing/UNKNOWN = INCONCLUSIVE) and price on
the resource's own grid; an uncatalogued region falls back to the
us-east-1 grid with price_region_exact=false stamped on the payload.
The per-version `*_usd_per_vcpu_hour` fields below mirror the
us-east-1 (default) grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Catalog version string. Bumped on every data update (EOL dates, pricing,
# vCPU map, etc.) and surfaced in the rds_eol insight payload so the sales
# conversation can cite a concrete source-of-truth date: "based on AWS RDS
# PG release calendar dated 2026-07-19". When V2 swaps the dicts for a
# reference_datasets table, this constant moves to the same provider.
CATALOG_VERSION = "2026-07-19"


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


# The fallback grid when a region isn't catalogued. Same default as
# catalog/ebs.py: us-east-1 is the region AWS uses in its own pricing
# examples, so region-less callers keep the legacy behavior.
ES_DEFAULT_REGION = "us-east-1"

# Extended Support $/vCPU-hr by region and tier (reviewed 2026-07-19).
# Source: AWS Price List API publication 2026-07-17 (the same data the
# RDS pricing pages' region selector serves — the pages' own FAQ states
# the charge depends on the AWS Region), cross-checked against
# https://aws.amazon.com/rds/postgresql/pricing/ ,
# https://aws.amazon.com/rds/mysql/pricing/ ,
# https://aws.amazon.com/rds/aurora/pricing/ .
# eu-west-1 is 12% above us-east-1, eu-west-3 18%. Only the V1 pilot
# regions are catalogued; anything else falls back to the us-east-1
# grid and the lookup reports exact=False (see es_price_per_vcpu_hour).
ES_PRICE_BY_REGION: dict[str, dict[str, float]] = {
    "us-east-1": {"year_1_2": 0.100, "year_3_plus": 0.200},
    "eu-west-1": {"year_1_2": 0.112, "year_3_plus": 0.224},
    "eu-west-3": {"year_1_2": 0.118, "year_3_plus": 0.235},
}


def es_price_per_vcpu_hour(tier: str, region: str | None = None) -> tuple[float, str, bool]:
    """Return (rate, region_used, exact) for an Extended Support tier.

    `region=None` prices on the us-east-1 grid (backward compatibility —
    exact by definition, like ebs.price_region_exact). A catalogued
    region prices on its own grid; an uncatalogued region falls back to
    the us-east-1 grid and returns exact=False — the caller must surface
    `price_region_exact: false` instead of silently presenting a
    us-east-1 number as local.
    """
    if region is not None and region in ES_PRICE_BY_REGION:
        return ES_PRICE_BY_REGION[region][tier], region, True
    return (
        ES_PRICE_BY_REGION[ES_DEFAULT_REGION][tier],
        ES_DEFAULT_REGION,
        region is None,
    )


def price_per_vcpu_hour(eol_info: PostgresEOLInfo, today: date) -> float:
    """Return the current per-vCPU-hour price for this version, tiered.

    Region-less: prices on the default (us-east-1) grid. Kept for
    backward compatibility — region-aware callers use
    `es_price_per_vcpu_hour(tier, region)` directly.
    """
    tier = extended_support_tier(eol_info.eol_date, today)
    rate, _, _ = es_price_per_vcpu_hour(tier)
    return rate


# ---------------------------------------------------------------------------
# MySQL / Aurora EOL + Extended Support
#
# Same shape as PostgresEOLInfo, with two nullable fields for behaviors AWS
# documents differently across engines (reviewed 2026-07-18):
# - `year_3_plus_usd_per_vcpu_hour` is None when the engine has NO year-3
#   tier: Aurora MySQL bills the single year 1-2 rate for the entire
#   Extended Support window (per the Aurora MySQL release calendar, the
#   "start of year 3 pricing" column is "Not applicable", and the Aurora
#   pricing page states year 3 pricing is Aurora-PostgreSQL-only).
# - `year_3_start` is the exact calendar date year-3 pricing begins, taken
#   from the AWS release calendars. It is NOT always EOL + 730 days (e.g.
#   Aurora PostgreSQL 11: EOL 2024-02-29, year-3 starts 2026-04-01), so we
#   store the published date rather than deriving it.
#
# Versions whose Extended Support pricing AWS has not published yet are
# deliberately absent (e.g. RDS MySQL 8.4, Aurora MySQL 8.4): the insights
# treat them as "no known EOL alert" instead of pricing on invented numbers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineEOLInfo:
    """Per-major-version EOL + Extended Support pricing tiering (MySQL/Aurora).

    See PostgresEOLInfo for the shared semantics; the two nullable fields
    are explained in the section comment above.
    """

    eol_date: date  # RDS/Aurora end of standard support
    year_1_2_usd_per_vcpu_hour: float
    year_3_plus_usd_per_vcpu_hour: float | None  # None = no year-3 tier
    end_of_extended_support: date  # AWS force-upgrades after this
    year_3_start: date | None = None  # None = no year-3 tier


# Source: "MySQL on Amazon RDS versions" (reviewed 2026-07-18),
# https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.VersionMgmt.html
# Pricing: https://aws.amazon.com/rds/mysql/pricing/ (US East; year 1-2
# $0.100/vCPU-hr, year 3 $0.200/vCPU-hr). MySQL major = X.Y (e.g. "8.0").
# MySQL 5.7 end of Extended Support is 2029-06-30, extended from
# 2027-02-28 by the 2026-06-17 AWS announcement.
MYSQL_EOL: dict[str, EngineEOLInfo] = {
    "5.7": EngineEOLInfo(
        eol_date=date(2024, 2, 29),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2029, 6, 30),
        year_3_start=date(2026, 3, 1),
    ),
    "8.0": EngineEOLInfo(
        eol_date=date(2026, 7, 31),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2029, 7, 31),
        year_3_start=date(2028, 8, 1),
    ),
    # 8.4 (RDS end of standard support 2029-07-31): Extended Support pricing
    # not yet published by AWS — intentionally absent.
}

# Source: "Release calendars for Amazon Aurora MySQL" (reviewed 2026-07-18),
# https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/AuroraMySQL.release-calendars.html
# Pricing: https://aws.amazon.com/rds/aurora/pricing/ — per vCPU-hour for
# provisioned instances; Aurora MySQL has NO year-3 tier (year-3 start date
# "Not applicable" in the calendar; the pricing page restricts year 3
# pricing to Aurora PostgreSQL). Keyed by Aurora major version
# (2 = MySQL 5.7-compatible, 3 = MySQL 8.0-compatible).
AURORA_MYSQL_EOL: dict[int, EngineEOLInfo] = {
    2: EngineEOLInfo(
        eol_date=date(2024, 10, 31),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=None,
        end_of_extended_support=date(2029, 6, 30),
        year_3_start=None,
    ),
    3: EngineEOLInfo(
        eol_date=date(2028, 4, 30),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=None,
        end_of_extended_support=date(2029, 7, 31),
        year_3_start=None,
    ),
    # 8.4: Extended Support dates "to be determined" in the calendar —
    # intentionally absent.
}

# Source: "Release calendars for Aurora PostgreSQL" (reviewed 2026-07-18),
# https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/aurorapostgresql-release-calendar.html
# Pricing: https://aws.amazon.com/rds/aurora/pricing/ (US East; year 1-2
# $0.100/vCPU-hr, year 3 $0.200/vCPU-hr — confirmed by the page's own
# pricing example for Aurora PostgreSQL 12). Keyed by PostgreSQL major.
AURORA_POSTGRES_EOL: dict[int, EngineEOLInfo] = {
    11: EngineEOLInfo(
        eol_date=date(2024, 2, 29),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2027, 3, 31),
        year_3_start=date(2026, 4, 1),
    ),
    12: EngineEOLInfo(
        eol_date=date(2025, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2028, 2, 29),
        year_3_start=date(2027, 3, 1),
    ),
    13: EngineEOLInfo(
        eol_date=date(2026, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2029, 2, 28),
        year_3_start=date(2028, 3, 1),
    ),
    14: EngineEOLInfo(
        eol_date=date(2027, 2, 28),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2030, 2, 28),
        year_3_start=date(2029, 3, 1),
    ),
    15: EngineEOLInfo(
        eol_date=date(2028, 2, 29),
        year_1_2_usd_per_vcpu_hour=0.10,
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2031, 2, 28),
        year_3_start=date(2030, 3, 1),
    ),
    # 16+ are LTS as of 2026-07. When AWS publishes EOL for them, add here.
}


def mysql_eol_info(major: str) -> EngineEOLInfo | None:
    """Return EOL info for an RDS MySQL major version ("5.7", "8.0"), or None."""
    return MYSQL_EOL.get(major)


def aurora_mysql_eol_info(major_version: int) -> EngineEOLInfo | None:
    """Return EOL info for an Aurora MySQL major version (2, 3), or None."""
    return AURORA_MYSQL_EOL.get(major_version)


def aurora_postgres_eol_info(major_version: int) -> EngineEOLInfo | None:
    """Return EOL info for an Aurora PostgreSQL major version, or None."""
    return AURORA_POSTGRES_EOL.get(major_version)


def engine_extended_support_tier(info: EngineEOLInfo, today: date) -> str:
    """Return 'year_1_2' or 'year_3_plus' using the published year-3 start date.

    Engines with no year-3 tier (Aurora MySQL) always return 'year_1_2'.
    """
    if (
        info.year_3_start is not None
        and info.year_3_plus_usd_per_vcpu_hour is not None
        and today >= info.year_3_start
    ):
        return "year_3_plus"
    return "year_1_2"


def engine_price_per_vcpu_hour(info: EngineEOLInfo, today: date) -> float:
    """Return the current per-vCPU-hour Extended Support price, tiered."""
    if engine_extended_support_tier(info, today) == "year_3_plus":
        # engine_extended_support_tier only returns year_3_plus when the
        # year-3 price is not None.
        assert info.year_3_plus_usd_per_vcpu_hour is not None
        return info.year_3_plus_usd_per_vcpu_hour
    return info.year_1_2_usd_per_vcpu_hour
