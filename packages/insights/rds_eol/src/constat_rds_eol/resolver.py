"""RDS PostgreSQL Extended Support insight.

Chantier III.1: the evaluation logic (4 fact gates, 3-branch
severity, payload assembly) lives in `constat_core.insights.eol`.
This file is the engine-specific config (catalog lookup, major
parser, display name) + a thin wrapper. The existing test suite
passes unchanged.

Region honesty: Extended Support pricing is not region-uniform, so
the aws.rds.region fact is mandatory — missing/UNKNOWN region =
INCONCLUSIVE, never a silently mis-gridded amount. The shared
function emits the inconclusive reason; this file just provides
the matcher config that picks the PostgreSQL catalog.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    CATALOG_VERSION,
    es_price_per_vcpu_hour,
    extended_support_tier,
    postgres_eol_info,
)
from constat_core.insights.eol import (
    EngineEolMatcher,
    EolInsightResult,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.models import Fact

RULE_NAME = "rds_eol"


def _parse_postgres_major(raw: str) -> int | None:
    """PostgreSQL major from a raw engine_version fact (e.g. "14.7" -> 14).

    Returns None on malformed input; the shared function emits the
    inconclusive reason in that case.
    """
    parts = raw.split(".")
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0])


def _postgres_upgrade_target(major: int) -> str:
    """The major + 1 rule is the AWS-published upgrade path for
    PostgreSQL LTS-to-LTS (11 -> 12 -> 13 -> 14 -> 15)."""
    return f"PostgreSQL {major + 1}"


# The matcher's `compute_tier` and `price_per_vcpu_hour` are
# imported from `constat_core.catalog.aws`. The shared
# `_make_insight` calls them with the EOL info + current date (for
# tier) and tier + region (for price). The arithmetic
# `vcpu x tier rate x 730h` lives in `_make_insight`, not here.
#
# `compute_tier` has a different shape for the two catalog types:
# - `extended_support_tier(eol_date, today)` for `PostgresEOLInfo`
#   (EOL date is the only input)
# - `engine_extended_support_tier(info, today)` for `EngineEOLInfo`
#   (the full info object, because year-3 may have a different start
#   date than EOL + 2 years)
# The shared function calls `compute_tier(eol_info, current)` — the
# PG matcher adapts the signature to take the whole info by passing
# only the eol_date.
def _pg_compute_tier(info, today):
    return extended_support_tier(info.eol_date, today)


POSTGRES_MATCHER = EngineEolMatcher(
    engine_value="postgres",
    display_name="RDS PostgreSQL",
    service_canonical="managed_postgres",
    lookup_eol_info=postgres_eol_info,
    parse_major=_parse_postgres_major,
    format_major=str,
    upgrade_target=_postgres_upgrade_target,
    compute_tier=_pg_compute_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
    # rds_eol's pre-refactor payload used the `vcpu` key (not
    # `vcpu_count`) and did not write a `value_basis` field. The
    # test_monetary_extraction + test_reconcile_with_azure_focus
    # suites both pin those shapes; the matcher keeps the old
    # behavior so they pass unchanged. New engines: leave the
    # defaults (`vcpu_count` + `"ESTIMATED"`).
    vcpu_payload_key="vcpu",
    value_basis=None,
)

CONFIG = EolRuleConfig(rule_name=RULE_NAME, engines=(POSTGRES_MATCHER,))


# Re-export so the rule's test file (which imports `InsightResult`
# from this module) keeps working without touching the test.
InsightResult = EolInsightResult


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> EolInsightResult:
    """Evaluate one RDS resource. Returns the same InsightResult
    shape as the per-rule evaluators did before the refactor."""
    return evaluate_eol(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=CATALOG_VERSION,
    )
