# Test fixtures

## `focus_golden_v1_0.csv` — golden FOCUS 1.0 dataset

**Provenance:** synthetic but spec-shaped. 22 rows, hand-written to carry
the FULL official FOCUS 1.0 column set (43 columns, alphabetical, per
<https://focus.finops.org/focus-specification/v1-0/>). All account IDs,
ARNs, SKU IDs, and amounts are invented; no real customer data.

**Why it exists:** the earlier home-grown CSV fixtures silently diverged
from the spec — they used `AmortizedCost` (a FOCUS 0.5 name, renamed
`EffectiveCost` in 1.0) and `Region` (renamed `RegionId`, with
`RegionName` added, in 1.0). The `AmortizedCost` non-compliance was
caught and fixed in the loader; the `Region` one was **not** —
`constat_focus/loader.py` still requires a `Region` column that no
spec-conformant export contains, so it currently rejects this golden
file (`missing required columns: ['Region']`). That bug is pinned by an
`xfail(strict=True)` test in `tests/test_focus_golden.py`; remove the
marker (and the test's Region shim) when the loader is fixed.

**Coverage:** two services (Amazon Relational Database Service, Amazon
Elastic Compute Cloud - Compute), two regions (eu-west-1, us-east-1),
on-demand usage rows, RI/Savings Plan amortization rows
(`PricingCategory=Committed`, `BilledCost=0`, `EffectiveCost>0`),
commitment purchase rows (`ChargeCategory=Purchase`, `EffectiveCost=0`),
refund/credit rows (`ChargeCategory=Credit`, negative amounts), and one
tagged resource (`Tags` JSON: `{"Application": "web", "CostCenter": "42"}`).

**Open task (roadmap M-item, awaiting real data):** replace or extend
this file with a real, anonymized AWS Data Exports (FOCUS) export from
the first prospect. Synthetic data proves we parse the *shape*; only a
real export proves we survive provider quirks (NULL sentinels, locale
decimals, per-row granularity, actual `ServiceName` values).

## `focus_azure_v1_0.csv` — golden FOCUS 1.0 dataset, Azure variant

**Provenance:** synthetic but spec-shaped, hand-written as the Azure
twin of `focus_golden_v1_0.csv`. Same FULL official FOCUS 1.0 column
set (43 columns, alphabetical). All account IDs, subscription GUIDs,
ARM resource IDs, SKU IDs, and amounts are invented; no real customer
data.

**Why it exists:** the FOCUS 1.0 spec is provider-agnostic and Azure
Cost Management exports FOCUS natively. This fixture proves the loader,
aggregator, and chargeback resolver have no hidden AWS assumption
(nothing in the pipeline reads `ProviderName` — the check is by shape,
not by provider).

**Coverage:** 18 rows, three services (Virtual Machines, Azure Database
for PostgreSQL, Storage Accounts), two regions (`westeurope`,
`francecentral`), ARM-format `ResourceId`s
(`/subscriptions/<guid>/resourceGroups/<rg>/providers/...`), EA-style
`BillingAccountId`, GUID `SubAccountId`s, on-demand usage rows,
reservation / savings-plan amortization rows (`PricingCategory=Committed`,
`BilledCost=0`, `EffectiveCost>0`), commitment purchase rows
(`ChargeCategory=Purchase`, `EffectiveCost=0`), refund/credit rows
(`ChargeCategory=Credit`, negative amounts), and tagged resources.
**Mixed currency on purpose:** most rows are `EUR`, three rows (a
second subscription's billing profile) are `USD` — Virtual Machines and
Storage Accounts each carry both currencies in the same period. This
pins two behaviors in `tests/test_azure_focus.py`: the chargeback
resolver's currency-aware grouping, and the loud ingest refusal of
mixed-currency (service, period) buckets (V1 storage keys cost rows by
(account, service, period) — one currency per row).
