"""Repository unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from constat_api.orm import AccountORM
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import insights as insights_repo
from constat_core.models import Fact, Insight, Severity, ValueState
from sqlalchemy.orm import Session


def _make_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="test")
    session.add(acc)
    session.commit()
    return acc


def _make_resource(session: Session, account_id) -> str:
    from constat_api.orm import ResourceORM

    r = ResourceORM(
        account_id=account_id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:test",
    )
    session.add(r)
    session.commit()
    return str(r.id)


def test_insert_and_list_insight(session: Session) -> None:
    acc = _make_account(session)
    insight = Insight(
        rule_name="rds_eol",
        account_id=str(acc.id),
        severity=Severity.WARNING,
        title="RDS PG 14 EOL in 89 days",
        payload={"days_to_eol": 89},
        computed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    created = insights_repo.insert_insight(session, insight)
    session.commit()

    assert created.id is not None
    assert created.severity == Severity.WARNING

    listed = insights_repo.list_insights(session, rule_name="rds_eol")
    assert len(listed) == 1
    assert listed[0].id == created.id


def test_count_insights(session: Session) -> None:
    acc = _make_account(session)
    for _ in range(3):
        insights_repo.insert_insight(
            session,
            Insight(
                rule_name="rds_eol",
                account_id=str(acc.id),
                severity=Severity.WARNING,
                title="x",
                payload={},
            ),
        )
    session.commit()
    assert insights_repo.count_insights(session, rule_name="rds_eol") == 3
    assert insights_repo.count_insights(session, rule_name="chargeback") == 0


def test_insert_and_list_facts(session: Session) -> None:
    acc = _make_account(session)
    resource_id = _make_resource(session, acc.id)
    now = datetime(2026, 7, 18, tzinfo=UTC)

    facts = [
        Fact(
            resource_id=resource_id,
            account_id=str(acc.id),
            namespace="aws.rds",
            key="engine",
            value="postgres",
            value_state=ValueState.KNOWN,
            source="aws_rds",
            observed_at=now,
        ),
        Fact(
            resource_id=resource_id,
            account_id=str(acc.id),
            namespace="aws.rds",
            key="engine_version",
            value="14.7",
            value_state=ValueState.KNOWN,
            source="aws_rds",
            observed_at=now,
        ),
    ]
    n = facts_repo.insert_facts(session, facts)
    session.commit()
    assert n == 2

    listed = facts_repo.list_facts_for_resource(session, resource_id)
    assert len(listed) == 2
    by_key = {f.key: f for f in listed}
    assert by_key["engine"].value == "postgres"
    assert by_key["engine_version"].value == "14.7"
