"""Drift pins for hardcoded registries that have no other guard.

Every list pinned here can silently drift between two copies that must
stay equal — the failure mode the monetary registry pin (ADR-13) and the
.env.example drift (collect env vars documented nowhere) already proved
real. Existing pins (kept in their own files, not duplicated here):

- constat_core.monetary.MONETARY <-> RUNNERS <-> RULE_MONETARY (TS):
  tests/test_monetary_extraction.py
- TENANT_GUC == "app.current_tenant_id" (vs migrations): tests/test_rls.py
- fact_definitions.yaml <-> producers/consumers: tests/test_fact_definitions.py
- JOB_REGISTRY default scope: tests/test_collect_async.py
- RLS_TABLES <-> migration policies: tests/test_rls.py
- FX_USD_TO_EUR / FX_RATE_DATE (TS) <-> catalog fx.py: tests/test_fx_mirror.py
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from constat_core.models import Severity

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_API_TS = REPO_ROOT / "apps" / "web" / "lib" / "api.ts"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
API_SRC = REPO_ROOT / "apps" / "api" / "src"
FOCUS_GOLDEN = REPO_ROOT / "tests" / "golden" / "focus_aws.csv"


def _ts_union_literals(source: str, type_name: str) -> set[str]:
    """Extract the string literals of a TS union type (`export type X = "a" | "b";`)."""
    match = re.search(rf"export type {type_name} =([^;]+);", source)
    assert match, f"apps/web/lib/api.ts lost its {type_name} type"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


# ---------------------------------------------------------------------------
# ACK_STATUSES: API repository <-> TS AckStatus union
# ---------------------------------------------------------------------------


def test_ack_statuses_match_ts_union() -> None:
    """ACK_STATUSES (repositories/insights.py) is mirrored by the AckStatus
    union in apps/web/lib/api.ts; the web ack buttons send these values."""
    from constat_api.repositories.insights import ACK_STATUSES

    ts_values = _ts_union_literals(WEB_API_TS.read_text(encoding="utf-8"), "AckStatus")
    assert ts_values == set(ACK_STATUSES)


# ---------------------------------------------------------------------------
# Severity enum: core <-> TS Severity union
# ---------------------------------------------------------------------------


def test_severity_enum_matches_ts_union() -> None:
    ts_values = _ts_union_literals(WEB_API_TS.read_text(encoding="utf-8"), "Severity")
    assert ts_values == {s.value for s in Severity}


# ---------------------------------------------------------------------------
# FOCUS_REQUIRED_COLUMNS: loader <-> spec-shaped golden fixture
# ---------------------------------------------------------------------------


def test_focus_loader_column_sets_exist_in_golden_fixture() -> None:
    """The golden fixture's header is pinned to the full FOCUS 1.0 spec
    column set (tests/test_focus_golden.py). Pinning the loader's column
    sets against that header ties the loader to the spec transitively:
    a required column the spec does not define (the pre-1.0 `Region`
    regression) fails here."""
    from constat_focus.loader import (
        FOCUS_OPTIONAL_COLUMNS,
        FOCUS_REGION_COLUMNS,
        FOCUS_REQUIRED_COLUMNS,
    )

    with FOCUS_GOLDEN.open(newline="", encoding="utf-8") as f:
        header = set(csv.DictReader(f).fieldnames or [])
    assert header >= FOCUS_REQUIRED_COLUMNS
    assert header >= FOCUS_OPTIONAL_COLUMNS
    # At least one accepted region column name must be spec-conformant.
    assert set(FOCUS_REGION_COLUMNS) & header


# ---------------------------------------------------------------------------
# ADAPTIVE_RETRY_CONFIG: lives in constat_core.collectors.aws (§III.3)
# ---------------------------------------------------------------------------


def test_adaptive_retry_config_lives_in_constat_core_collectors() -> None:
    """Chantier III.3: the retry policy is stated ONCE in the lib
    (`constat_core.collectors.aws`) and consumed by both AWS
    inventory connectors. Drift was the original failure mode
    (the policy was duplicated in `constat_aws_rds.collector`
    AND `constat_aws_ec2.collector`, with a drift pin guarding
    equality). The refactor makes drift structurally impossible:
    both connectors import the same object from the lib, and a
    new connector (e.g. Azure Resource Graph) gets the policy
    for free.
    """
    from constat_core.collectors.aws import ADAPTIVE_RETRY_CONFIG as LIB_CONFIG

    assert LIB_CONFIG.retries == {"mode": "adaptive", "max_attempts": 10}
    assert LIB_CONFIG.connect_timeout == 10
    assert LIB_CONFIG.read_timeout == 30


# ---------------------------------------------------------------------------
# .env.example: exactly the CONSTAT_* vars the API reads
# ---------------------------------------------------------------------------

_ENV_READ_RE = re.compile(r"""os\.(?:getenv|environ\.get)\(\s*["'](CONSTAT_[A-Z0-9_]+)["']""")


def _env_vars_read_by_api() -> set[str]:
    names: set[str] = set()
    for path in sorted(API_SRC.rglob("*.py")):
        names.update(_ENV_READ_RE.findall(path.read_text(encoding="utf-8")))
    return names


def _env_vars_documented() -> set[str]:
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names


def test_env_example_matches_env_vars_read_by_api() -> None:
    """Every CONSTAT_* var the API reads must be documented in .env.example,
    and .env.example must not document vars nothing reads. This drift class
    already bit once (the async-collection knobs shipped undocumented)."""
    read = _env_vars_read_by_api()
    documented = _env_vars_documented()
    assert read, "env-var scan found nothing — the regex is broken, not the code"
    missing = read - documented
    stale = documented - read
    assert not missing, f"read by the API but missing from .env.example: {sorted(missing)}"
    assert not stale, f"in .env.example but never read by the API: {sorted(stale)}"
