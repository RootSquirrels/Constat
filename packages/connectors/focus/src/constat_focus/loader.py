"""FOCUS 1.0 CSV loader.

We use stdlib only (csv + dataclasses). FOCUS exports are large but chunking is
the caller's problem — this module just streams rows.

Reference: https://focus.finops.org/focus-specification/
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

# FOCUS 1.0 columns we actually use in V1. The full spec has 60+; we only require
# what the chargeback insight needs. If a column is missing, we raise — better to
# fail loud than to silently drop cost data.
FOCUS_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "BillingAccountId",
        "BillingAccountName",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "AmortizedCost",
        "EffectiveCost",
        "PricingCategory",
        "Region",
    }
)


@dataclass(frozen=True)
class FocusCharge:
    """One FOCUS charge row, normalized for our schema.

    We keep the original column names where the meaning is unambiguous, and
    document the mapping in the loader.
    """

    account_id: str
    account_name: str
    service: str
    region: str | None
    pricing_category: str | None
    period_start: date
    period_end: date
    billed_cost: Decimal
    amortized_cost: Decimal
    effective_cost: Decimal


def _parse_date(s: str) -> date:
    """FOCUS uses ISO 8601: '2026-07-01T00:00:00Z' or '2026-07-01'."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(s[:10])


def _parse_decimal(s: str | None) -> Decimal:
    if s is None or s == "":
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        logger.warning("FOCUS: invalid decimal %r, defaulting to 0", s)
        return Decimal("0")


def _row_to_charge(row: dict[str, str]) -> FocusCharge:
    return FocusCharge(
        account_id=row["BillingAccountId"].strip(),
        account_name=row.get("BillingAccountName", "").strip(),
        service=row["ServiceName"].strip(),
        region=(row.get("Region") or "").strip() or None,
        pricing_category=(row.get("PricingCategory") or "").strip() or None,
        period_start=_parse_date(row["ChargePeriodStart"]),
        period_end=_parse_date(row["ChargePeriodEnd"]),
        billed_cost=_parse_decimal(row.get("BilledCost")),
        amortized_cost=_parse_decimal(row.get("AmortizedCost")),
        effective_cost=_parse_decimal(row.get("EffectiveCost")),
    )


def load_focus_csv(path: str | Path) -> Iterator[FocusCharge]:
    """Stream FOCUS charges from a CSV file.

    Validates required columns up front. Bad rows are logged and skipped, not
    fatal — FOCUS exports in the wild contain occasional garbage.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = FOCUS_REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"FOCUS file missing required columns: {sorted(missing)}")

        for line_no, row in enumerate(reader, start=2):  # header is line 1
            try:
                yield _row_to_charge(row)
            except Exception as exc:
                logger.warning("FOCUS: skipping malformed row at line %d: %s", line_no, exc)
                continue
