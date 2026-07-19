"""Proof tests for ack carry-over across delete-and-replace (point 2 of
the strategic plan, narrow fix).

The runner's delete-and-replace wipes the `insights` table each run,
which also wipes the operator's `ack_status` / `ack_at` / `ack_by`. The
lifecycle log (insight_events) preserves appeared/resolved history via
fingerprint, but the operator's *decision* on the current gap was lost
on every re-run.

The fix: snapshot acks by `stable_id` (the gap identity, not the title)
before the delete, and re-apply them to the fresh rows after the insert.
The stable_id is `(rule_name, resource_id)` for resource rules, and
`(account_id, service, period_label, tag_key, tag_value)` for chargeback
(read from the payload).

These tests prove:
- the carry-over happens when the underlying gap is unchanged, even
  though the title (and therefore the fingerprint) has changed.
- the carry-over does NOT happen when the gap genuinely closes (no
  fresh insight to carry to) or when a different gap appears.
- the carry-over is per-tag for chargeback (ack on one tag bucket
  does not bleed into another).
- the snapshot only captures acked rows (unacked rows have nothing
  to carry; capturing them would be wasted work).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from constat_api.insights.runner import run_chargeback, run_resource_rule
from constat_api.orm import AccountORM, InsightEventORM, InsightORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import insights as insights_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, ValueState
from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy.orm import Session

# Source name for RDS scans, mirrored from the runner's RULE_SOURCES.
_RDS_SOURCE = "aws_rds"


# ---- helpers ---------------------------------------------------------------


def _bootstrap_pg(
    session: Session,
    *,
    major: str,
    version: str,
    native_id: str = "arn:aws:rds:eu-west-1:111111111111:db:pg",
) -> ResourceORM:
    """Account + resource + scope proof + facts for a PG instance."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id=native_id,
    )
    session.add(resource)
    session.commit()
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=_RDS_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()
    _set_pg_facts(session, resource, acc.id, run.id, major=major, version=version)
    return resource


def _set_pg_facts(
    session: Session,
    resource: ResourceORM,
    account_id,
    run_id,
    *,
    major: str,
    version: str,
) -> None:
    facts_repo.upsert_facts(
        session,
        [
            Fact(
                resource_id=resource.id,
                account_id=str(account_id),
                namespace="aws.rds",
                key=key,
                value=value,
                value_state=ValueState.KNOWN,
                source=_RDS_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
            for key, value in [
                ("engine", "postgres"),
                ("engine_version", version),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 2),
                ("region", "eu-west-1"),
            ]
        ],
        source_run_id=run_id,
    )
    session.commit()


def _ack(session: Session, insight_id, *, status: str = "acknowledged", by: str = "ops") -> None:
    """PATCH an insight's ack via the repo (mirrors the HTTP path)."""
    result = insights_repo.update_ack(session, insight_id, ack_status=status, ack_by=by)
    assert result is not None
    session.commit()


def _current_insight(session: Session, rule_name: str) -> InsightORM:
    return session.query(InsightORM).filter_by(rule_name=rule_name).one()


def _events(session: Session) -> list[InsightEventORM]:
    return (
        session.query(InsightEventORM)
        .order_by(InsightEventORM.occurred_at, InsightEventORM.id)
        .all()
    )


# ---- the main bug: ack lost on title-changing re-run ----------------------


def test_ack_survives_rerun_with_changed_days_to_eol(session: Session) -> None:
    """The original bug: PG13 with days_to_eol=89, ack, re-run a day
    later (days_to_eol=88). The title embeds the countdown, so the
    fingerprint changes. Without stable_id carry-over, the ack is lost
    on every daily re-run. With it, the operator's decision survives.
    """
    _bootstrap_pg(session, major="13", version="13.13")

    first = run_resource_rule(session, "rds_eol", today=date(2025, 12, 1))
    assert first.insights_emitted == 1
    insight = _current_insight(session, "rds_eol")
    assert "in 89 days" in insight.title, (
        "test setup: PG13 eol=2026-02-28, today=2025-12-01 should produce "
        f"a dynamic 'in N days' title, got {insight.title!r}"
    )
    title_day1 = insight.title
    _ack(session, insight.id, status="in_progress", by="platform-team")

    # Re-run a day later: days_to_eol drops by 1, the title changes.
    # The lifecycle log records appeared + resolved churn (the title's
    # instability is a separate problem we're not solving here). We
    # don't pin the exact fingerprint here — what matters is the
    # carry-over.
    second = run_resource_rule(session, "rds_eol", today=date(2025, 12, 2))
    assert second.insights_emitted == 1
    refreshed = _current_insight(session, "rds_eol")
    assert refreshed.title != title_day1, "test setup: the title must change between runs"
    assert refreshed.id != insight.id, "test setup: delete-and-replace must produce a new row id"

    # The fix: same stable_id, ack carries despite the title change.
    assert refreshed.ack_status == "in_progress"
    assert refreshed.ack_by == "platform-team"
    assert refreshed.ack_at is not None


