"""Tests for the retention runner.

The headline test pins the focus_charges vs observations isolation:
running focus_charges retention must only touch focus_charges rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from constat_api.orm import (
    AccountORM,
    FocusChargeORM,
    ObservationORM,
    RetentionPolicyORM,
)
from constat_api.repositories import accounts as accounts_repo
from constat_api.retention import apply_retention, seed_default_policies
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.orm import Session


def _seed_account(session: Session) -> AccountORM:
    return accounts_repo.get_or_create(session, "111111111111", "test")


def _seed_focus_charges(
    session: Session, account: AccountORM, *, count: int, ingested_at: datetime
) -> list[FocusChargeORM]:
    rows = []
    for _ in range(count):
        rows.append(
            FocusChargeORM(
                tenant_id=DEFAULT_TENANT_ID,
                account_id=account.id,
                period_start=ingested_at.date(),
                period_end=ingested_at.date(),
                service="AmazonRDS",
                region="eu-west-1",
                billed_cost=10.0,
                amortized_cost=10.0,
                charge_count=1,
                ingested_at=ingested_at,
            )
        )
    session.add_all(rows)
    session.commit()
    return rows


def _seed_observations(
    session: Session, *, count: int, ingested_at: datetime
) -> list[ObservationORM]:
    from uuid import uuid4

    rows = []
    for _ in range(count):
        rows.append(
            ObservationORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                resource_id=uuid4(),  # not FK-resolved; we just want the row to exist
                source="aws_rds",
                observed_at=ingested_at,
                payload={"x": 1},
                ingested_at=ingested_at,
            )
        )
    session.add_all(rows)
    session.commit()
    return rows


# ---------------------------------------------------------------------------
# The headline regression: focus_charges retention must NOT touch observations
# ---------------------------------------------------------------------------


def test_focus_charges_retention_does_not_delete_observations(session: Session) -> None:
    """Before the fix, focus_charges retention deleted from observations
    (wrong table). This test catches any regression of that bug.

    Setup: 3 focus_charges + 3 observations, all old enough to be deleted.
    Action: apply focus_charges retention with retention_days=0.
    Expected: focus_charges rows gone, observations rows intact.
    """
    acc = _seed_account(session)
    old = datetime.now(tz=UTC) - timedelta(days=400)
    _seed_focus_charges(session, acc, count=3, ingested_at=old)
    _seed_observations(session, count=3, ingested_at=old)

    # Sanity: both tables have rows
    assert session.query(FocusChargeORM).count() == 3
    assert session.query(ObservationORM).count() == 3

    deleted = apply_retention(session, table_name="focus_charges", retention_days=0)
    session.commit()

    # The bug would have returned 3 (it deleted the observations)
    # and left focus_charges intact. The fix deletes the focus_charges.
    assert deleted == 3, f"expected 3 focus_charges deleted, got {deleted}"
    assert session.query(FocusChargeORM).count() == 0
    assert session.query(ObservationORM).count() == 3, (
        "observations must not be touched by focus_charges retention"
    )


def test_observations_retention_does_not_delete_focus_charges(session: Session) -> None:
    """Symmetric guard: observations retention must not touch focus_charges."""
    acc = _seed_account(session)
    old = datetime.now(tz=UTC) - timedelta(days=400)
    _seed_focus_charges(session, acc, count=2, ingested_at=old)
    _seed_observations(session, count=2, ingested_at=old)

    deleted = apply_retention(session, table_name="observations", retention_days=0)
    session.commit()

    assert deleted == 2
    assert session.query(ObservationORM).count() == 0
    assert session.query(FocusChargeORM).count() == 2


def test_focus_charges_retention_keeps_recent_rows(session: Session) -> None:
    """Sanity: the cutoff is honored. Recent rows survive."""
    acc = _seed_account(session)
    old = datetime.now(tz=UTC) - timedelta(days=400)
    recent = datetime.now(tz=UTC) - timedelta(days=10)
    _seed_focus_charges(session, acc, count=2, ingested_at=old)
    _seed_focus_charges(session, acc, count=3, ingested_at=recent)

    deleted = apply_retention(session, table_name="focus_charges", retention_days=90)
    session.commit()

    assert deleted == 2
    assert session.query(FocusChargeORM).count() == 3


# ---------------------------------------------------------------------------
# Whitelist: unknown / disallowed table names raise, never silently wipe
# ---------------------------------------------------------------------------


def test_retention_rejects_unknown_table(session: Session) -> None:
    """A typo in a policy row must NOT wipe a wrong table. The whitelist
    is the defense-in-depth that turns a typo into a loud error."""
    import pytest

    with pytest.raises(ValueError, match="unknown or disallowed table"):
        apply_retention(session, table_name="users", retention_days=90)


def test_retention_rejects_negative_days(session: Session) -> None:
    """Negative retention would mean 'delete rows from the future'.
    A typo here is a deletion bomb. Refuse loudly."""
    import pytest

    with pytest.raises(ValueError, match="retention_days must be >= 0"):
        apply_retention(session, table_name="observations", retention_days=-1)


# ---------------------------------------------------------------------------
# Seed: idempotent + covers the full set
# ---------------------------------------------------------------------------


def test_seed_default_policies_is_idempotent(session: Session) -> None:
    """Seeding twice inserts zero rows on the second pass. Operators
    re-running the boot hook (or running it manually) must not
    duplicate policies."""
    n1 = seed_default_policies(session)
    n2 = seed_default_policies(session)
    # audit_events is deliberately excluded: migration 0014 makes it
    # immutable (UPDATE/DELETE/TRUNCATE triggers) — its retention is
    # archival, never deletion.
    assert n1 == len(
        {
            "observations",
            "focus_charges",
            "insights",
            "inconclusive",
            "source_runs",
        }
    )
    assert n2 == 0
    # Total policy count: 5 (one per table).
    n_policies = session.query(RetentionPolicyORM).count()
    assert n_policies == 5
