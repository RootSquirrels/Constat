"""Proof tests for the audit findings F-02, F-03, F-13, F-16.

F-02: scope freshness window — a successful run older than 24h no longer
      proves the scope; the resource goes INCONCLUSIVE scope_stale.
F-03: delete-and-replace — re-running a rule never duplicates its rows.
F-13: chargeback titles use the account display name, drift severity is
      capped at INFO (normal RI mechanics).
F-16: the rds_eol runner fetches facts in one bulk query, grouped in
      memory — behavior unchanged for multi-resource scopes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from constat_api.insights.runner import (
    DEFAULT_SCOPE_MAX_AGE,
    DEFAULT_SOURCE,
    run_chargeback,
    run_rds_eol,
)
from constat_api.orm import InconclusiveORM, InsightORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import inconclusive as inconclusive_repo
from constat_api.repositories import insights as insights_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_chargeback.resolver import aggregate, build_insights
from constat_core.models import Fact, Severity, ValueState
from constat_focus.aggregator import AggregatedFocusCharge
from constat_focus.loader import FocusCharge
from sqlalchemy.orm import Session


def _resource(session: Session, account_id, native_id: str) -> ResourceORM:
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account_id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id=native_id,
    )
    session.add(resource)
    session.commit()
    return resource


def _successful_run(session: Session, account_id, *, age: timedelta = timedelta(0)):
    """A successful source_run for the eu-west-1 RDS scope, optionally aged."""
    run = source_runs_repo.start_run(
        session,
        account_id=account_id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    if age > timedelta(0):
        run.finished_at = datetime.now(tz=UTC) - age
    session.commit()
    return run


def _pg14_facts(session: Session, resource: ResourceORM, account_id, run_id) -> None:
    facts_repo.upsert_facts(
        session,
        [
            Fact(
                resource_id=resource.id,
                account_id=str(account_id),
                namespace="aws.rds",
                key=key,
                value=value,
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
            for key, value in [
                ("engine", "postgres"),
                ("engine_version", "14.7"),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 4),
                ("region", "eu-west-1"),
            ]
        ],
        source_run_id=run_id,
    )
    session.commit()


def _add_focus(
    session: Session,
    account_id,
    *,
    service: str = "AmazonRDS",
    billed: str = "100.00",
    amortized: str = "100.00",
) -> None:
    agg = AggregatedFocusCharge(
        service=service,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal(billed),
        amortized_cost=Decimal(amortized),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[],
        per_row_tag_dicts=[],
    )
    focus_charges_repo.upsert_aggregated(session, account_id, [agg])
    session.commit()


# ---- F-02: freshness window -------------------------------------------------


def test_scope_stale_when_successful_run_older_than_24h(session: Session) -> None:
    """Run finished 25h ago -> INCONCLUSIVE scope_stale, no insight."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = _resource(session, acc.id, "arn:stale-scope")
    run = _successful_run(session, acc.id, age=timedelta(hours=25))
    _pg14_facts(session, resource, acc.id, run.id)

    result = run_rds_eol(session, today=date(2026, 12, 1))
    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1

    inc = session.query(InconclusiveORM).one()
    assert "scope_stale" in inc.missing_facts
    assert "scope_stale" in inc.reason
    # The human-readable message carries the run age (~25h -> "1 day, 1:...").
    assert "1 day" in inc.reason


def test_scope_proven_when_successful_run_is_fresh(session: Session) -> None:
    """Run finished 1h ago -> normal evaluation (PG14 insight in window)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = _resource(session, acc.id, "arn:fresh-scope")
    run = _successful_run(session, acc.id, age=timedelta(hours=1))
    _pg14_facts(session, resource, acc.id, run.id)

    result = run_rds_eol(session, today=date(2026, 12, 1))
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 0


def test_scope_max_age_override_relaxes_freshness(session: Session) -> None:
    """The scope_max_age keyword overrides the 24h default (testability)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = _resource(session, acc.id, "arn:relaxed-scope")
    run = _successful_run(session, acc.id, age=timedelta(hours=25))
    _pg14_facts(session, resource, acc.id, run.id)

    result = run_rds_eol(session, today=date(2026, 12, 1), scope_max_age=timedelta(days=7))
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 0


def test_default_scope_max_age_is_24h() -> None:
    assert timedelta(hours=24) == DEFAULT_SCOPE_MAX_AGE


