"""Tests for the RDS PostgreSQL Extended Support insight.

Uses injectable `today` to be deterministic. Real EOL dates and tiered
pricing per the catalog/aws.py source.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from constat_core.catalog.aws import (
    POSTGRES_EOL,
    extended_support_tier,
    price_per_vcpu_hour,
)
from constat_core.models import Fact, Severity, ValueState
from constat_rds_eol.resolver import RULE_NAME, evaluate


def _fact(
    namespace: str,
    key: str,
    value,
    account_id: str = "111111111111",
    value_state: ValueState = ValueState.KNOWN,
) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id=account_id,
        namespace=namespace,
        key=key,
        value=value,
        value_state=value_state,
        source="test",
        observed_at=date.today(),
    )


def _pg_facts(major_version: int, vcpu: int = 4, region: str = "us-east-1") -> list[Fact]:
    return [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", f"{major_version}.7"),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


# ---- Real EOL dates sanity -------------------------------------------------


def test_postgres_12_real_eol_date_is_2025_02_28():
    """Regression: previously we had PG12 EOL as 2024-02-29 (one year off)."""
    assert POSTGRES_EOL[12].eol_date == date(2025, 2, 28)


def test_postgres_13_real_eol_date_is_2026_02_28():
    assert POSTGRES_EOL[13].eol_date == date(2026, 2, 28)


def test_postgres_14_real_eol_date_is_2027_02_28():
    assert POSTGRES_EOL[14].eol_date == date(2027, 2, 28)


def test_postgres_15_real_eol_date_is_2028_02_29():
    assert POSTGRES_EOL[15].eol_date == date(2028, 2, 29)


def test_postgres_11_real_eol_date_is_2024_02_29():
    assert POSTGRES_EOL[11].eol_date == date(2024, 2, 29)


# ---- Tiered pricing --------------------------------------------------------


def test_year_1_2_pricing_for_fresh_eol():
    # PG12 EOL: 2025-02-28. From 2025-12-01 (~9 months later), still year 1-2.
    tier = extended_support_tier(date(2025, 2, 28), date(2025, 12, 1))
    assert tier == "year_1_2"


def test_year_3_pricing_for_old_eol():
    # PG11 EOL: 2024-02-29. From 2026-07-18 (~2.5 years later), year 3+.
    tier = extended_support_tier(date(2024, 2, 29), date(2026, 7, 18))
    assert tier == "year_3_plus"


def test_year_3_pricing_doubles_rate():
    info = POSTGRES_EOL[11]
    today = date(2026, 7, 18)
    rate = price_per_vcpu_hour(info, today)
    assert rate == 0.20  # year 3+ rate, US East pricing


def test_year_1_2_pricing_is_cheaper():
    info = POSTGRES_EOL[12]
    today = date(2025, 12, 1)
    rate = price_per_vcpu_hour(info, today)
    assert rate == 0.10  # year 1-2 rate, US East pricing


# ---- Insight behavior ------------------------------------------------------


def test_postgres_14_within_90_days_emits_warning():
    # PG14 EOL = 2027-02-28. From 2026-12-01, that's 89 days.
    resource_id = uuid4()
    result = evaluate(resource_id, _pg_facts(14), today=date(2026, 12, 1))

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    assert insight.resource_id == resource_id
    assert insight.severity == Severity.WARNING
    assert insight.payload["days_to_event"] == 89
    assert insight.payload["major_version"] == 14
    assert insight.payload["pricing_tier"] == "year_1_2"
    # 4 vCPU * $0.10 * 730h = $292/month (year 1-2 rate, NOT $584)
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.10


def test_postgres_11_past_eol_uses_year_3_rate():
    # PG11 EOL was 2024-02-29. From 2026-07-18 (2.5y later), year 3 rate applies.
    result = evaluate(uuid4(), _pg_facts(11, vcpu=2), today=date(2026, 7, 18))

    assert len(result.insights) == 1
    assert result.insights[0].severity == Severity.CRITICAL
    assert result.insights[0].payload["days_to_event"] < 0
    assert result.insights[0].payload["pricing_tier"] == "year_3_plus"
    assert result.insights[0].payload["pricing_usd_per_vcpu_hour"] == 0.20


def test_postgres_15_too_far_emits_nothing():
    # PG15 EOL = 2028-02-29. From 2026-07-18, 591 days away.
    assert evaluate(uuid4(), _pg_facts(15), today=date(2026, 7, 18)).insights == []


def test_postgres_16_emits_nothing():
    # 16+ are LTS in our catalog; no EOL date means no alert.
    assert evaluate(uuid4(), _pg_facts(16), today=date(2026, 7, 18)).insights == []


def test_non_postgres_engine_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "8.0.32"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)).insights == []


# ---- INCONCLUSIVE (criterion n°15) ----------------------------------------


def test_unknown_engine_emits_inconclusive_not_silent():
    facts = [
        _fact("aws.rds", "engine", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "engine_version", "14.7"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.engine" in result.inconclusive_reasons


def test_unknown_vcpu_emits_inconclusive_not_silent():
    # Graviton instance class NOT in the vCPU table = UNKNOWN vCPU. Before
    # fix: insight silently disappeared. After fix: INCONCLUSIVE.
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "14.7"),
        _fact("aws.rds", "vcpu", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.vcpu" in result.inconclusive_reasons


def test_unknown_engine_version_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.rds.engine_version" in result.inconclusive_reasons


def test_multiple_missing_facts_all_listed():
    facts = [
        _fact("aws.rds", "engine", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "engine_version", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "vcpu", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "region", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert set(result.inconclusive_reasons) == {
        "aws.rds.engine",
        "aws.rds.engine_version",
        "aws.rds.vcpu",
        "aws.rds.region",
    }


def test_malformed_version_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "banana"),
        _fact("aws.rds", "vcpu", 4),
        _fact("aws.rds", "region", "us-east-1"),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.rds.engine_version.malformed" in result.inconclusive_reasons


def test_empty_facts_emits_inconclusive():
    result = evaluate(uuid4(), [], today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert len(result.inconclusive_reasons) == 4


# ---- Region-aware pricing (ES grids are not region-uniform) ----------------


def test_missing_region_emits_inconclusive():
    """Facts written before the collector emitted aws.rds.region lack it —
    INCONCLUSIVE until the next daily scan heals them."""
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "11.22"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.region" in result.inconclusive_reasons


def test_unknown_region_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "11.22"),
        _fact("aws.rds", "vcpu", 4),
        _fact("aws.rds", "region", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.rds.region" in result.inconclusive_reasons


def test_eu_west_1_prices_on_its_own_grid():
    # PG11, 4 vCPU, year-3 tier: 4 * $0.224 * 730h = $654.08/month.
    result = evaluate(uuid4(), _pg_facts(11, vcpu=4, region="eu-west-1"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_tier"] == "year_3_plus"
    assert payload["pricing_usd_per_vcpu_hour"] == 0.224
    assert payload["extended_support_monthly_usd"] == 654.08
    assert payload["pricing_region"] == "eu-west-1"
    assert payload["price_region_exact"] is True
    assert payload["source_currency"] == "USD"


def test_eu_west_3_prices_on_its_own_grid():
    # PG11, 4 vCPU, year-3 tier: 4 * $0.235 * 730h = $686.20/month.
    result = evaluate(uuid4(), _pg_facts(11, vcpu=4, region="eu-west-3"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_usd_per_vcpu_hour"] == 0.235
    assert payload["extended_support_monthly_usd"] == 686.20
    assert payload["pricing_region"] == "eu-west-3"
    assert payload["price_region_exact"] is True


def test_uncatalogued_region_falls_back_to_default_grid():
    # eu-central-1 isn't catalogued: MATCH on the us-east-1 grid, flagged.
    result = evaluate(
        uuid4(), _pg_facts(11, vcpu=4, region="eu-central-1"), today=date(2026, 7, 18)
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    # 4 vCPU * $0.20 * 730h = $584/month on the fallback grid.
    assert payload["pricing_usd_per_vcpu_hour"] == 0.20
    assert payload["extended_support_monthly_usd"] == 584.0
    assert payload["pricing_region"] == "us-east-1"
    assert payload["price_region_exact"] is False


def test_es_price_per_vcpu_hour_region_semantics():
    """Catalog contract: None -> default grid (exact), catalogued region ->
    its own grid (exact), uncatalogued region -> default grid (not exact)."""
    from constat_core.catalog.aws import es_price_per_vcpu_hour

    assert es_price_per_vcpu_hour("year_1_2") == (0.10, "us-east-1", True)
    assert es_price_per_vcpu_hour("year_3_plus", "eu-west-1") == (0.224, "eu-west-1", True)
    assert es_price_per_vcpu_hour("year_1_2", "eu-west-3") == (0.118, "eu-west-3", True)
    assert es_price_per_vcpu_hour("year_3_plus", "ap-southeast-2") == (0.20, "us-east-1", False)


# ---- Graviton -------------------------------------------------------------


def test_graviton_instance_vcpu_known():
    """Without Graviton in the vCPU table, the insight silently disappears
    on a Graviton fleet (the most common case in recent years)."""
    from constat_core.catalog.aws import vcpu_for_instance_class

    assert vcpu_for_instance_class("db.m6g.xlarge") == 4
    assert vcpu_for_instance_class("db.m7g.2xlarge") == 8
    assert vcpu_for_instance_class("db.r7g.4xlarge") == 16
    assert vcpu_for_instance_class("db.t4g.large") == 2


def test_graviton_pg11_emits_critical_with_year_3_rate():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "11.22"),
        _fact("aws.rds", "vcpu", 8),  # db.m6g.2xlarge vCPU
        _fact("aws.rds", "region", "us-east-1"),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert result.is_conclusive
    assert result.has_gap
    assert result.insights[0].payload["pricing_tier"] == "year_3_plus"
    # 8 vCPU * $0.20 * 730h = $1168/month
    assert result.insights[0].payload["pricing_usd_per_vcpu_hour"] == 0.20


# ---- Catalog version (source-of-truth stamp) ------------------------------


def test_catalog_version_constant_exists():
    """The catalog exposes a version string. The sales conversation needs a
    concrete date to cite ('based on AWS RDS PG release calendar dated
    YYYY-MM-DD'). Without this constant, the only version is the docstring
    'Last reviewed' note — unauditable from the payload alone."""
    from constat_core.catalog.aws import CATALOG_VERSION

    assert isinstance(CATALOG_VERSION, str)
    # ISO date format: YYYY-MM-DD
    assert len(CATALOG_VERSION) == 10
    assert CATALOG_VERSION[4] == "-"
    assert CATALOG_VERSION[7] == "-"


def test_insight_payload_includes_catalog_version():
    """Every emitted insight must carry the catalog version that produced it.
    Regression guard: if someone refactors _make_insight and drops the field,
    the sales defensibility silently regresses."""
    from constat_core.catalog.aws import CATALOG_VERSION

    result = evaluate(uuid4(), _pg_facts(11), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["catalog_version"] == CATALOG_VERSION
