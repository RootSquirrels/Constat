"""Tests for the inconclusive cleanup path (UX/ops P2 item 8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from constat_api.orm import InconclusiveORM
from constat_api.repositories import inconclusive as inconclusive_repo
from sqlalchemy.orm import Session


def _insert_inconclusive(
    session: Session, *, age_days: int, reason: str = "missing_facts"
) -> InconclusiveORM:
    orm = InconclusiveORM(
        id=uuid4(),
        rule_name="rds_eol",
        missing_facts=["aws.rds.engine"],
        reason=reason,
        computed_at=datetime.now(tz=UTC) - timedelta(days=age_days),
    )
    session.add(orm)
    session.flush()
    return orm


def test_delete_older_than_removes_only_old_records(session: Session) -> None:
    """Records older than the cutoff are deleted; fresher ones survive."""
    old_1_id = _insert_inconclusive(session, age_days=45).id
    old_2_id = _insert_inconclusive(session, age_days=60).id
    fresh_id = _insert_inconclusive(session, age_days=5).id
    session.commit()

    deleted = inconclusive_repo.delete_older_than(session, older_than_days=30)
    session.commit()

    assert deleted == 2
    surviving = session.query(InconclusiveORM).all()
    surviving_ids = {r.id for r in surviving}
    assert fresh_id in surviving_ids
    assert old_1_id not in surviving_ids
    assert old_2_id not in surviving_ids


def test_delete_older_than_deletes_all_when_threshold_is_zero(session: Session) -> None:
    """older_than_days=0 means 'older than now', which is everything in the past.
    Useful for an explicit 'delete everything' (rare, but exists for ops)."""
    _insert_inconclusive(session, age_days=1)
    _insert_inconclusive(session, age_days=10)
    session.commit()

    deleted = inconclusive_repo.delete_older_than(session, older_than_days=0)
    session.commit()

    assert deleted == 2
    assert session.query(InconclusiveORM).count() == 0


def test_delete_older_than_rejects_negative(session: Session) -> None:
    """Negative age is meaningless and rejected (rather than deleting everything)."""
    with pytest.raises(ValueError, match="older_than_days must be >= 0"):
        inconclusive_repo.delete_older_than(session, older_than_days=-1)
