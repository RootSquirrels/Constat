"""Aurora MySQL / PostgreSQL Extended Support insight.

Chantier III.1: the evaluation logic (4 fact gates, 3-branch
severity, payload assembly) lives in `constat_core.insights.eol`.
This file is the engine-specific config for BOTH aurora-mysql and
aurora-postgresql (one rule, two engines) + a thin wrapper. The
existing test suite passes unchanged.

Aurora-specific quirks:
- Aurora MySQL has NO year-3 Extended Support tier (per the Aurora
  MySQL release calendar, year-3 start is "Not applicable") — the
  matcher uses `engine_extended_support_tier` which returns
  `year_1_2` for the whole window.
- Aurora MySQL versions are Aurora-numbered ("2.12.4" -> 2,
  "3.08.1" -> 3); older fleets may report them community-prefixed
  ("5.7.mysql_aurora.2.11.4" -> 2). The `_parse_aurora_major` helper
  strips the `mysql_aurora.` prefix when present.
- Aurora PostgreSQL versions carry the community major directly
  ("14.9" -> 14), same parser as standalone PG.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    CATALOG_VERSION,
    aurora_mysql_eol_info,
    aurora_postgres_eol_info,
    engine_extended_support_tier,
    es_price_per_vcpu_hour,
)
from constat_core.insights.eol import (
    EngineEolMatcher,
    EolInsightResult,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.models import Fact

RULE_NAME = "aurora_eol"

# Engines this rule evaluates. Anything else is a definitive NO_MATCH
# (the shared function's Gate 1).
AURORA_ENGINES = ("aurora-mysql", "aurora-postgresql")

# Human-readable upgrade target for Aurora MySQL majors (keys mirror
# AURORA_MYSQL_EOL exactly). Aurora PostgreSQL uses major + 1
# instead — published upgrade paths are PG-major aligned.
AURORA_MYSQL_NEXT_MAJOR = {2: "Aurora MySQL 3 (MySQL 8.0)", 3: "Aurora MySQL 8.4"}


def _parse_aurora_major(engine_value: str, raw: str) -> int | None:
    """Extract the Aurora major from an engine_version fact.

    Aurora MySQL versions carry an Aurora-numbered major
    ("2.12.4" -> 2, "3.08.1" -> 3); older fleets may report them
    community-prefixed ("5.7.mysql_aurora.2.11.4" -> 2 after the
    prefix is stripped). Aurora PostgreSQL versions carry the
    community major directly ("14.9" -> 14).
    """
    if engine_value == "aurora-mysql" and "mysql_aurora." in raw:
        raw = raw.split("mysql_aurora.", 1)[1]
    try:
        return int(raw.split(".")[0])
    except (ValueError, IndexError):
        return None


def _aurora_mysql_upgrade_target(major: int) -> str:
    return AURORA_MYSQL_NEXT_MAJOR[major]


def _aurora_postgres_upgrade_target(major: int) -> str:
    return f"Aurora PostgreSQL {major + 1}"


def _aurora_mysql_parse(raw: str) -> int | None:
    return _parse_aurora_major("aurora-mysql", raw)


def _aurora_postgres_parse(raw: str) -> int | None:
    return _parse_aurora_major("aurora-postgresql", raw)


AURORA_MYSQL_MATCHER = EngineEolMatcher(
    engine_value="aurora-mysql",
    display_name="Aurora MySQL",
    service_canonical="managed_mysql",
    lookup_eol_info=aurora_mysql_eol_info,
    parse_major=_aurora_mysql_parse,
    format_major=str,
    upgrade_target=_aurora_mysql_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

AURORA_POSTGRES_MATCHER = EngineEolMatcher(
    engine_value="aurora-postgresql",
    display_name="Aurora PostgreSQL",
    service_canonical="managed_postgres",
    lookup_eol_info=aurora_postgres_eol_info,
    parse_major=_aurora_postgres_parse,
    format_major=str,
    upgrade_target=_aurora_postgres_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

CONFIG = EolRuleConfig(
    rule_name=RULE_NAME,
    engines=(AURORA_MYSQL_MATCHER, AURORA_POSTGRES_MATCHER),
)

# Re-export so the rule's test file (which imports `InsightResult`
# from this module) keeps working without touching the test.
InsightResult = EolInsightResult


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> EolInsightResult:
    """Evaluate one Aurora resource (MySQL or PostgreSQL)."""
    return evaluate_eol(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=CATALOG_VERSION,
    )
