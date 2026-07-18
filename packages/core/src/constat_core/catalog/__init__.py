"""Versioned reference data (EOL dates, pricing, instance maps).

Update cadence: monthly, on AWS announcement. PRs welcome when AWS publishes changes.
"""

from constat_core.catalog.aws import (
    POSTGRES_EOL,
    RDS_INSTANCE_VCPU,
    PostgresEOLInfo,
    extended_support_tier,
    postgres_eol_info,
    price_per_vcpu_hour,
    vcpu_for_instance_class,
)

__all__ = [
    "POSTGRES_EOL",
    "RDS_INSTANCE_VCPU",
    "PostgresEOLInfo",
    "extended_support_tier",
    "postgres_eol_info",
    "price_per_vcpu_hour",
    "vcpu_for_instance_class",
]
