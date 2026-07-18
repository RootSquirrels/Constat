"""Tests for the Inconclusive repository and HTTP endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from constat_api.orm import AccountORM
from constat_api.repositories import inconclusive as inc_repo
from constat_core.models import Inconclusive
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def _make_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="test")
    session.add(acc)
    session.commit()
    return acc


def test_insert_and_list_inconclusive(session: Session) -> None:
    _make_account(session)
    item = Inconclusive(
        rule_name="rds_eol",
        missing_facts=["aws.rds.vcpu", "aws.rds.engine_version"],
        reason="Insufficient data to assess EOL",
        computed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    created = inc_repo.insert_inconclusive(session, item)
    session.commit()

    assert created.id is not None
    assert created.missing_facts == ["aws.rds.vcpu", "aws.rds.engine_version"]

    listed = inc_repo.list_inconclusive(session, rule_name="rds_eol")
    assert len(listed) == 1
    assert listed[0].id == created.id


def test_count_inconclusive(session: Session) -> None:
    _make_account(session)
    for i in range(3):
        inc_repo.insert_inconclusive(
            session,
            Inconclusive(
                rule_name="rds_eol",
                missing_facts=[f"aws.rds.fact_{i}"],
            ),
        )
    session.commit()
    assert inc_repo.count_inconclusive(session, rule_name="rds_eol") == 3
    assert inc_repo.count_inconclusive(session, rule_name="chargeback") == 0


def test_inconclusive_endpoint_roundtrip(client: TestClient) -> None:
    payload = {
        "rule_name": "rds_eol",
        "missing_facts": ["aws.rds.vcpu"],
        "reason": "test",
    }
    response = client.post("/inconclusives", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["missing_facts"] == ["aws.rds.vcpu"]
    assert created["rule_name"] == "rds_eol"
    assert "id" in created


def test_inconclusive_endpoint_list(client: TestClient) -> None:
    client.post("/inconclusives", json={"rule_name": "rds_eol", "missing_facts": ["x"]})
    response = client.get("/inconclusives")
    assert response.status_code == 200
    assert len(response.json()) == 1
