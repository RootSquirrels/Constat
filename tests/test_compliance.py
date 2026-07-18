"""Tests for the V1 security & compliance feature set (migration 0010).

Covers:
- audit: record, read, PII-key validation, actor formatting
- PII: classification rules, hash, recording
- retention: apply_retention, apply_all_enabled, seed_default_policies
- HTTP: /audit-events, /pii-classifications, /retention-policies, /retention/run
- integration: AWS scan writes audit + pii rows
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from constat_api.audit import (
    AuditLogger,
    actor_for_api_key,
    record_event,
)
from constat_api.orm import (
    AuditEventORM,
    PIIClassificationORM,
    RetentionPolicyORM,
)
from constat_api.pii import (
    ALL_SENSITIVITIES,
    CONFIDENTIAL,
    INTERNAL,
    PUBLIC,
    PIIClassifier,
    classify,
    hash_value,
)
from constat_api.retention import (
    DEFAULT_RETENTION_DAYS,
    apply_all_enabled,
    apply_retention,
    seed_default_policies,
)
from constat_api.settings import DEFAULT_TENANT_ID
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import make_rds_db_dict

# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_record_stores_event(session: Session) -> None:
    AuditLogger(session).record(
        action="test_action",
        actor="api_key:abc123",
        target_type="account",
        target_id="111111111111",
        metadata={"regions_scanned": 7},
    )
    session.commit()
    rows = session.query(AuditEventORM).all()
    assert len(rows) == 1
    assert rows[0].action == "test_action"
    assert rows[0].actor == "api_key:abc123"
    assert rows[0].target_type == "account"
    assert rows[0].target_id == "111111111111"
    assert rows[0].metadata_json == {"regions_scanned": 7}


def test_audit_record_rejects_pii_keys_in_metadata(session: Session) -> None:
    """Defense in depth: the metadata dict must NOT contain PII keys.
    An attempt to log such a dict raises ValueError before the DB
    write happens."""
    for forbidden_key in ["account_id", "arn", "tag_value", "value", "api_key"]:
        with pytest.raises(ValueError, match="forbidden PII keys"):
            AuditLogger(session).record(
                action="test",
                metadata={forbidden_key: "secret"},
            )
    # And no rows were written
    assert session.query(AuditEventORM).count() == 0


def test_audit_actor_for_api_key_hashes_the_key() -> None:
    """The API key value must never appear in the audit log — only
    its hash (first 16 chars of SHA-256)."""
    actor = actor_for_api_key("secret-key-12345")
    assert actor.startswith("api_key:")
    # Same key -> same hash (deterministic).
    assert actor == actor_for_api_key("secret-key-12345")
    # Different key -> different hash.
    assert actor != actor_for_api_key("other-key")
    # The hash prefix is short — full key not leaked.
    assert "secret-key-12345" not in actor


def test_audit_record_event_convenience_function(session: Session) -> None:
    record_event(
        session,
        action="convenience_test",
        metadata={"count": 1},
    )
    session.commit()
    assert session.query(AuditEventORM).one().action == "convenience_test"


# ---------------------------------------------------------------------------
# PII classification
# ---------------------------------------------------------------------------


def test_pii_classify_known_field() -> None:
    assert classify("account_id", "111111111111")[0] == CONFIDENTIAL
    assert classify("arn", "arn:aws:rds:eu-west-1:111111111111:db:t")[0] == CONFIDENTIAL
    assert classify("engine", "postgres")[0] == PUBLIC
    assert classify("region", "eu-west-1")[0] == INTERNAL


def test_pii_classify_unknown_field_defaults_to_internal() -> None:
    assert classify("made_up_field", "anything")[0] == INTERNAL


def test_pii_classify_subkeys_use_prefix_rule() -> None:
    """Field name 'tag:Application' uses the rule for 'tag' (CONFIDENTIAL)."""
    assert classify("tag:Application", "web")[0] == CONFIDENTIAL
    assert classify("tag:CostCenter", "42")[0] == CONFIDENTIAL


def test_pii_classify_hash_is_deterministic_and_hex() -> None:
    h1 = hash_value("111111111111")
    h2 = hash_value("111111111111")
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)
    # Different value -> different hash
    assert hash_value("222222222222") != h1


def test_pii_classifier_record_stores_hash_not_value(session: Session) -> None:
    PIIClassifier(session).record(
        resource_type="account",
        resource_id="111111111111",
        field_name="aws_account_id",
        value="111111111111",
    )
    session.commit()
    row = session.query(PIIClassificationORM).one()
    # Hash is the SHA-256 of the value, not the value itself
    assert row.value_hash == hash_value("111111111111")
    assert "111111111111" not in str(row.value_hash)
    # Sensitivity default for aws_account_id is confidential
    assert row.sensitivity == CONFIDENTIAL


def test_pii_classifier_record_rejects_empty_value(session: Session) -> None:
    """Empty values are silently skipped (no row, no error)."""
    result = PIIClassifier(session).record(
        resource_type="account",
        resource_id="111",
        field_name="aws_account_id",
        value="",
    )
    assert result is None
    assert session.query(PIIClassificationORM).count() == 0


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_seed_default_policies_is_idempotent(session: Session) -> None:
    """Running seed_default_policies twice is a no-op on the second call."""
    first = seed_default_policies(session)
    second = seed_default_policies(session)
    assert first == len(DEFAULT_RETENTION_DAYS)
    assert second == 0
    assert session.query(RetentionPolicyORM).count() == len(DEFAULT_RETENTION_DAYS)


def test_retention_apply_deletes_only_old_rows(session: Session) -> None:
    """Rows older than the retention window are deleted; fresher ones survive."""
    from constat_api.orm import ObservationORM

    # Just use direct observation inserts (no FK to account in V1).
    old_time = datetime.now(tz=UTC) - timedelta(days=200)
    fresh_time = datetime.now(tz=UTC) - timedelta(days=10)
    session.add(
        ObservationORM(  # type: ignore[call-arg]
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=__import__("uuid").uuid4(),
            source="test",
            observed_at=old_time,
            ingested_at=old_time,
            payload={},
        )
    )
    session.add(
        ObservationORM(  # type: ignore[call-arg]
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=__import__("uuid").uuid4(),
            source="test",
            observed_at=fresh_time,
            ingested_at=fresh_time,
            payload={},
        )
    )
    session.commit()
    assert session.query(ObservationORM).count() == 2

    deleted = apply_retention(session, table_name="observations", retention_days=90)
    assert deleted == 1
    assert session.query(ObservationORM).count() == 1


def test_retention_apply_rejects_unknown_table(session: Session) -> None:
    """A typo in table_name should NOT wipe the wrong table."""
    with pytest.raises(ValueError, match="disallowed table"):
        apply_retention(session, table_name="users", retention_days=30)
    with pytest.raises(ValueError, match="disallowed table"):
        apply_retention(session, table_name="", retention_days=30)


def test_retention_apply_rejects_negative_days(session: Session) -> None:
    with pytest.raises(ValueError, match="retention_days must be >= 0"):
        apply_retention(session, table_name="observations", retention_days=-1)


def test_retention_apply_all_enabled_updates_policy_audit_fields(
    session: Session,
) -> None:
    """apply_all_enabled records last_applied_at and last_deleted_count
    for the operator's audit trail."""
    seed_default_policies(session)
    results = apply_all_enabled(session)
    # No data to delete (empty tables), but every policy was "applied"
    assert all(v == 0 for v in results.values())
    # last_applied_at is set
    policies = session.query(RetentionPolicyORM).all()
    for p in policies:
        assert p.last_applied_at is not None
        assert p.last_deleted_count == 0


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_audit_events_endpoint_lists_recent_events(client: TestClient, session) -> None:
    record_event(
        session,
        action="http_test",
        actor="api_key:deadbeef",
        metadata={"count": 5},
    )
    session.commit()

    response = client.get("/compliance/audit-events")
    assert response.status_code == 200
    events = response.json()
    assert len(events) >= 1
    # Most recent first
    last = events[0]
    assert last["action"] == "http_test"
    assert last["actor"] == "api_key:deadbeef"
    assert last["metadata"] == {"count": 5}


