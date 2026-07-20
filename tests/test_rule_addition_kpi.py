"""KPI measurement (roadmap consolidation III.4): cost of adding a 4th rule.

This is an INSTRUMENT, not a product rule. It defines a fictive 4th
EOL rule (RDS MariaDB) purely as EngineEolMatcher config + catalog
data and runs it through the shared `evaluate_eol` pipeline — proving
the III.1-III.3 consolidation delivers the "new rule = config +
catalog + test" path with ZERO change to constat_core.

The measured file count is recorded in docs/roadmap-consolidation.md
(III.4 row) and summarized in this module's docstring:

  Rule logic ............ 3 files (config, catalog, test) — KPI met
  Product registration .. +7 (runner.py, monetary.py, web TS mirror,
                            fact_definitions.yaml, pyproject.toml,
                            uv.lock, package scaffold) — data-driven
                            registration is the remaining move.

Fictive rule data (sourced, marked as measurement data):
- RDS MariaDB 10.6 end of standard support: 2026-11-30
  (https://endoflife.date/amazon-rds-mariadb, reviewed 2026-07-20).
- ES pricing: NOT PUBLISHED by AWS for MariaDB 10.6 at review time
  (usage.ai, 2026-05). The 0.100/0.200 grid below is the standard
  RDS Extended Support grid used ONLY to exercise the pricing
  pipeline — a real mariadb_eol rule must source the actual rates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from constat_core.catalog.aws import (
    EngineEOLInfo,
    engine_extended_support_tier,
    es_price_per_vcpu_hour,
)
from constat_core.insights.eol import (
    EngineEolMatcher,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.models import Fact, ValueState

# --- Fictive catalog (would live in catalog/aws.py for a real rule) ---

MARIADB_EOL_FICTIVE: dict[str, EngineEOLInfo] = {
    "10.6": EngineEOLInfo(
        eol_date=date(2026, 11, 30),
        year_1_2_usd_per_vcpu_hour=0.10,  # standard ES grid — measurement only
        year_3_plus_usd_per_vcpu_hour=0.20,
        end_of_extended_support=date(2029, 11, 30),  # fictive end of ES
        year_3_start=date(2028, 11, 30),
    ),
}


def _parse_mariadb_major(raw: str) -> str | None:
    parts = raw.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


MARIADB_MATCHER = EngineEolMatcher(
    engine_value="mariadb",
    display_name="RDS MariaDB",
    service_canonical="managed_mariadb",
    lookup_eol_info=MARIADB_EOL_FICTIVE.get,
    parse_major=_parse_mariadb_major,
    format_major=str,
    upgrade_target=lambda major: "MariaDB 10.11",
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

CONFIG = EolRuleConfig(rule_name="mariadb_eol_kpi", engines=(MARIADB_MATCHER,))


def _fact(key: str, value: object, resource_id=None) -> Fact:
    return Fact(
        resource_id=resource_id or uuid4(),
        account_id="111111111111",
        namespace="aws.rds",
        key=key,
        value=value,
        value_state=ValueState.KNOWN,
        source="test",
        observed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _facts_for(resource_id, engine="mariadb", version="10.6.18", vcpu=4, region="eu-west-3"):
    return [
        _fact("engine", engine, resource_id),
        _fact("engine_version", version, resource_id),
        _fact("vcpu", vcpu, resource_id),
        _fact("region", region, resource_id),
    ]


def test_fictive_rule_matches_pre_eol_window_with_computed_cost() -> None:
    """The generic pipeline runs the 4th rule unchanged: 2026-09-15 is
    76 days before EOL 2026-11-30 -> WARNING, eu-west-3 year_1_2 grid:
    4 vCPU x $0.118 x 730h = $344.56 (regional pricing applies)."""
    rid = uuid4()
    result = evaluate_eol(rid, _facts_for(rid), CONFIG, today=date(2026, 9, 15))

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == "mariadb_eol_kpi"
    assert insight.severity.value == "warning"
    assert "MariaDB 10.6" in insight.title
    assert insight.payload["extended_support_monthly_usd"] == 344.56
    assert insight.payload["pricing_region"] == "eu-west-3"
    assert insight.payload["price_region_exact"] is True


def test_fictive_rule_in_extended_support_is_critical() -> None:
    rid = uuid4()
    result = evaluate_eol(rid, _facts_for(rid), CONFIG, today=date(2027, 1, 15))

    assert len(result.insights) == 1
    assert result.insights[0].severity.value == "critical"
    assert result.insights[0].payload["pricing_tier"] == "year_1_2"


def test_fictive_rule_no_match_for_other_engines() -> None:
    rid = uuid4()
    result = evaluate_eol(rid, _facts_for(rid, engine="postgres"), CONFIG, today=date(2026, 9, 15))
    assert result.insights == []
    assert result.is_conclusive


def test_fictive_rule_inconclusive_without_region() -> None:
    rid = uuid4()
    facts = [f for f in _facts_for(rid) if f.key != "region"]
    result = evaluate_eol(rid, facts, CONFIG, today=date(2026, 9, 15))
    assert result.insights == []
    assert "aws.rds.region" in result.inconclusive_reasons


def test_fictive_rule_inconclusive_on_malformed_version() -> None:
    rid = uuid4()
    result = evaluate_eol(
        rid, _facts_for(rid, version="not-a-version"), CONFIG, today=date(2026, 9, 15)
    )
    assert result.insights == []
    assert result.inconclusive_reasons