def test_ack_survives_phase_transition(session: Session) -> None:
    """The EOL phase changes (days_to_eol=1 -> days_to_eol=0): the title
    format switches from 'in 1 day' to 'is in Extended Support'. Both
    formats embed no dynamic number, but the string itself is different
    and so is the fingerprint. The gap is the same resource, the ack
    carries.
    """
    _bootstrap_pg(session, major="13", version="13.13")

    first = run_resource_rule(session, "rds_eol", today=date(2026, 2, 27))
    assert first.insights_emitted == 1
    first_title = _current_insight(session, "rds_eol").title
    assert "in 1 day" in first_title, f"setup: {first_title!r}"
    _ack(session, _current_insight(session, "rds_eol").id, status="acknowledged", by="secops")

    # Cross the EOL boundary. days_to_eol goes from 1 to 0; the resolver
    # switches to the 'is in Extended Support' branch. Title format
    # changes; payload amount changes (same tier); stable_id unchanged.
    second = run_resource_rule(session, "rds_eol", today=date(2026, 2, 28))
    assert second.insights_emitted == 1
    refreshed = _current_insight(session, "rds_eol")
    assert "Extended Support" in refreshed.title
    assert refreshed.title != first_title
    assert refreshed.ack_status == "acknowledged"
    assert refreshed.ack_by == "secops"


# ---- negative cases: ack is NOT carried when it shouldn't be --------------


def test_ack_lost_when_gap_resolves_then_reappears(session: Session) -> None:
    """The gap genuinely closes (PG11 -> PG14): the old insight is
    gone, no fresh insight to carry the ack to. The lifecycle log
    records the resolution with the old amount.

    When the gap reappears (back to PG11), the new insight is a
    genuinely new gap and does NOT inherit the old ack — the operator
    must re-triage.
    """
    resource = _bootstrap_pg(session, major="11", version="11.22")
    first = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert first.insights_emitted == 1
    _ack(session, _current_insight(session, "rds_eol").id, status="dismissed")

    # Close the gap.
    _set_pg_facts(session, resource, resource.account_id, run_id=None, major="11", version="14.7")
    second = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert second.insights_emitted == 0
    assert session.query(InsightORM).filter_by(rule_name="rds_eol").count() == 0

    # Reopen the gap.
    _set_pg_facts(session, resource, resource.account_id, run_id=None, major="11", version="11.22")
    third = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert third.insights_emitted == 1
    reopened = _current_insight(session, "rds_eol")
    assert reopened.ack_status is None, "a reappearing gap is a new triage, not a carried decision"


def test_ack_survives_only_for_matching_stable_id(session: Session) -> None:
    """Two PG13 resources, one acked, one not. A re-run must carry
    the ack to its resource only — not bleed it to the sibling."""
    _bootstrap_pg(session, major="13", version="13.13", native_id="arn:pg-a")
    acc = session.query(AccountORM).one()
    sibling = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:pg-b",
    )
    session.add(sibling)
    session.commit()
    # Facts and scope proof for the sibling.
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=_RDS_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()
    _set_pg_facts(session, sibling, acc.id, run.id, major="13", version="13.13")

    # Use a today where both resources emit an insight (PG13, within 90d of EOL).
    run_resource_rule(session, "rds_eol", today=date(2025, 12, 1))
    rows = {
        r.resource_id: r for r in session.query(InsightORM).filter_by(rule_name="rds_eol").all()
    }
    assert len(rows) == 2
    # Pick one resource's insight; ack it.
    target_resource_id, target_insight = next(iter(rows.items()))
    _ack(session, target_insight.id, status="acknowledged", by="only-this-one")

    run_resource_rule(session, "rds_eol", today=date(2025, 12, 2))
    rows_after = {
        r.resource_id: r for r in session.query(InsightORM).filter_by(rule_name="rds_eol").all()
    }
    acked = [r for r in rows_after.values() if r.ack_status == "acknowledged"]
    unacked = [r for r in rows_after.values() if r.ack_status is None]
    assert len(acked) == 1, f"exactly one insight should carry the ack, got {len(acked)}"
    assert len(unacked) == 1
    assert acked[0].resource_id == target_resource_id
    assert acked[0].ack_by == "only-this-one"


