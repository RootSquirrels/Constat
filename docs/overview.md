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

1. Which databases pay Extended Support surcharges and which EBS/EC2
   assets silently waste money, with the monthly cost in dollars (the
   7 resource rules).
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

Eight insight rules over real AWS data and FOCUS exports:

| Insight | Source | What it proves |
|---|---|---|
| `rds_eol` | `aws_rds` collector | RDS PostgreSQL in Extended Support: the engine, the vCPU count, the pricing tier, the monthly licence cost |
| `mysql_eol` | `aws_rds` collector | Same, for MySQL 5.7/8.0 |
| `aurora_eol` | `aws_rds` collector | Same, engine-aware for Aurora MySQL/PG |
| `ebs_gp2_to_gp3` | `aws_ec2` collector | gp2 volumes paying more for less performance |
| `ebs_unattached` | `aws_ec2` collector | Available volumes billed for nothing |
| `snapshot_orphan` | `aws_ec2` collector | Snapshots whose volume is gone |
| `ec2_stopped_with_storage` | `aws_ec2` collector | Stopped instances still burning EBS budget |
| `chargeback` | FOCUS 1.0 CSV | Per-account × service amortized-vs-billed cost drift |

Plus the **INCONCLUSIVE** surface: a record for every resource the rule
could not evaluate, with the exact fact that was missing. This is the
visible proof of the inventory-first promise.

## V1 is not

- Not a CNAPP — no vulnerability scanning, no exposure analysis.
- Not a FinOps showback — per-tag aggregation via `tag_key` is
  supported (proportional per-row attribution), but there are no RI/SP
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
│   ├── core/                    # models, namespaces, catalogs (EOL, vCPU, EBS pricing)
│   ├── connectors/
│   │   ├── aws_rds/             # boto3 RDS scan
│   │   ├── aws_ec2/             # boto3 EC2/EBS scan (volumes, snapshots, instances)
│   │   └── focus/               # FOCUS 1.0 CSV → focus_charges
│   └── insights/
│       ├── rds_eol/             # PG Extended Support rule
│       ├── mysql_eol/           # MySQL Extended Support rule
│       ├── aurora_eol/          # Aurora Extended Support rule
│       ├── ebs_gp2_to_gp3/      # gp2 → gp3 savings rule
│       ├── ebs_unattached/      # unattached-volume waste rule
│       ├── snapshot_orphan/     # orphan-snapshot waste rule
│       ├── ec2_stopped_with_storage/  # stopped-instance storage rule
│       └── chargeback/          # FOCUS drift rule
├── apps/
│   ├── api/                     # FastAPI, 11 routers
│   └── web/                     # Next.js 15, 10 pages
├── db/                          # Alembic-managed schema (db/alembic/, ADR-17); _archived/ holds the 21 historical SQL files
└── tests/                       # 50 pytest files
```

## Where to read next

- **Customers / sales**: [`gtm/positioning.md`](./gtm/positioning.md)
- **Engineers joining the project**: [`architecture.md`](./architecture.md)
  → [`concepts.md`](./concepts.md) → [`data-model.md`](./data-model.md)
- **Pilot integrators**: [`api/endpoints.md`](./api/endpoints.md) +
  [`insights/rds-extended-support.md`](./insights/rds-extended-support.md)
- **Anyone debugging a deploy**: [`development/known-issues.md`](./development/known-issues.md)
