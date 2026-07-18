"""Versioned reference data (EOL dates, pricing, instance maps).

Update cadence: monthly, on AWS announcement. PRs welcome when AWS publishes changes.
"""

from constat_core.catalog.aws import (
    EXT_SUPPORT_USD_PER_VCPU_HOUR,
    POSTGRES_EOL_DATE,
    RDS_INSTANCE_VCPU,
)

__all__ = [
    "EXT_SUPPORT_USD_PER_VCPU_HOUR",
    "POSTGRES_EOL_DATE",
    "RDS_INSTANCE_VCPU",
]
