"""Proof tests for the run_insights CLI `--all` mode (scheduler drift fix).

Committee finding: the scheduled ECS task hardcoded two `--rule` lines, so
4 of 6 registered rules never ran automatically — the same drift class as
the monetary extraction bug (a hardcoded list diverging from RUNNERS).

SRE-review follow-up (2026-07-19): the scheduled task no longer runs the
collector or the rules in-task at all — it ENQUEUES (cli.aws --enqueue-all
→ SQS → worker), and rule evaluation is chained by the worker when a
collect job completes (migration 0021). `--all` remains the manual/ops path.

These tests pin both sides:
1. `--all` runs exactly the rules registered in RUNNERS (no more, no less),
   and keeps going when one rule fails (exit code 2, others still run).
2. infra/ecs.tf schedules `--enqueue-all` and contains neither a targets
   file nor an in-task run_insights, so the old drift cannot come back.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from constat_api.cli import run_insights as cli
from constat_api.insights.runner import RUNNERS, RunResult

REPO_ROOT = Path(__file__).resolve().parent.parent
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"


@contextmanager
def _fake_session():
    yield object()


def _ok_result(rule_name: str) -> RunResult:
    return RunResult(
        rule_name=rule_name,
        resources_scanned=0,
        insights_emitted=0,
        inconclusive_emitted=0,
        errors=[],
    )


@pytest.fixture
def recorded(monkeypatch) -> list[str]:
    """Patch SessionLocal + run_rule in the CLI module; record rule calls."""
    calls: list[str] = []

    def _fake_run_rule(session, rule_name, **kwargs):
        calls.append(rule_name)
        return _ok_result(rule_name)

    monkeypatch.setattr(cli, "SessionLocal", _fake_session)
    monkeypatch.setattr(cli, "run_rule", _fake_run_rule)
    return calls


# ---------------------------------------------------------------------------
# --all runs the registry, exactly
# ---------------------------------------------------------------------------


def test_all_runs_every_registered_rule(recorded) -> None:
    rc = cli.main(["--all"])
    assert rc == 0
    assert recorded == sorted(RUNNERS), (
        "--all must run exactly the rules registered in RUNNERS — "
        "this is the anti-drift contract the scheduler relies on"
    )


def test_single_rule_still_works(recorded) -> None:
    rc = cli.main(["--rule", "chargeback"])
    assert rc == 0
    assert recorded == ["chargeback"]


def test_all_and_rule_are_mutually_exclusive(recorded) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--all", "--rule", "chargeback"])
    assert recorded == []


def test_no_selection_is_an_error(recorded) -> None:
    """No silent default: the old implicit `--rule rds_eol` default is
    exactly how a scheduler ends up running one rule forever."""
    with pytest.raises(SystemExit):
        cli.main([])
    assert recorded == []


def test_one_failing_rule_does_not_stop_the_others(monkeypatch) -> None:
    """A rule that raises must not prevent the remaining rules from
    running (each has its own session); the exit code reports failure."""
    calls: list[str] = []
    rules = sorted(RUNNERS)
    failing = rules[0]

    def _flaky_run_rule(session, rule_name, **kwargs):
        calls.append(rule_name)
        if rule_name == failing:
            raise RuntimeError("boom")
        return _ok_result(rule_name)

    monkeypatch.setattr(cli, "SessionLocal", _fake_session)
    monkeypatch.setattr(cli, "run_rule", _flaky_run_rule)

    rc = cli.main(["--all"])
    assert rc == 2
    assert calls == rules, "remaining rules must still run after a failure"


# ---------------------------------------------------------------------------
# Terraform pin: the hardcoded rule list cannot come back
# ---------------------------------------------------------------------------


def test_ecs_task_enqueues_instead_of_scanning_directly() -> None:
    """SRE review (2026-07-19): the scheduled task must go through the
    queue + persisted targets, and evaluation must come from the worker
    chain — never from an in-task hardcoded invocation."""
    source = ECS_TF.read_text(encoding="utf-8")
    # Comments mention the old world for context ("no more run_insights…")
    # — assertions apply to executable lines only.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "--enqueue-all" in code, (
        "infra/ecs.tf must schedule the queue path (python -m constat_api.cli.aws --enqueue-all)"
    )
    assert "--targets" not in code, (
        "infra/ecs.tf reads a targets JSON file again — the scan-targets "
        "secret path is deprecated; persisted collect_targets are the source"
    )
    assert "run_insights" not in code, (
        "infra/ecs.tf runs rule evaluation in-task again — evaluation is "
        "chained by the worker when a collect job completes (migration 0021)"
    )
