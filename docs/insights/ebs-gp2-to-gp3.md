# Insight: EBS gp2 → gp3 migration

> Spec for the `ebs_gp2_to_gp3` rule. Catalog data and resolver implementation
> live in `packages/core/src/constat_core/catalog/ebs.py` and
> `packages/insights/ebs_gp2_to_gp3/src/constat_ebs_gp2_to_gp3/resolver.py`.
> Source: `packages/connectors/aws_ec2` (DescribeVolumes).
> Source-of-truth stamp: `EBS_CATALOG_VERSION = "2026-07-18"`.

## What it answers

> "How much can we save by migrating EBS volumes from gp2 to gp3, and which
> volumes are the candidates?"

This is the cheapest, most defensible FinOps win on a typical AWS account:
gp3 is 20% cheaper than gp2 on storage, with no behavior change and no
migration window (online, in-place, no downtime). AWS has been defaulting
new volumes to gp3 since 2021, so any gp2 volume in 2026 is a legacy
artifact — and at fleet scale, the savings are non-trivial.

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
  (provisioned IOPS charge, workload-dependent). Out of scope for V1.
- **Multi-region pricing variance** — V1 uses US East pricing as the
  single-region basis. EU/Asia regions have 1-3% premiums; the rule
  flags `value_basis=ESTIMATED` so the operator knows. V2 will
  read region-specific pricing from the AWS Pricing API and flip
  the basis to ACTUAL on reconciliation.

## Scope-completeness (the GTM promise)

The rule only emits MATCH/NO_MATCH for volumes whose **EC2 scope** has
been proven. A successful `aws_ec2` source_run must exist for
(account, region, resource_type=`AWS::EC2::Volume`) within the freshness
window (24h by default — see audit F-02).

An existing `aws_rds` source_run does NOT prove EC2 scope. This is the
critical fix in the vertical slice: the runner used to hardcode
`source="aws_rds"` for the scope check, which would have made every EC2
volume INCONCLUSIVE forever (the EC2 source is distinct from RDS, so
an RDS scan doesn't prove EC2 completeness). The new
`RULE_SOURCES = {"ebs_gp2_to_gp3": "aws_ec2"}` registry binds the rule
to the right source. A specific test
(`test_run_ebs_gp2_to_gp3_emits_inconclusive_when_rds_scan_exists_but_no_ec2_scan`)
pins this contract: even with multiple successful RDS scans, the EC2
rule still emits INCONCLUSIVE.

## Cost reconciliation roadmap

`value_basis=ESTIMATED` is stamped on every insight. V2 will:

1. Read the FOCUS line for the resource's `ResourceId == volume-arn`
   (already stored in `focus_charges.resource_id`).
2. Compare the catalog estimate to the actual FOCUS billed/amortized
   cost.
3. Flip `value_basis=ACTUAL` on the insight when the two match within
   tolerance.

Until then, the estimate is the best we have, and the catalog stamp
(`catalog_version`) makes the estimate auditable: "based on EBS pricing
dated 2026-07-18".

## Operator playbook

1. Run `POST /insights/run {"rule_name": "ebs_gp2_to_gp3"}` after a
   successful EC2 scan (`POST /collect/aws` with
   `resource_types: ["ec2_volume"]`).
2. Sort the insights view by `savings_monthly_usd` DESC.
3. For each CRITICAL/WARNING insight, open the AWS console for the
   volume, "Modify", change type to gp3, no IOPS adjustment needed
   (gp3 includes 3000 IOPS / 125 MB/s baseline — the workload's
   real IOPS profile is out of scope for V1).
4. Confirm in the next scan that the volume is now gp3 (the
   next `ebs_gp2_to_gp3` run will emit nothing for it).

## Test coverage

- `tests/test_aws_ec2_connector.py` — 16 tests for the connector
  (volume/snapshot/instance pagination, resource/fact/observation mappers)
- `tests/test_ebs_catalog.py` — 18 tests for the catalog (prices,
  review dates, source URLs, monthly storage cost math)
- `tests/test_ebs_gp2_to_gp3.py` — 14 tests for the resolver
  (MATCH/NO_MATCH/INCONCLUSIVE, severity thresholds, catalog version
  stamp, no-match edge cases)
- `tests/test_ebs_gp2_to_gp3_runner.py` — 9 tests for the runner
  integration (RESOURCE_RULES registration, RULE_SOURCES binding,
  scope-not-proven via RDS-only scans, stale-scope INCONCLUSIVE,
  delete-and-replace)
- Total: 57 new tests. All 418 in the suite pass (24 Postgres-only
  skipped, expected).

## What this rule does NOT do (V2 scope)

- Does not flag gp2 → io2 migration (different pricing model).
- Does not recommend "you should use gp3 with custom IOPS" — the
  workload's actual IOPS profile is not in scope.
- Does not surface gp2 volumes that are intentionally kept (e.g. for
  compatibility with a specific AMI). The operator decides per-volume.
- Does not handle gp2 → io1 Block Express (newer generation, different
  pricing). Out of scope for V1.
