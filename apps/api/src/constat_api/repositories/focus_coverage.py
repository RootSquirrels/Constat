"""FOCUS coverage detection — never charge back silently on a partial export.

Detection half of known-issues.md §4: the ingestion path upserts per
(account, service, period), so nothing upstream guarantees that every
month between the first and the last ingested period is actually present,
nor that the data is fresh. `compute_focus_coverage` reports those gaps
per account so the API (and the web banner) can warn instead of showing
a wrong chargeback.

Coverage semantics (deliberately simple):
- A period counts as covering month M when `period_start` falls in M.
  `period_end` is ignored for gap detection — FOCUS billing periods are
  monthly in practice, and spanning periods would only *hide* a gap if
  we considered every month they touch.
- `missing_months` are the YYYY-MM labels absent between the first and
  the last covered month (inclusive). Nothing before the first or after
  the last covered month is reported — we only flag holes *inside* the
  observed range; freshness is the `stale` flag's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import FocusChargeORM

# Staleness window in days. AWS bills monthly and a period's data lands
# within a few days of period close; 45 days covers a full extra month
# plus ~2 weeks of export/ingestion slack before we call the data stale.
STALE_AFTER_DAYS = 45


@dataclass(frozen=True)
class AccountCoverage:
    """FOCUS coverage summary for one account."""

    account_id: UUID
    periods: list[tuple[date, date]] = field(default_factory=list)
    covered_months: int = 0
    missing_months: list[str] = field(default_factory=list)
    stale: bool = False
    first_period: date | None = None  # earliest period_start seen
    last_period: date | None = None  # latest period_end seen


def _month_labels(first: date, last: date) -> list[str]:
    """All YYYY-MM labels from first's month to last's month, inclusive."""
    labels: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        labels.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return labels


def compute_focus_coverage(
    session: Session,
    *,
    today: date | None = None,
) -> list[AccountCoverage]:
    """Per-account FOCUS coverage: periods present, month gaps, staleness.

    `today` is injectable for deterministic tests; defaults to the real
    current date. Returns one AccountCoverage per account that has FOCUS
    data, sorted by account_id; empty list when no FOCUS data exists.
    """
    today = today or date.today()

    rows = session.execute(
        select(
            FocusChargeORM.account_id,
            FocusChargeORM.period_start,
            FocusChargeORM.period_end,
        ).distinct()
    ).all()

    periods_by_account: dict[UUID, set[tuple[date, date]]] = {}
    for account_id, period_start, period_end in rows:
        periods_by_account.setdefault(account_id, set()).add((period_start, period_end))

    coverages: list[AccountCoverage] = []
    for account_id in sorted(periods_by_account, key=str):
        periods = sorted(periods_by_account[account_id])
        covered = {f"{start.year:04d}-{start.month:02d}" for start, _ in periods}
        first_start = periods[0][0]
        last_end = max(end for _, end in periods)
        expected = _month_labels(first_start, periods[-1][0])
        missing = [label for label in expected if label not in covered]
        coverages.append(
            AccountCoverage(
                account_id=account_id,
                periods=periods,
                covered_months=len(covered),
                missing_months=missing,
                stale=(today - last_end).days > STALE_AFTER_DAYS,
                first_period=first_start,
                last_period=last_end,
            )
        )
    return coverages
