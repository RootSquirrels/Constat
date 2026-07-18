# Insight: EBS gp2 → gp3 migration

> Spec for the `ebs_gp2_to_gp3` rule. Catalog data and resolver implementation
> live in `packages/core/src/constat_core/catalog/ebs.py` and
> `packages/insights/ebs_gp2_to_gp3/src/constat_ebs_gp2_to_gp3/resolver.py`.
> Source: `packages/connectors/aws_ec2` (DescribeVolumes).
> Source-of-truth stamp: `EBS_CATALOG_VERSION = "2026-07-18"`.

## What it answers

> "How much can we save by migrating EBS volumes from gp2 to gp3, and which
> volumes are the candidates?"

gp3 is 20% cheaper than gp2 on storage, with no behavior change and no
migration window (online, in-place, no downtime). AWS has been defaulting
new volumes to gp3 since 2021, so any gp2 volume is a legacy artifact —
and at fleet scale, the savings are non-trivial.

## Inputs

- `aws.ec2.volume.volume_type` — KNOWN, value == "gp2"
- `aws.ec2.volume.size_gb` — KNOWN, integer

If either fact is missing or UNKNOWN, the rule emits INCONCLUSIVE (not
silent — criterion n°15). The operator sees the gap in their data and
knows to re-run the scan.

## Output

For each `AWS::EC2::Volume` resource of type `gp2` with a real saving
(> $0.50/month, to filter out 1-25 GB scratch volumes), the rule emits
one `Insight` with payload:

```json
{
  "volume_size_gb": 1000,
  "current_volume_type": "gp2",
  "target_volume_type": "gp3",
  "current_monthly_usd": 100.00,
  "target_monthly_usd": 80.00,
  "savings_monthly_usd": 20.00,
  "savings_pct": 20.0,
  "value_basis": "ESTIMATED",
  "recommendation": "Migrate to gp3 (online, no downtime): $100.00/month → $80.00/month. Same API, no behavior change, ~20% storage saving.",
  "catalog_version": "2026-07-18"
}
```

### Severity thresholds

Severity is by monthly savings, not by volume size:

| Monthly savings | Severity |
|---|---|
| >= $500 | CRITICAL |
| >= $50  | WARNING  |
| < $50   | INFO     |

The thresholds are pragmatic: $50/month is "a real number" the operator
notices; $500/month is "a fleet-level problem". The dashboard sort
(savings DESC) surfaces the biggest wins first regardless of severity.

## What it does NOT cover

- **io1 / io2 / st1 / sc1 / standard / magnetic** — not migration
  candidates. NO_MATCH (no insight).
- **gp2 with size = 0 or size < 25 GB** — savings below $0.50/month, the
  noise threshold. NO_MATCH. The operator can still see these volumes
  in the inventory; the insight is for "what to act on this quarter".
- **io1/io2 IOPS savings** — those volumes need a separate analysis
  (provisioned IOPS charge, workload-dependent). Out of scope here.
- **Multi-region pricing variance** — the catalog is US East; EU/Asia
  regions have 1-3% premiums. The rule flags `value_basis=ESTIMATED`
  so the operator knows. When the catalog is extended with region-
  specific pricing, the basis will flip to ACTUAL on reconciliation.
- **Workload-aware IOPS recommendations** (e.g. "use gp3 with custom
  IOPS instead of io1") — out of scope; the workload's actual IOPS
  profile is not observed.
- **Per-volume opt-out for intentionally-kept gp2** (e.g. AMI
  compatibility) — the operator decides per-volume from the inventory.

## Scope-completeness (the GTM promise)

The rule only emits MATCH/NO_MATCH for volumes whose **EC2 scope** has
been proven. A successful `aws_ec2` source_run must exist for
(account, region, resource_type=`AWS::EC2::Volume`) within the freshness
window (24h by default).

An existing `aws_rds` source_run does NOT prove EC2 scope: the
`RULE_SOURCES` registry binds the rule to the right source. A
successful RDS scan leaves every EC2 volume INCONCLUSIVE until an
EC2 scan completes.

## Operator playbook

1. Run `POST /insights/run {"rule_name": "ebs_gp2_to_gp3"}` after a
   successful EC2 scan (`POST /collect/aws` with
   `resource_types: ["ec2_volume"]`).
2. Sort the insights view by `savings_monthly_usd` DESC.
3. For each CRITICAL/WARNING insight, open the AWS console for the
   volume, "Modify", change type to gp3, no IOPS adjustment needed
   (gp3 includes 3000 IOPS / 125 MB/s baseline).
4. Confirm in the next scan that the volume is now gp3 (the
   next `ebs_gp2_to_gp3` run will emit nothing for it).
