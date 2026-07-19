"""AWS collector helpers shared by the inventory connectors.

Chantier III.3 of the roadmap consolidation. The V1
connectors (`constat_aws_rds`, `constat_aws_ec2`) duplicate
the adaptive-retry boto3 config, the default region list, the
per-region paginator pattern, and the per-connector `_fact`
closure. This package is the single home of that scaffolding;
a new inventory connector consumes these helpers and the
duplicate code (and its drift-pin) goes away.
"""

from constat_core.collectors.aws import (
    ADAPTIVE_RETRY_CONFIG,
    DEFAULT_REGIONS,
    FactBuilder,
    ItemsExtractor,
    known_or_unknown,
    make_fact_builder,
    now_utc,
    paginate_aws,
)

__all__ = [
    "ADAPTIVE_RETRY_CONFIG",
    "DEFAULT_REGIONS",
    "FactBuilder",
    "ItemsExtractor",
    "known_or_unknown",
    "make_fact_builder",
    "now_utc",
    "paginate_aws",
]
