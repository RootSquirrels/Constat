"""FOCUS 1.0 dialect registry.

The loader uses `auto_detect` to pick a dialect from a file's
header + first row, or `get_dialect` when the caller passes the
provider explicitly (e.g. from a CLI flag).

Adding a provider = one `Dialect` subclass + one entry in
`REGISTRY` below. The rules and aggregator never see the dialect
directly — they consume the canonical `service_canonical` from
the loaded FocusCharge.
"""

from __future__ import annotations

from constat_focus.dialects.aws import AwsDialect
from constat_focus.dialects.azure import AzureDialect
from constat_focus.dialects.base import Dialect

# Ordered: the first dialect whose `detect` returns the highest
# confidence wins on a tie (convention: AWS first, since the loader
# was originally AWS-shaped).
REGISTRY: dict[str, Dialect] = {
    "aws": AwsDialect(),
    "azure": AzureDialect(),
}


def get_dialect(provider: str) -> Dialect:
    """Return the dialect for a known provider name.

    Raises KeyError when the provider is not registered. The CLI
    must validate user input against this set before calling
    `get_dialect`; the loader also catches the KeyError to produce
    a clear error message.
    """
    return REGISTRY[provider]


def auto_detect(
    fieldnames: list[str],
    first_row: dict[str, str | None],
) -> Dialect:
    """Pick the dialect with the highest detection confidence.

    Returns the AWS dialect when no other dialect scores (the
    default for legacy / unknown exports — the AWS dialect is the
    passthrough, which matches FOCUS 1.0 exactly when the export is
    spec-conformant).
    """
    best: Dialect = REGISTRY["aws"]
    best_score = best.detect(fieldnames, first_row)
    for prov, dialect in REGISTRY.items():
        if prov == "aws":
            continue  # already the baseline
        score = dialect.detect(fieldnames, first_row)
        if score > best_score:
            best = dialect
            best_score = score
    return best


__all__ = ["REGISTRY", "Dialect", "auto_detect", "get_dialect"]
