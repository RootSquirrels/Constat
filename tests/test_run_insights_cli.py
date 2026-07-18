"""Proof tests for the run_insights CLI `--all` mode (scheduler drift fix).

Committee finding: the scheduled ECS task hardcoded two `--rule` lines, so
4 of 6 registered rules never ran automatically — the same drift class as
the monetary extraction bug (a hardcoded list diverging from RUNNERS).

These tests pin both sides:
1. `--all` runs exactly the rules registered in RUNNERS (no more, no less),
   and keeps going when one rule fails (exit code 2, others still run).
2. infra/ecs.tf uses `--all` and contains no per-rule invocation, so the
   hardcoded list cannot come back through Terraform.
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


def test_ecs_task_uses_all_and_no_hardcoded_rules() -> None:
    source = ECS_TF.read_text(encoding="utf-8")
    assert "run_insights --all" in source, "infra/ecs.tf must schedule run_insights --all"
    assert "run_insights --rule" not in source, (
        "infra/ecs.tf hardcodes a --rule invocation again — that is the "
        "drift that silently skipped 4 of 6 rules (see this test's docstring)"
    )
