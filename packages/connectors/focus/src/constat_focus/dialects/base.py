"""FOCUS 1.0 dialect — provider-specific pre-parse normalization.

A `Dialect` knows how to:
- Identify itself from a FOCUS file's header / first row (auto-detect).
- Pre-process a raw row dict before the canonical FocusCharge loader
  parses it. Most rows are passthrough (FOCUS 1.0 is provider-
  agnostic in the columns Constat uses), but a dialect can override
  columns or set defaults when a provider's export deviates from
  the spec.

Adding a provider = one `Dialect` subclass + one entry in
`dialects/__init__.py::REGISTRY`. The rules and the aggregator
never see the provider — they consume the canonical
`service_canonical` from the loaded FocusCharge.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Dialect(Protocol):
    """The contract a FOCUS dialect must satisfy.

    `provider_name` is the stable identifier used in
    `accounts.provider`, `focus_charges.provider`, etc. Keep it
    short and lower-snake-case (e.g. "aws", "azure", "gcp").
    """

    @property
    def provider_name(self) -> str:
        ...

    def detect(self, fieldnames: list[str], first_row: dict[str, str | None]) -> float:
        """Return a confidence in [0.0, 1.0] that this dialect applies.

        The loader calls `detect` on every registered dialect and
        picks the one with the highest confidence; ties go to the
        dialect registered first (AWS today, by convention).

        A dialect returns 0.0 when it has no signal. 1.0 is reserved
        for an exact match (a column name that is unique to the
        provider, or a marker value in a spec-defined column).
        """
        ...

    def normalize_charge(self, row: dict[str, str | None]) -> dict[str, str | None]:
        """Pre-process one raw FOCUS row before the loader parses it.

        The returned dict is what `_row_to_charge` sees. The default
        (passthrough) is correct for AWS today: its FOCUS 1.0 export
        matches the spec in every column Constat consumes. Future
        provider-specific quirks (e.g. Azure's optional extra columns
        or a GCP column rename) plug in here.

        The dialect must NOT mutate the input dict — copy first when
        the normalization is non-trivial.
        """
        ...
