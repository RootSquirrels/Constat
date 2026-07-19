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

ValueBasis in V1: every rule is ESTIMATED. The audit committee
(FinOps re-audit) flagged the prior ESTIMATED -> ACTUAL flip as
unsound: an RDS EOL estimate cannot be "confirmed" by the resource's
total FOCUS cost (the total is the whole DB bill, not the Extended
Support supplement). Same shape for EBS gp2->gp3 (the FOCUS line is
the volume cost, not the migration savings). chargeback is built
FROM FOCUS but its drift amount is still a derived signal
(amortized minus billed, not a FOCUS line itself), so it is also
ESTIMATED. ACTUAL will be reserved for V2 when a per-charge-type
matcher (e.g. line description contains "Extended Support") can
link a rule's amount to a specific FOCUS component.

Decision record: docs/adr/ADR-13-monetary-extraction-registry.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ValueBasis(StrEnum):
    """What the amount is derived from.

    V1: every rule is ESTIMATED. ACTUAL is reserved for V2 — when a
    per-charge-type matcher (e.g. "Extended Support" in the FOCUS
    line description) can link a rule's amount to one specific FOCUS
    component, that component's billed cost carries the ACTUAL label.
    Until then, the label "ACTUAL" is structurally unsound: the
    audit committee's deal-breaker was "any amount presented as
    ACTUAL, 'confirmed by invoice' or 'avoidable' without a
    traceable link to the FOCUS line, the currency, and the exact
    cost component".
    """

    ESTIMATED = "ESTIMATED"  # catalog-priced (EOL calendars, public rates)
    ACTUAL = "ACTUAL"  # V2: read from a single, line-matched FOCUS component


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
# in apps/api insights runner RUNNERS. V1: every value_basis is ESTIMATED
# (the audit committee fix; see module docstring for the rationale).
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
        # V1: chargeback is ESTIMATED — it is a derived signal
        # (amortized minus billed over a FOCUS period), not a
        # FOCUS line itself. V2: a per-charge-type matcher (line
        # description contains "Extended Support" or similar) can
        # promote the matching slice to ACTUAL.
        payload_key="drift_amortized_minus_billed_usd",
        value_basis=ValueBasis.ESTIMATED,
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

    V1: this function never returns ACTUAL. The reconciliation pass
    that used to flip value_basis to ACTUAL is now a no-op for the
    label (see apps/api/insights/reconcile.py). The basis reflects
    the rule's registered value_basis, which is ESTIMATED for every
    V1 rule. V2 will add a per-charge-type matcher that returns
    ACTUAL when a specific FOCUS component can be linked to the
    rule's amount.
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
