"""Property-based test for the runner idempotence invariant (§IV.1).

The 4th property of §IV.1 in the roadmap: "idempotence worker
(rejouer un job = état identique)". The example tests
`test_ebs_unattached.py::test_run_ebs_unattached_replaces_previous_insights`
and `test_snapshot_orphan.py::test_run_snapshot_orphan_replaces_previous_insights`
pin the specific cases (1 resource, 3 consecutive runs, the
insight count is 1). This file pins the SHAPE: ∀ a set of
resources with facts that match the rule's NO_MATCH
predicate, the per-rule insight count is STABLE across
`run_resource_rule` re-runs (the delete-and-replace per rule
prevents duplicate insights from accumulating).

The property is scoped to one rule (ebs_unattached) because
each V1 rule has its own NO_MATCH setup (gp2 volume vs orphan
snapshot vs EOL'd engine). Pinning the SHAPE on one rule is
enough to catch the class of bug the example tests are after
("a refactor breaks delete-and-replace" or "a refactor changes
the rule's ID generation and creates duplicate insights").
The other rules are covered by their own example tests.

This test exercises the FULL runner (not just the rule
resolver): the source_run scope, the per-resource fact
loading, the rule evaluation, and the post-run delete-and-
replace. The point of the property is the END-TO-END
idempotence, not just the resolver's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from constat_api.collectors import aws as aws_collector
from constat_api.orm import AccountORM, FactORM, InsightORM, ResourceORM
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_aws_ec2.collector import (
    SOURCE_NAME,
    VOLUME_RESOURCE_TYPE,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A volume is (volume_id, size_gb, volume_type). We constrain to
# types the V1 EBS catalog knows (`gp2`, `gp3`, `io1`, `standard`,
# `st1`, `sc1`) and to "available" state so the ebs_unattached
# rule's NO_MATCH predicate matches (it only emits insights for
# `state == "available"`).
VOLUME = st.tuples(
    st.text(min_size=1, max_size=8).filter(lambda s: s.strip() and "\x00" not in s),
    st.integers(min_value=10, max_value=10_000),  # size_gb (>= 10 to be > the gp2 noise threshold of $0.50)
    st.sampled_from(["gp2", "gp3", "gp2", "standard", "io1"]),  # gp2 weighted
)
VOLUME_SET = st.lists(VOLUME, min_size=1, max_size=8)
N_RUNS = st.integers(min_value=2, max_value=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_all(session: Session) -> None:
    """Wipe the relevant tables at the start of each example.

    Hypothesis runs the test function with many generated inputs
    in a single pytest fixture scope, so a function-scoped
    `session` would accumulate rows across examples."""
    session.rollback()
    # Delete in FK order (insights, facts reference resources).
    session.execute(text("DELETE FROM insights"))
    session.execute(text("DELETE FROM inconclusive"))
    session.execute(text("DELETE FROM facts"))
    session.execute(text("DELETE FROM observations"))
    session.execute(text("DELETE FROM source_runs"))
    session.execute(text("DELETE FROM resources"))
    session.execute(text("DELETE FROM accounts"))
    session.commit()
    session.expire_all()


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="idempotence-prop")
    session.add(acc)
    session.commit()
    return acc


def _seed_available_volume(
    session: Session,
    acc: AccountORM,
    *,
    volume_id: str,
    size_gb: int,
    volume_type: str,
    region: str = "eu-west-1",
) -> ResourceORM:
    """Create one available EBS volume with all the facts the
    ebs_unattached rule reads (state, size_gb, volume_type,
    region). The state is always 'available' so the rule
    matches (NO_MATCH would be state != 'available')."""
    now = datetime(2026, 7, 18, tzinfo=UTC)
    res = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region=region,
        resource_type=VOLUME_RESOURCE_TYPE,
        native_id=volume_id,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(res)
    session.commit()
    session.refresh(res)
    # The 4 facts the ebs_unattached rule reads. We write them
    # directly as FactORM rows (avoiding the Pydantic-shaped
    # volume_to_facts → ORM conversion path) so the test stays
    # close to the DB layer the runner actually reads.
    facts_by_key = {
        ("aws.ec2.volume", "state"): "available",
        ("aws.ec2.volume", "size_gb"): size_gb,
        ("aws.ec2.volume", "volume_type"): volume_type,
        ("aws.ec2.volume", "region"): region,
    }
    for (namespace, key), value in facts_by_key.items():
        session.add(
            FactORM(
                tenant_id=DEFAULT_TENANT_ID,
                resource_id=res.id,
                account_id=str(acc.id),
                namespace=namespace,
                key=key,
                value=value,
                value_state="KNOWN",
                source=SOURCE_NAME,
                observed_at=now,
            )
        )
    session.commit()
    return res


def _seed_ec2_scope_proof(session: Session, acc: AccountORM, region: str = "eu-west-1") -> None:
    """Create a successful source_run in the (account, region,
    AWS::EC2::Volume, aws_ec2) scope. The runner's scope check
    uses this to decide MATCH/INCONCLUSIVE; without it, every
    resource goes INCONCLUSIVE."""
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region=region,
        resource_type=VOLUME_RESOURCE_TYPE,
        source=SOURCE_NAME,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()


def _patch_ebs_scan_empty() -> Any:
    """The ebs_unattached rule is invoked via `run_ebs_unattached`
    which calls `run_resource_rule`. The runner's drain invokes
    boto3 collectors — we don't need boto3 at all in this test
    (we seed facts directly), but the runner chain might still
    call into the worker. Patch the boto3-level call paths to
    no-op so the runner doesn't try to talk to AWS."""

    def _empty_iter(_session: Any, _regions: Any) -> Any:
        return iter([])

    return [
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch.dict(
            aws_collector.JOB_REGISTRY,
            {
                key: (
                    job
                    if key == "rds"
                    else dataclasses.replace(job, scan_fn=_empty_iter)
                )
                for key, job in aws_collector.JOB_REGISTRY.items()
            },
        ),
    ]


