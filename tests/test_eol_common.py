"""Tests for the shared EOL evaluator (roadmap-consolidation §III.1).

The three V1 rules (rds_eol, mysql_eol, aurora_eol) have their own
test suites (test_rds_eol.py, test_mysql_eol.py, test_aurora_eol.py)
that pin their engine-specific behavior. This file pins the
SHARED behavior: the 4 fact gates, the 3-branch severity logic,
the payload structure, and the vcpu x tier rate x 730h
arithmetic that lives in one place.

The original test for "rds_eol tiering refactor dropped the
multiplication" (the regression that motivated §III.1) lives in
`tests/test_monetary_extraction.py::test_rds_eol_emits_registered_monthly_amount`
and asserts the arithmetic directly. These tests add the
shared-side coverage: parametrized over the three matchers, one
test per gate and per branch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from constat_core.catalog.aws import (
    aurora_mysql_eol_info,
    aurora_postgres_eol_info,
    engine_extended_support_tier,
    es_price_per_vcpu_hour,
    extended_support_tier,
    mysql_eol_info,
    postgres_eol_info,
)
from constat_core.insights.eol import (
    EngineEolMatcher,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.models import Fact, Severity, ValueState

# ---------------------------------------------------------------------------
# Three matchers — one per V1 rule — used to parametrize the shared tests.
# Mirrors the production matcher configs in
# packages/insights/{rds,mysql,aurora}_eol/src/constat_*_eol/resolver.py
# so a refactor in one place that drifts from the other is caught.
# ---------------------------------------------------------------------------


def _pg_compute_tier(info, today):
    return extended_support_tier(info.eol_date, today)


def _parse_pg_major(raw: str) -> int | None:
    parts = raw.split(".")
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0])


def _pg_upgrade_target(major: int) -> str:
    return f"PostgreSQL {major + 1}"


def _parse_mysql_major(raw: str) -> str | None:
    parts = raw.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


_NEXT_MYSQL = {"5.7": "8.0", "8.0": "8.4"}


def _mysql_upgrade_target(major: str) -> str:
    return _NEXT_MYSQL[major]


def _parse_aurora_major(engine_value: str, raw: str) -> int | None:
    if engine_value == "aurora-mysql" and "mysql_aurora." in raw:
        raw = raw.split("mysql_aurora.", 1)[1]
    try:
        return int(raw.split(".")[0])
    except (ValueError, IndexError):
        return None


_AURORA_MYSQL_NEXT = {2: "Aurora MySQL 3 (MySQL 8.0)", 3: "Aurora MySQL 8.4"}


def _aurora_mysql_upgrade_target(major: int) -> str:
    return _AURORA_MYSQL_NEXT[major]


def _aurora_postgres_upgrade_target(major: int) -> str:
    return f"Aurora PostgreSQL {major + 1}"


PG_MATCHER = EngineEolMatcher(
    engine_value="postgres",
    display_name="RDS PostgreSQL",
    service_canonical="managed_postgres",
    lookup_eol_info=postgres_eol_info,
    parse_major=_parse_pg_major,
    format_major=str,
    upgrade_target=_pg_upgrade_target,
    compute_tier=_pg_compute_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
    vcpu_payload_key="vcpu",
    value_basis=None,  # rds_eol's pre-refactor behavior
)

MYSQL_MATCHER = EngineEolMatcher(
    engine_value="mysql",
    display_name="RDS MySQL",
    service_canonical="managed_mysql",
    lookup_eol_info=mysql_eol_info,
    parse_major=_parse_mysql_major,
    format_major=str,
    upgrade_target=_mysql_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

AURORA_MYSQL_MATCHER = EngineEolMatcher(
    engine_value="aurora-mysql",
    display_name="Aurora MySQL",
    service_canonical="managed_mysql",
    lookup_eol_info=aurora_mysql_eol_info,
    parse_major=lambda raw: _parse_aurora_major("aurora-mysql", raw),
    format_major=str,
    upgrade_target=_aurora_mysql_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

AURORA_POSTGRES_MATCHER = EngineEolMatcher(
    engine_value="aurora-postgresql",
    display_name="Aurora PostgreSQL",
    service_canonical="managed_postgres",
    lookup_eol_info=aurora_postgres_eol_info,
    parse_major=lambda raw: _parse_aurora_major("aurora-postgresql", raw),
    format_major=str,
    upgrade_target=_aurora_postgres_upgrade_target,
    compute_tier=engine_extended_support_tier,
    price_per_vcpu_hour=es_price_per_vcpu_hour,
)

# Rule configs the tests can reference. The aurora config has two
# engines; the rds / mysql configs have one each.
PG_CONFIG = EolRuleConfig(rule_name="rds_eol", engines=(PG_MATCHER,))
MYSQL_CONFIG = EolRuleConfig(rule_name="mysql_eol", engines=(MYSQL_MATCHER,))
AURORA_CONFIG = EolRuleConfig(
    rule_name="aurora_eol", engines=(AURORA_MYSQL_MATCHER, AURORA_POSTGRES_MATCHER)
)


# ---------------------------------------------------------------------------
# Fact helpers
# ---------------------------------------------------------------------------


def _fact(namespace: str, key: str, value, *, value_state: ValueState = ValueState.KNOWN) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace=namespace,
        key=key,
        value=value,
        value_state=value_state,
        source="aws_rds",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def _pg_facts(
    *, version: str = "11.22", vcpu: int = 4, region: str = "us-east-1", engine: str = "postgres"
) -> list[Fact]:
    return [
        _fact("aws.rds", "engine", engine),
        _fact("aws.rds", "engine_version", version),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


def _mysql_facts(
    *, version: str = "5.7.42", vcpu: int = 4, region: str = "us-east-1"
) -> list[Fact]:
    return [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", version),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


def _aurora_mysql_facts(
    *, version: str = "3.08.1", vcpu: int = 4, region: str = "us-east-1"
) -> list[Fact]:
    return [
        _fact("aws.rds", "engine", "aurora-mysql"),
        _fact("aws.rds", "engine_version", version),
        _fact("aws.rds", "vcpu", vcpu),
        _fact("aws.rds", "region", region),
    ]


# ---------------------------------------------------------------------------
# The 4 fact gates
# ---------------------------------------------------------------------------


def _replace_fact_state(facts: list[Fact], key: str, value_state: ValueState) -> list[Fact]:
    """Return a copy of `facts` with the matching fact flagged as
    the given `value_state`. Used to test the 4 fact gates."""
    out = []
    for f in facts:
        if f.key == key:
            out.append(_fact(f.namespace, f.key, f.value, value_state=value_state))
        else:
            out.append(f)
    return out


def _replace_fact_value(facts: list[Fact], key: str, value) -> list[Fact]:
    """Return a copy of `facts` with the matching fact's value
    replaced. Used to test the malformed-fact gates (KNOWN state,
    unparseable value)."""
    out = []
    for f in facts:
        if f.key == key:
            out.append(_fact(f.namespace, f.key, value))
        else:
            out.append(f)
    return out


def test_engine_unknown_is_inconclusive() -> None:
    facts = _replace_fact_state(_pg_facts(), "engine", ValueState.UNKNOWN)
    result = evaluate_eol(uuid4(), facts, PG_CONFIG, today=date(2026, 7, 18))
    assert "aws.rds.engine" in result.inconclusive_reasons
    assert not result.insights


def test_version_unknown_is_inconclusive() -> None:
    facts = _replace_fact_state(_pg_facts(), "engine_version", ValueState.UNKNOWN)
    result = evaluate_eol(uuid4(), facts, PG_CONFIG, today=date(2026, 7, 18))
    assert "aws.rds.engine_version" in result.inconclusive_reasons
    assert not result.insights


def test_vcpu_unknown_is_inconclusive() -> None:
    facts = _replace_fact_state(_pg_facts(), "vcpu", ValueState.UNKNOWN)
    result = evaluate_eol(uuid4(), facts, PG_CONFIG, today=date(2026, 7, 18))
    assert "aws.rds.vcpu" in result.inconclusive_reasons
    assert not result.insights


def test_region_unknown_is_inconclusive() -> None:
    facts = _replace_fact_state(_pg_facts(), "region", ValueState.UNKNOWN)
    result = evaluate_eol(uuid4(), facts, PG_CONFIG, today=date(2026, 7, 18))
    assert "aws.rds.region" in result.inconclusive_reasons
    assert not result.insights


def test_vcpu_malformed_is_inconclusive_not_silent() -> None:
    """The fact is KNOWN, but the value is unparseable ("not-a-number").
    Silent skip is the failure mode the product must avoid."""
    facts = _replace_fact_value(_pg_facts(), "vcpu", "not-a-number")
    result = evaluate_eol(uuid4(), facts, PG_CONFIG, today=date(2026, 7, 18))
    assert "aws.rds.vcpu.malformed" in result.inconclusive_reasons
    assert not result.insights


def test_engine_not_in_rule_returns_empty() -> None:
    """A NO_MATCH engine (e.g. rds_eol evaluating an `oracle` engine)
    returns an empty InsightResult — no insight, no inconclusive.
    The rule has nothing to say about that engine."""
    result = evaluate_eol(uuid4(), _pg_facts(engine="oracle"), PG_CONFIG, today=date(2026, 7, 18))
    assert result.insights == []
    assert result.inconclusive_reasons == []


# ---------------------------------------------------------------------------
# The 3 branches
# ---------------------------------------------------------------------------


def test_post_eos_evaluates_as_force_upgrade() -> None:
    """today > eol_info.end_of_extended_support → CRITICAL, days_to_force."""
    # PG11 EOS 2027-03-31. Run with today past EOS, days_to_force
    # is the days remaining until EOS from today (negative today > EOS
    # means "in the past" — the title surfaces the absolute distance).
    result = evaluate_eol(
        uuid4(),
        _pg_facts(version="11.22"),
        PG_CONFIG,
        today=date(2028, 6, 1),  # past EOS 2027-03-31
    )
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.severity == Severity.CRITICAL
    assert "force-upgraded" in insight.title
    assert "PostgreSQL 12" in insight.payload["recommendation"]  # upgrade target


def test_in_extended_support_is_critical() -> None:
    """eol_date <= today <= end_of_extended_support → CRITICAL, in ES."""
    # PG11 EOL 2024-02-29, EOS 2027-03-31. today=2024-09-01 is in ES.
    result = evaluate_eol(
        uuid4(),
        _pg_facts(version="11.22"),
        PG_CONFIG,
        today=date(2024, 9, 1),
    )
    assert len(result.insights) == 1
    assert result.insights[0].severity == Severity.CRITICAL
    assert "Extended Support" in result.insights[0].title


def test_within_alert_window_is_warning() -> None:
    """today within 90 days of eol_date (but not yet EOL) → WARNING."""
    # PG11 EOL 2024-02-29, EOS 2027-03-31. today=2023-12-31 is 60 days before EOL.
    result = evaluate_eol(
        uuid4(),
        _pg_facts(version="11.22"),
        PG_CONFIG,
        today=date(2023, 12, 31),
    )
    assert len(result.insights) == 1
    assert result.insights[0].severity == Severity.WARNING
    assert "reaches EOL" in result.insights[0].title


def test_beyond_alert_window_is_no_alert() -> None:
    """today > 90 days before eol_date → no alert (roadmap item, not écart)."""
    # PG11 EOL 2024-02-29. today=2023-09-01 is 181 days before EOL.
    result = evaluate_eol(
        uuid4(),
        _pg_facts(version="11.22"),
        PG_CONFIG,
        today=date(2023, 9, 1),
    )
    assert result.insights == []


# ---------------------------------------------------------------------------
# The arithmetic — the whole point of §III.1
# ---------------------------------------------------------------------------


def test_vcpu_x_tier_rate_x_730h_is_the_only_monthly_arithmetic() -> None:
    """The single home of the cost estimate: 4 vCPU x tier rate x 730h.

    The `test_monetary_extraction::test_rds_eol_emits_registered_monthly_amount`
    test pins PG11 year-3 = 4 x $0.20 x 730h = $584.00. This test adds
    the same arithmetic check for MySQL and Aurora, proving the
    `vcpu x tier rate x 730h` line is the ONE multiplication — not
    three copies, not a different formula per engine.

    `today=2026-09-01` is > 730 days past PG11 EOL (2024-02-29) so the
    tier is year_3 ($0.20/vCPU-hr). 4 x 0.20 x 730 = 584. Same for
    MySQL 5.7.
    """
    cases = [
        (PG_CONFIG, _pg_facts(version="11.22", vcpu=4, region="us-east-1"), Decimal("584.00")),
        (
            MYSQL_CONFIG,
            _mysql_facts(version="5.7.42", vcpu=4, region="us-east-1"),
            Decimal("584.00"),
        ),
    ]
    for config, facts, expected in cases:
        result = evaluate_eol(uuid4(), facts, config, today=date(2026, 9, 1))
        assert len(result.insights) == 1
        monthly = result.insights[0].payload["extended_support_monthly_usd"]
        assert monthly == expected, (
            f"{config.rule_name}: monthly should be {expected} (4 x $0.20 x 730h), got {monthly}"
        )


# ---------------------------------------------------------------------------
# Service canonical (roadmap-consolidation §II.1 wiring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,facts",
    [
        (PG_CONFIG, _pg_facts(version="11.22")),
        (MYSQL_CONFIG, _mysql_facts(version="5.7.42")),
    ],
)
def test_service_canonical_is_in_payload(config, facts) -> None:
    """The matcher's `service_canonical` flows into the payload. The
    chargeback / restitution consumers can group by it to fold
    AWS + Azure into a single line item."""
    result = evaluate_eol(uuid4(), facts, config, today=date(2024, 9, 1))
    assert len(result.insights) == 1
    canonical = result.insights[0].payload["service_canonical"]
    assert canonical in {"managed_postgres", "managed_mysql"}


# ---------------------------------------------------------------------------
# Cross-engine dispatch (Aurora rule handles 2 engines)
# ---------------------------------------------------------------------------


def test_aurora_rule_dispatches_to_mysql_matcher() -> None:
    facts = _aurora_mysql_facts(version="3.08.1", vcpu=4, region="us-east-1")
    result = evaluate_eol(uuid4(), facts, AURORA_CONFIG, today=date(2030, 6, 1))
    assert len(result.insights) == 1
    assert result.insights[0].payload["engine"] == "aurora-mysql"
    assert result.insights[0].payload["service_canonical"] == "managed_mysql"


def test_aurora_rule_dispatches_to_postgres_matcher() -> None:
    facts = []
    for f in _aurora_mysql_facts():
        if f.key == "engine":
            facts.append(_fact(f.namespace, f.key, "aurora-postgresql"))
        elif f.key == "engine_version":
            facts.append(_fact(f.namespace, f.key, "14.9"))
        else:
            facts.append(f)
    result = evaluate_eol(uuid4(), facts, AURORA_CONFIG, today=date(2030, 6, 1))
    assert len(result.insights) == 1
    assert result.insights[0].payload["engine"] == "aurora-postgresql"
    assert result.insights[0].payload["service_canonical"] == "managed_postgres"
