"""FOCUS 1.0 CSV loader.

We use stdlib only (csv + dataclasses). FOCUS exports are large but chunking is
the caller's problem — this module just streams rows.

Reference: https://focus.finops.org/focus-specification/v1-0/
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

# FOCUS 1.0 columns we actually require in V1. The full spec has 43+; we only
# fail-loud on what the chargeback insight and cost-to-resource attribution need.
# A column missing from the source file means the export is not FOCUS 1.0 conformant.
FOCUS_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "BillingAccountId",
        "BillingAccountName",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "EffectiveCost",  # FOCUS 1.0: the amortized cost (AmortizedCost was renamed in 1.0)
        "PricingCategory",
        "Region",
        "ResourceId",  # FOCUS 1.0: for cost-to-resource attribution
        "SubAccountId",  # FOCUS 1.0: AWS Organizations account ID
    }
)


@dataclass(frozen=True)
class FocusCharge:
    """One FOCUS 1.0 charge row, normalized.

    Field naming uses our mental model:
    - billed_cost    ← FOCUS BilledCost (what you pay)
    - amortized_cost ← FOCUS EffectiveCost (amortized over the period)
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
    resource_id: str | None
    sub_account_id: str | None


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


def _opt_str(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def _row_to_charge(row: dict[str, str]) -> FocusCharge:
    return FocusCharge(
        account_id=row["BillingAccountId"].strip(),
        account_name=row.get("BillingAccountName", "").strip(),
        service=row["ServiceName"].strip(),
        region=_opt_str(row.get("Region")),
        pricing_category=_opt_str(row.get("PricingCategory")),
        period_start=_parse_date(row["ChargePeriodStart"]),
        period_end=_parse_date(row["ChargePeriodEnd"]),
        billed_cost=_parse_decimal(row.get("BilledCost")),
        amortized_cost=_parse_decimal(row.get("EffectiveCost")),
        resource_id=_opt_str(row.get("ResourceId")),
        sub_account_id=_opt_str(row.get("SubAccountId")),
    )


def load_focus_csv(path: str | Path) -> Iterator[FocusCharge]:
    """Stream FOCUS 1.0 charges from a CSV file.

    Validates required columns up front. Bad rows are logged and skipped, not
    fatal — FOCUS exports in the wild contain occasional garbage.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = FOCUS_REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"FOCUS 1.0 file missing required columns: {sorted(missing)}")

        for line_no, row in enumerate(reader, start=2):  # header is line 1
            try:
                yield _row_to_charge(row)
            except Exception as exc:
                logger.warning("FOCUS: skipping malformed row at line %d: %s", line_no, exc)
                continue
