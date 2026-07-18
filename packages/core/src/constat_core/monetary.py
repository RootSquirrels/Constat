"""Monetary semantics of insights — single source of truth.

Says, for every rule: WHERE the monthly amount lives in the payload,
WHAT the amount is based on (catalog estimate vs FOCUS billing line),
and WHAT it means (an avoidable saving vs an accounting delta).

Consumers:
- apps/api routers/insights.py (`/insights/export.csv`) — direct import;
- apps/web/lib/api.ts (`RULE_MONETARY`) — a TS mirror of this table,
  pinned by tests/test_monetary_extraction.py so the two cannot drift.

Why this exists (client-evaluation committee finding): the extraction
was hardcoded to `extended_support_monthly_usd`, so `ebs_gp2_to_gp3`
savings silently dropped out of the POC restitution, and the rds_eol
refactor had even stopped emitting a monthly amount at all — nobody
noticed because no test tied "rule emits money" to "restitution shows
money". This registry + the completeness test in
tests/test_monetary_extraction.py make that class of bug impossible:
a rule registered in RUNNERS must either declare its monetary key here
or be explicitly listed as non-monetary.

MonetaryKind is the committee's other finding made structural: a FOCUS
amortized-vs-billed drift is an ACCOUNTING_DELTA — real money, but not
money the customer saves by acting. It must NEVER be summed with
AVOIDABLE_SAVING amounts in a total presented as savings. The
restitution reads `kind` to keep the two columns separate.

Decision record: docs/adr/ADR-13-monetary-extraction-registry.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ValueBasis(StrEnum):
    """What the amount is derived from."""

    ESTIMATED = "ESTIMATED"  # catalog-priced (EOL calendars, public rates)
    ACTUAL = "ACTUAL"  # read from FOCUS billing rows


class MonetaryKind(StrEnum):
    """What the amount MEANS — drives how totals may be built."""

    AVOIDABLE_SAVING = "AVOIDABLE_SAVING"  # stops if the customer acts
    ACCOUNTING_DELTA = "ACCOUNTING_DELTA"  # informational; never sum as savings


@dataclass(frozen=True)
class MonetaryExtraction:
    payload_key: str
    value_basis: ValueBasis
    kind: MonetaryKind


# One entry per money-emitting rule. Keys are rule_names as registered
# in apps/api insights runner RUNNERS.
MONETARY: dict[str, MonetaryExtraction] = {
    "rds_eol": MonetaryExtraction(
        payload_key="extended_support_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "mysql_eol": MonetaryExtraction(
        payload_key="extended_support_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "aurora_eol": MonetaryExtraction(
        payload_key="extended_support_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "ebs_gp2_to_gp3": MonetaryExtraction(
        payload_key="savings_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "ebs_unattached": MonetaryExtraction(
        payload_key="monthly_waste_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "snapshot_orphan": MonetaryExtraction(
        payload_key="orphan_snapshot_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "ec2_stopped_with_storage": MonetaryExtraction(
        payload_key="stopped_storage_monthly_usd",
        value_basis=ValueBasis.ESTIMATED,
        kind=MonetaryKind.AVOIDABLE_SAVING,
    ),
    "chargeback": MonetaryExtraction(
        payload_key="drift_amortized_minus_billed_usd",
        value_basis=ValueBasis.ACTUAL,
        kind=MonetaryKind.ACCOUNTING_DELTA,
    ),
}

# Rules that legitimately emit no monetary amount. Empty today: every
# V1 rule prices its finding. A rule present in RUNNERS but in neither
# MONETARY nor NON_MONETARY_RULES fails the completeness test — that
# is the contract, not a convention.
NON_MONETARY_RULES: frozenset[str] = frozenset()


def monthly_cost_and_basis(
    rule_name: str, payload: dict[str, Any]
) -> tuple[float | None, str | None]:
    """Extract (monthly_usd, value_basis) for a rule's insight payload.

    Returns (None, None) for unregistered rules, and (None, basis) when
    the registered key is absent or non-numeric (bool excluded: it IS
    an int in Python, and True must not become $1.00).
    """
    entry = MONETARY.get(rule_name)
    if entry is None:
        return None, None
    raw = payload.get(entry.payload_key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None, entry.value_basis.value
    return float(raw), entry.value_basis.value


def monetary_kind(rule_name: str) -> MonetaryKind | None:
    """The MonetaryKind for a rule, or None if unregistered."""
    entry = MONETARY.get(rule_name)
    return entry.kind if entry else None