def test_audit_events_endpoint_filters_by_actor(client: TestClient, session) -> None:
    record_event(session, action="a", actor="actor_one")
    record_event(session, action="b", actor="actor_two")
    session.commit()

    response = client.get("/compliance/audit-events", params={"actor": "actor_one"})
    assert response.status_code == 200
    events = response.json()
    assert all(e["actor"] == "actor_one" for e in events)
    assert all(e["action"] == "a" for e in events)


def test_pii_classifications_endpoint_filters_by_sensitivity(client: TestClient, session) -> None:
    PIIClassifier(session).record(
        resource_type="account",
        resource_id="111",
        field_name="aws_account_id",
        value="111",
    )
    PIIClassifier(session).record(
        resource_type="resource",
        resource_id="arn:1",
        field_name="engine",
        value="postgres",
    )
    session.commit()

    response = client.get("/compliance/pii-classifications", params={"sensitivity": "confidential"})
    assert response.status_code == 200
    rows = response.json()
    assert all(r["sensitivity"] == "confidential" for r in rows)
    # We never see the engine row (it's public)
    assert not any(r["field_name"] == "engine" for r in rows)


def test_pii_classifications_endpoint_does_not_leak_values(client: TestClient, session) -> None:
    """The endpoint returns the hash, not the value. The resource_id
    IS the value (we identify rows by it), but the value field is
    never stored — only the SHA-256. We assert the value_hash field
    matches the expected SHA-256."""
    PIIClassifier(session).record(
        resource_type="account",
        resource_id="999999999999",
        field_name="aws_account_id",
        value="999999999999",
    )
    session.commit()

    response = client.get("/compliance/pii-classifications")
    assert response.status_code == 200
    rows = response.json()
    matching = [r for r in rows if r["resource_id"] == "999999999999"]
    assert len(matching) == 1
    # The value_hash is SHA-256 of the value (64 hex chars), NOT the
    # value itself.
    assert matching[0]["value_hash"] == hash_value("999999999999")
    assert matching[0]["value_hash"] != "999999999999"
    # The raw 12-digit value is the resource_id (which we identify
    # rows by), but it should NOT appear in the value_hash field
    # (which is what a security team would check for PII leakage).
    assert matching[0]["value_hash"] not in [
        "999999999999",  # the value
    ]