def test_latest_successful_run_max_age_filters_old_runs(session: Session) -> None:
    """Repository level: max_age=None keeps historical behavior; set -> filter."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    _successful_run(session, acc.id, age=timedelta(hours=25))

    kwargs = {
        "account_id": acc.id,
        "region": "eu-west-1",
        "resource_type": "AWS::RDS::DBInstance",
        "source": DEFAULT_SOURCE,
    }
    assert source_runs_repo.latest_successful_run(session, **kwargs) is not None
    assert (
        source_runs_repo.latest_successful_run(session, **kwargs, max_age=timedelta(hours=24))
        is None
    )
    assert (
        source_runs_repo.latest_successful_run(session, **kwargs, max_age=timedelta(days=2))
        is not None
    )


# ---- F-03: delete-and-replace -----------------------------------------------


def test_rds_eol_rerun_does_not_duplicate_rows(session: Session) -> None:
    """3 runs -> the rule's insight count stays constant (not 3x)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = _resource(session, acc.id, "arn:dedup")
    run = _successful_run(session, acc.id)
    _pg14_facts(session, resource, acc.id, run.id)

    for _ in range(3):
        result = run_rds_eol(session, today=date(2026, 12, 1))
        assert result.insights_emitted == 1

    assert insights_repo.count_insights(session, rule_name="rds_eol") == 1
    assert session.query(InsightORM).filter_by(rule_name="rds_eol").count() == 1


def test_rds_eol_rerun_does_not_duplicate_inconclusive(session: Session) -> None:
    """Same dedup guarantee for the inconclusive table."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    _resource(session, acc.id, "arn:dedup-inc")  # no source_run -> scope_not_proven

    for _ in range(3):
        result = run_rds_eol(session, today=date(2026, 12, 1))
        assert result.inconclusive_emitted == 1

    assert inconclusive_repo.count_inconclusive(session, rule_name="rds_eol") == 1


def test_chargeback_rerun_does_not_duplicate_insights(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    _add_focus(session, acc.id)

    for _ in range(3):
        result = run_chargeback(session)
        assert result.insights_emitted == 1

    assert insights_repo.count_insights(session, rule_name="chargeback") == 1


# ---- F-13: readable chargeback ----------------------------------------------


def test_chargeback_title_uses_account_name(session: Session) -> None:
    """The title carries the accounts-table display name, not the UUID."""
    acc = accounts_repo.get_or_create(session, "111111111111", name="Production")
    _add_focus(session, acc.id, billed="1000", amortized="2500")

    run_chargeback(session)
    insight = session.query(InsightORM).one()
    assert "Production" in insight.title
    assert str(acc.id) not in insight.title


def test_build_insights_title_falls_back_to_account_id() -> None:
    """Resolver level: empty account_name -> account_id in the title."""
    charge = FocusCharge(
        account_id="111111111111",
        account_name="",
        service="AmazonRDS",
        region="eu-west-1",
        pricing_category="On-Demand",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        resource_id=None,
        sub_account_id=None,
        tags=[],
    )
    insights = build_insights(aggregate([charge]))
    assert "on 111111111111" in insights[0].title


def test_build_insights_title_prefers_account_name() -> None:
    charge = FocusCharge(
        account_id="111111111111",
        account_name="Production",
        service="AmazonRDS",
        region="eu-west-1",
        pricing_category="On-Demand",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        resource_id=None,
        sub_account_id=None,
        tags=[],
    )
    insights = build_insights(aggregate([charge]))
    assert "on Production" in insights[0].title
    assert "111111111111" not in insights[0].title


def test_build_insights_drift_severity_always_info() -> None:
    """Drift magnitude never escalates severity (normal RI mechanics)."""
    for billed, amortized in [("100", "110"), ("1000", "1120"), ("1000", "2500")]:
        charge = FocusCharge(
            account_id="111111111111",
            account_name="",
            service="AmazonRDS",
            region="eu-west-1",
            pricing_category="On-Demand",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal(billed),
            amortized_cost=Decimal(amortized),
            resource_id=None,
            sub_account_id=None,
            tags=[],
        )
        insights = build_insights(aggregate([charge]))
        assert insights[0].severity == Severity.INFO


# ---- F-16: bulk fact fetch ---------------------------------------------------


def test_rds_eol_groups_bulk_facts_per_resource(session: Session) -> None:
    """Two resources in one scope: facts land on the right resource.

    Resource A (PG14 facts) -> insight; resource B (no facts) -> the
    '<no facts>' inconclusive. Same behavior as the old per-resource
    queries, from a single bulk fetch.
    """
    acc = accounts_repo.get_or_create(session, "111111111111")
    res_a = _resource(session, acc.id, "arn:bulk-a")
    _resource(session, acc.id, "arn:bulk-b")
    run = _successful_run(session, acc.id)
    _pg14_facts(session, res_a, acc.id, run.id)

    result = run_rds_eol(session, today=date(2026, 12, 1))
    assert result.resources_scanned == 2
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 1

    insight = session.query(InsightORM).one()
    assert insight.resource_id == res_a.id
    inc = session.query(InconclusiveORM).one()
    assert "<no facts>" in inc.missing_facts
