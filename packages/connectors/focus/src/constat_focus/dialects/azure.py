"""Azure FOCUS 1.0 dialect.

The Azure Cost Management FOCUS 1.0 export is spec-conformant in
every column Constat uses (ServiceName, BillingAccountId,
ChargePeriod*, ResourceId in ARM-id form, etc.). What it adds on top
of AWS:
- a `ServiceCategory` column with a coarser grouping (Compute,
  Database, Storage, ...) that AWS also emits but doesn't always
  populate,
- ARM ResourceIds (`/subscriptions/{sub}/resourceGroups/{rg}/...`)
  in `ResourceId` — the AWS column is a plain ARN; the loader
  treats both as opaque strings, so no normalization is needed,
- reservations expressed through `CommitmentDiscount*` columns the
  AWS CUR doesn't populate; again, the loader ignores them.

The dialect therefore:
- detects via `ProviderName == "Microsoft"` (1.0 §2.49) or
  `PublisherName == "Microsoft"`,
- passes rows through unchanged, but sets the `provider` hint on
  the row (a copy is required so we don't mutate the caller's
  dict) so the loader knows which catalog namespace to use when
  resolving `ServiceName` -> canonical.

A future Azure-specific quirk (e.g. Cost Management adding a
"ReservationId" rename) plugs in via `normalize_charge`.
"""

from __future__ import annotations


class AzureDialect:
    """The Azure FOCUS 1.0 dialect. Passthrough with provider detection."""

    @property
    def provider_name(self) -> str:
        return "azure"

    def detect(self, fieldnames: list[str], first_row: dict[str, str | None]) -> float:
        provider = (first_row.get("ProviderName") or "").strip()
        if provider == "Microsoft":
            return 1.0
        publisher = (first_row.get("PublisherName") or "").strip()
        if publisher == "Microsoft":
            return 1.0
        issuer = (first_row.get("InvoiceIssuerName") or "").strip()
        if "Microsoft" in issuer:
            return 0.9
        return 0.0

    def normalize_charge(self, row: dict[str, str | None]) -> dict[str, str | None]:
        # Passthrough. The dialect is detected once at file-open time
        # and the resulting `provider_name` is passed to the loader,
        # so the per-row normalize hook stays a no-op.
        return row
