"""Tests for the shared storage cost evaluator (roadmap-consolidation §III.2).

The three V1 storage rules (`ebs_gp2_to_gp3`, `ebs_unattached`,
`snapshot_orphan`) have their own test suites that pin their
rule-specific behavior. This file pins the SHARED behavior: the
required-facts gate, the NO_MATCH predicate, the $500/$50
severity thresholds, and the `size_gb x $/GB-month` arithmetic
that lives in one place.

The original tests for "ebs catalog drop" (a class of bugs the
§III.2 refactor eliminates) live in the per-rule test files
(`test_ebs_gp2_to_gp3.py`, `test_ebs_unattached.py`,
`test_snapshot_orphan.py`) and assert the per-rule shape
directly. These tests add the shared-side coverage: parametrized
over the three rules, one test per fact gate, per NO_MATCH
branch, and per severity threshold.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from constat_core.insights.storage import evaluate_storage
from constat_core.models import Fact, Severity, ValueState
from constat_ebs_gp2_to_gp3.resolver import CONFIG as GP2_TO_GP3_CONFIG
from constat_ebs_unattached.resolver import CONFIG as UNATTACHED_CONFIG
from constat_snapshot_orphan.resolver import CONFIG as SNAPSHOT_ORPHAN_CONFIG

# ---------------------------------------------------------------------------
# Fact helpers
# ---------------------------------------------------------------------------


def _fact(
    namespace: str,
    key: str,
    value,
    *,
    value_state: ValueState = ValueState.KNOWN,
) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace=namespace,
        key=key,
        value=value,
        value_state=value_state,
        source="aws_ec2",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def _gp2_to_gp3_match() -> list[Fact]:
    """A 1000 GB gp2 volume in us-east-1 with a $20/month saving."""
    return [
        _fact("aws.ec2.volume", "volume_type", "gp2"),
        _fact("aws.ec2.volume", "size_gb", 1000),
        _fact("aws.ec2.volume", "region", "us-east-1"),
    ]


def _unattached_match() -> list[Fact]:
    """A 1000 GB available gp2 volume in us-east-1 with $100/month waste."""
    return [
        _fact("aws.ec2.volume", "state", "available"),
        _fact("aws.ec2.volume", "size_gb", 1000),
        _fact("aws.ec2.volume", "volume_type", "gp2"),
        _fact("aws.ec2.volume", "region", "us-east-1"),
    ]


def _snapshot_orphan_match() -> list[Fact]:
    """A 1000 GB completed orphan standard-tier snapshot in us-east-1
    with $50/month cost."""
    return [
        _fact("aws.ec2.snapshot", "state", "completed"),
        _fact("aws.ec2.snapshot", "size_gb", 1000),
        _fact("aws.ec2.snapshot", "volume_exists", False),
        _fact("aws.ec2.snapshot", "description", "manual backup"),
        _fact("aws.ec2.snapshot", "storage_tier", "standard"),
        _fact("aws.ec2.snapshot", "region", "us-east-1"),
    ]


# Snapshot_orphan has 5 required facts; the other two rules have
# 3 and 4. We parametrize the "UNKNOWN fact -> inconclusive" check
# per-rule so a regression in one rule's gate doesn't get masked by
# the others.
@pytest.mark.parametrize(
    "config,required_facts",
    [
        (
            GP2_TO_GP3_CONFIG,
            (
                "aws.ec2.volume.volume_type",
                "aws.ec2.volume.size_gb",
                "aws.ec2.volume.region",
            ),
        ),
        (
            UNATTACHED_CONFIG,
            (
                "aws.ec2.volume.state",
                "aws.ec2.volume.size_gb",
                "aws.ec2.volume.volume_type",
                "aws.ec2.volume.region",
            ),
        ),
        (
            SNAPSHOT_ORPHAN_CONFIG,
            (
                "aws.ec2.snapshot.state",
                "aws.ec2.snapshot.size_gb",
                "aws.ec2.snapshot.volume_exists",
                "aws.ec2.snapshot.description",
                "aws.ec2.snapshot.region",
            ),
        ),
    ],
)
def test_all_required_facts_unknown_emits_inconclusive(config, required_facts) -> None:
    """If every required fact is UNKNOWN, the rule emits an
    inconclusive reason for each and nothing else. Sanity: the
    shared gate works across the three V1 rules."""
    facts = []
    for dotted in required_facts:
        ns, key = dotted.rsplit(".", 1)
        facts.append(_fact(ns, key, None, value_state=ValueState.UNKNOWN))
    result = evaluate_storage(uuid4(), facts, config, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert result.insights == []
    assert set(result.inconclusive_reasons) == set(required_facts)


# ---------------------------------------------------------------------------
# The $500 / $50 severity thresholds (shared across the V1 rules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,match_facts,expected_at_50,expected_at_500",
    [
        # gp2_to_gp3's MIN_SAVINGS noise filter caps the tested
        # ranges; its severity thresholds are pinned in
        # test_ebs_gp2_to_gp3.py::test_gp2_severity_thresholds_are_correct.
        pytest.param(
            UNATTACHED_CONFIG,
            _unattached_match,
            500,  # 500 GB gp2 available = $50/month -> WARNING
            5000,  # 5000 GB gp2 available = $500/month -> CRITICAL
            id="ebs_unattached_at_50_500",
        ),
        pytest.param(
            SNAPSHOT_ORPHAN_CONFIG,
            _snapshot_orphan_match,
            1000,  # 1000 GB standard orphan = $50/month -> WARNING
            10000,  # 10000 GB standard orphan = $500/month -> CRITICAL
            id="snapshot_orphan_at_50_500",
        ),
    ],
)
def test_severity_thresholds_are_shared(config, match_facts, expected_at_50, expected_at_500) -> None:
    """Same severity scale across the V1 storage rules: >= $500 =
    CRITICAL, >= $50 = WARNING, else INFO. The shared function
    applies the thresholds; the rules declare them in their
    StorageRuleConfig (the default is the same)."""
    # At the WARNING boundary
    facts_w = [
        f if f.key != "size_gb" else _fact(f.namespace, f.key, expected_at_50)
        for f in match_facts()
    ]
    result = evaluate_storage(uuid4(), facts_w, config, today=date(2026, 7, 18))
    assert result.has_gap
    assert result.insights[0].severity == Severity.WARNING

    # At the CRITICAL boundary
    facts_c = [
        f if f.key != "size_gb" else _fact(f.namespace, f.key, expected_at_500)
        for f in match_facts()
    ]
    result = evaluate_storage(uuid4(), facts_c, config, today=date(2026, 7, 18))
    assert result.has_gap
    assert result.insights[0].severity == Severity.CRITICAL

    # Below the WARNING threshold
    facts_i = [
        f if f.key != "size_gb" else _fact(f.namespace, f.key, 100)
        for f in match_facts()
    ]
    result = evaluate_storage(uuid4(), facts_i, config, today=date(2026, 7, 18))
    assert result.has_gap
    assert result.insights[0].severity == Severity.INFO


# ---------------------------------------------------------------------------
# The arithmetic — the whole point of §III.2
# ---------------------------------------------------------------------------


def test_size_gb_x_usd_per_gb_month_is_the_only_arithmetic() -> None:
    """The single home of the cost estimate: the rule's
    `compute_cost` returns `size_gb x $/GB-month`, the shared
    function never sees the multiplication.

    1000 GB unattached gp2 = 1000 x $0.10 = $100/month. The
    `test_ebs_unattached::test_available_gp2_volume_emits_match`
    test pins the same arithmetic for a 100 GB volume; this test
    adds the shared-side check across rules.
    """
    result = evaluate_storage(uuid4(), _unattached_match(), UNATTACHED_CONFIG, today=date(2026, 7, 18))
    assert len(result.insights) == 1
    assert result.insights[0].payload["monthly_waste_usd"] == 100.00


def test_savings_arithmetic_uses_two_prices() -> None:
    """The gp2_to_gp3 rule is the only one with a 2-price
    arithmetic (savings = gp2 - gp3). Pinned here because the
    shared function never sees the subtraction — it's the rule's
    compute_cost that returns the savings as the monthly_usd."""
    result = evaluate_storage(uuid4(), _gp2_to_gp3_match(), GP2_TO_GP3_CONFIG, today=date(2026, 7, 18))
    assert len(result.insights) == 1
    payload = result.insights[0].payload
    # 1000 GB gp2 = $100, gp3 = $80, savings = $20
    assert payload["savings_monthly_usd"] == 20.00
    assert payload["current_monthly_usd"] == 100.00
    assert payload["target_monthly_usd"] == 80.00


# ---------------------------------------------------------------------------
# NO_MATCH: the per-rule should_emit predicate
# ---------------------------------------------------------------------------


def test_gp2_to_gp3_no_match_for_non_gp2() -> None:
    """volume_type != "gp2" -> NO_MATCH, not INCONCLUSIVE."""
    facts = [
        _fact("aws.ec2.volume", "volume_type", "gp3"),
        _fact("aws.ec2.volume", "size_gb", 1000),
        _fact("aws.ec2.volume", "region", "us-east-1"),
    ]
    result = evaluate_storage(uuid4(), facts, GP2_TO_GP3_CONFIG, today=date(2026, 7, 18))
    assert result.is_conclusive
    assert not result.has_gap


def test_unattached_no_match_for_in_use() -> None:
    """state != "available" -> NO_MATCH, not INCONCLUSIVE."""
    facts = [
        _fact("aws.ec2.volume", "state", "in-use"),
        _fact("aws.ec2.volume", "size_gb", 1000),
        _fact("aws.ec2.volume", "volume_type", "gp2"),
        _fact("aws.ec2.volume", "region", "us-east-1"),
    ]
    result = evaluate_storage(uuid4(), facts, UNATTACHED_CONFIG, today=date(2026, 7, 18))
    assert result.is_conclusive
    assert not result.has_gap


def test_snapshot_orphan_no_match_for_volume_exists() -> None:
    """volume_exists=True -> NO_MATCH (volume still there)."""
    facts = _snapshot_orphan_match()
    facts = [
        f if f.key != "volume_exists" else _fact(f.namespace, f.key, True)
        for f in facts
    ]
    result = evaluate_storage(uuid4(), facts, SNAPSHOT_ORPHAN_CONFIG, today=date(2026, 7, 18))
    assert result.is_conclusive
    assert not result.has_gap


def test_snapshot_orphan_no_match_for_ami_referenced() -> None:
    """description contains "ami-" -> NO_MATCH (cannot prove
    orphan without DescribeImages)."""
    facts = _snapshot_orphan_match()
    facts = [
        f
        if f.key != "description"
        else _fact(f.namespace, f.key, "Created by CreateImage(i-0) for ami-0abcdef")
        for f in facts
    ]
    result = evaluate_storage(uuid4(), facts, SNAPSHOT_ORPHAN_CONFIG, today=date(2026, 7, 18))
    assert result.is_conclusive
    assert not result.has_gap


# ---------------------------------------------------------------------------
# Inconclusive from a malformed cost input
# ---------------------------------------------------------------------------


def test_snapshot_orphan_malformed_start_time_is_inconclusive() -> None:
    """start_time is optional (missing -> age_days=None, no
    INCONCLUSIVE), but a value that's present and unparseable IS
    INCONCLUSIVE. The shared function catches the
    `StorageInconclusive` raised by compute_cost."""
    facts = _snapshot_orphan_match()
    facts.append(_fact("aws.ec2.snapshot", "start_time", "not-a-date"))
    result = evaluate_storage(uuid4(), facts, SNAPSHOT_ORPHAN_CONFIG, today=date(2026, 7, 18))
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.start_time.malformed" in result.inconclusive_reasons


def test_snapshot_orphan_missing_start_time_is_not_inconclusive() -> None:
    """start_time is OPTIONAL: a snapshot without a start_time
    still matches (age_days=None in the payload)."""
    facts = _snapshot_orphan_match()
    result = evaluate_storage(uuid4(), facts, SNAPSHOT_ORPHAN_CONFIG, today=date(2026, 7, 18))
    assert result.has_gap
    assert result.insights[0].payload["snapshot_age_days"] is None


def test_snapshot_orphan_missing_tier_defaults_to_standard() -> None:
    """storage_tier is OPTIONAL: a snapshot without the fact
    prices on the standard tier (AWS default), not INCONCLUSIVE."""
    facts = [f for f in _snapshot_orphan_match() if f.key != "storage_tier"]
    result = evaluate_storage(uuid4(), facts, SNAPSHOT_ORPHAN_CONFIG, today=date(2026, 7, 18))
    assert result.has_gap
    assert result.insights[0].payload["storage_tier"] == "standard"


# ---------------------------------------------------------------------------
# Catalog honesty — the per-region grid + price_region_exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,match_facts,region,expected_exact,expected_region",
    [
        (UNATTACHED_CONFIG, _unattached_match, "us-east-1", True, "us-east-1"),
        (UNATTACHED_CONFIG, _unattached_match, "eu-west-3", True, "eu-west-3"),
        (UNATTACHED_CONFIG, _unattached_match, "ap-southeast-2", False, "us-east-1"),
        (SNAPSHOT_ORPHAN_CONFIG, _snapshot_orphan_match, "us-east-1", True, "us-east-1"),
        (SNAPSHOT_ORPHAN_CONFIG, _snapshot_orphan_match, "eu-west-3", True, "eu-west-3"),
        (SNAPSHOT_ORPHAN_CONFIG, _snapshot_orphan_match, "ap-southeast-2", False, "us-east-1"),
    ],
)
def test_pricing_region_and_exactness(
    config, match_facts, region, expected_exact, expected_region
) -> None:
    """A catalogued region prices on its own grid (`price_region_exact=True`);
    an uncatalogued region falls back to us-east-1 and the payload
    admits the fallback (`price_region_exact=False`). The shared
    function carries the field through from the rule's compute_cost."""
    facts = [f if f.key != "region" else _fact(f.namespace, f.key, region) for f in match_facts()]
    result = evaluate_storage(uuid4(), facts, config, today=date(2026, 7, 18))
    assert result.has_gap
    payload = result.insights[0].payload
    assert payload["pricing_region"] == expected_region
    assert payload["price_region_exact"] is expected_exact
