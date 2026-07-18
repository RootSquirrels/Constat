"""Tests for the POST /insights/run HTTP endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from constat_api.orm import ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, ValueState
from fastapi.testclient import TestClient


def _bootstrap_pg14(session, *, with_facts: bool = True) -> ResourceORM:
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:pg14",
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

    if with_facts:
        facts_repo.upsert_facts(
            session,
            [
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="engine",
                    value="postgres",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="engine_version",
                    value="14.7",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="instance_class",
                    value="db.m5.xlarge",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="vcpu",
                    value=4,
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
            ],
            source_run_id=run.id,
        )
        session.commit()
    return resource


def test_run_endpoint_emits_insight(client: TestClient, session) -> None:
    _bootstrap_pg14(session)
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        params={"today": "2026-12-01"},  # PG14 EOL is 2027-02-28
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule_name"] == "rds_eol"
    assert body["resources_scanned"] == 1
    assert body["insights_emitted"] == 1
    assert body["inconclusive_emitted"] == 0
    assert body["errors"] == []


def test_run_endpoint_with_inconclusive(client: TestClient, session) -> None:
    """Resource with no facts -> INCONCLUSIVE, no insight."""
    _bootstrap_pg14(session, with_facts=False)
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["insights_emitted"] == 0
    assert body["inconclusive_emitted"] == 1


def test_run_endpoint_rejects_unknown_rule(client: TestClient) -> None:
    response = client.post(
        "/insights/run",
        json={"rule": "made_up_rule"},
    )
    assert response.status_code == 400
    assert "unknown rule" in response.json()["detail"]


def test_run_endpoint_supports_chargeback(client: TestClient, session) -> None:
    """chargeback is now a supported V1 rule."""
    response = client.post(
        "/insights/run",
        json={"rule": "chargeback", "period_label": "all-time"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule_name"] == "chargeback"
    assert body["period_label"] == "all-time"


def test_run_endpoint_optional_today(client: TestClient, session) -> None:
    """today is optional; defaults to date.today() inside the runner."""
    _bootstrap_pg14(session)
    response = client.post("/insights/run", json={"rule": "rds_eol"})
    assert response.status_code == 200
    body = response.json()
    # PG14 in 2026-07-18: too far from EOL (591 days) -> no insight
    assert body["insights_emitted"] == 0


# ---------------------------------------------------------------------------
# Tag-based chargeback via the HTTP endpoint
# ---------------------------------------------------------------------------


def test_run_endpoint_chargeback_with_tag_key(client: TestClient, session) -> None:
    """The chargeback rule accepts a `tag_key` body field. When set,
    insights are split by the matching tag value (V2: proportional
    to per-row tag counts, see migration 0009)."""
    from datetime import date
    from decimal import Decimal

    from constat_api.repositories import accounts as accounts_repo
    from constat_api.repositories import focus_charges as focus_charges_repo
    from constat_focus.aggregator import AggregatedFocusCharge

    acc = accounts_repo.get_or_create(session, "111111111111")
    focus_charges_repo.upsert_aggregated(
        session,
        acc.id,
        [
            AggregatedFocusCharge(
                service="AmazonRDS",
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
                billed_cost=Decimal("200"),
                amortized_cost=Decimal("200"),
                charge_count=2,
                region="eu-west-1",
                pricing_category="On-Demand",
                resource_id=None,
                sub_account_id=None,
                tags=[{"Application": "web"}, {"Application": "api"}],
                per_row_tag_dicts=[{"Application": "web"}, {"Application": "api"}],
            )
        ],
    )
    session.commit()

    response = client.post(
        "/insights/run",
        json={"rule": "chargeback", "tag_key": "Application"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule_name"] == "chargeback"
    assert body["insights_emitted"] == 2
    assert "tag_key=Application" in body["period_label"]


def test_run_endpoint_chargeback_rejects_empty_tag_key_in_body(client: TestClient, session) -> None:
    """tag_key is optional; an empty string is treated as None (no split)."""
    response = client.post(
        "/insights/run",
        json={"rule": "chargeback", "tag_key": ""},
    )
    assert response.status_code == 200
    body = response.json()
    # No data -> no insights; the empty tag_key is treated as not set.
    assert body["insights_emitted"] == 0
