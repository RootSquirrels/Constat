# Insight: EBS unattached volumes

> Spec for the `ebs_unattached` rule. Resolver: `packages/insights/ebs_unattached/`.
> Source: `packages/connectors/aws_ec2` (DescribeVolumes).
> Source-of-truth stamp: `EBS_CATALOG_VERSION = "2026-07-18"`.

## What it answers

> "Which EBS volumes are paying storage cost with no consumer?"

A volume in `state=available` is unattached — it has no EC2 instance
using it, but it still costs the storage rate. A typical fleet has
5-20% of its EBS footprint unattached (old databases, dev sandboxes,
forgotten scratch volumes). This is pure waste.

## Output

For each `AWS::EC2::Volume` resource with `state=available` and KNOWN
size+type, the rule emits one `Insight`:

```json
{
  "volume_size_gb": 1000,
  "volume_type": "gp2",
  "state": "available",
  "monthly_waste_usd": 100.00,
  "value_basis": "ESTIMATED",
  "recommendation": "Delete the volume (after snapshotting if needed) — it has no consumer. ...",
  "catalog_version": "2026-07-18"
}
```

### Severity

Same scale as `ebs_gp2_to_gp3` for dashboard consistency:

| Monthly waste | Severity |
|---|---|
| >= $500 | CRITICAL |
| >= $50  | WARNING  |
| < $50   | INFO     |

## What it does NOT cover

- **`state=in-use`** — attached, working as intended. NO_MATCH.
- **`state=creating`/`deleting`** — transient, the operator should
  ignore. NO_MATCH.
- **`state=error`** — the volume may still cost money but the
  situation is unclear (broken attach? half-deleted?). NO_MATCH.
  Surface in the inventory view, not as a cost-savings insight.
- **`state=deleted`** — should never appear (retired resources are
  filtered out by the source_run logic), but if it does, NO_MATCH.

## Operator playbook

1. Run `POST /insights/run {"rule_name": "ebs_unattached"}` after a
   successful EC2 scan.
2. Sort the insights view by `monthly_waste_usd` DESC.
3. For each CRITICAL/WARNING insight, snapshot the volume first
   (`aws ec2 create-snapshot --volume-id <id>`), then delete
   (`aws ec2 delete-volume --volume-id <id>`).
4. Confirm in the next scan that the volume is gone.
