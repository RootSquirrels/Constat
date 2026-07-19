"""AWS FOCUS 1.0 dialect.

The AWS Cost and Usage Report (CUR) FOCUS 1.0 export is the shape the
Constat loader was originally written against. Every column the
loader uses (BillingAccountId, ServiceName, ChargePeriod*, etc.) is
present with the spec-mandated names, and the values are within the
FOCUS 1.0 vocabulary (no AWS-specific quirks to normalize).

The dialect therefore:
- detects via `ProviderName == "AWS"` (1.0 column) or the legacy
  `InvoiceIssuerName` containing "Amazon Web Services" (pre-1.0
  export),
- passes rows through unchanged.

A future AWS-specific quirk (e.g. a new column the loader doesn't
recognize, or a sub-account type that needs special handling) plugs
in via `normalize_charge` without touching the rest of the pipeline.
"""

from __future__ import annotations


class AwsDialect:
    """The AWS FOCUS 1.0 dialect. Passthrough with provider detection."""

    @property
    def provider_name(self) -> str:
        return "aws"

    def detect(self, fieldnames: list[str], first_row: dict[str, str | None]) -> float:
        # 1.0 has ProviderName (1.0 §2.49). Pre-1.0 exports signal via
        # InvoiceIssuerName containing "Amazon Web Services". Either
        # is a strong (1.0) signal — there is no overlap with Azure
        # (PublisherName "Microsoft") or GCP (InvoiceIssuerName "Google").
        provider = (first_row.get("ProviderName") or "").strip()
        if provider == "AWS":
            return 1.0
        publisher = (first_row.get("PublisherName") or "").strip()
        if publisher == "Amazon Web Services":
            return 1.0
        issuer = (first_row.get("InvoiceIssuerName") or "").strip()
        if "Amazon Web Services" in issuer:
            return 0.9
        return 0.0

    def normalize_charge(self, row: dict[str, str | None]) -> dict[str, str | None]:
        # Passthrough: AWS FOCUS 1.0 columns match the spec. A copy is
        # not necessary — the loader reads the row, the dialect does
        # not mutate it.
        return row
