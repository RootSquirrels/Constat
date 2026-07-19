"""Tests for batch onboarding (roadmap 1.3): persisted collect targets.

Covers:
- POST /collect/targets/import (CSV upsert, per-row validation, idempotent
  re-import, external_id never echoed),
- GET /collect/targets (external_id masked),
- DELETE /collect/targets/{aws_account_id} (offboard),
- POST /collect/aws with no explicit targets -> collects all persisted
  targets.
"""

from __future__ import annotations

from constat_api.orm import AuditEventORM, CollectJobORM, CollectTargetORM
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

CSV_HEADER = "aws_account_id,role_arn,external_id,name,regions"
SECRET = "ext-secret-aaa111"


def _csv(*rows: str) -> str:
    return "\n".join([CSV_HEADER, *rows]) + "\n"


def _row(
    account_id: str,
    role_arn: str = "arn:aws:iam::111111111111:role/constat-collector",
    external_id: str = SECRET,
    name: str = "prod",
    regions: str = "",
) -> str:
    return f"{account_id},{role_arn},{external_id},{name},{regions}"


def _import(client: TestClient, csv_text: str):
    return client.post(
        "/collect/targets/import",
        content=csv_text,
        headers={"content-type": "text/csv"},
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_happy_path(client: TestClient, session: Session) -> None:
    csv_text = _csv(
        _row("111111111111", name="prod"),
        _row("222222222222", name="staging", regions="eu-west-1;eu-central-1"),
        _row("333333333333", name="dev"),
    )
    response = _import(client, csv_text)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["imported"] == 3
    assert payload["updated"] == 0
    assert payload["rejected"] == []

    rows = session.execute(select(CollectTargetORM)).scalars().all()
    assert len(rows) == 3
    staging = next(r for r in rows if r.aws_account_id == "222222222222")
    assert staging.regions == ["eu-west-1", "eu-central-1"]

    # Operator action audited, counts only.
    event = session.execute(
        select(AuditEventORM).where(AuditEventORM.action == "collect_targets_imported")
    ).scalar_one()
    assert event.metadata_json == {"imported": 3, "updated": 0, "rejected": 0}


def test_import_rejects_f06_row_role_arn_without_external_id(
    client: TestClient, session: Session
) -> None:
    csv_text = _csv(
        _row("111111111111"),
        _row("222222222222", external_id=""),  # F-06: role_arn without external_id
    )
    payload = _import(client, csv_text).json()
    assert payload["imported"] == 1
    assert len(payload["rejected"]) == 1
    assert payload["rejected"][0]["line"] == 3
    assert "F-06" in payload["rejected"][0]["reason"]
    assert session.execute(select(CollectTargetORM)).scalars().all().__len__() == 1


def test_import_rejects_bad_account_id(client: TestClient) -> None:
    payload = _import(client, _csv(_row("not-an-account"))).json()
    assert payload["imported"] == 0
    assert payload["rejected"][0]["reason"] == "aws_account_id must be 12 digits"


def test_import_rejects_unknown_region(client: TestClient) -> None:
    payload = _import(client, _csv(_row("111111111111", regions="eu-west-1;mars-1"))).json()
    assert payload["imported"] == 0
    assert "unknown region" in payload["rejected"][0]["reason"]


def test_reimport_is_idempotent_upsert(client: TestClient, session: Session) -> None:
    first = _import(client, _csv(_row("111111111111"))).json()
    assert first == {"imported": 1, "updated": 0, "rejected": []}

    new_role = "arn:aws:iam::111111111111:role/constat-collector-v2"
    second = _import(client, _csv(_row("111111111111", role_arn=new_role, name="prod-v2"))).json()
    assert second == {"imported": 0, "updated": 1, "rejected": []}

    rows = session.execute(select(CollectTargetORM)).scalars().all()
    assert len(rows) == 1  # no duplicate
    assert rows[0].role_arn == new_role
    assert rows[0].name == "prod-v2"


def test_import_accepts_json_envelope(client: TestClient) -> None:
    response = client.post(
        "/collect/targets/import",
        json={"csv": _csv(_row("111111111111"))},
    )
    assert response.status_code == 200
    assert response.json()["imported"] == 1


def test_import_rejects_bad_header(client: TestClient) -> None:
    response = _import(client, "aws_account_id,role_arn\n111111111111,arn:x\n")
    assert response.status_code == 422


def test_external_id_never_echoed(client: TestClient, session: Session) -> None:
    """The shared secret must not appear in ANY API response (write-only)."""
    import_response = _import(client, _csv(_row("111111111111")))
    assert SECRET not in import_response.text

    get_response = client.get("/collect/targets")
    assert SECRET not in get_response.text

    delete_response = client.delete("/collect/targets/111111111111")
    assert SECRET not in delete_response.text


# ---------------------------------------------------------------------------
# GET list (masked)
# ---------------------------------------------------------------------------


def test_get_list_masks_external_id(client: TestClient) -> None:
    _import(client, _csv(_row("111111111111"), _row("222222222222", name="staging")))
    response = client.get("/collect/targets")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    for row in rows:
        assert row["external_id_set"] is True
        assert "external_id" not in row  # masked, never the value
        assert row["role_arn"].startswith("arn:aws:iam::")  # role_arn is not secret
    assert {r["aws_account_id"] for r in rows} == {"111111111111", "222222222222"}


def test_repository_list_without_secrets_defers_external_id(session: Session) -> None:
    """The read path must not even SELECT the secret column."""
    from constat_api.repositories import collect_targets as repo

    repo.upsert(
        session,
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/constat-collector",
        external_id=SECRET,
    )
    session.commit()

    masked = repo.list_targets(session)
    assert "external_id" in inspect(masked[0]).unloaded

    full = repo.list_targets(session, with_secrets=True)
    assert full[0].external_id == SECRET


# ---------------------------------------------------------------------------
# DELETE (offboard)
# ---------------------------------------------------------------------------


def test_delete_offboards_target(client: TestClient, session: Session) -> None:
    _import(client, _csv(_row("111111111111"), _row("222222222222")))
    response = client.delete("/collect/targets/111111111111")
    assert response.status_code == 200

    remaining = client.get("/collect/targets").json()
    assert [r["aws_account_id"] for r in remaining] == ["222222222222"]

    event = session.execute(
        select(AuditEventORM).where(AuditEventORM.action == "collect_target_deleted")
    ).scalar_one()
    assert event.metadata_json == {"deleted": 1}  # counts only


def test_delete_unknown_target_404(client: TestClient) -> None:
    assert client.delete("/collect/targets/999999999999").status_code == 404


# ---------------------------------------------------------------------------
# POST /collect/aws empty-body fallback (roadmap 1.3)
# ---------------------------------------------------------------------------


def test_collect_aws_without_targets_uses_persisted_targets(
    client: TestClient, session: Session
) -> None:
    _import(
        client,
        _csv(
            _row("111111111111", regions="eu-west-1;eu-central-1"),
            _row("222222222222", regions="eu-west-1;eu-central-1"),
        ),
    )
    response = client.post("/collect/aws", json={})
    assert response.status_code == 202, response.text
    # 2 persisted targets x 2 regions each.
    assert response.json()["items_enqueued"] == 4

    job = session.execute(select(CollectJobORM)).scalar_one()
    assert job.summary["accounts"] == 2
    assert job.summary["regions"] == 4


def test_collect_aws_without_targets_and_nothing_persisted_is_422(client: TestClient) -> None:
    response = client.post("/collect/aws", json={})
    assert response.status_code == 422
    assert "/collect/targets/import" in response.json()["detail"]


def test_collect_aws_explicit_targets_still_work(client: TestClient) -> None:
    """Regression guard: the pre-1.3 explicit-targets path is unchanged."""
    response = client.post(
        "/collect/aws",
        json={"targets": [{"aws_account_id": "111111111111", "regions": ["eu-west-1"]}]},
    )
    assert response.status_code == 202
    assert response.json()["items_enqueued"] == 1
