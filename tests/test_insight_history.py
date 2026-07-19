"""Tests for the appeared/resolved insight history (roadmap 2.4).

The runner's delete-and-replace wipes insights each run; insight_events
keeps the lifecycle. These tests run rules twice with fact changes and
assert the event stream + the /insights/history endpoint summary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from constat_api.insights.runner import run_chargeback, run_resource_rule
from constat_api.orm import AccountORM, InsightEventORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_core.models import Fact, ValueState
from constat_focus.aggregator import AggregatedFocusCharge
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# PG11, 2 vCPU, 2026-07-18 -> year-3 tier: 2 x $0.20 x 730h = $292.00.
ESTIMATE = 292.0


def _bootstrap_pg11(session: Session) -> ResourceORM:
    """Account + PG11 resource + scope proof + facts. One EOL insight."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:pg11",
    )
    session.add(resource)
    session.commit()
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()
    _set_engine_version(session, resource, "11.22", run.id)
    return resource


def _set_engine_version(session: Session, resource: ResourceORM, version: str, run_id) -> None:
    """Upsert the full fact set with a given engine version (current-state)."""
    facts_repo.upsert_facts(
        session,
        [
            Fact(
                resource_id=resource.id,
                account_id=str(resource.account_id),
                namespace="aws.rds",
                key=key,
                value=value,
                value_state=ValueState.KNOWN,
                source="aws_rds",
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
            for key, value in [
                ("engine", "postgres"),
                ("engine_version", version),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 2),
            ]
        ],
        source_run_id=run_id,
    )
    session.commit()


def _run_rds(session: Session) -> None:
    result = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert result.errors == []


def _events(session: Session) -> list[InsightEventORM]:
    return (
        session.query(InsightEventORM)
        .order_by(InsightEventORM.occurred_at, InsightEventORM.id)
        .all()
    )


# ---- Runner diff ------------------------------------------------------------


def test_first_run_records_appeared_with_amount(session: Session) -> None:
    _bootstrap_pg11(session)
    _run_rds(session)

    events = _events(session)
    assert len(events) == 1
    event = events[0]
    assert event.event == "appeared"
    assert event.rule_name == "rds_eol"
    assert event.monthly_usd == pytest.approx(ESTIMATE)
    assert event.insight_run_id is not None
    assert len(event.fingerprint) == 64  # sha256 hex


def test_rerun_without_changes_records_nothing(session: Session) -> None:
    """Idempotent: same facts -> same fingerprints -> zero new events."""
    _bootstrap_pg11(session)
    _run_rds(session)
    _run_rds(session)
    _run_rds(session)

    events = _events(session)
    assert len(events) == 1
    assert events[0].event == "appeared"


def test_gap_closing_records_resolved_with_old_amount(session: Session) -> None:
    """Fact change closing the gap (PG11 -> PG14, far from EOL) -> resolved,
    carrying the OLD monthly amount: the money recovered."""
    resource = _bootstrap_pg11(session)
    _run_rds(session)

    _set_engine_version(session, resource, "14.7", run_id=None)
    _run_rds(session)

    events = _events(session)
    # sqlite timestamps tie at second precision; compare as a multiset.
    assert sorted(e.event for e in events) == ["appeared", "resolved"]
    appeared = next(e for e in events if e.event == "appeared")
    resolved = next(e for e in events if e.event == "resolved")
    assert resolved.monthly_usd == pytest.approx(ESTIMATE)
    assert resolved.title == appeared.title
    assert resolved.fingerprint == appeared.fingerprint


def test_new_gap_records_appeared(session: Session) -> None:
    """A second resource going EOL appears as its own event on the next run."""
    _bootstrap_pg11(session)
    _run_rds(session)

    acc = session.query(AccountORM).one()
    resource2 = ResourceORM(
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:pg11-bis",
    )
    session.add(resource2)
    session.commit()
    _set_engine_version(session, resource2, "11.22", run_id=None)
    _run_rds(session)

    events = _events(session)
    assert [e.event for e in events] == ["appeared", "appeared"]
    assert any(e.resource_id == resource2.id for e in events)


def test_chargeback_history_is_tracked_too(session: Session) -> None:
    """run_chargeback also deletes/replaces: same appeared/resolved diff."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[],
        per_row_tag_dicts=[],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    session.commit()

    first = run_chargeback(session)
    assert first.insights_emitted == 1
    appeared = [e for e in _events(session) if e.event == "appeared"]
    assert len(appeared) == 1

    # Re-run with identical data: no new events.
    run_chargeback(session)
    assert len(_events(session)) == 1


# ---- Endpoint ---------------------------------------------------------------


def test_history_endpoint_returns_events_and_summary(client: TestClient, session: Session) -> None:
    resource = _bootstrap_pg11(session)
    _run_rds(session)
    _set_engine_version(session, resource, "14.7", run_id=None)
    _run_rds(session)

    response = client.get("/insights/history")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["summary"]["appeared_count"] == 1
    assert body["summary"]["resolved_count"] == 1
    assert body["summary"]["resolved_monthly_usd_total"] == pytest.approx(ESTIMATE)

    events = body["events"]
    assert len(events) == 2
    # sqlite timestamps tie at second precision; assert content, not order.
    assert sorted(e["event"] for e in events) == ["appeared", "resolved"]
    assert all(e["rule_name"] == "rds_eol" for e in events)


def test_history_endpoint_filters(client: TestClient, session: Session) -> None:
    _bootstrap_pg11(session)
    _run_rds(session)

    resolved = client.get("/insights/history", params={"event": "resolved"})
    assert resolved.status_code == 200
    assert resolved.json()["events"] == []
    assert resolved.json()["summary"]["resolved_count"] == 0

    by_rule = client.get("/insights/history", params={"rule_name": "rds_eol"})
    assert len(by_rule.json()["events"]) == 1
    other_rule = client.get("/insights/history", params={"rule_name": "mysql_eol"})
    assert other_rule.json()["events"] == []

    bad = client.get("/insights/history", params={"event": "bogus"})
    assert bad.status_code == 400
