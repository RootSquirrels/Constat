"""Tests for the Aurora MySQL/PostgreSQL Extended Support insight.

Uses injectable `today` to be deterministic. Real EOL dates and tiered
pricing per the catalog/aws.py source (Aurora release calendars + Aurora
pricing page, reviewed 2026-07-18).
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
from constat_aurora_eol.resolver import RULE_NAME, evaluate
from constat_core.catalog.aws import (
    AURORA_MYSQL_EOL,
    AURORA_POSTGRES_EOL,
    CATALOG_VERSION,
    engine_extended_support_tier,
    engine_price_per_vcpu_hour,
)
from constat_core.models import Fact, Severity, ValueState
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


def _aurora_facts(
    engine: str, engine_version: str, vcpu: int = 4, region: str = "us-east-1"
) -> list[Fact]:
    return [
        _fact("aws.rds", "engine", engine),
        _fact("aws.rds", "engine_version", engine_version),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


# ---- Real EOL dates sanity -------------------------------------------------


def test_aurora_mysql_2_real_eol_date_is_2024_10_31():
    assert AURORA_MYSQL_EOL[2].eol_date == date(2024, 10, 31)


def test_aurora_mysql_2_end_of_extended_support_is_2029_06_30():
    # Extended from 2027-02-28 by the June 2026 AWS announcement.
    assert AURORA_MYSQL_EOL[2].end_of_extended_support == date(2029, 6, 30)


def test_aurora_mysql_3_real_eol_date_is_2028_04_30():
    assert AURORA_MYSQL_EOL[3].eol_date == date(2028, 4, 30)


def test_aurora_postgres_13_real_eol_date_is_2026_02_28():
    assert AURORA_POSTGRES_EOL[13].eol_date == date(2026, 2, 28)


# ---- Tiered pricing --------------------------------------------------------


def test_aurora_mysql_has_no_year_3_tier():
    # Aurora MySQL bills the single year 1-2 rate for the whole ES window
    # (year-3 start "Not applicable" in the release calendar).
    info = AURORA_MYSQL_EOL[2]
    assert info.year_3_plus_usd_per_vcpu_hour is None
    assert engine_extended_support_tier(info, date(2029, 1, 1)) == "year_1_2"
    assert engine_price_per_vcpu_hour(info, date(2029, 1, 1)) == 0.10


def test_aurora_postgres_11_year_3_boundary():
    # Aurora PG 11 year-3 pricing starts 2026-04-01 per AWS.
    info = AURORA_POSTGRES_EOL[11]
    assert engine_extended_support_tier(info, date(2026, 3, 31)) == "year_1_2"
    assert engine_extended_support_tier(info, date(2026, 4, 1)) == "year_3_plus"


# ---- Aurora MySQL insight behavior -----------------------------------------


def test_aurora_mysql_2_past_eol_emits_critical_with_monthly_cost():
    # Aurora MySQL 2 EOL was 2024-10-31; single $0.10 rate (no year-3 tier).
    resource_id = uuid4()
    result = evaluate(
        resource_id, _aurora_facts("aurora-mysql", "2.12.4", vcpu=4), today=date(2026, 7, 18)
    )

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    assert insight.resource_id == resource_id
    assert insight.severity == Severity.CRITICAL
    assert insight.payload["days_to_event"] < 0
    assert insight.payload["major_version"] == 2
    assert insight.payload["pricing_tier"] == "year_1_2"
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.10
    # 4 vCPU * $0.10 * 730h = $292/month
    assert insight.payload["extended_support_monthly_usd"] == 292.0


def test_aurora_mysql_2_legacy_version_format_parsed():
    # Older fleets report "5.7.mysql_aurora.2.11.4" -> Aurora major 2.
    result = evaluate(
        uuid4(),
        _aurora_facts("aurora-mysql", "5.7.mysql_aurora.2.11.4"),
        today=date(2026, 7, 18),
    )
    assert len(result.insights) == 1
    assert result.insights[0].payload["major_version"] == 2


def test_aurora_mysql_3_within_90_days_emits_warning():
    # Aurora MySQL 3 EOL = 2028-04-30. From 2028-03-01, that's 60 days.
    result = evaluate(
        uuid4(), _aurora_facts("aurora-mysql", "3.08.1", vcpu=2), today=date(2028, 3, 1)
    )

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.severity == Severity.WARNING
    assert insight.payload["days_to_event"] == 60
    assert insight.payload["major_version"] == 3


def test_aurora_mysql_3_too_far_emits_nothing():
    # Aurora MySQL 3 EOL = 2028-04-30. From 2026-07-18, 651 days away.
    assert (
        evaluate(uuid4(), _aurora_facts("aurora-mysql", "3.08.1"), today=date(2026, 7, 18)).insights
        == []
    )


def test_aurora_mysql_2_past_end_of_extended_support_emits_force_upgrade():
    result = evaluate(uuid4(), _aurora_facts("aurora-mysql", "2.12.4"), today=date(2029, 7, 15))

    assert len(result.insights) == 1
    assert result.insights[0].severity == Severity.CRITICAL
    assert "force-upgraded" in result.insights[0].title


# ---- Aurora PostgreSQL insight behavior ------------------------------------


def test_aurora_postgres_13_past_eol_year_1_2_rate():
    # Aurora PG 13 EOL was 2026-02-28; year-3 pricing starts 2028-03-01.
    result = evaluate(
        uuid4(), _aurora_facts("aurora-postgresql", "13.9", vcpu=2), today=date(2026, 7, 18)
    )

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.severity == Severity.CRITICAL
    assert insight.payload["pricing_tier"] == "year_1_2"
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.10
    # 2 vCPU * $0.10 * 730h = $146/month
    assert insight.payload["extended_support_monthly_usd"] == 146.0


def test_aurora_postgres_12_year_3_rate():
    # Aurora PG 12 EOL was 2025-02-28; year-3 pricing starts 2027-03-01.
    result = evaluate(
        uuid4(), _aurora_facts("aurora-postgresql", "12.22", vcpu=2), today=date(2027, 4, 1)
    )

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.payload["pricing_tier"] == "year_3_plus"
    assert insight.payload["pricing_usd_per_vcpu_hour"] == 0.20
    # 2 vCPU * $0.20 * 730h = $292/month
    assert insight.payload["extended_support_monthly_usd"] == 292.0


def test_aurora_postgres_16_emits_nothing():
    # 16+ are LTS in our catalog; no EOL date means no alert.
    assert (
        evaluate(
            uuid4(), _aurora_facts("aurora-postgresql", "16.4"), today=date(2026, 7, 18)
        ).insights
        == []
    )


def test_non_aurora_engine_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "5.7.44"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)).insights == []


# ---- ESTIMATED basis + catalog version -------------------------------------


def test_insight_payload_value_basis_is_estimated():
    """Catalog-derived figure until a FOCUS line confirms the actual charge."""
    result = evaluate(uuid4(), _aurora_facts("aurora-mysql", "2.12.4"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["value_basis"] == "ESTIMATED"


def test_insight_payload_includes_catalog_version():
    result = evaluate(uuid4(), _aurora_facts("aurora-mysql", "2.12.4"), today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["catalog_version"] == CATALOG_VERSION


# ---- INCONCLUSIVE (criterion n°15) ----------------------------------------


def test_unknown_engine_emits_inconclusive_not_silent():
    facts = [
        _fact("aws.rds", "engine", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "engine_version", "2.12.4"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.engine" in result.inconclusive_reasons


def test_unknown_vcpu_emits_inconclusive_not_silent():
    facts = [
        _fact("aws.rds", "engine", "aurora-mysql"),
        _fact("aws.rds", "engine_version", "2.12.4"),
        _fact("aws.rds", "vcpu", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.vcpu" in result.inconclusive_reasons


def test_unknown_engine_version_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "aurora-postgresql"),
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
        _fact("aws.rds", "engine", "aurora-mysql"),
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
        _fact("aws.rds", "engine", "aurora-mysql"),
        _fact("aws.rds", "engine_version", "2.12.4"),
        _fact("aws.rds", "vcpu", 4),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert "aws.rds.region" in result.inconclusive_reasons


def test_unknown_region_emits_inconclusive():
    facts = [
        _fact("aws.rds", "engine", "aurora-postgresql"),
        _fact("aws.rds", "engine_version", "13.9"),
        _fact("aws.rds", "vcpu", 2),
        _fact("aws.rds", "region", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.rds.region" in result.inconclusive_reasons


def test_eu_west_1_prices_on_its_own_grid():
    # Aurora MySQL 2, 4 vCPU, single year-1-2 tier: 4 * $0.112 * 730h = $327.04/month.
    result = evaluate(
        uuid4(),
        _aurora_facts("aurora-mysql", "2.12.4", vcpu=4, region="eu-west-1"),
        today=date(2026, 7, 18),
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_tier"] == "year_1_2"
    assert payload["pricing_usd_per_vcpu_hour"] == 0.112
    assert payload["extended_support_monthly_usd"] == 327.04
    assert payload["pricing_region"] == "eu-west-1"
    assert payload["price_region_exact"] is True
    assert payload["source_currency"] == "USD"


def test_eu_west_3_prices_on_its_own_grid():
    # Aurora PG 13, 2 vCPU, year-1-2 tier: 2 * $0.118 * 730h = $172.28/month.
    result = evaluate(
        uuid4(),
        _aurora_facts("aurora-postgresql", "13.9", vcpu=2, region="eu-west-3"),
        today=date(2026, 7, 18),
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    assert payload["pricing_usd_per_vcpu_hour"] == 0.118
    assert payload["extended_support_monthly_usd"] == 172.28
    assert payload["pricing_region"] == "eu-west-3"
    assert payload["price_region_exact"] is True


def test_uncatalogued_region_falls_back_to_default_grid():
    # eu-central-1 isn't catalogued: MATCH on the us-east-1 grid, flagged.
    result = evaluate(
        uuid4(),
        _aurora_facts("aurora-mysql", "2.12.4", vcpu=4, region="eu-central-1"),
        today=date(2026, 7, 18),
    )
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    # 4 vCPU * $0.10 * 730h = $292/month on the fallback grid.
    assert payload["pricing_usd_per_vcpu_hour"] == 0.10
    assert payload["extended_support_monthly_usd"] == 292.0
    assert payload["pricing_region"] == "us-east-1"
    assert payload["price_region_exact"] is False


# ---- Runner level (generic runner, delete-and-replace) ---------------------


def _bootstrap_aurora_mysql_2(session: Session) -> ResourceORM:
    """Account + resource + successful source_run + Aurora MySQL 2 facts."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:aurora-mysql-2",
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
                ("engine", "aurora-mysql"),
                ("engine_version", "2.12.4"),
                ("instance_class", "db.r6g.large"),
                ("vcpu", 2),
                ("region", "us-east-1"),
            ]
        ],
        source_run_id=run.id,
    )
    session.commit()
    return resource


def test_runner_emits_aurora_eol_insight(session: Session) -> None:
    """End-to-end via the generic runner: Aurora MySQL 2 in Extended Support."""
    _bootstrap_aurora_mysql_2(session)
    result = run_resource_rule(session, "aurora_eol", today=date(2026, 7, 18))

    assert result.rule_name == "aurora_eol"
    assert result.resources_scanned == 1
    assert result.insights_emitted == 1
    assert result.errors == []

    rows = session.query(InsightORM).all()
    assert len(rows) == 1
    assert rows[0].rule_name == "aurora_eol"
    assert rows[0].severity == "critical"
    # 2 vCPU * $0.10 * 730h = $146/month
    assert rows[0].payload["extended_support_monthly_usd"] == 146.0


def test_runner_aurora_eol_reruns_do_not_duplicate(session: Session) -> None:
    """Delete-and-replace: 3 consecutive runs keep the insight count constant."""
    _bootstrap_aurora_mysql_2(session)
    for _ in range(3):
        result = run_resource_rule(session, "aurora_eol", today=date(2026, 7, 18))
        assert result.insights_emitted == 1
    assert session.query(InsightORM).count() == 1
