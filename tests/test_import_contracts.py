"""Regression guard for the import-linter contracts (Chantier I.4).

The 6 import contracts that enforce AGENTS.md's ownership rules
live in `pyproject.toml` under `[tool.importlinter]`. CI runs
`uv run lint-imports` in a separate step; this test runs the
same check in-process so a developer running `pytest` locally
sees the failure without waiting for CI. The check is cheap
(grimp builds the import graph from the workspace once and
caches it in `.import_linter_cache/`).

The test asserts the ALL contracts are KEPT (the linter exits
non-zero on the first broken contract). A failure here means
the architectural rule was violated — see the contract name
in the error message for which one. The most common violation
during development is "accidentally imported from apps/api in
a connector" (catches a back-reference that would couple the
adapter to the orchestrator's wiring) and "imported another
insight in a rule" (catches cross-rule coupling that defeats
the "one function, N configs" Chantier III consolidation).
"""

from __future__ import annotations

import sys
from pathlib import Path

# import-linter 2.x: the workspace src/ roots are NOT on sys.path
# by default (unlike the `conftest.py` pytest setup that adds
# them). We mirror the [tool.pytest.ini_options].pythonpath and
# the CI step's PYTHONPATH here so grimp's `build_graph` walks
# the same set of packages the linter would see on a CI run.
# Without this, `importlib.util.find_spec("constat_core")` fails
# and the linter exits with "Could not find package" before it
# gets to check any contract.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_SRC = [
    "packages/core/src",
    "packages/connectors/aws_rds/src",
    "packages/connectors/aws_ec2/src",
    "packages/connectors/focus/src",
    "packages/insights/rds_eol/src",
    "packages/insights/mysql_eol/src",
    "packages/insights/aurora_eol/src",
    "packages/insights/ebs_gp2_to_gp3/src",
    "packages/insights/ebs_unattached/src",
    "packages/insights/snapshot_orphan/src",
    "packages/insights/ec2_stopped_with_storage/src",
    "packages/insights/chargeback/src",
    "apps/api/src",
]


def test_lint_imports_passes_against_the_workspace_graph() -> None:
    """Run import-linter programmatically and assert no contract
    is BROKEN. The 6 contracts in `pyproject.toml`'s
    `[tool.importlinter]` are the architectural floor; this test
    is the local equivalent of the CI step."""
    import importlinter.application.use_cases as use_cases
    import importlinter.configuration

    # 1. Mirror the CI PYTHONPATH so grimp finds the workspace
    #    packages. The change is process-local: pytest's sys.path
    #    is the dev's sys.path, so this only affects the import
    #    linter's view of the graph, not other tests' imports.
    original_path = list(sys.path)
    sys.path.insert(0, str(_REPO_ROOT))
    for src in _WORKSPACE_SRC:
        sys.path.insert(0, str(_REPO_ROOT / src))
    try:
        importlinter.configuration.configure()
        result = use_cases.lint_imports(
            config_filename=str(_REPO_ROOT / "pyproject.toml"),
            cache_dir=str(_REPO_ROOT / ".import_linter_cache"),
        )
    finally:
        sys.path[:] = original_path

    # 2. The function returns a bool: True iff every contract is
    #    KEPT. A False return means at least one contract is
    #    BROKEN (the linter already printed the broken contracts
    #    to stdout, with the chain that violated each one). The
    #    CI step exits non-zero on the same condition.
    assert result is True, (
        "import-linter reported a broken contract. See the stdout "
        "above for which contract(s) broke and the import chain that "
        "violated it. AGENTS.md ownership rules: see the contract "
        "name in the error. The most common violation during dev is "
        "a connector reaching back into apps/api, or an insight "
        "importing another insight (defeats Chantier III)."
    )