import dataclasses  # noqa: E402  (imported here because _patch_ebs_scan_empty uses it; keep top of file clean)

# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(volumes=VOLUME_SET, n_runs=N_RUNS)
def test_run_ebs_unattached_is_idempotent_across_n_drains(
    session: Session, volumes: list[tuple[str, int, str]], n_runs: int
) -> None:
    """∀ a set of available EBS volumes, ∀ a re-run count — the
    ebs_unattached insight count is STABLE across N consecutive
    `run_ebs_unattached` calls.

    This is the runner-level delete-and-replace contract: the
    rule re-evaluates each resource and writes one insight per
    matching resource, but the post-run cleanup deletes the
    rule's prior insights before the new ones land. A refactor
    that breaks this (e.g., a missing `delete_insights_for_rule`
    call, or a rule-name change that makes the cleanup a no-op)
    fails this property on a wider input space than the example
    tests' single-resource, 3-runs scenario.
    """
    from constat_api.insights.runner import run_ebs_unattached

    _truncate_all(session)
    acc = _seed_account(session)

    # Deduplicate volume_ids (the strategy can produce duplicates)
    seen: set[str] = set()
    seeded: list[ResourceORM] = []
    for volume_id, size_gb, volume_type in volumes:
        if volume_id in seen:
            continue
        seen.add(volume_id)
        seeded.append(
            _seed_available_volume(
                session, acc, volume_id=volume_id, size_gb=size_gb, volume_type=volume_type
            )
        )
    _seed_ec2_scope_proof(session, acc)

    patches = _patch_ebs_scan_empty()
    with patches[0], patches[1]:
        # First run — establish the baseline insight count.
        result_first = run_ebs_unattached(session)
        first_count = session.query(InsightORM).filter(
            InsightORM.rule_name == "ebs_unattached"
        ).count()
        assert result_first.insights_emitted == first_count, (
            f"function returned {result_first.insights_emitted} but DB has "
            f"{first_count} insights — runner inserts should match its return value"
        )

        # N-1 more runs — the count must not grow.
        for run_idx in range(n_runs - 1):
            run_ebs_unattached(session)
            current_count = session.query(InsightORM).filter(
                InsightORM.rule_name == "ebs_unattached"
            ).count()
            assert current_count == first_count, (
                f"after {run_idx + 2} runs, insight count is {current_count} "
                f"(expected stable {first_count}) — delete-and-replace broke"
            )

        # The per-(rule_name, resource_id) cardinality is at most
        # 1: a re-run must not create a duplicate insight for the
        # same resource (which would double-count in the
        # restitution's "total waste" column).
        per_resource_count = (
            session.query(InsightORM.resource_id)
            .filter(InsightORM.rule_name == "ebs_unattached")
            .group_by(InsightORM.resource_id)
            .having(
                # 2+ insights for the same (rule, resource) is a bug
                # (the "duplicate insight" failure mode the
                # delete-and-replace was added to prevent).
                # Use a raw expression: HAVING COUNT(*) > 1.
                text("COUNT(*) > 1")
            )
            .count()
        )
        assert per_resource_count == 0, (
            f"after {n_runs} runs, {per_resource_count} resources have "
            f"more than 1 ebs_unattached insight — delete-and-replace is broken"
        )
