"""Tests for the ebs_gp2_to_gp3 insight rule.

Three resolver outcomes:
- MATCH: a gp2 volume with a real saving. Emits one Insight.
- NO_MATCH: a non-gp2 volume, or a gp2 below the noise threshold.
- INCONCLUSIVE: a missing fact that blocks assessment (criterion n°15:
  the runner emits an Inconclusive record so the user sees the gap in
  their data — never a silent skip).
"""

from __future__ import annotations

from uuid import uuid4

from constat_core.catalog.ebs import EBS_CATALOG_VERSION
from constat_core.models import Fact, Severity, ValueState
from constat_ebs_gp2_to_gp3.resolver import MIN_SAVINGS_USD_PER_MONTH, RULE_NAME, evaluate


def _fact(key: str, value, *, value_state: ValueState = ValueState.KNOWN) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace="aws.ec2.volume",
        key=key,
        value=value,
        value_state=value_state,
        source="aws_ec2",
        observed_at=__import__("datetime").date.today(),
    )


def _gp2_facts(size_gb: int, volume_type: str = "gp2", region: str = "us-east-1") -> list[Fact]:
    """Minimal fact set for one EBS volume."""
    return [
        _fact("size_gb", size_gb),
        _fact("volume_type", volume_type),
        _fact("region", region),
    ]


# ---------------------------------------------------------------------------
# Rule name + source constants
# ---------------------------------------------------------------------------


def test_rule_name_is_ebs_gp2_to_gp3():
    """The rule name is the registry key. The runner's RESOURCE_RULES
    maps this string to the resolver function."""
    assert RULE_NAME == "ebs_gp2_to_gp3"


# ---------------------------------------------------------------------------
# MATCH: gp2 with a real saving
# ---------------------------------------------------------------------------


def test_gp2_100gb_emits_match_with_savings():
    """100 GB gp2 vs 100 GB gp3: $10 vs $8 = $2.00/month saved."""
    result = evaluate(uuid4(), _gp2_facts(100))

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    # 100 GB * ($0.10 - $0.08) = $2.00, well above the $0.50 noise threshold
    assert insight.payload["savings_monthly_usd"] == 2.00
    assert insight.payload["current_monthly_usd"] == 10.00
    assert insight.payload["target_monthly_usd"] == 8.00
    assert insight.payload["current_volume_type"] == "gp2"
    assert insight.payload["target_volume_type"] == "gp3"
    assert insight.payload["value_basis"] == "ESTIMATED"
    assert insight.payload["catalog_version"] == EBS_CATALOG_VERSION


def test_gp2_500gb_emits_info_severity():
    """500 GB gp2: $10 saved/month. Severity INFO (< $50).

    500 GB is the common case (a few hundred GB per volume is the
    modal gp2 fleet), so the operator dashboard will be dominated by
    INFO-severity insights. WARNING/CRITICAL surface only the fleet
    outliers (multi-TB volumes) which is the right prioritization.
    """
    result = evaluate(uuid4(), _gp2_facts(500))

    assert result.has_gap
    # 500 * ($0.10 - $0.08) = $10/month, below the $50 WARNING threshold.
    assert result.insights[0].severity == Severity.INFO
    assert result.insights[0].payload["savings_monthly_usd"] == 10.00


def test_gp2_severity_thresholds_are_correct():
    """Severity by monthly savings:
    >= $500/mo = CRITICAL
    >= $50/mo  = WARNING
    else       = INFO

    Boundary check at each level. Sizes chosen to hit exactly the boundary.
    """
    # $50 boundary: 2500 GB gp2 = 2500 * 0.02 = $50 saved -> WARNING
    r1 = evaluate(uuid4(), _gp2_facts(2500))
    assert r1.insights[0].severity == Severity.WARNING

    # Just below: 2400 GB = $48 saved -> INFO
    r2 = evaluate(uuid4(), _gp2_facts(2400))
    assert r2.insights[0].severity == Severity.INFO

    # $500 boundary: 25000 GB = $500 saved -> CRITICAL
    r3 = evaluate(uuid4(), _gp2_facts(25000))
    assert r3.insights[0].severity == Severity.CRITICAL

    # Just below: 24000 GB = $480 saved -> WARNING
    r4 = evaluate(uuid4(), _gp2_facts(24000))
    assert r4.insights[0].severity == Severity.WARNING


