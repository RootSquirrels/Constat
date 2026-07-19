"""Tests for the RDS MySQL Extended Support insight.

Uses injectable `today` to be deterministic. Real EOL dates and tiered
pricing per the catalog/aws.py source (MySQL on Amazon RDS versions doc +
RDS for MySQL pricing page, reviewed 2026-07-18).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from constat_api.insights.runner import (
    DEFAULT_SOURCE,
    run_resource_rule,
)
from constat_api.orm import InsightORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.catalog.aws import (
    CATALOG_VERSION,
    MYSQL_EOL,
    engine_extended_support_tier,
    engine_price_per_vcpu_hour,
)
from constat_core.models import Fact, Severity, ValueState
from constat_mysql_eol.resolver import RULE_NAME, evaluate
from sqlalchemy.orm import Session


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


def _mysql_facts(engine_version: str, vcpu: int = 4, region: str = "us-east-1") -> list[Fact]:
    return [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", engine_version),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


# ---- Real EOL dates sanity -------------------------------------------------


def test_mysql_57_real_eol_date_is_2024_02_29():
    assert MYSQL_EOL["5.7"].eol_date == date(2024, 2, 29)


def test_mysql_57_end_of_extended_support_is_2029_06_30():
    # Extended from 2027-02-28 by the June 2026 AWS announcement.
    assert MYSQL_EOL["5.7"].end_of_extended_support == date(2029, 6, 30)


def test_mysql_80_real_eol_date_is_2026_07_31():
    assert MYSQL_EOL["8.0"].eol_date == date(2026, 7, 31)


# ---- Tiered pricing --------------------------------------------------------


def test_mysql_57_year_1_2_before_year_3_start():
    # MySQL 5.7 year-3 pricing starts 2026-03-01 per AWS.
    tier = engine_extended_support_tier(MYSQL_EOL["5.7"], date(2026, 2, 28))
    assert tier == "year_1_2"


def test_mysql_57_year_3_from_year_3_start():
    tier = engine_extended_support_tier(MYSQL_EOL["5.7"], date(2026, 3, 1))
    assert tier == "year_3_plus"


def test_mysql_57_year_3_pricing_doubles_rate():
    rate = engine_price_per_vcpu_hour(MYSQL_EOL["5.7"], date(2026, 7, 18))
    assert rate == 0.20  # year 3+ rate, US East pricing


def test_mysql_80_year_1_2_pricing_is_cheaper():
    # MySQL 8.0 ES charges start 2026-08-01 at the year 1-2 rate.
    rate = engine_price_per_vcpu_hour(MYSQL_EOL["8.0"], date(2026, 8, 15))
    assert rate == 0.10


# ---- Insight behavior ------------------------------------------------------


def test_mysql_57_past_eol_emits_critical_with_monthly_cost():
    # MySQL 5.7 EOL was 2024-02-29. From 2026-07-18, year 3 rate applies.
    resource_id = uuid4()
    result = evaluate(resource_id, _mysql_facts("5.7.44", vcpu=4), today=date(2026, 7, 18))

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    assert insight.resource_id == resource_id
    assert insight.severity == Severity.CRITICAL
    assert insight.payload["days_to_event"] < 0
    assert insight.payload["major_version"] == "5.7"
    assert insight.payload["pricing_tier"] == "year_3_plus"
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.20
    # 4 vCPU * $0.20 * 730h = $584/month
    assert insight.payload["extended_support_monthly_usd"] == 584.0


def test_mysql_80_within_90_days_emits_warning():
    # MySQL 8.0 EOL = 2026-07-31. From 2026-06-01, that's 60 days.
    result = evaluate(uuid4(), _mysql_facts("8.0.42", vcpu=2), today=date(2026, 6, 1))

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.severity == Severity.WARNING
    assert insight.payload["days_to_event"] == 60
    assert insight.payload["major_version"] == "8.0"


def test_mysql_80_past_eol_uses_year_1_2_rate():
    # MySQL 8.0 year-3 pricing only starts 2028-08-01.
    result = evaluate(uuid4(), _mysql_facts("8.0.42", vcpu=2), today=date(2026, 8, 15))

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.severity == Severity.CRITICAL
    assert insight.payload["pricing_tier"] == "year_1_2"
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.10
    # 2 vCPU * $0.10 * 730h = $146/month
    assert insight.payload["extended_support_monthly_usd"] == 146.0


def test_mysql_80_too_far_emits_nothing():
    # MySQL 8.0 EOL = 2026-07-31. From 2026-01-01, 211 days away.
    assert evaluate(uuid4(), _mysql_facts("8.0.42"), today=date(2026, 1, 1)).insights == []


def test_mysql_84_emits_nothing():
    # 8.4 ES pricing isn't published by AWS yet — no catalog entry, no alert
    # (never price on invented numbers).
    assert evaluate(uuid4(), _mysql_facts("8.4.5"), today=date(2026, 7, 18)).insights == []


def test_non_mysql_engine_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "14.7"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)).insights == []


def test_mysql_57_past_end_of_extended_support_emits_force_upgrade():
    result = evaluate(uuid4(), _mysql_facts("5.7.44"), today=date(2029, 7, 15))

    assert len(result.insights) == 1
    assert result.insights[0].severity == Severity.CRITICAL
    assert "force-upgraded" in result.insights[0].title


# ---- ESTIMATED basis + catalog version -------------------------------------


def test_insight_payload_value_basis_is_estimated():
    """Catalog-derived figure until a FOCUS line confirms the actual charge."""
    result = evaluate(uuid4(), _mysql_facts("5.7.44"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["value_basis"] == "ESTIMATED"


def test_insight_payload_includes_catalog_version():
    result = evaluate(uuid4(), _mysql_facts("5.7.44"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["catalog_version"] == CATALOG_VERSION


# ---- INCONCLUSIVE (criterion n°15) ----------------------------------------


def test_unknown_engine_emits_inconclusive_not_silent():
    facts = [
        _fact("aws.rds", "engine", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "engine_version", "8.0.42"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.engine" in result.inconclusive_reasons


def test_unknown_vcpu_emits_inconclusive_not_silent():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "5.7.44"),
        _fact("aws.rds", "vcpu", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.vcpu" in result.inconclusive_reasons


def test_unknown_engine_version_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
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
        _fact("aws.rds", "engine", "mysql"),
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
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "5.7.44"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.region" in result.inconclusive_reasons


def test_unknown_region_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "5.7.44"),
        _fact("aws.rds", "vcpu", 4),
        _fact("aws.rds", "region", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.rds.region" in result.inconclusive_reasons


def test_eu_west_1_prices_on_its_own_grid():
    # MySQL 5.7, 4 vCPU, year-3 tier: 4 * $0.224 * 730h = $654.08/month.
    result = evaluate(
        uuid4(), _mysql_facts("5.7.44", vcpu=4, region="eu-west-1"), today=date(2026, 7, 18)
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_tier"] == "year_3_plus"
    assert payload["pricing_usd_per_vcpu_hour"] == 0.224
    assert payload["extended_support_monthly_usd"] == 654.08
    assert payload["pricing_region"] == "eu-west-1"
    assert payload["price_region_exact"] is True
    assert payload["source_currency"] == "USD"


def test_eu_west_3_prices_on_its_own_grid():
    # MySQL 5.7, 4 vCPU, year-3 tier: 4 * $0.235 * 730h = $686.20/month.
    result = evaluate(
        uuid4(), _mysql_facts("5.7.44", vcpu=4, region="eu-west-3"), today=date(2026, 7, 18)
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_usd_per_vcpu_hour"] == 0.235
    assert payload["extended_support_monthly_usd"] == 686.20
    assert payload["pricing_region"] == "eu-west-3"
    assert payload["price_region_exact"] is True


def test_uncatalogued_region_falls_back_to_default_grid():
    # eu-west-2 isn't catalogued: MATCH on the us-east-1 grid, flagged.
    result = evaluate(
        uuid4(), _mysql_facts("5.7.44", vcpu=4, region="eu-west-2"), today=date(2026, 7, 18)
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    # 4 vCPU * $0.20 * 730h = $584/month on the fallback grid.
    assert payload["pricing_usd_per_vcpu_hour"] == 0.20
    assert payload["extended_support_monthly_usd"] == 584.0
    assert payload["pricing_region"] == "us-east-1"
    assert payload["price_region_exact"] is False


# ---- Runner level (generic runner, delete-and-replace) ---------------------


def _bootstrap_mysql_57(session: Session) -> ResourceORM:
    """Account + resource + successful source_run + MySQL 5.7 facts."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:mysql57",
    )
    session.add(resource)
    session.commit()

    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()

    facts_repo.upsert_facts(
        session,
        [
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key=key,
                value=value,
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
            for key, value in [
                ("engine", "mysql"),
                ("engine_version", "5.7.44"),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 4),
                ("region", "us-east-1"),
            ]
        ],
        source_run_id=run.id,
    )
    session.commit()
    return resource


def test_runner_emits_mysql_eol_insight(session: Session) -> None:
    """End-to-end via the generic runner: MySQL 5.7 in Extended Support."""
    _bootstrap_mysql_57(session)
    result = run_resource_rule(session, "mysql_eol", today=date(2026, 7, 18))

    assert result.rule_name == "mysql_eol"
    assert result.resources_scanned == 1
    assert result.insights_emitted == 1
    assert result.errors == []

    rows = session.query(InsightORM).all()
    assert len(rows) == 1
    assert rows[0].rule_name == "mysql_eol"
    assert rows[0].severity == "critical"
    assert rows[0].payload["extended_support_monthly_usd"] == 584.0


def test_runner_mysql_eol_reruns_do_not_duplicate(session: Session) -> None:
    """Delete-and-replace: 3 consecutive runs keep the insight count constant."""
    _bootstrap_mysql_57(session)
    for _ in range(3):
        result = run_resource_rule(session, "mysql_eol", today=date(2026, 7, 18))
        assert result.insights_emitted == 1
    assert session.query(InsightORM).count() == 1
