"""FOCUS 1.0 loaders.

Two formats supported in V1:
- CSV: stdlib `csv` (no extra deps).
- Parquet: `pyarrow` (>= 23.0).

The `load_focus()` dispatcher picks the right loader by file extension
(`.csv` vs `.parquet`).

Reference: https://focus.finops.org/focus-specification/v1-0/
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

# FOCUS 1.0 columns we actually require in V1. The full spec has 43+; we only
# fail-loud on what the chargeback insight and cost-to-resource attribution need.
# A column missing from the source file means the export is not FOCUS 1.0 conformant.
#
# Region is NOT in this set on purpose: FOCUS 1.0 renamed `Region` to
# `RegionId` (spec §2.32/2.33, https://focus.finops.org/focus-specification/v1-0/).
# Requiring `Region` would reject every spec-conformant export. The region
# check is done separately in _validate_columns (either name accepted).
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
        "ResourceId",  # FOCUS 1.0: for cost-to-resource attribution
        "SubAccountId",  # FOCUS 1.0: AWS Organizations account ID
    }
)

# Accepted region columns, in preference order: the FOCUS 1.0 name first,
# the pre-1.0 name second (older exports keep loading).
FOCUS_REGION_COLUMNS: tuple[str, ...] = ("RegionId", "Region")

# Optional FOCUS 1.0 columns. Missing -> empty/default. Present -> parsed.
# `Tags` is the JSON-encoded map<string,string> for resource tags.
FOCUS_OPTIONAL_COLUMNS: frozenset[str] = frozenset({"Tags"})

# FOCUS 1.0 represents absence with a single space (" ") in some exports.
# pyarrow reads it back as " " for string columns. We treat it as missing.
FOCUS_NULL_SENTINEL = " "


@dataclass(frozen=True)
class FocusCharge:
    """One FOCUS 1.0 charge row, normalized.

    Field naming uses our mental model:
    - billed_cost    ← FOCUS BilledCost (what you pay)
    - amortized_cost ← FOCUS EffectiveCost (amortized over the period)
    - tags           ← FOCUS Tags (list of unique tag dicts seen)

    `tags` is always a list. A raw FOCUS row carries exactly one tag dict, so
    the loader wraps it in a single-element list. After aggregation by
    (service, period), the list contains all unique tag dicts across input
    rows — heterogeneous tag values are preserved, not collapsed to a mode.
    The chargeback runner iterates this list to re-aggregate by any tag key
    (Application, CostCenter, ...).
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
    tags: list[dict[str, str]]


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
    if s == "" or s == FOCUS_NULL_SENTINEL:
        return None
    return s


