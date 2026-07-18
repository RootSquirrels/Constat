"""Focus charges upsert with dedup by (account, service, period).

V1: portable manual upsert (query + insert/update). Works on sqlite + postgres.
V2: switch to postgres-native INSERT ... ON CONFLICT for large ingestions.
"""

from __future__ import annotations

from uuid import UUID

from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import FocusChargeORM


def upsert_aggregated(
    session: Session,
    account_id: UUID,
    aggregated: list[AggregatedFocusCharge],
) -> tuple[int, int]:
    """Insert or update rows. Returns (inserted, updated) counts.

    Natural key: (account_id, service, period_start, period_end).
    """
    inserted = 0
    updated = 0

    for agg in aggregated:
        existing = session.execute(
            select(FocusChargeORM).where(
                FocusChargeORM.account_id == account_id,
                FocusChargeORM.service == agg.service,
                FocusChargeORM.period_start == agg.period_start,
                FocusChargeORM.period_end == agg.period_end,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.billed_cost = agg.billed_cost
            existing.amortized_cost = agg.amortized_cost
            existing.charge_count = agg.charge_count
            existing.region = agg.region
            existing.pricing_category = agg.pricing_category
            existing.resource_id = agg.resource_id
            existing.sub_account_id = agg.sub_account_id
            existing.tags = list(agg.tags)
            updated += 1
        else:
            session.add(
                FocusChargeORM(
                    account_id=account_id,
                    period_start=agg.period_start,
                    period_end=agg.period_end,
                    service=agg.service,
                    region=agg.region,
                    pricing_category=agg.pricing_category,
                    billed_cost=agg.billed_cost,
                    amortized_cost=agg.amortized_cost,
                    resource_id=agg.resource_id,
                    sub_account_id=agg.sub_account_id,
                    tags=list(agg.tags),
                    charge_count=agg.charge_count,
                )
            )
            inserted += 1

    session.flush()
    return inserted, updated


def count_charges(session: Session, account_id: UUID | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(FocusChargeORM.id))
    if account_id is not None:
        stmt = stmt.where(FocusChargeORM.account_id == account_id)
    return int(session.execute(stmt).scalar_one())
