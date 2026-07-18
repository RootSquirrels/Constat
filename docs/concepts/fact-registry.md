# Fact registry (the data contract)

> V1 has a **test-time** fact registry that catches typos, missing
> entries, and producer/consumer drift in CI. V2's strategic brief
> describes a runtime `FactDefinitionRegistry` table — this YAML is
> the input for that migration (no data migration, just promotion).

## What is it

A YAML file at
`packages/core/src/constat_core/catalog/fact_definitions.yaml` that
declares every fact published to the `facts` table. Each entry says:

- the `namespace.key`
- the value type (`string` / `integer` / `decimal` / `boolean` /
  `date` / `datetime` / `json`)
- the producer (which connector module writes it)
- the consumers (which insight rules read it)
- optional: `allowed_values`, `pattern`, `minimum`, `maximum`,
  `description`, `since`

A pytest test (`tests/test_fact_definitions.py`) cross-checks the
YAML against the actual producer and consumer code. **CI fails** if:

- a connector produces a fact that isn't in the registry
- an insight reads a fact that isn't in the registry
- a registry entry references a producer / consumer that doesn't exist
- a registry entry is orphaned (no producer, no consumer, not even
  marked as "collected for archive")
- the YAML has a typo (e.g. `value_type: strint`)

The test is the contract. There is no runtime enforcement in V1.

## What is in the registry today

Facts from two producers: `aws_rds` (4 facts under `aws.rds.*`) and
`aws_ec2` (21 facts under `aws.ec2.volume.*`, `aws.ec2.snapshot.*`,
`aws.ec2.instance.*`). Headlines:

| Key | Type | Producer | Consumers |
|---|---|---|---|
| `aws.rds.engine` | string (enum) | `aws_rds` | `rds_eol`, `mysql_eol`, `aurora_eol` |
| `aws.rds.engine_version` | string (regex) | `aws_rds` | `rds_eol`, `mysql_eol`, `aurora_eol` |
| `aws.rds.instance_class` | string (regex) | `aws_rds` | _(collected for archive)_ |
| `aws.rds.vcpu` | integer (0..1024) | `aws_rds` | `rds_eol`, `mysql_eol`, `aurora_eol` |
| `aws.ec2.volume.{size_gb,volume_type,state}` | int / enum / enum | `aws_ec2` | `ebs_unattached`, `ebs_gp2_to_gp3` |
| `aws.ec2.snapshot.*` (state, size_gb, storage_tier, volume_id, start_time, description) | mixed | `aws_ec2` | `snapshot_orphan` |
| `aws.ec2.snapshot.volume_exists` | boolean | `aws_ec2` (correlation post-pass) | `snapshot_orphan` |
| `aws.ec2.instance.{state,attached_volumes}` | enum / json | `aws_ec2` | `ec2_stopped_with_storage` |

The remaining `aws.ec2.*` entries (encrypted, iops, throughput,
attached_instance_id, attached_device, create_time, instance_type,
launch_time, block_device_volume_ids) are collected for archive with
`consumers: []` — the full table is the YAML itself, which is the
source of truth.

Two facts deserve a callout: `volume_exists` and `attached_volumes`
are **correlation facts** — they span two scan jobs (snapshot×volume,
instance×volume) and are written by the collector's per-region
post-pass only when both jobs succeeded. They are ABSENT (not False)
when proof is missing, which is what turns the rules INCONCLUSIVE
instead of wrong.

`aws.rds.instance_class` is intentionally listed with
`consumers: []`: the connector publishes it for the vCPU lookup
(and for future filters), but no insight reads it directly.
The test allows this — a registry entry with no consumer is OK
**iff** the producer is in the registry's known-producers list.

## How to add a fact (the 3-step)

When you add a fact, you need to touch **three places** — the test
will fail if any of them is missing.

1. **YAML** (`packages/core/src/constat_core/catalog/fact_definitions.yaml`).
   Add the entry. Bump `last_reviewed` to today.
2. **Test** (`tests/test_fact_definitions.py`).
   Add the `(namespace, key)` to `EXPECTED_PRODUCED` (if it's a new
   fact a connector writes) or `EXPECTED_CONSUMED` (if it's a new
   fact an insight reads). Add a new producer/consumer to the dict
   keys if applicable.
3. **PR title**. Mention the fact you added. The reviewer should
   verify all three places.

### Example: adding `aws.rds.allocated_storage`

Suppose a new insight wants to flag tiny RDS instances. You add a
fact in the collector:

```python
# in db_to_facts
_fact("allocated_storage", db.get("AllocatedStorage"), ValueState.KNOWN),
```

Then:

1. YAML:
   ```yaml
     - namespace: aws.rds
       key: allocated_storage
       value_type: integer
       description: Allocated storage in GiB. UNKNOWN when not returned.
       minimum: 0
       maximum: 65536
       producer: aws_rds
       consumers:
         - rds_size_insight   # the new insight
       since: "2026-08-15"
   ```
2. Test (`EXPECTED_PRODUCED["aws_rds"]`):
   ```python
   ("aws.rds", "allocated_storage"),
   ```
3. Test (`EXPECTED_CONSUMED["rds_size_insight"]`):
   ```python
   ("aws.rds", "allocated_storage"),
   ```

Without step 2 or 3, the test fails with a clear message:
"produced by code but missing from fact_definitions.yaml".

## What the registry is NOT in V1

- **No runtime check on insert.** Nothing prevents a connector
  from writing `aws.rds.engne` (typo) directly to the `facts`
  table. The test catches it only if a connector hard-codes the
  fact list in the test. V2: the runtime `FactDefinitionRegistry`
  table + CHECK constraints on the `facts` table.
- **No automatic discovery.** The test relies on the human keeping
  `EXPECTED_PRODUCED` / `EXPECTED_CONSUMED` in sync with the code.
  This is intentional: explicit > implicit. The cost is one line
  per fact added.
- **No UI to browse the registry.** A `GET /registry` endpoint
  (returns the YAML as JSON) is V2.
- **No versioning per fact.** When `aws.rds.engine` evolves (e.g.
  Aurora adds a new mode), the registry is replaced in place. V2
  uses a `schema_version` on each entry + a migration path.
- **No drift detection on the consumer side.** The test reads
  `EXPECTED_CONSUMED` (a constant) and the registry. If the
  resolver code reads a fact that the constant doesn't list, the
  test passes — but the consumer is invisible to the audit. We
  rely on the developer's discipline here. V2: AST scan the
  resolver code for `aws\\.rds\\.\\w+` patterns.

## V2 migration path

When the strategic brief's `FactDefinitionRegistry` table ships,
the migration is **additive**:

1. New table `fact_definitions` with the same columns as the
   YAML entries.
2. A startup task reads `fact_definitions.yaml` and `INSERT
   IGNORE`s each entry.
3. The `facts` table gets a `fact_definition_id` FK column
   (nullable for backward compat; the runner fills it on insert).
4. The runtime check (validate `value` against `value_type` and
   `allowed_values` on insert) lives in a CHECK trigger or in
   the repository layer.
5. The YAML stays as the source of truth for the build (tests
   still cross-check it). The DB table is a runtime cache.

No data migration. The `facts` table is unchanged in shape; the
new FK is a nullable addition.

## See also

- [`../concepts.md`](../concepts.md) — the 9 core concepts, including
  `Fact` and `SourceRun`
- [`../architecture.md`](../architecture.md) — the four-box view
- [`../data-model.md`](../data-model.md) — the `facts` table schema
