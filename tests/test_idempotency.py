"""Tests for the Idempotency-Key support on write endpoints.

Strategy: a unit test for the cache (the IdempotencyCache class) plus
end-to-end tests through /collect/aws and /insights/run that verify:
- Same key within TTL returns the cached response.
- Different keys produce fresh runs.
- The body is ignored on replay (same key = same response).
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from constat_api import idempotency
from constat_api.idempotency import (
    IdempotencyCache,
    cache_response,
    get_cached_or_none,
    idempotency_cache,
)
from constat_api.main import app
from constat_api.orm import InsightRunORM
from constat_api.settings import DEFAULT_TENANT_ID
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import drain_inline_queue


@pytest.fixture(autouse=True)
def _clear_idempotency_cache():
    """Reset the module-level cache between tests to prevent bleed."""
    idempotency_cache.clear()
    yield
    idempotency_cache.clear()


# ---------------------------------------------------------------------------
# Unit tests for IdempotencyCache
# ---------------------------------------------------------------------------


def test_cache_set_and_get():
    cache = IdempotencyCache()
    cache.put("k1", "body-1")
    assert cache.get("k1") == "body-1"
    assert cache.size() == 1


def test_cache_miss_returns_none():
    cache = IdempotencyCache()
    assert cache.get("nope") is None


def test_cache_lazy_expiry():
    """Lazy expiry: a `get` on an expired key returns None and drops it."""
    cache = IdempotencyCache(ttl=timedelta(milliseconds=1))
    cache.put("k1", "body")
    time.sleep(0.05)
    assert cache.get("k1") is None
    assert cache.size() == 0


def test_cache_evicts_oldest_when_full():
    """FIFO eviction when over capacity."""
    cache = IdempotencyCache(max_entries=3)
    cache.put("a", "1")
    cache.put("b", "2")
    cache.put("c", "3")
    cache.put("d", "4")  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") == "2"
    assert cache.size() == 3


def test_make_cache_key_namespaces():
    assert idempotency.make_cache_key("collect_aws", "abc") == "collect_aws:abc"
    assert idempotency.make_cache_key("insights_run", "abc") == "insights_run:abc"


def test_get_cached_or_none_returns_dict():
    cache_response("scope", "key", {"foo": "bar"})
    out = get_cached_or_none("scope", "key")
    assert out == {"foo": "bar"}


def test_get_cached_or_none_handles_missing():
    assert get_cached_or_none("scope", "missing") is None


# ---------------------------------------------------------------------------
# End-to-end: /collect/aws (async: 202 + job id; replay must not re-enqueue)
# ---------------------------------------------------------------------------


def test_collect_aws_same_idempotency_key_returns_cached_response(
    client: TestClient, session
) -> None:
    """Two POSTs with the same Idempotency-Key return identical bodies
    and the second one does NOT create a second job or re-enqueue."""
    from constat_api.orm import CollectJobORM

    body = {
        # Explicit rds-only scope (SRE-2b): the drain below mocks only the
        # RDS scan; the default is now ALL registered jobs.
        "targets": [
            {
                "aws_account_id": "111111111111",
                "regions": ["eu-west-1"],
                "resource_types": ["rds"],
            }
        ],
    }
    # First call: job created, item enqueued
    r1 = client.post(
        "/collect/aws",
        json=body,
        headers={"Idempotency-Key": "k1"},
    )
    assert r1.status_code == 202
    assert r1.json()["items_enqueued"] == 1

    # Second call with same key: cached response, no fresh enqueue
    r2 = client.post(
        "/collect/aws",
        json=body,
        headers={"Idempotency-Key": "k1"},
    )
    assert r2.status_code == 202
    assert r2.json() == r1.json()

    # Exactly one job row and one queued item: the replay did nothing.
    assert session.query(CollectJobORM).count() == 1
    with (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch(
            "constat_api.collectors.aws.collect_db_instances",
            return_value=iter([]),
        ),
    ):
        outcomes = drain_inline_queue(session)
    assert len(outcomes) == 1
    assert outcomes[0].status == "success"


def test_collect_aws_different_idempotency_keys_trigger_fresh_runs(
    client: TestClient, session
) -> None:
    body = {
        "targets": [{"aws_account_id": "111111111111", "regions": ["eu-west-1"]}],
    }
    r1 = client.post("/collect/aws", json=body, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/collect/aws", json=body, headers={"Idempotency-Key": "k2"})
    # Both accepted; distinct jobs, no shared cache entry.
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["job_id"] != r2.json()["job_id"]


def test_collect_aws_no_idempotency_key_does_not_cache(client: TestClient) -> None:
    """Without the header, every request enqueues fresh."""
    body = {
        "targets": [{"aws_account_id": "111111111111", "regions": ["eu-west-1"]}],
    }
    for _ in range(3):
        r = client.post("/collect/aws", json=body)
        assert r.status_code == 202
    # No key = no cache entries
    assert idempotency_cache.size() == 0


# ---------------------------------------------------------------------------
# End-to-end: /insights/run
# ---------------------------------------------------------------------------


def test_insights_run_same_idempotency_key_returns_cached_response(
    client: TestClient, session
) -> None:
    """The insight_runs table gets ONE row even when the caller retries
    with the same key."""
    body = {"rule": "rds_eol"}
    # First call
    r1 = client.post("/insights/run", json=body, headers={"Idempotency-Key": "ir1"})
    assert r1.status_code == 200
    # Second call with same key
    r2 = client.post("/insights/run", json=body, headers={"Idempotency-Key": "ir1"})
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    # Verify only one insight_runs row was written
    runs = (
        session.execute(select(InsightRunORM).where(InsightRunORM.tenant_id == DEFAULT_TENANT_ID))
        .scalars()
        .all()
    )
    assert len(runs) == 1


def test_insights_run_different_idempotency_keys_each_create_runs(
    client: TestClient, session
) -> None:
    """Different keys = fresh runs (each creates a new insight_runs row)."""
    body = {"rule": "rds_eol"}
    client.post("/insights/run", json=body, headers={"Idempotency-Key": "a"})
    client.post("/insights/run", json=body, headers={"Idempotency-Key": "b"})

    runs = (
        session.execute(select(InsightRunORM).where(InsightRunORM.tenant_id == DEFAULT_TENANT_ID))
        .scalars()
        .all()
    )
    assert len(runs) == 2


# Silence unused import warning for `app` (used implicitly by client fixture)
_ = app
