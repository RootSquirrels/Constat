"""Property-based test for the retirement invariant (§IV.1).

The 3rd property of §IV.1 in the roadmap: "retirement (jamais
sans 2 runs complets)". The example tests in
`test_resources_repository.py` pin the specific cases (1 miss
is not proof, 0 runs is not proof, 2 misses retire, 3 misses
stay retired). This file pins the SHAPE: ∀ a sequence of source
runs and a set of resources with their `last_seen_at`, a resource
is retired iff it was missed by the 2 most recent successful
source runs in the scope.

F-08 is the underlying rule: a resource is retired only after
CONSECUTIVE_SCANS_FOR_RETIREMENT (= 2) successful scans in the
same scope both missed it. One scan is not proof of deletion
(F-01 failure mode: a transient collection gap would otherwise
"delete" live resources). The property encodes the rule in a
form that catches a future refactor that:
- drops the ≥ 2 successful runs check (would over-retire)
- forgets the `started_at is not None` filter (would crash on a
  run that was started but never finished)
- changes the comparison from `<` to `<=` (would retire resources
  that were observed in the second-most-recent run, not "missed
  in BOTH")
- or any other off-by-one on the 2-consecutive-miss invariant
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from constat_api.orm import AccountORM, ResourceORM, SourceRunORM
from constat_api.repositories import resources as resources_repo
from constat_api.repositories import source_runs as source_runs_repo
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session

# Strategy: a source_run is (offset_minutes_from_now, status).
# `offset_minutes_from_now` is the time of `started_at` relative
# to NOW (negative = past). Status is "success" or any other.
# We constrain to 0..6 successful runs and 0..3 failed runs
# interleaved; the function ignores non-success runs, so the
# distribution of failures mostly matters for the "must have 2
# successful runs" branch.
SUCCESS_RUN = st.tuples(
    st.integers(min_value=-720, max_value=-1),  # started 12h..1min ago
    st.just("success"),
)
FAILED_RUN = st.tuples(
    st.integers(min_value=-720, max_value=-1),
    st.sampled_from(["failed", "partial"]),
)
RUN = st.one_of(SUCCESS_RUN, FAILED_RUN)
RUN_SEQ = st.lists(RUN, min_size=0, max_size=8)


# A resource: native_id, last_seen_offset (negative = past).
# We constrain last_seen to a range wide enough to span the
# full run sequence the strategy generates.
RESOURCE = st.tuples(
    st.text(min_size=1, max_size=16).filter(lambda s: s.strip() and "\x00" not in s),
    st.integers(min_value=-1500, max_value=10),  # last_seen offset from now
)
RESOURCE_SET = st.lists(RESOURCE, min_size=0, max_size=8)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _truncate_all(session: Session) -> None:
    """Wipe resources + source_runs at the start of each example.

    Hypothesis runs the test function with many generated inputs
    in a single pytest fixture scope, so a function-scoped `session`
    fixture would accumulate rows across examples. The conftest's
    `engine` fixture (StaticPool) keeps a single shared connection
    — we DELETE both tables and the foreign-key-dependent rows,
    then expire so the next upsert doesn't reattach a stale
    identity-map reference."""
    session.rollback()
    # order matters: resource.delete_first because source_runs has
    # a FK to account (not resource). resources is leaf here.
    session.execute(text("DELETE FROM source_runs"))
    session.execute(text("DELETE FROM resources"))
    session.execute(text("DELETE FROM accounts"))
    session.commit()
    session.expire_all()


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="retirement-prop")
    session.add(acc)
    session.commit()
    return acc


def _seed_run(
    session: Session,
    acc: AccountORM,
    *,
    started_offset_min: int,
    status: str,
) -> SourceRunORM:
    """Create a SourceRun with `started_at` = NOW + offset (negative
    = in the past), and finish it. We set `started_at` AFTER
    creation (rather than letting `start_run` use NOW) so the
    property can order the runs by `started_at` independently of
    real wall-clock time."""
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    run.started_at = _now() + timedelta(minutes=started_offset_min)
    source_runs_repo.finish_run(session, run, status=status, resources_found=0)
    session.commit()
    return run


def _seed_resource(
    session: Session,
    acc: AccountORM,
    *,
    native_id: str,
    last_seen_offset_min: int,
) -> ResourceORM:
    """Create a resource, then backdate `last_seen_at` to the
    requested offset. The resource starts active (`retired_at`
    is NULL by default)."""
    r = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id=native_id,
    )
    r.last_seen_at = _now() + timedelta(minutes=last_seen_offset_min)
    session.commit()
    session.refresh(r)
    return r


def _dedup_resources(
    resources: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    """Match the seeding-side dedup: same native_id twice in the
    strategy output is suffixed (`0`, `0_2`, `0_3`, ...) so the
    DB-level unique constraint is not violated. Both the seed
    loop and the expected-set computation MUST apply the same
    dedup, otherwise the property's 'expected' set diverges
    from the 'actual' set on duplicate native_ids."""
    seen: set[str] = set()
    result: list[tuple[str, int]] = []
    for native_id, last_seen_offset in resources:
        unique = native_id
        suffix = 1
        while unique in seen:
            suffix += 1
            unique = f"{native_id}_{suffix}"
        seen.add(unique)
        result.append((unique, last_seen_offset))
    return result


def _expected_retired_set(
    *,
    runs: list[tuple[int, str]],
    resources: list[tuple[str, int]],
) -> set[str]:
    """Compute the expected set of retired native_ids under the
    F-08 invariant. The property's "ground truth" — what the
    function SHOULD retire, against which we assert the
    function's actual return matches.

    Rule (F-08):
    1. Take the 2 most recent SUCCESSFUL runs (by `started_at`,
       newest first). The function ignores non-success runs.
    2. If fewer than 2 exist, NO retirement (one scan is not
       proof of deletion).
    3. For each active resource, it is retired iff
       `last_seen_at < min(started_at of the 2 runs)` — i.e.
       it was missed in BOTH.
    """
    deduped = _dedup_resources(resources)
    # 1. Sort runs newest-first by started_at
    successful = sorted(
        [(offset, status) for offset, status in runs if status == "success"],
        key=lambda t: t[0],
        reverse=True,
    )[:2]
    if len(successful) < 2:
        return set()
    # 2. The OLDEST of the two is the last in newest-first order.
    #    Its `started_at` is the threshold: anything before it
    #    was missed in BOTH runs.
    threshold_offset = successful[-1][0]
    threshold = _now() + timedelta(minutes=threshold_offset)
    # 3. Each resource with last_seen < threshold is retired.
    return {
        native_id
        for native_id, last_seen_offset in deduped
        if (_now() + timedelta(minutes=last_seen_offset)) < threshold
    }


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(runs_seq=RUN_SEQ, resource_set=RESOURCE_SET)
def test_retire_stale_resources_matches_f08_invariant(
    session: Session, runs_seq: list[tuple[int, str]], resource_set: list[tuple[str, int]]
) -> None:
    """∀ runs, resources — `retire_stale_resources` retires EXACTLY
    the set of resources the F-08 invariant says it should.

    Off-by-one regressions: a future refactor that uses `≤` instead
    of `<` (would over-retire a resource seen in the second-most-
    recent run), or drops the `started_at is not None` check
    (would crash), or counts failed runs (would mis-fire after a
    transient collection gap), is caught here on a wider input
    space than the example tests."""
    _truncate_all(session)
    acc = _seed_account(session)

    # Seed runs in the order the strategy generated them (which
    # is NOT necessarily time-ordered — we re-sort by started_at
    # inside the function and the property expects the same).
    for offset, status in runs_seq:
        _seed_run(session, acc, started_offset_min=offset, status=status)

    # Seed resources
    seeded: dict[str, ResourceORM] = {}
    for native_id, last_seen_offset in _dedup_resources(resource_set):
        seeded[native_id] = _seed_resource(
            session, acc, native_id=native_id, last_seen_offset_min=last_seen_offset
        )

    expected = _expected_retired_set(runs=runs_seq, resources=resource_set)

    # Act
    actual_count = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()

    # The function returns the COUNT; the actual retired set is
    # recoverable from the DB after the call.
    actual_retired = {
        r.native_id
        for r in session.query(ResourceORM).filter(ResourceORM.retired_at.is_not(None)).all()
    }

    # 1. The count returned matches the DB state.
    assert actual_count == len(actual_retired), (
        f"function returned {actual_count} but DB has {len(actual_retired)} retired rows"
    )
    # 2. The actual retired set is EXACTLY the F-08 expected set.
    assert actual_retired == expected, (
        f"expected retired: {sorted(expected)}, "
        f"actually retired: {sorted(actual_retired)}, "
        f"runs: {runs_seq}, resources: {resource_set}"
    )
    # 3. Idempotence (the function is a pure function over the
    # current state): a second call with no new state retires 0
    # more rows.
    second = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert second == 0, (
        f"second call retired {second} more rows — the function is "
        f"not idempotent over the same DB state"
    )
