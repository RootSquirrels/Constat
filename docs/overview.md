# Overview — what Constat is

## The one-line

**Constat is inventory-first cloud observability that proves the gap**
**(the *écart chiffré*) between what a cloud account should look like
and what it actually looks like, in euros, with provenance.**

It is not an inventory tool. It is not a CNAPP. It is not a FinOps
showback. The product is the *écart* — the provable, sourced, dated
delta — that only appears when you cross-reference inventory, lifecycle
and cost.

## The promise

> *In 2 hours of connection, we prove what you don't know about your
> fleet — and what it costs.*

The proof is the demo. A read-only cross-account role, a FOCUS CSV
export, and the V1 view shows:

1. Which RDS instances are in PostgreSQL Extended Support, with the
   monthly cost in dollars (`rds_eol` insight).
2. Per-account × service cost with the amortized-vs-billed drift
   (`chargeback` insight).
3. What the system *could not* conclude, and which fact was missing
   (the `inconclusive` view).

The third one is the differentiator. Trusted Advisor and Cost Explorer
silently omit; Constat surfaces the missing data explicitly. The
customer's first reaction to a clear "we don't know — here's why" is
the proof of the product.

## Who it is for

| Criterion | Target |
|---|---|
| Company size | ETI / mid-market, 200–5,000 employees |
| Cloud footprint | 5–150 AWS accounts, 1k–100k resources |
| Team shape | 2–10 cloud/infra people, no dedicated FinOps or asset manager |
| Trigger event | Rising bill, ISO 27001 / DORA / cyber-insurance audit, CMDB never up to date |
| Buyer | Head of infra/cloud or CIO; sponsor potentially CFO (via the écart chiffré) |

**Explicitly not for:** CAC 40 and large enterprises with dedicated
cloud teams. They have the headcount to run Axonius / Wiz / ServiceNow.
We don't.

## V1 deliverable

Two demoable insights over real AWS data:

| Insight | Source | What it proves |
|---|---|---|
| `rds_eol` | `aws_rds` collector | RDS PostgreSQL in Extended Support: the engine, the vCPU count, the pricing tier, the monthly licence cost |
| `chargeback` | FOCUS 1.0 CSV | Per-account × service amortized-vs-billed cost drift |

Plus the **INCONCLUSIVE** surface: a record for every resource the rule
could not evaluate, with the exact fact that was missing. This is the
visible proof of the inventory-first promise.

## V1 is not

- Not a CNAPP — no vulnerability scanning, no exposure analysis.
- Not a FinOps showback — no chargeback across teams without tag
  data; per-tag aggregation via `tag_key` is supported but uses a
  1/N cost split. No RI/SP
  optimization recommendations.
- Not a CMDB — no ServiceNow-style configuration items, no
  reconciliation workflow.
- Not a remediation tool — no auto-remediation, no `SendCommand`, no
  destructive actions.
- Not multi-cloud — AWS only in V1. Azure connector is V2.

## What is in the box

```
Constat/
├── packages/
│   ├── core/                    # models, namespaces, catalog (AWS PG EOL, vCPU)
│   ├── connectors/
│   │   ├── aws_rds/             # boto3 RDS scan
│   │   └── focus/               # FOCUS 1.0 CSV → focus_charges
│   └── insights/
│       ├── rds_eol/             # PG Extended Support rule
│       └── chargeback/          # FOCUS drift rule
├── apps/
│   ├── api/                     # FastAPI, 6 routers
│   └── web/                     # Next.js 15, 5 pages
├── db/migrations/               # 6 raw-SQL migrations
└── tests/                       # 19 pytest files
```

## Where to read next

- **Customers / sales**: [`gtm/positioning.md`](./gtm/positioning.md)
- **Engineers joining the project**: [`architecture.md`](./architecture.md)
  → [`concepts.md`](./concepts.md) → [`data-model.md`](./data-model.md)
- **Pilot integrators**: [`api/endpoints.md`](./api/endpoints.md) +
  [`insights/rds-extended-support.md`](./insights/rds-extended-support.md)
- **Anyone debugging a deploy**: [`development/known-issues.md`](./development/known-issues.md)
