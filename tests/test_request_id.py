"""Tests for the request_id middleware (UX/ops P2 item 9)."""

from __future__ import annotations

from constat_api.middleware import REQUEST_ID_HEADER, RequestIDMiddleware
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "true"}

    @app.get("/request-id")
    def echo_request_id(request: Request) -> dict[str, str | None]:
        return {"request_id": getattr(request.state, "request_id", None)}

    return app


def test_request_id_is_generated_when_header_absent() -> None:
    client = TestClient(_build_app())
    response = client.get("/ping")
    assert response.status_code == 200
    # Server should have generated a request_id and echoed it.
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id is not None
    # UUID4 has 36 chars including hyphens.
    assert len(request_id) == 36
    assert request_id.count("-") == 4


def test_request_id_is_preserved_when_header_present() -> None:
    client = TestClient(_build_app())
    supplied = "test-correlation-id-12345"
    response = client.get("/ping", headers={REQUEST_ID_HEADER: supplied})
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == supplied


def test_request_id_is_exposed_in_handler_via_state() -> None:
    client = TestClient(_build_app())
    supplied = "abc-def-1234"
    response = client.get("/request-id", headers={REQUEST_ID_HEADER: supplied})
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == supplied
    assert response.headers[REQUEST_ID_HEADER] == supplied
