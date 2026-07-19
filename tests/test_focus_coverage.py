"""Tests for FOCUS coverage detection (GET /focus/coverage).

Covers compute_focus_coverage semantics (gap detection, staleness with an
injected `today`) and the HTTP endpoint shape. See known-issues.md §4.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from constat_api.orm import AccountORM, FocusChargeORM
from constat_api.repositories.focus_coverage import STALE_AFTER_DAYS, compute_focus_coverage
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def _account(session: Session, external_id: str = "111111111111") -> AccountORM:
    account = AccountORM(external_id=external_id, name="prod")
    session.add(account)
    session.flush()
    return account


def _charge(
    session: Session,
    account_id: UUID,
    start: date,
    end: date,
    service: str = "AmazonRDS",
) -> None:
    session.add(
        FocusChargeORM(
            account_id=account_id,
            period_start=start,
            period_end=end,
            service=service,
            billed_cost=Decimal("100.00"),
            amortized_cost=Decimal("100.00"),
            charge_count=1,
        )
    )
    session.flush()


def test_gap_in_the_middle_is_detected(session: Session) -> None:
    """Two months present, one missing in between -> missing_months flags it."""
    account = _account(session)
    _charge(session, account.id, date(2026, 1, 1), date(2026, 1, 31))
    _charge(session, account.id, date(2026, 3, 1), date(2026, 3, 31))

    coverage = compute_focus_coverage(session, today=date(2026, 4, 10))

    assert len(coverage) == 1
    cov = coverage[0]
    assert cov.account_id == account.id
    assert cov.covered_months == 2
    assert cov.missing_months == ["2026-02"]
    assert cov.first_period == date(2026, 1, 1)
    assert cov.last_period == date(2026, 3, 31)
    assert cov.periods == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 3, 1), date(2026, 3, 31)),
    ]
    assert cov.stale is False  # last period_end is 10 days before `today`


def test_no_gap_fresh_data_is_clean(session: Session) -> None:
    account = _account(session)
    _charge(session, account.id, date(2026, 5, 1), date(2026, 5, 31))
    _charge(session, account.id, date(2026, 6, 1), date(2026, 6, 30))

    coverage = compute_focus_coverage(session, today=date(2026, 7, 10))

    assert len(coverage) == 1
    cov = coverage[0]
    assert cov.missing_months == []
    assert cov.covered_months == 2
    assert cov.stale is False


def test_stale_when_latest_period_older_than_45_days(session: Session) -> None:
    account = _account(session)
    # period_end 60 days before `today` -> stale.
    _charge(session, account.id, date(2026, 4, 1), date(2026, 4, 30))

    coverage = compute_focus_coverage(session, today=date(2026, 6, 29))

    assert (date(2026, 6, 29) - date(2026, 4, 30)).days == 60
    assert STALE_AFTER_DAYS < 60
    assert coverage[0].stale is True
    assert coverage[0].missing_months == []


def test_stale_boundary_is_not_stale_at_exactly_45_days(session: Session) -> None:
    account = _account(session)
    _charge(session, account.id, date(2026, 5, 1), date(2026, 5, 31))

    today = date(2026, 5, 31) + timedelta(days=STALE_AFTER_DAYS)
    coverage = compute_focus_coverage(session, today=today)
    assert coverage[0].stale is False


def test_no_focus_data_returns_empty(session: Session) -> None:
    assert compute_focus_coverage(session) == []


def test_multiple_accounts_reported_independently(session: Session) -> None:
    a = _account(session, "111111111111")
    b = _account(session, "222222222222")
    _charge(session, a.id, date(2026, 1, 1), date(2026, 1, 31))
    _charge(session, a.id, date(2026, 3, 1), date(2026, 3, 31))
    _charge(session, b.id, date(2026, 2, 1), date(2026, 2, 28))

    coverage = compute_focus_coverage(session, today=date(2026, 3, 15))
    by_account = {str(c.account_id): c for c in coverage}

    assert by_account[str(a.id)].missing_months == ["2026-02"]
    assert by_account[str(b.id)].missing_months == []


def test_coverage_endpoint_empty_db(client: TestClient) -> None:
    response = client.get("/focus/coverage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"accounts": [], "has_gaps": False, "has_stale": False}


def test_coverage_endpoint_shape_and_gaps(client: TestClient, session: Session) -> None:
    account = _account(session)
    _charge(session, account.id, date(2026, 1, 1), date(2026, 1, 31))
    _charge(session, account.id, date(2026, 3, 1), date(2026, 3, 31))

    response = client.get("/focus/coverage")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["has_gaps"] is True
    assert body["has_stale"] is True  # 2026-03 is far older than 45 days at test time
    assert len(body["accounts"]) == 1
    entry = body["accounts"][0]
    assert entry["account_id"] == str(account.id)
    assert entry["missing_months"] == ["2026-02"]
    assert entry["covered_months"] == 2
    assert entry["stale"] is True
    assert entry["first_period"] == "2026-01-01"
    assert entry["last_period"] == "2026-03-31"
    assert entry["periods"] == [["2026-01-01", "2026-01-31"], ["2026-03-01", "2026-03-31"]]
