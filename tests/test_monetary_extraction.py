"""Proof tests for the monetary extraction registry (ADR-13).

Client-committee finding: the restitution/CSV extraction only knew
`extended_support_monthly_usd`, so ebs_gp2_to_gp3 savings silently
dropped out — and the rds_eol tiering refactor had stopped emitting a
monthly amount entirely without any test noticing. These tests make
both failure modes structural:

1. Completeness: every rule registered in RUNNERS must either declare
   its monetary payload key in constat_core.monetary.MONETARY or be
   explicitly listed in NON_MONETARY_RULES.
2. Extraction: each registered key is actually extracted, bools and
   garbage are rejected, unknown rules yield (None, None).
3. Emission: rds_eol (the regressed rule) emits the registered key
   again, with the right arithmetic.
4. Mirror pin: the TS table in apps/web/lib/api.ts contains every rule
   and payload key of the Python registry — the two cannot drift.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from constat_core.models import Fact, ValueState
from constat_core.monetary import (
    MONETARY,
    NON_MONETARY_RULES,
    MonetaryKind,
    ValueBasis,
    monetary_kind,
    monthly_cost_and_basis,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_API_TS = REPO_ROOT / "apps" / "web" / "lib" / "api.ts"


# ---------------------------------------------------------------------------
# 1. Completeness: RUNNERS and the registry cannot drift
# ---------------------------------------------------------------------------


def test_every_runner_rule_has_a_monetary_decision() -> None:
    """A rule in RUNNERS must be in MONETARY or NON_MONETARY_RULES.

    This is the CI guard the committee's bug proved missing: a new
    insight that emits money but never reaches the restitution now
    fails here instead of failing in front of a prospect.
    """
    from constat_api.insights.runner import RUNNERS

    undecided = set(RUNNERS) - set(MONETARY) - NON_MONETARY_RULES
    assert not undecided, (
        f"rules with no monetary decision: {sorted(undecided)} — add them to "
        "constat_core.monetary.MONETARY (with payload_key/basis/kind) or to "
        "NON_MONETARY_RULES, and mirror apps/web/lib/api.ts (ADR-13)"
    )


def test_registry_has_no_orphan_rules() -> None:
    """The registry must not reference rules that no longer exist."""
    from constat_api.insights.runner import RUNNERS

    orphans = set(MONETARY) - set(RUNNERS)
    assert not orphans, f"registry entries without a runner: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# 2. Extraction semantics
# ---------------------------------------------------------------------------


def test_extracts_every_registered_key() -> None:
    for rule_name, entry in MONETARY.items():
        cost, basis = monthly_cost_and_basis(rule_name, {entry.payload_key: 123.45})
        assert cost == 123.45, f"{rule_name}: registered key not extracted"
        assert basis == entry.value_basis.value


def test_ebs_savings_are_extracted() -> None:
    """The exact committee bug: ebs_gp2_to_gp3 amounts were dropped."""
    cost, basis = monthly_cost_and_basis("ebs_gp2_to_gp3", {"savings_monthly_usd": 60.0})
    assert cost == 60.0
    assert basis == ValueBasis.ESTIMATED.value
    assert monetary_kind("ebs_gp2_to_gp3") == MonetaryKind.AVOIDABLE_SAVING


def test_chargeback_is_actual_and_accounting_delta() -> None:
    cost, basis = monthly_cost_and_basis("chargeback", {"drift_amortized_minus_billed_usd": -42.0})
    assert cost == -42.0
    assert basis == ValueBasis.ACTUAL.value
    # The kind that must keep drift OUT of any "savings" total.
    assert monetary_kind("chargeback") == MonetaryKind.ACCOUNTING_DELTA


def test_unknown_rule_yields_none_none() -> None:
    assert monthly_cost_and_basis("no_such_rule", {"x": 1}) == (None, None)
    assert monetary_kind("no_such_rule") is None


def test_missing_key_yields_none_with_basis() -> None:
    cost, basis = monthly_cost_and_basis("rds_eol", {"unrelated": 1})
    assert cost is None
    assert basis == ValueBasis.ESTIMATED.value


def test_bool_and_garbage_are_rejected() -> None:
    # bool IS an int in Python; True must not become $1.00.
    assert monthly_cost_and_basis("rds_eol", {"extended_support_monthly_usd": True})[0] is None
    assert monthly_cost_and_basis("rds_eol", {"extended_support_monthly_usd": "584"})[0] is None


# ---------------------------------------------------------------------------
# 3. Emission: rds_eol produces the registered key again (the regression)
# ---------------------------------------------------------------------------


def _fact(key: str, value: object) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace="aws.rds",
        key=key,
        value=value,
        value_state=ValueState.KNOWN,
        source="test",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def test_rds_eol_emits_registered_monthly_amount() -> None:
    """PG11, 4 vCPU, 2026-07-18 => year-3 tier: 4 x $0.20 x 730h = $584.00.

    The tiering refactor had dropped this multiplication entirely
    (vcpu was gated, then never used). This test ties the resolver's
    output to the registry: if the payload key or the arithmetic
    changes, extraction and emission fail together, loudly.
    """
    from constat_rds_eol.resolver import evaluate

    facts = [
        _fact("engine", "postgres"),
        _fact("engine_version", "11.22"),
        _fact("vcpu", 4),
        _fact("region", "us-east-1"),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))

    assert result.insights, "PG11 in 2026 must produce an Extended Support insight"
    payload = result.insights[0].payload
    entry = MONETARY["rds_eol"]
    assert entry.payload_key in payload, "resolver no longer emits the registered key"
    assert payload[entry.payload_key] == 584.0
    assert payload["vcpu"] == 4

    cost, basis = monthly_cost_and_basis("rds_eol", payload)
    assert cost == 584.0
    assert basis == ValueBasis.ESTIMATED.value


def test_rds_eol_malformed_vcpu_is_inconclusive_not_silent() -> None:
    from constat_rds_eol.resolver import evaluate

    facts = [
        _fact("engine", "postgres"),
        _fact("engine_version", "11.22"),
        _fact("vcpu", "not-a-number"),
        _fact("region", "us-east-1"),
    ]
    result = evaluate(uuid4(), facts, today=date(2026, 7, 18))
    assert result.insights == []
    assert "aws.rds.vcpu.malformed" in result.inconclusive_reasons


# ---------------------------------------------------------------------------
# 4. TS mirror pin: apps/web/lib/api.ts cannot drift from the registry
# ---------------------------------------------------------------------------


def test_ts_mirror_contains_every_registry_entry() -> None:
    """Cheap but effective drift guard (same spirit as the TENANT_GUC pin):
    the TS RULE_MONETARY table must mention every rule name and payload
    key of the Python registry. Renaming either side breaks this test."""
    source = WEB_API_TS.read_text(encoding="utf-8")
    assert "RULE_MONETARY" in source, "apps/web/lib/api.ts lost its RULE_MONETARY table"
    for rule_name, entry in MONETARY.items():
        assert rule_name in source, f"TS mirror missing rule {rule_name!r}"
        assert entry.payload_key in source, (
            f"TS mirror missing payload key {entry.payload_key!r} for {rule_name!r}"
        )
        assert entry.kind.value in source, f"TS mirror missing kind {entry.kind.value!r}"
