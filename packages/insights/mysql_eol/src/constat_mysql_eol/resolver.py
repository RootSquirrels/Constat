"""RDS MySQL Extended Support insight.

Chantier III.1: the evaluation logic (4 fact gates, 3-branch
severity, payload assembly) lives in `constat_core.insights.eol`.
This file is the engine-specific config (catalog lookup, major
parser, upgrade target) + a thin wrapper. The existing test
suite passes unchanged.

MySQL-specific note: the major is `X.Y` (e.g. "8.0.42" -> "8.0"),
not a single int like PG. The upgrade target uses a static table
(NEXT_MAJOR) — RDS MySQL's published upgrade path is 5.7 -> 8.0 ->
8.4, not always major + 1.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    CATALOG_VERSION,
    engine_extended_support_tier,
    es_price_per_vcpu_hour,
    mysql_eol_info,
)
from constat_core.insights.eol import (
    EngineEolMatcher,
    EolInsightResult,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.models import Fact

RULE_NAME = "mysql_eol"

# Upgrade target for each catalogued MySQL major (RDS supports
# 5.7 -> 8.0 -> 8.4 major upgrades). Keys mirror MYSQL_EOL exactly.
NEXT_MAJOR = {"5.7": "8.0", "8.0": "8.4"}


def _parse_mysql_major(raw: str) -> str | None:
    """MySQL major from a raw engine_version fact (e.g. "8.0.42" -> "8.0").

    Returns None on malformed input; the shared function emits
    the inconclusive reason.
    """
    parts = raw.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


def _mysql_upgrade_target(major: str) -> str:
    # Static table because MySQL's published upgrade path doesn't
    # always follow major + 1 (RDS MySQL 5.7 -> 8.0 is one major jump,
    # not two).
    return NEXT_MAJOR[major]


MYSQL_MATCHER = EngineEolMatcher(
    engine_value="mysql",
    display_name="RDS MySQL",
    service_canonical="managed_mysql",
    lookup_eol_info=mysql_eol_info,
    parse_major=_parse_mysql_major,
    format_major=str,  # major is already a string ("8.0")
    upgrade_target=_mysql_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

CONFIG = EolRuleConfig(rule_name=RULE_NAME, engines=(MYSQL_MATCHER,))

# Re-export so the rule's test file (which imports `InsightResult`
# from this module) keeps working without touching the test.
InsightResult = EolInsightResult


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> EolInsightResult:
    """Evaluate one RDS MySQL resource."""
    return evaluate_eol(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=CATALOG_VERSION,
    )
