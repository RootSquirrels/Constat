"""Tests for the V1 RBAC layer (CISO review).

Model: CONSTAT_API_KEYS carries comma-separated `name:role:key` entries.
Roles: `reader` (read endpoints only) and `operator` (everything). The
legacy CONSTAT_API_KEY maps to an implicit ("default", "operator")
principal. No keys at all -> auth open (dev), anonymous operator.

Enforcement contract: a reader can call every GET but must get 403 on
every write surface — scan, rule run, ack, retention action, cleanup.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from constat_api.auth import (
    ANONYMOUS_PRINCIPAL,
    Principal,
    _get_settings,
    require_operator,
    verify_api_key,
)
from constat_api.main import app
from constat_api.settings import ApiKeyEntry, Settings, parse_api_keys
from fastapi import HTTPException
from fastapi.testclient import TestClient

ALICE_KEY = "alice-operator-key"
BOB_KEY = "bob-reader-key"


@pytest.fixture
def rbac_settings(client: TestClient):
    """Configure two named principals (alice=operator, bob=reader)."""
    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key=ALICE_KEY),
            ApiKeyEntry(name="bob", role="reader", key=BOB_KEY),
        )
    )
    app.dependency_overrides[_get_settings] = lambda: cfg
    yield cfg
    app.dependency_overrides.pop(_get_settings, None)


@pytest.fixture
def legacy_settings(client: TestClient):
    """Legacy single-key config: CONSTAT_API_KEY only."""
    cfg = Settings(api_key="legacy-single-key")
    app.dependency_overrides[_get_settings] = lambda: cfg
    yield cfg
    app.dependency_overrides.pop(_get_settings, None)


# ---------------------------------------------------------------------------
# Parsing (settings.parse_api_keys)
# ---------------------------------------------------------------------------


def test_parse_api_keys_happy_path() -> None:
    entries = parse_api_keys("alice:operator:K1,bob:reader:K2")
    assert entries == (
        ApiKeyEntry(name="alice", role="operator", key="K1"),
        ApiKeyEntry(name="bob", role="reader", key="K2"),
    )


def test_parse_api_keys_empty_string_is_empty() -> None:
    assert parse_api_keys("") == ()


def test_parse_api_keys_rejects_malformed_entry() -> None:
    with pytest.raises(ValueError, match="expected 'name:role:key'"):
        parse_api_keys("alice:operator")


def test_parse_api_keys_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown role 'admin'"):
        parse_api_keys("alice:admin:K1")


def test_parse_api_keys_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="empty key"):
        parse_api_keys("alice:operator:")


def test_parse_api_keys_error_never_leaks_key_material() -> None:
    """The startup error names the entry but never the secret."""
    with pytest.raises(ValueError) as exc_info:
        parse_api_keys("alice:wrongrole:super-secret-key-material")
    assert "super-secret-key-material" not in str(exc_info.value)
    assert "alice" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Principal resolution (unit, no HTTP)
# ---------------------------------------------------------------------------


def test_verify_api_key_returns_named_principal() -> None:
    cfg = Settings(api_keys=(ApiKeyEntry(name="bob", role="reader", key="K2"),))
    principal = verify_api_key(x_api_key="K2", cfg=cfg)
    assert principal == Principal(name="bob", role="reader")


def test_verify_api_key_open_returns_anonymous_operator() -> None:
    cfg = Settings()
    assert verify_api_key(x_api_key=None, cfg=cfg) == ANONYMOUS_PRINCIPAL
    assert ANONYMOUS_PRINCIPAL.role == "operator"


def test_legacy_key_maps_to_default_operator() -> None:
    cfg = Settings(api_key="legacy")
    principal = verify_api_key(x_api_key="legacy", cfg=cfg)
    assert principal == Principal(name="default", role="operator")


def test_require_operator_rejects_reader() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_operator(principal=Principal(name="bob", role="reader"))
    assert exc_info.value.status_code == 403


def test_require_operator_accepts_operator() -> None:
    principal = Principal(name="alice", role="operator")
    assert require_operator(principal=principal) is principal


# ---------------------------------------------------------------------------
# HTTP: readers read, readers can't write, operators can
# ---------------------------------------------------------------------------


def test_reader_can_get_insights(rbac_settings: Settings, client: TestClient) -> None:
    response = client.get("/insights", headers={"X-API-Key": BOB_KEY})
    assert response.status_code == 200


def test_reader_gets_403_on_every_write_surface(
    rbac_settings: Settings, client: TestClient
) -> None:
    """One sweep over every write endpoint class: a reader must not be
    able to trigger a scan, a run, an ack, or a retention action."""
    headers = {"X-API-Key": BOB_KEY}
    write_calls = [
        ("post", "/insights/run", {"json": {"rule": "rds_eol"}}),
        ("post", "/collect/aws", {"json": {"targets": [{"aws_account_id": "1"}]}}),
        ("post", "/collect/aws/cleanup-stuck-runs", {}),
        ("post", "/collect/focus", {"json": {"account_external_id": "1", "file_path": "x"}}),
        ("post", "/insights", {"json": {}}),
        ("patch", f"/insights/{uuid4()}", {"json": {"ack_status": "acknowledged"}}),
        ("post", "/inconclusives", {"json": {}}),
        ("post", "/admin/cleanup-inconclusives", {}),
        ("post", "/compliance/retention/run", {}),
    ]
    for method, url, kwargs in write_calls:
        response = getattr(client, method)(url, headers=headers, **kwargs)
        assert response.status_code == 403, f"{method.upper()} {url}: {response.status_code}"


def test_reader_cannot_read_audit_events(rbac_settings: Settings, client: TestClient) -> None:
    """The 'who saw my data' log is operator-only — readers must not
    enumerate other principals' reads."""
    response = client.get("/compliance/audit-events", headers={"X-API-Key": BOB_KEY})
    assert response.status_code == 403


def test_operator_can_read_audit_events(rbac_settings: Settings, client: TestClient) -> None:
    response = client.get("/compliance/audit-events", headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 200


def test_operator_write_is_allowed(rbac_settings: Settings, client: TestClient) -> None:
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        headers={"X-API-Key": ALICE_KEY},
    )
    assert response.status_code == 200


def test_unknown_key_is_401(rbac_settings: Settings, client: TestClient) -> None:
    response = client.get("/insights", headers={"X-API-Key": "not-a-key"})
    assert response.status_code == 401


def test_legacy_key_is_operator(legacy_settings: Settings, client: TestClient) -> None:
    """Backward compat: an existing single-key deployment keeps full access."""
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        headers={"X-API-Key": "legacy-single-key"},
    )
    assert response.status_code == 200


def test_no_keys_configured_auth_is_open(client: TestClient) -> None:
    """Dev mode: no CONSTAT_API_KEY(S) -> everything allowed, no header."""
    response = client.post("/insights/run", json={"rule": "rds_eol"})
    assert response.status_code == 200
