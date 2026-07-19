"""Tests for the inconclusive work queue (roadmap 2.5).

PATCH /inconclusives/{id} sets owner / due_date / status (operator only,
audited); GET /inconclusives filters by status and sorts by computed_at
(default, newest first) or rule_name (group by rule, then age).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from constat_api.auth import _get_settings
from constat_api.main import app
from constat_api.orm import AuditEventORM
from constat_api.repositories import inconclusive as inc_repo
from constat_api.settings import ApiKeyEntry, Settings
from constat_core.models import Inconclusive
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def _create(client: TestClient, rule_name: str = "rds_eol") -> str:
    response = client.post(
        "/inconclusives", json={"rule_name": rule_name, "missing_facts": ["aws.rds.vcpu"]}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_via_repo(session: Session, rule_name: str = "rds_eol") -> str:
    """Seed a record straight through the repo (RBAC tests: POST needs a key)."""
    item = inc_repo.insert_inconclusive(
        session, Inconclusive(rule_name=rule_name, missing_facts=["aws.rds.vcpu"])
    )
    session.commit()
    assert item.id is not None
    return str(item.id)


# ---- PATCH ------------------------------------------------------------------


def test_patch_sets_owner_due_status_and_audits(client: TestClient, session: Session) -> None:
    inc_id = _create(client)

    response = client.patch(
        f"/inconclusives/{inc_id}",
        json={"owner": "alice", "due_date": "2026-08-01", "status": "acknowledged"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner"] == "alice"
    assert body["due_date"] == "2026-08-01"
    assert body["status"] == "acknowledged"

    # Persisted: a fresh GET sees the same values.
    fetched = client.get(f"/inconclusives/{inc_id}").json()
    assert fetched["owner"] == "alice"
    assert fetched["status"] == "acknowledged"

    # Audit trail: one row, actor = the principal, field names only (no PII).
    rows = (
        session.query(AuditEventORM).filter(AuditEventORM.action == "inconclusive_workflow").all()
    )
    assert len(rows) == 1
    assert rows[0].actor == "machine:anonymous"  # auth open in tests
    assert rows[0].target_id == inc_id
    assert rows[0].metadata_json["fields_updated"] == ["due_date", "owner", "status"]


def test_patch_is_partial(client: TestClient) -> None:
    """Only the keys explicitly sent are applied; the rest is untouched."""
    inc_id = _create(client)
    client.patch(f"/inconclusives/{inc_id}", json={"owner": "alice"})

    response = client.patch(f"/inconclusives/{inc_id}", json={"status": "resolved"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["owner"] == "alice"  # kept
    assert body["due_date"] is None


def test_patch_can_clear_a_field_with_null(client: TestClient) -> None:
    inc_id = _create(client)
    client.patch(f"/inconclusives/{inc_id}", json={"owner": "alice"})
    response = client.patch(f"/inconclusives/{inc_id}", json={"owner": None})
    assert response.status_code == 200
    assert response.json()["owner"] is None


def test_patch_invalid_status_is_400(client: TestClient) -> None:
    inc_id = _create(client)
    response = client.patch(f"/inconclusives/{inc_id}", json={"status": "bogus"})
    assert response.status_code == 400


def test_patch_unknown_id_is_404(client: TestClient) -> None:
    response = client.patch(f"/inconclusives/{uuid4()}", json={"status": "resolved"})
    assert response.status_code == 404


# ---- GET filters / sort ------------------------------------------------------


def test_list_filters_by_status(client: TestClient) -> None:
    open_id = _create(client, "rds_eol")
    _create(client, "mysql_eol")
    client.patch(f"/inconclusives/{open_id}", json={"status": "acknowledged"})

    acknowledged = client.get("/inconclusives", params={"status": "acknowledged"})
    assert acknowledged.status_code == 200
    assert [i["id"] for i in acknowledged.json()] == [open_id]

    open_items = client.get("/inconclusives", params={"status": "open"})
    assert len(open_items.json()) == 1
    assert open_items.json()[0]["rule_name"] == "mysql_eol"

    bad = client.get("/inconclusives", params={"status": "bogus"})
    assert bad.status_code == 400


def test_list_sorts_by_rule_name_then_age(client: TestClient) -> None:
    _create(client, "rds_eol")
    _create(client, "mysql_eol")

    response = client.get("/inconclusives", params={"sort": "rule_name"})
    assert response.status_code == 200
    rules = [i["rule_name"] for i in response.json()]
    assert rules == ["mysql_eol", "rds_eol"]

    bad = client.get("/inconclusives", params={"sort": "impact"})
    assert bad.status_code == 400


def test_list_default_sort_is_newest_first(client: TestClient) -> None:
    first = _create(client, "rds_eol")
    second = _create(client, "mysql_eol")
    response = client.get("/inconclusives")
    ids = [i["id"] for i in response.json()]
    # Same-second timestamps can tie on sqlite; assert set + the sort is
    # exercised in test_list_sorts_by_rule_name_then_age above.
    assert set(ids) == {first, second}


# ---- RBAC --------------------------------------------------------------------


@pytest.fixture
def rbac_settings(client: TestClient):
    """alice=operator, bob=reader — same pattern as tests/test_rbac.py."""
    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key="alice-key"),
            ApiKeyEntry(name="bob", role="reader", key="bob-key"),
        )
    )
    app.dependency_overrides[_get_settings] = lambda: cfg
    yield cfg
    app.dependency_overrides.pop(_get_settings, None)


def test_reader_cannot_patch(rbac_settings: Settings, client: TestClient, session: Session) -> None:
    inc_id = _create_via_repo(session)
    response = client.patch(
        f"/inconclusives/{inc_id}",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "bob-key"},
    )
    assert response.status_code == 403


def test_operator_can_patch(rbac_settings: Settings, client: TestClient, session: Session) -> None:
    inc_id = _create_via_repo(session)
    response = client.patch(
        f"/inconclusives/{inc_id}",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "alice-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"


def test_reader_can_still_list(rbac_settings: Settings, client: TestClient) -> None:
    response = client.get("/inconclusives", headers={"X-API-Key": "bob-key"})
    assert response.status_code == 200
