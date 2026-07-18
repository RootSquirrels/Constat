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
