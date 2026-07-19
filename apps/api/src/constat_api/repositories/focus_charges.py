"""Focus charges upsert with dedup by (account, service, period).

V1: portable manual upsert (query + insert/update). Works on sqlite + postgres.
V2 (migration 0009): also writes per-input-row tag data to
`focus_charge_tags`. The cost attribution in the chargeback runner
now uses these per-row rows to split cost proportionally, not evenly.
Migration 0020: also writes per-input-row cost to focus_charge_tags
(billed_cost, amortized_cost, input_row_index). The resolver uses
these for cost-weighted tag attribution, not count-weighted.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from constat_api.orm import FocusChargeORM, FocusChargeTagORM
from constat_api.tenant import tenant_or_default


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
    Migration 0020: per-row costs from `AggregatedFocusCharge.per_row_costs`
    are written to the same focus_charge_tags rows, denormalized
    across all (key, value) rows of the same input row. The resolver
    groups by input_row_index to reconstruct per-row (cost, tag) pairs.
    """
    inserted = 0
    updated = 0
    # Stamped once per ingest, not per row: every row of this batch
    # belongs to the session's tenant (RLS WITH CHECK rejects the ORM
    # default under a non-default tenant).
    tenant_id = tenant_or_default(session)
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
            existing.billing_currency = agg.billing_currency
            _write_per_row_tags(
                session,
                existing.id,
                agg.per_row_tag_dicts,
                agg.per_row_costs,
                tenant_id=tenant_id,
            )
            updated += 1
        else:
            new_row = FocusChargeORM(
                tenant_id=tenant_id,
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
                billing_currency=agg.billing_currency,
            )
            session.add(new_row)
            session.flush()  # get new_row.id
            _write_per_row_tags(
                session,
                new_row.id,
                agg.per_row_tag_dicts,
                agg.per_row_costs,
                tenant_id=tenant_id,
            )
            inserted += 1

    session.flush()
    return inserted, updated


def _write_per_row_tags(
    session: Session,
    focus_charge_id: int,
    per_row_tag_dicts: list[dict[str, str]],
    per_row_costs: list[tuple[float, float]] | list[tuple[Decimal, Decimal]] | None = None,
    *,
    tenant_id: UUID,
) -> None:
    """Write per-input-row tag + cost data to focus_charge_tags.

    For each input row, write one focus_charge_tags row per
    (key, value) pair. Duplicate (key, value) pairs across input
    rows are preserved — the runner uses the row count to attribute
    cost proportionally. The (focus_charge_id, key, value) UNIQUE
    constraint is intentionally not enforced at the row level: a
    focus_charge representing 5 input rows can have the same
    (key, value) appear multiple times, once per contributing row.

    Migration 0020: each focus_charge_tags row also carries the
    per-input-row billed_cost and amortized_cost (denormalized
    across all (key, value) rows of the same input row) and the
    input_row_index (so the runner can group them back into
    per-input-row records).
    """
    if not per_row_tag_dicts:
        return

    # Delete any existing tags for this focus_charge (in case of
    # re-ingest of the same period). The source of truth is the
    # current ingest, not a previous one.
    session.execute(
        delete(FocusChargeTagORM).where(FocusChargeTagORM.focus_charge_id == focus_charge_id)
    )

    for row_index, tag_dict in enumerate(per_row_tag_dicts):
        # Migration 0020: per-row cost from the parallel list. If
        # the cost list is missing (old callers / pre-0020 data),
        # billed_cost and amortized_cost default to 0 in the DB and
        # the resolver falls back to row-count weighting for that row.
        if per_row_costs is not None and row_index < len(per_row_costs):
            row_billed, row_amortized = per_row_costs[row_index]
        else:
            row_billed = Decimal("0")
            row_amortized = Decimal("0")
        for key, value in tag_dict.items():
            session.add(
                FocusChargeTagORM(
                    tenant_id=tenant_id,
                    focus_charge_id=focus_charge_id,
                    key=key,
                    value=value,
                    input_row_index=row_index,
                    billed_cost=row_billed,
                    amortized_cost=row_amortized,
                )
            )
    session.flush()


def count_charges(session: Session, account_id: UUID | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(FocusChargeORM.id))
    if account_id is not None:
        stmt = stmt.where(FocusChargeORM.account_id == account_id)
    return int(session.execute(stmt).scalar_one())
