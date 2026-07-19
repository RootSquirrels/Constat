"""Tests for tenant resolution from the authenticated identity (roadmap 3.1).

Contract under test:

- CONSTAT_API_KEYS entries extend to `name:role:key[:tenant_uuid[:kind]]`.
  Missing tenant -> V1 default tenant; missing kind -> `machine`.
  Invalid tenant UUID / unknown kind -> startup ValueError (loud fail,
  same style as the existing role validation).
- `Principal` carries `tenant_id` and `kind`; `audit_actor` renders
  `kind:name` for audit_events.
- `get_db` binds the principal's tenant to the session (identity ->
  session -> RLS). Anonymous / open-route callers keep the default
  tenant — /health must never start 401-ing.
- A client may NEVER choose its tenant: any request carrying an
  `X-Tenant-ID` header gets a 400, whatever the route or role.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from constat_api import db as db_module
from constat_api.auth import (
    ANONYMOUS_PRINCIPAL,
    Principal,
    _get_settings,
    optional_principal,
    verify_api_key,
)
from constat_api.main import app
from constat_api.settings import (
    DEFAULT_TENANT_ID,
    ApiKeyEntry,
    Settings,
    parse_api_keys,
)
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

KEY_A = "tenant-a-operator-key"
KEY_B = "tenant-b-reader-key"


@pytest.fixture
def two_tenant_settings(client: TestClient) -> Iterator[Settings]:
    """Two named keys on two different tenants (A=operator, B=reader)."""
    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key=KEY_A, tenant_id=TENANT_A),
            ApiKeyEntry(name="bob", role="reader", key=KEY_B, tenant_id=TENANT_B),
        )
    )
    app.dependency_overrides[_get_settings] = lambda: cfg
    yield cfg
    app.dependency_overrides.pop(_get_settings, None)


# ---------------------------------------------------------------------------
# Parsing: name:role:key[:tenant_uuid[:kind]]
# ---------------------------------------------------------------------------


def test_parse_extended_entry_with_tenant_and_kind() -> None:
    entries = parse_api_keys(f"alice:operator:K1:{TENANT_A}:human")
    assert entries == (
        ApiKeyEntry(name="alice", role="operator", key="K1", tenant_id=TENANT_A, kind="human"),
    )


def test_parse_tenant_without_kind_defaults_to_machine() -> None:
    (entry,) = parse_api_keys(f"alice:operator:K1:{TENANT_A}")
    assert entry.tenant_id == TENANT_A
    assert entry.kind == "machine"


def test_parse_legacy_three_fields_defaults_to_default_tenant() -> None:
    """Backward compat: existing `name:role:key` configs keep working and
    land on the V1 default tenant as machines."""
    (entry,) = parse_api_keys("alice:operator:K1")
    assert entry.tenant_id == DEFAULT_TENANT_ID
    assert entry.kind == "machine"


def test_parse_rejects_invalid_tenant_uuid() -> None:
    with pytest.raises(ValueError, match="invalid tenant UUID"):
        parse_api_keys("alice:operator:K1:not-a-uuid")


def test_parse_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown kind 'robot'"):
        parse_api_keys(f"alice:operator:K1:{TENANT_A}:robot")


def test_parse_rejects_too_many_fields() -> None:
    with pytest.raises(ValueError, match=r"optional trailing ':tenant_uuid\[:kind\]'"):
        parse_api_keys(f"alice:operator:K1:{TENANT_A}:machine:extra")


def test_parse_colon_key_with_trailing_field_fails_loudly() -> None:
    """Pinned trade-off: the key may contain ':' only in the 3-field form.
    `alice:operator:K:1` looks like a colon-key but is parsed as
    tenant='1' — invalid UUID, loud startup failure instead of silently
    mis-binding a tenant."""
    with pytest.raises(ValueError, match="invalid tenant UUID"):
        parse_api_keys("alice:operator:K:1")


def test_parse_tenant_error_never_leaks_key_material() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_api_keys("alice:operator:super-secret-key-material:not-a-uuid")
    assert "super-secret-key-material" not in str(exc_info.value)
    assert "alice" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Principal: tenant_id + kind resolution
# ---------------------------------------------------------------------------


def test_verify_api_key_resolves_tenant_and_kind() -> None:
    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key="K1", tenant_id=TENANT_A, kind="human"),
        )
    )
    principal = verify_api_key(x_api_key="K1", cfg=cfg)
    assert principal == Principal(name="alice", role="operator", tenant_id=TENANT_A, kind="human")


def test_legacy_key_maps_to_default_tenant_machine() -> None:
    cfg = Settings(api_key="legacy")
    principal = verify_api_key(x_api_key="legacy", cfg=cfg)
    assert principal == Principal(
        name="default", role="operator", tenant_id=DEFAULT_TENANT_ID, kind="machine"
    )


def test_anonymous_principal_is_default_tenant_machine() -> None:
    assert ANONYMOUS_PRINCIPAL.tenant_id == DEFAULT_TENANT_ID
    assert ANONYMOUS_PRINCIPAL.kind == "machine"


def test_audit_actor_is_kind_colon_name() -> None:
    assert Principal(name="deploy-bot", role="operator").audit_actor == "machine:deploy-bot"
    assert Principal(name="alice", role="reader", kind="human").audit_actor == "human:alice"
    assert ANONYMOUS_PRINCIPAL.audit_actor == "machine:anonymous"


# ---------------------------------------------------------------------------
# optional_principal: never 401s, only picks the tenant
# ---------------------------------------------------------------------------


def test_optional_principal_resolves_valid_key() -> None:
    cfg = Settings(
        api_keys=(ApiKeyEntry(name="alice", role="operator", key="K1", tenant_id=TENANT_A),)
    )
    assert optional_principal(x_api_key="K1", cfg=cfg) == Principal(
        name="alice", role="operator", tenant_id=TENANT_A
    )


def test_optional_principal_missing_key_returns_anonymous() -> None:
    """A missing key must NOT 401 here — /health depends on get_db and
    must stay open. Protected routes enforce verify_api_key themselves."""
    cfg = Settings(api_keys=(ApiKeyEntry(name="alice", role="operator", key="K1"),))
    assert optional_principal(x_api_key=None, cfg=cfg) == ANONYMOUS_PRINCIPAL


def test_optional_principal_unknown_key_returns_anonymous() -> None:
    cfg = Settings(api_keys=(ApiKeyEntry(name="alice", role="operator", key="K1"),))
    assert optional_principal(x_api_key="wrong", cfg=cfg) == ANONYMOUS_PRINCIPAL


def test_optional_principal_no_keys_returns_anonymous() -> None:
    assert optional_principal(x_api_key=None, cfg=Settings()) == ANONYMOUS_PRINCIPAL


# ---------------------------------------------------------------------------
# get_db binds the principal's tenant to the session
# ---------------------------------------------------------------------------


class _StubSession:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}

    def close(self) -> None:
        pass


def _run_get_db(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> _StubSession:
    stub = _StubSession()
    monkeypatch.setattr(db_module, "SessionLocal", lambda: stub)
    gen = db_module.get_db(principal)
    session = next(gen)
    try:
        assert session is stub
        return stub
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_get_db_binds_principals_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal(name="alice", role="operator", tenant_id=TENANT_A)
    stub = _run_get_db(monkeypatch, principal)
    assert stub.info["tenant_id"] == TENANT_A


def test_get_db_binds_another_principals_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal(name="bob", role="reader", tenant_id=TENANT_B)
    stub = _run_get_db(monkeypatch, principal)
    assert stub.info["tenant_id"] == TENANT_B


def test_get_db_binds_default_tenant_for_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _run_get_db(monkeypatch, ANONYMOUS_PRINCIPAL)
    assert stub.info["tenant_id"] == DEFAULT_TENANT_ID


# ---------------------------------------------------------------------------
# Anti-cross-tenant guard: X-Tenant-ID is always a 400
# ---------------------------------------------------------------------------


def test_tenant_header_rejected_when_auth_open(client: TestClient) -> None:
    response = client.get("/insights", headers={"X-Tenant-ID": str(TENANT_A)})
    assert response.status_code == 400
    assert "API key" in response.json()["detail"]


def test_tenant_header_rejected_case_insensitive(client: TestClient) -> None:
    for header in ("X-Tenant-Id", "x-tenant-id", "X-TENANT-ID"):
        response = client.get("/insights", headers={header: str(TENANT_A)})
        assert response.status_code == 400, header


def test_tenant_header_rejected_for_operator(
    two_tenant_settings: Settings, client: TestClient
) -> None:
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        headers={"X-API-Key": KEY_A, "X-Tenant-ID": str(TENANT_B)},
    )
    assert response.status_code == 400


def test_tenant_header_rejected_for_reader(
    two_tenant_settings: Settings, client: TestClient
) -> None:
    """The guard fires before RBAC: a reader with the header gets 400,
    not 403 — the header is never even looked at."""
    response = client.get(
        "/insights",
        headers={"X-API-Key": KEY_B, "X-Tenant-ID": str(TENANT_A)},
    )
    assert response.status_code == 400


def test_tenant_header_rejected_on_open_routes(client: TestClient) -> None:
    """Even /health rejects the header — there is no route where a client
    may name a tenant."""
    response = client.get("/health", headers={"X-Tenant-ID": str(TENANT_A)})
    assert response.status_code == 400


def test_tenant_header_rejection_carries_request_id(client: TestClient) -> None:
    """The guard executes inside RequestIDMiddleware, so the rejection is
    correlated like any other request."""
    response = client.get("/insights", headers={"X-Tenant-ID": str(TENANT_A)})
    assert response.status_code == 400
    assert response.headers.get("X-Request-ID")


# ---------------------------------------------------------------------------
# Regression: /health stays open with keys configured (real get_db)
# ---------------------------------------------------------------------------


def test_health_stays_open_with_keys_configured(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_db resolves the principal leniently: without this, adding auth
    resolution inside get_db would have made /health 401 (LB probes do
    not carry keys). Uses the real get_db against the sqlite engine."""
    cfg = Settings(api_keys=(ApiKeyEntry(name="alice", role="operator", key="K1"),))
    app.dependency_overrides[_get_settings] = lambda: cfg
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, future=True))
    try:
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code in (200, 503)  # never 401
    finally:
        app.dependency_overrides.pop(_get_settings, None)


def test_health_binds_default_tenant_for_anonymous(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same wiring, seen from the tenant side: an anonymous /health call
    reports the default tenant, exactly as before 3.1."""
    cfg = Settings(api_keys=(ApiKeyEntry(name="alice", role="operator", key="K1"),))
    app.dependency_overrides[_get_settings] = lambda: cfg
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, future=True))
    try:
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.json()["tenant"] == str(DEFAULT_TENANT_ID)
    finally:
        app.dependency_overrides.pop(_get_settings, None)
