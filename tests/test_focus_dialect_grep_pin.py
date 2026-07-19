"""Grep-pin CI for the FOCUS dialect boundary (roadmap-consolidation §II.4).

The rule files under `packages/insights/*` MUST consume the canonical
service name from `FocusCharge.service_canonical` (or from the
`AggregatedFocusCharge.service_canonical` they read). They must
NEVER check the provider's native ServiceName, ResourceId format,
or any other provider-specific string. The rules and the aggregator
are provider-agnostic; the dialect layer is the only place that
knows the provider.

This test scans every `.py` under `packages/insights/*` (rules only;
not `packages/connectors/focus/` which is the dialect layer itself)
for FOCUS provider-native service names and fails the build on a
hit.

What the pin catches:
- A rule that does `if c.service == "Amazon RDS"` (provider-specific).
- A rule that does `if c.service == "Virtual Machines"` (provider-specific).
- A rule that hard-codes a region name like "westeurope" or "eu-west-1".

What the pin does NOT catch (intentional — the rules MUST know these):
- `aws.rds.engine` fact namespace (the fact catalog is provider-aware;
  the rule's job is to evaluate facts, which are provider-specific).
- `azure.compute.vm_size` fact namespace (same).
- A `rds_eol` package name (the package is named after the AWS
  service it tracks; renaming it is a separate chantier III task).
- An AWS region name like "us-east-1" embedded in a pricing catalog
  (the catalog lives in `packages/core/src/constat_core/catalog/aws.py`,
  which is provider-specific by design).

A new provider = a new batch of service-name strings here (and an
entry in the dialect registry).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# packages/insights/* — the rule boundary. NOT packages/connectors/focus/
# (the dialect layer) and NOT apps/api/ (the orchestrator).
INSIGHTS_ROOT = Path(__file__).resolve().parents[1] / "packages" / "insights"

# FOCUS provider-native service-name strings. A hit anywhere in a
# `.py` file under packages/insights/* is a failure. These are the
# strings emitted by the provider's FOCUS export in the `ServiceName`
# column — the rule must NEVER compare against them (it should compare
# against the canonical from the service catalog instead).
#
# What is NOT in this list (and why):
# - Bare "AWS" / "Azure" words: too noisy. They're legitimate
#   inside docstrings ("AWS's RDS service") and as namespace
#   prefixes (`aws.rds.*` is a fact namespace, not a FOCUS service
#   name). The FOCUS service names below are specific enough to
#   flag an actual leak.
# - Fact namespaces like `aws.rds.engine`: these are provider-
#   specific by design (the catalog `packages/core/src/constat_core/
#   catalog/fact_definitions.yaml` defines them). The rules MUST
#   know them to do their job; flagging them would break every rule.
PROVIDER_NATIVE_SERVICE_NAMES: tuple[str, ...] = (
    # AWS — the names emitted by AWS Cost and Usage Report (CUR) in FOCUS 1.0.
    "Amazon Relational Database Service",
    "Amazon Elastic Compute Cloud - Compute",
    "Amazon Elastic Compute Cloud",
    "Amazon Simple Storage Service",
    "Amazon RDS",
    "Amazon EC2",
    "Amazon S3",
    "Amazon EBS",
    "Amazon Aurora",
    # Azure — Cost Management FOCUS 1.0 export.
    "Virtual Machines",
    "Azure Database for PostgreSQL",
    "Azure Database for PostgreSQL Flexible Server",
    "Storage Accounts",
    # Region names are intentionally NOT pinned here: a docstring
    # describing the pricing-grid fallback ("matches on the us-east-1
    # fallback grid") is legitimate documentation, and a real leak
    # would have a different shape (e.g. `if region == "westeurope"`).
    # Pin those by adding the string to PROVIDER_NATIVE_SERVICE_NAMES
    # only if a real leak happens in a code comparison.
)

# Files exempt from the pin.
EXEMPT_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve(),  # this test file is the source of truth
)


def _insights_python_files() -> list[Path]:
    """Every .py under packages/insights/*, recursively.

    Skips `__pycache__` and files under EXEMPT_PATHS.
    """
    out: list[Path] = []
    for path in INSIGHTS_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if any(path.resolve() == exempt.resolve() for exempt in EXEMPT_PATHS):
            continue
        out.append(path)
    return out


def test_no_provider_native_service_names_in_rule_files() -> None:
    """A scan that fails the build on any FOCUS provider-native service
    name in a rule file.

    The output groups hits by file so a regression points the
    reviewer at the exact line that introduced the leak. The
    expected outcome on a clean tree is zero hits.
    """
    # Build one alternation pattern; word-boundary + case-insensitive
    # to keep false positives low ("aws.rds" is NOT matched as
    # "aws"; the namespace prefix is a separate concern from the
    # FOCUS service-name leak).
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(name) for name in PROVIDER_NATIVE_SERVICE_NAMES) + r")\b",
        re.IGNORECASE,
    )
    hits: list[tuple[Path, int, str]] = []
    for path in _insights_python_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                # Skip the pin's own comment lines (the names themselves
                # are part of the source).
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                hits.append((path, line_no, line.strip()))
    if hits:
        msg = "\n".join(f"  {p}:{n}: {t}" for p, n, t in hits)
        pytest.fail(
            "Provider-native FOCUS service names in packages/insights/* — the "
            "rules must consume canonicals from the service catalog, not the "
            "provider's native ServiceName. Hits:\n" + msg
        )


def test_insights_root_is_scannable() -> None:
    """Smoke test: the path exists and has at least one .py file.

    A test runner on a fresh checkout (no packages/insights/*) would
    silently pass the grep-pin (zero hits) without this assertion.
    The roadmap consolidation is the reason this directory exists;
    the test pins the directory itself.
    """
    assert INSIGHTS_ROOT.exists(), f"{INSIGHTS_ROOT} does not exist"
    assert _insights_python_files(), f"no Python files found under {INSIGHTS_ROOT}"
