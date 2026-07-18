"""Focus charges upsert with dedup by (account, service, period).

V1: portable manual upsert (query + insert/update). Works on sqlite + postgres.
V2 (migration 0009): also writes per-input-row tag data to
`focus_charge_tags`. The cost attribution in the chargeback runner
now uses these per-row rows to split cost proportionally, not evenly.
"""

from __future__ import annotations

from uuid import UUID

from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from constat_api.orm import FocusChargeORM, FocusChargeTagORM
from constat_api.settings import DEFAULT_TENANT_ID


def upsert_aggregated(
    session: Session,
    account_id: UUID,
    aggregated: list[AggregatedFocusCharge],
) -> tuple[int, int]:
    """Insert or update rows. Returns (inserted, updated) counts.

    Natural key: (account_id, service, period_start, period_end).
    V2: per-row tags from `AggregatedFocusCharge.per_row_tag_dicts`
    are written to `focus_charge_tags` after each focus_charges
    upsert. Existing focus_charge_tags rows for the focus_charge
    are deleted first (the source of truth is the new ingest).
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
            _write_per_row_tags(session, existing.id, agg.per_row_tag_dicts)
            updated += 1
        else:
            new_row = FocusChargeORM(
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
            session.add(new_row)
            session.flush()  # get new_row.id
            _write_per_row_tags(session, new_row.id, agg.per_row_tag_dicts)
            inserted += 1

    session.flush()
    return inserted, updated


def _write_per_row_tags(
    session: Session,
    focus_charge_id: int,
    per_row_tag_dicts: list[dict[str, str]],
) -> None:
    """Write per-input-row tag data to focus_charge_tags.

    For each input row's tag dict, write one focus_charge_tags row
    per (key, value) pair. Duplicate (key, value) pairs across input
    rows are preserved — the runner uses the row count to attribute
    cost proportionally. The (focus_charge_id, key, value) UNIQUE
    constraint is intentionally not enforced at the row level: a
    focus_charge representing 5 input rows can have the same
    (key, value) appear multiple times, once per contributing row.
    """
    if not per_row_tag_dicts:
        return

    # Delete any existing tags for this focus_charge (in case of
    # re-ingest of the same period). The source of truth is the
    # current ingest, not a previous one.
    session.execute(
        delete(FocusChargeTagORM).where(FocusChargeTagORM.focus_charge_id == focus_charge_id)
    )

    for tag_dict in per_row_tag_dicts:
        for key, value in tag_dict.items():
            session.add(
                FocusChargeTagORM(
                    tenant_id=DEFAULT_TENANT_ID,
                    focus_charge_id=focus_charge_id,
                    key=key,
                    value=value,
                )
            )
    session.flush()


def count_charges(session: Session, account_id: UUID | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(FocusChargeORM.id))
    if account_id is not None:
        stmt = stmt.where(FocusChargeORM.account_id == account_id)
    return int(session.execute(stmt).scalar_one())