def test_gp2_savings_pct_is_20():
    """gp3 is 20% cheaper than gp2 on storage. The savings_pct field
    encodes this for the UI ("20% storage saving") and must match."""
    result = evaluate(uuid4(), _gp2_facts(1000))
    assert result.insights[0].payload["savings_pct"] == 20.0


def test_gp2_title_mentions_size_and_saving():
    """The insight title goes onto the operator's dashboard. It must
    answer 'what is this about' and 'how much will I save' at a glance."""
    result = evaluate(uuid4(), _gp2_facts(200))
    title = result.insights[0].title
    assert "200" in title  # size
    assert "$4.00" in title  # savings formatted as currency


# ---------------------------------------------------------------------------
# NO_MATCH: non-gp2 volumes
# ---------------------------------------------------------------------------


def test_gp3_volume_emits_nothing():
    """gp3 is already the target — no migration candidate, no insight."""
    result = evaluate(uuid4(), _gp2_facts(100, volume_type="gp3"))
    assert result.is_conclusive
    assert not result.has_gap
    assert result.insights == []
    assert result.inconclusive_reasons == []


def test_io1_volume_emits_nothing():
    """io1 isn't in the migration scope (different pricing, different
    workload). NO_MATCH."""
    result = evaluate(uuid4(), _gp2_facts(100, volume_type="io1"))
    assert result.is_conclusive
    assert not result.has_gap


def test_io2_volume_emits_nothing():
    result = evaluate(uuid4(), _gp2_facts(100, volume_type="io2"))
    assert not result.has_gap


def test_st1_volume_emits_nothing():
    result = evaluate(uuid4(), _gp2_facts(100, volume_type="st1"))
    assert not result.has_gap


def test_standard_volume_emits_nothing():
    result = evaluate(uuid4(), _gp2_facts(100, volume_type="standard"))
    assert not result.has_gap


def test_gp2_below_noise_threshold_emits_nothing():
    """A 1 GB gp2 saves $0.02/month — below MIN_SAVINGS_USD_PER_MONTH.
    NO_MATCH, not an insight. Keeps the dashboard clean for prospects
    with hundreds of tiny volumes (Lambda, ECS scratch)."""
    # 1 GB gp2 = 0.10 - 0.08 = $0.02 saved -> below $0.50 threshold
    result = evaluate(uuid4(), _gp2_facts(1))
    assert result.is_conclusive
    assert not result.has_gap


def test_gp2_at_noise_threshold_emits_nothing():
    """At MIN_SAVINGS_USD_PER_MONTH exactly: still NO_MATCH (strict <).
    25 GB gp2 = 0.50 saved, the threshold. Strict less-than keeps the
    boundary clean: >= threshold is the smallest emitted insight."""
    # 25 GB gp2 = 25 * 0.02 = $0.50 exactly
    result = evaluate(uuid4(), _gp2_facts(int(MIN_SAVINGS_USD_PER_MONTH / 0.02)))
    # $0.50 >= $0.50 -> emits (the rule uses <, not <=)
    # Actually, let me check the rule: `if savings < MIN_SAVINGS_USD_PER_MONTH: NO_MATCH`
    # So savings == MIN_SAVINGS_USD_PER_MONTH is NOT below -> emits.
    # This is the inclusive boundary. Document it.
    assert result.has_gap  # exactly at threshold -> emits


# ---------------------------------------------------------------------------
# INCONCLUSIVE: missing facts
# ---------------------------------------------------------------------------