def _parse_tags(raw: str | None) -> dict[str, str]:
    """FOCUS Tags column: JSON-encoded map<string,string>. Empty/None -> {}.

    Spec edge cases we handle defensively:
    - The FOCUS NULL sentinel " " (a single space) is treated as empty.
    - A non-JSON value is logged and treated as empty (don't fail the whole
      load because one row had a typo in the Tags column).
    """
    if raw is None:
        return {}
    raw = raw.strip()
    if raw == "" or raw == FOCUS_NULL_SENTINEL:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("FOCUS: invalid Tags JSON %r, defaulting to {}: %s", raw, exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("FOCUS: Tags must be a JSON object, got %r", type(parsed).__name__)
        return {}
    # Coerce values to str (FOCUS spec says string, but we don't trust real-world data).
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def _row_to_charge(row: dict[str, str | None]) -> FocusCharge:
    """Build a FocusCharge from a dict row (CSV) or from a pyarrow Row mapping."""
    raw_tags = _parse_tags(row.get("Tags"))
    return FocusCharge(
        account_id=str(row.get("BillingAccountId", "")).strip(),
        account_name=str(row.get("BillingAccountName", "")).strip(),
        service=str(row.get("ServiceName", "")).strip(),
        region=_opt_str(row.get("RegionId")) or _opt_str(row.get("Region")),
        pricing_category=_opt_str(row.get("PricingCategory")),
        period_start=_parse_date(str(row["ChargePeriodStart"])),
        period_end=_parse_date(str(row["ChargePeriodEnd"])),
        billed_cost=_parse_decimal(row.get("BilledCost")),
        amortized_cost=_parse_decimal(row.get("EffectiveCost")),
        resource_id=_opt_str(row.get("ResourceId")),
        sub_account_id=_opt_str(row.get("SubAccountId")),
        tags=[raw_tags] if raw_tags else [],
    )


def _validate_columns(fieldnames: list[str] | None, *, source: str) -> None:
    if fieldnames is None:
        raise ValueError(f"FOCUS {source} has no header / fieldnames")
    missing = FOCUS_REQUIRED_COLUMNS - set(fieldnames)
    if missing:
        raise ValueError(f"FOCUS 1.0 {source} missing required columns: {sorted(missing)}")
    if not any(c in fieldnames for c in FOCUS_REGION_COLUMNS):
        raise ValueError(
            f"FOCUS 1.0 {source} missing required columns: "
            f"['RegionId'] (pre-1.0 'Region' also accepted)"
        )


def load_focus_csv(
    path: str | Path,
    *,
    on_skip: Callable[[int, Exception], None] | None = None,
) -> Iterator[FocusCharge]:
    """Stream FOCUS 1.0 charges from a CSV file.

    Validates required columns up front. Bad rows are logged and skipped, not
    fatal — FOCUS exports in the wild contain occasional garbage.

    `on_skip(line_no, exc)` is called for each row that fails to parse. Use it
    to track rows_skipped in the ingest result. The caller is responsible for
    counting rows_total separately (e.g. `sum(1 for _ in open(path)) - 1`).
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        _validate_columns(reader.fieldnames, source="CSV")
        for line_no, row in enumerate(reader, start=2):  # header is line 1
            try:
                yield _row_to_charge(row)
            except Exception as exc:
                logger.warning("FOCUS CSV: skipping malformed row at line %d: %s", line_no, exc)
                if on_skip is not None:
                    on_skip(line_no, exc)
                continue


def load_focus_parquet(
    path: str | Path,
    *,
    on_skip: Callable[[int, Exception], None] | None = None,
) -> Iterator[FocusCharge]:
    """Stream FOCUS 1.0 charges from a Parquet file.

    Reads the table in row groups; pyarrow handles the columnar->row conversion.
    Tags is a JSON-encoded string column in the Parquet file (FOCUS 1.0 spec),
    not a struct column, so we parse it the same way as the CSV loader.

    `on_skip(row_idx, exc)` is called for each row that fails to parse.
    """
    import pyarrow.parquet as pq  # local import: pyarrow is a heavy dep

    path = Path(path)
    table = pq.read_table(path)
    _validate_columns(table.column_names, source="Parquet")

    # to_pylist() does the columnar->row conversion once; avoids per-row overhead.
    for row_idx, raw in enumerate(table.to_pylist()):
        # pyarrow returns None for missing columns; we want "" for required ones
        # to keep _row_to_charge's str() coercion happy. Same shape as csv.DictReader.
        row: dict[str, str | None] = {k: ("" if v is None else str(v)) for k, v in raw.items()}
        try:
            yield _row_to_charge(row)
        except Exception as exc:
            logger.warning("FOCUS Parquet: skipping malformed row at index %d: %s", row_idx, exc)
            if on_skip is not None:
                on_skip(row_idx, exc)
            continue


def load_focus(
    path: str | Path,
    *,
    on_skip: Callable[[int, Exception], None] | None = None,
) -> Iterator[FocusCharge]:
    """Dispatch to CSV or Parquet loader based on file extension.

    V1: extension-based dispatch (.csv, .parquet). Other extensions raise.
    Caller is responsible for `list()`-ing the iterator if it needs a list.

    `on_skip(line_or_index, exc)` is forwarded to the chosen loader.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_focus_csv(path, on_skip=on_skip)
    if suffix == ".parquet":
        return load_focus_parquet(path, on_skip=on_skip)
    raise ValueError(f"Unsupported FOCUS file extension: {suffix!r} (V1 supports .csv, .parquet)")
