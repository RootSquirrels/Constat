"""Write-path tenant stamping (roadmap 3.1 follow-up): every row created
under a session bound to a non-default tenant must carry THAT tenant's
id, not the ORM default.

Why: on Postgres the RLS WITH CHECK compares the inserted row's
tenant_id to the session GUC. A row stamped with DEFAULT_TENANT_ID
under tenant A's GUC is rejected fail-closed. Stamping is Python-side
(`tenant_or_default(session)`), so these tests run on sqlite — no RLS
needed. The unbound-session case must keep the historical
DEFAULT_TENANT_ID fallback (CLI / dev back-compat).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from constat_api.audit import AuditLogger, record_event
from constat_api.orm import (
    AuditEventORM,
    FactORM,
    FocusChargeORM,
    FocusChargeTagORM,
    InconclusiveORM,
    InsightEventORM,
    InsightORM,
    InsightRunORM,
    ObservationORM,
    ResourceORM,
    RetentionPolicyORM,
    SourceRunORM,
)
from constat_api.pii import PIIClassifier
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import inconclusive as inconclusive_repo
from constat_api.repositories import insight_events as insight_events_repo
from constat_api.repositories import insights as insights_repo
from constat_api.repositories import observations as observations_repo
from constat_api.repositories import resources as resources_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.retention import DEFAULT_RETENTION_DAYS, seed_default_policies
from constat_api.settings import DEFAULT_TENANT_ID
from constat_api.tenant import bind_tenant, tenant_or_default
from constat_core.models import Fact, Inconclusive, Insight, Observation, Severity, ValueState
from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy import select
from sqlalchemy.orm import Session

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
NOW = datetime(2026, 7, 19, tzinfo=UTC)


@pytest.fixture
def bound(session: Session) -> Session:
    """The shared sqlite session bound to a non-default tenant."""
    bind_tenant(session, TENANT)
    return session


def _only(session: Session, cls: type) -> object:
    return session.execute(select(cls)).scalars().one()


def _make_insight() -> Insight:
    return Insight(
        rule_name="rds_eol",
        resource_id=uuid4(),
        severity=Severity.WARNING,
        title="t",
        payload={},
        computed_at=NOW,
    )


def _make_agg() -> AggregatedFocusCharge:
    return AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("90"),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}],
        per_row_tag_dicts=[{"Application": "web"}],
    )


class TestHelper:
    def test_bound_session_returns_its_tenant(self, bound: Session) -> None:
        assert tenant_or_default(bound) == TENANT

    def test_unbound_session_falls_back_to_default(self, session: Session) -> None:
        assert tenant_or_default(session) == DEFAULT_TENANT_ID


class TestRepositoryWrites:
    def test_insert_facts(self, bound: Session) -> None:
        facts_repo.insert_facts(
            bound,
            [
                Fact(
                    resource_id=uuid4(),
                    namespace="aws.rds",
                    key="engine",
                    value="postgres",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=NOW,
                )
            ],
        )
        assert _only(bound, FactORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_upsert_facts(self, bound: Session) -> None:
        facts_repo.upsert_facts(
            bound,
            [
                Fact(
                    resource_id=uuid4(),
                    namespace="aws.rds",
                    key="engine",
                    value="postgres",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=NOW,
                )
            ],
        )
        assert _only(bound, FactORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_insert_observation(self, bound: Session) -> None:
        observations_repo.insert_observation(
            bound,
            Observation(resource_id=uuid4(), source="aws_rds", observed_at=NOW, payload={}),
        )
        assert _only(bound, ObservationORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_insert_insight(self, bound: Session) -> None:
        insights_repo.insert_insight(bound, _make_insight())
        assert _only(bound, InsightORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_insert_inconclusive(self, bound: Session) -> None:
        inconclusive_repo.insert_inconclusive(
            bound,
            Inconclusive(
                rule_name="rds_eol",
                resource_id=uuid4(),
                missing_facts=["aws.rds.engine"],
                reason="missing_facts",
                computed_at=NOW,
            ),
        )
        assert _only(bound, InconclusiveORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_upsert_resource(self, bound: Session) -> None:
        resources_repo.upsert_resource(
            bound,
            uuid4(),
            region="eu-west-1",
            resource_type="rds",
            native_id="db-1",
        )
        assert _only(bound, ResourceORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_start_run(self, bound: Session) -> None:
        run = source_runs_repo.start_run(
            bound,
            account_id=uuid4(),
            region="eu-west-1",
            resource_type="rds",
            source="aws_rds",
        )
        assert run is not None
        assert run.tenant_id == TENANT

    def test_upsert_aggregated_stamps_charge_and_tags(self, bound: Session) -> None:
        account = accounts_repo.get_or_create(bound, "111111111111")
        focus_charges_repo.upsert_aggregated(bound, account.id, [_make_agg()])
        assert _only(bound, FocusChargeORM).tenant_id == TENANT  # type: ignore[attr-defined]
        assert _only(bound, FocusChargeTagORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_diff_and_record_events(self, bound: Session) -> None:
        insights_repo.insert_insight(bound, _make_insight())
        appeared, resolved = insight_events_repo.diff_and_record_events(
            bound, rule_name="rds_eol", previous={}, insight_run_id=None
        )
        assert (appeared, resolved) == (1, 0)
        assert _only(bound, InsightEventORM).tenant_id == TENANT  # type: ignore[attr-defined]


class TestRunnerAndServices:
    def test_run_resource_rule_stamps_insight_run(self, bound: Session) -> None:
        from constat_api.insights.runner import run_resource_rule

        # No resources: the rule scans nothing but still writes its run row.
        result = run_resource_rule(bound, "rds_eol")
        assert result.errors == []
        assert _only(bound, InsightRunORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_run_chargeback_stamps_insight_run_and_audit(self, bound: Session) -> None:
        from constat_api.insights.runner import run_chargeback

        # No FOCUS charges: zero insights, but the run row and the audit
        # event (system actor, no explicit tenant) are still written.
        result = run_chargeback(bound)
        assert result.errors == []
        assert _only(bound, InsightRunORM).tenant_id == TENANT  # type: ignore[attr-defined]
        assert _only(bound, AuditEventORM).tenant_id == TENANT  # type: ignore[attr-defined]

    def test_seed_default_policies(self, bound: Session) -> None:
        assert seed_default_policies(bound) == len(DEFAULT_RETENTION_DAYS)
        tenants = {row.tenant_id for row in bound.execute(select(RetentionPolicyORM)).scalars()}
        assert tenants == {TENANT}

    def test_pii_classifier(self, bound: Session) -> None:
        row = PIIClassifier(bound).record(
            resource_type="account",
            resource_id="111111111111",
            field_name="aws_account_id",
            value="111111111111",
        )
        assert row is not None
        assert row.tenant_id == TENANT

    def test_audit_logger_falls_back_to_session_tenant(self, bound: Session) -> None:
        event = AuditLogger(bound).record(action="test_action")
        assert event.tenant_id == TENANT

    def test_audit_explicit_tenant_wins(self, bound: Session) -> None:
        other = uuid4()
        event = record_event(bound, action="test_action", tenant_id=other)
        assert event.tenant_id == other


class TestUnboundFallback:
    """An unbound session (CLI / dev) keeps the DEFAULT_TENANT_ID behavior."""

    def test_repositories_stamp_default(self, session: Session) -> None:
        facts_repo.insert_facts(
            session,
            [
                Fact(
                    resource_id=uuid4(),
                    namespace="aws.rds",
                    key="engine",
                    value="postgres",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=NOW,
                )
            ],
        )
        insights_repo.insert_insight(session, _make_insight())
        source_runs_repo.start_run(
            session,
            account_id=uuid4(),
            region="eu-west-1",
            resource_type="rds",
            source="aws_rds",
        )
        assert _only(session, FactORM).tenant_id == DEFAULT_TENANT_ID  # type: ignore[attr-defined]
        assert _only(session, InsightORM).tenant_id == DEFAULT_TENANT_ID  # type: ignore[attr-defined]
        assert _only(session, SourceRunORM).tenant_id == DEFAULT_TENANT_ID  # type: ignore[attr-defined]

    def test_services_stamp_default(self, session: Session) -> None:
        from constat_api.insights.runner import run_resource_rule

        run_resource_rule(session, "rds_eol")
        seed_default_policies(session)
        event = AuditLogger(session).record(action="test_action")
        assert _only(session, InsightRunORM).tenant_id == DEFAULT_TENANT_ID  # type: ignore[attr-defined]
        assert event.tenant_id == DEFAULT_TENANT_ID
        tenants = {row.tenant_id for row in session.execute(select(RetentionPolicyORM)).scalars()}
        assert tenants == {DEFAULT_TENANT_ID}