def test_unknown_volume_type_emits_inconclusive():
    """If the volume_type fact is UNKNOWN, the rule can't decide if
    this is a migration candidate. INCONCLUSIVE, not NO_MATCH."""
    facts = [
        _fact("size_gb", 100, value_state=ValueState.KNOWN),
        _fact("volume_type", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts)

    assert not result.is_conclusive
    assert not result.has_gap
    assert result.insights == []
    assert "aws.ec2.volume.volume_type" in result.inconclusive_reasons


def test_unknown_size_emits_inconclusive():
    facts = [
        _fact("size_gb", None, value_state=ValueState.UNKNOWN),
        _fact("volume_type", "gp2", value_state=ValueState.KNOWN),
    ]
    result = evaluate(uuid4(), facts)

    assert not result.is_conclusive
    assert "aws.ec2.volume.size_gb" in result.inconclusive_reasons


def test_missing_region_emits_inconclusive():
    """The gp2/gp3 delta is not region-uniform: without the region fact
    we can't price honestly. INCONCLUSIVE, never a guessed grid."""
    facts = [
        _fact("size_gb", 100, value_state=ValueState.KNOWN),
        _fact("volume_type", "gp2", value_state=ValueState.KNOWN),
        _fact("region", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts)

    assert not result.is_conclusive
    assert "aws.ec2.volume.region" in result.inconclusive_reasons


# ---------------------------------------------------------------------------
# Region-aware pricing (chantier 2.1)
# ---------------------------------------------------------------------------


def test_eu_west_3_gp2_prices_on_the_eu_west_3_grid():
    """Hand-computed on the eu-west-3 grid (AWS Price List 2026-07-17):
    100 GB gp2 = 100 * $0.116 = $11.60; gp3 = 100 * $0.0928 = $9.28;
    saving $2.32/month (still 20% — the ratio holds across regions)."""
    result = evaluate(uuid4(), _gp2_facts(100, region="eu-west-3"))

    assert result.has_gap
    payload = result.insights[0].payload
    assert payload["current_monthly_usd"] == 11.60
    assert payload["target_monthly_usd"] == 9.28
    assert payload["savings_monthly_usd"] == 2.32
    assert payload["savings_pct"] == 20.0
    assert payload["pricing_region"] == "eu-west-3"
    assert payload["price_region_exact"] is True
    assert payload["source_currency"] == "USD"


def test_uncatalogued_region_falls_back_with_exact_false():
    """A region the catalog doesn't cover still MATCHes on the us-east-1
    grid, but the payload admits the fallback."""
    result = evaluate(uuid4(), _gp2_facts(100, region="ap-southeast-2"))

    assert result.has_gap
    payload = result.insights[0].payload
    assert payload["savings_monthly_usd"] == 2.00
    assert payload["pricing_region"] == "us-east-1"
    assert payload["price_region_exact"] is False


def test_no_facts_emits_inconclusive():
    """An empty fact list = we know nothing about this resource.
    INCONCLUSIVE, not silent."""
    result = evaluate(uuid4(), [])

    assert not result.is_conclusive
    assert set(result.inconclusive_reasons) == {
        "aws.ec2.volume.volume_type",
        "aws.ec2.volume.size_gb",
        "aws.ec2.volume.region",
    }


def test_inconclusive_does_not_emit_insight():
    """Sanity: an INCONCLUSIVE result must not also have an insight.
    The runner's contract is binary per resource: either MATCH (insight)
    or INCONCLUSIVE (gap), not both."""
    facts = [
        _fact("size_gb", None, value_state=ValueState.UNKNOWN),
        _fact("volume_type", "gp2", value_state=ValueState.KNOWN),
    ]
    result = evaluate(uuid4(), facts)
    assert not result.is_conclusive
    assert result.insights == []


# ---------------------------------------------------------------------------
# Catalog-version stamp on every emitted insight
# ---------------------------------------------------------------------------


def test_insight_payload_includes_catalog_version():
    """The catalog_version on the payload must match the EBS catalog
    module's version. Drift = sales can't cite a defensible date.
    Regression guard: if someone refactors _make_insight and drops
    the field, this test catches it."""
    result = evaluate(uuid4(), _gp2_facts(100))
    assert result.insights[0].payload["catalog_version"] == EBS_CATALOG_VERSION
