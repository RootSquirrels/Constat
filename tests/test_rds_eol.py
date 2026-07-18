"""Tests for the RDS PostgreSQL Extended Support insight."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from constat_core.models import Fact, Severity, ValueState
from constat_rds_eol.resolver import RULE_NAME, evaluate


def _fact(
    namespace: str,
    key: str,
    value,
    account_id: str = "111111111111",
    value_state: ValueState = ValueState.KNOWN,
) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id=account_id,
        namespace=namespace,
        key=key,
        value=value,
        value_state=value_state,
        source="test",
        observed_at=date.today(),
    )


def _pg_facts(major_version: int, vcpu: int = 4) -> list[Fact]:
    return [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", f"{major_version}.7"),
        _fact("aws.rds", "vcpu", vcpu),
    ]


def test_postgres_14_within_90_days_emits_warning():
    # PG 14 EOL = 2026-02-28. From 2025-12-01, that's 89 days.
    resource_id = uuid4()
    insights = evaluate(resource_id, _pg_facts(14), today=date(2025, 12, 1))

    assert len(insights) == 1
    insight = insights[0]
    assert insight.rule_name == RULE_NAME
    assert insight.resource_id == resource_id
    assert insight.severity == Severity.WARNING
    assert insight.payload["days_to_eol"] == 89
    assert insight.payload["major_version"] == 14
    # 4 vCPU * 0.20 USD/vCPU-hour * 730 hours = 584 USD
    assert insight.payload["ext_support_monthly_usd_estimate"] == 584.0


def test_postgres_11_past_eol_emits_critical():
    # PG 11 EOL was 2024-02-29. Today is 2026-07-18, well past.
    insights = evaluate(uuid4(), _pg_facts(11), today=date(2026, 7, 18))

    assert len(insights) == 1
    assert insights[0].severity == Severity.CRITICAL
    assert insights[0].payload["days_to_eol"] < 0


def test_postgres_15_too_far_emits_nothing():
    # PG 15 EOL = 2027-02-27. From 2026-07-18, that's 224 days.
    insights = evaluate(uuid4(), _pg_facts(15), today=date(2026, 7, 18))
    assert insights == []


def test_postgres_16_emits_nothing():
    # 16+ are LTS in our catalog; no EOL date means no alert.
    insights = evaluate(uuid4(), _pg_facts(16), today=date(2026, 7, 18))
    assert insights == []


def test_non_postgres_engine_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", "mysql"),
        _fact("aws.rds", "engine_version", "8.0.32"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)) == []


def test_unknown_engine_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", None, value_state=ValueState.UNKNOWN),
        _fact("aws.rds", "engine_version", "14.7"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)) == []


def test_unknown_vcpu_emits_nothing():
    # Without vCPU we can't price Extended Support; emit nothing (not UNKNOWN).
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "14.7"),
        _fact("aws.rds", "vcpu", None, value_state=ValueState.UNKNOWN),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)) == []


def test_malformed_version_emits_nothing():
    facts = [
        _fact("aws.rds", "engine", "postgres"),
        _fact("aws.rds", "engine_version", "banana"),
        _fact("aws.rds", "vcpu", 4),
    ]
    assert evaluate(uuid4(), facts, today=date(2026, 7, 18)) == []


def test_empty_facts_emits_nothing():
    assert evaluate(uuid4(), [], today=date(2026, 7, 18)) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