# ---- the snapshot itself: only acked rows are captured ---------------------


def test_snapshot_only_captures_acked_rows(session: Session) -> None:
    """Sanity: the carry-over snapshot is acked rows only. Unacked
    rows are skipped (nothing to carry, and capturing them would be
    wasted work)."""
    _bootstrap_pg(session, major="11", version="11.22")
    run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))

    # No acks yet -> empty snapshot.
    assert insights_repo.snapshot_acks(session, "rds_eol") == {}

    # Ack one insight (the only one) -> snapshot has one entry.
    _ack(session, _current_insight(session, "rds_eol").id, status="in_progress")
    snap = insights_repo.snapshot_acks(session, "rds_eol")
    assert len(snap) == 1
    ack_status, ack_at, ack_by = next(iter(snap.values()))
    assert ack_status == "in_progress"
    assert ack_at is not None
    assert ack_by is not None


# ---- chargeback: tag-keyed carry-over --------------------------------------


def _seed_focus(
    session: Session,
    account_id,
    *,
    billed: str,
    amortized: str,
    period_start: date,
    period_end: date,
) -> None:
    """Insert one FOCUS charge so the next run_chargeback emits one drift insight."""
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=period_start,
        period_end=period_end,
        billed_cost=Decimal(billed),
        amortized_cost=Decimal(amortized),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[],
        per_row_tag_dicts=[],
    )
    focus_charges_repo.upsert_aggregated(session, account_id, [agg])
    session.commit()


def test_chargeback_ack_survives_drift_amount_change(session: Session) -> None:
    """Chargeback titles include the drift amount (dynamic). The
    stable_id is (account, service, period, tag). Re-running with new
    costs changes the drift, changes the title, changes the fingerprint
    — but the operator's ack on the (account, service, period) bucket
    must survive."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    _seed_focus(
        session,
        acc.id,
        billed="100",
        amortized="150",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
    )

    first = run_chargeback(session)
    assert first.insights_emitted == 1
    _ack(session, _current_insight(session, "chargeback").id, status="acknowledged", by="finops")

    # Drift amount changes; title changes; ack should still carry.
    _seed_focus(
        session,
        acc.id,
        billed="200",
        amortized="350",  # bigger drift
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
    )
    second = run_chargeback(session)
    assert second.insights_emitted == 1
    refreshed = _current_insight(session, "chargeback")
    assert refreshed.ack_status == "acknowledged"
    assert refreshed.ack_by == "finops"


def test_chargeback_ack_isolated_per_tag_value(session: Session) -> None:
    """Tag-keyed chargeback: one FOCUS bucket with two input rows carrying
    different tag values. Ack on the 'web' bucket must NOT carry to the
    'api' bucket — the stable_id includes tag_value for a reason.

    The data shape mirrors a real FOCUS ingest: one focus_charge row
    (dedup key = (account, service, period)) with two per-input-row
    tag dicts and parallel per-input-row costs (migration 0020).
    """
    acc = accounts_repo.get_or_create(session, "111111111111")

    multi_tag_charge = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("110"),
        amortized_cost=Decimal("220"),
        charge_count=2,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}, {"Application": "api"}],
        per_row_tag_dicts=[{"Application": "web"}, {"Application": "api"}],
        per_row_costs=[(Decimal("10"), Decimal("20")), (Decimal("100"), Decimal("200"))],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [multi_tag_charge])
    session.commit()

    first = run_chargeback(session, tag_key="Application")
    assert first.insights_emitted == 2, "one insight per tag_value"

    web_insight = next(
        r
        for r in session.query(InsightORM).filter_by(rule_name="chargeback").all()
        if "[Application=web]" in r.title
    )
    _ack(session, web_insight.id, status="acknowledged", by="finops-web")

    second = run_chargeback(session, tag_key="Application")
    assert second.insights_emitted == 2
    rows = {
        (r.payload.get("tag_value") or "UNTAGGED"): r
        for r in session.query(InsightORM).filter_by(rule_name="chargeback").all()
    }
    assert rows["web"].ack_status == "acknowledged", "web bucket ack must carry"
    assert rows["web"].ack_by == "finops-web"
    assert rows["api"].ack_status is None, "api bucket is a separate stable_id, no carry"