def test_retention_policies_endpoint_seeds_and_lists(client: TestClient, session) -> None:
    """First call to /retention/run auto-seeds the default policies."""
    response = client.post("/compliance/retention/run")
    assert response.status_code == 200
    body = response.json()
    assert body["tables_processed"] == len(DEFAULT_RETENTION_DAYS)

    list_response = client.get("/compliance/retention-policies")
    assert list_response.status_code == 200
    policies = list_response.json()
    table_names = {p["table_name"] for p in policies}
    assert "observations" in table_names
    assert "audit_events" in table_names
    assert all(p["enabled"] for p in policies)


def test_compliance_endpoints_require_auth_when_key_set(client: TestClient) -> None:
    """All /compliance/* endpoints require the X-API-Key header when
    CONSTAT_API_KEY is set. (V1 dev mode: no key = open. We
    simulate the prod mode by overriding the settings dep.)"""
    from constat_api.auth import _get_settings
    from constat_api.main import app
    from constat_api.settings import Settings

    test_key = "compliance-test-key"

    def _override():
        return Settings(api_key=test_key)

    app.dependency_overrides[_get_settings] = _override
    try:
        for url in [
            "/compliance/audit-events",
            "/compliance/pii-classifications",
            "/compliance/retention-policies",
            "/compliance/retention/run",
        ]:
            response = client.get(url) if "run" not in url else client.post(url)
            assert response.status_code == 401, f"{url} should require auth"
    finally:
        app.dependency_overrides.pop(_get_settings, None)


# ---------------------------------------------------------------------------
# Integration: AWS scan writes audit + PII rows
# ---------------------------------------------------------------------------


def test_aws_scan_writes_audit_and_pii_rows(client: TestClient, session: Session) -> None:
    """The full integration: POST /collect/aws writes both an audit
    event and PII classifications for the customer identifiers."""
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "role_arn": "arn:aws:iam::111111111111:role/ConstatReadOnly",
                "external_id": "secret",
                "name": "prod",
                "regions": ["eu-west-1"],
            }
        ],
        "dry_run": False,
    }
    with (
        patch("constat_api.routers.aws.get_base_aws_session") as mock_session,
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch(
            "constat_api.collectors.aws.collect_db_instances",
            side_effect=lambda s, regions: iter(
                {"_region": r, **make_rds_db_dict()} for r in regions
            ),
        ),
    ):
        mock_session.return_value = MagicMock()
        response = client.post("/collect/aws", json=body)
    assert response.status_code == 200

    # Audit: the scan was logged
    audit = session.query(AuditEventORM).filter(AuditEventORM.action == "aws_scan_completed").all()
    assert len(audit) == 1
    assert audit[0].target_id == "111111111111"
    assert audit[0].actor == "system:aws_collector"
    # Metadata is non-PII
    assert "resources_written" in audit[0].metadata_json
    assert "account_id" not in audit[0].metadata_json

    # PII: account_id + role_arn + region are classified
    pii_rows = session.query(PIIClassificationORM).all()
    field_names = {r.field_name for r in pii_rows}
    assert "aws_account_id" in field_names
    assert "arn" in field_names
    assert "region" in field_names

    # The PII rows store the hash, not the value
    for pii in pii_rows:
        if pii.field_name == "aws_account_id":
            assert pii.value_hash == hash_value("111111111111")
        assert pii.sensitivity in ALL_SENSITIVITIES
