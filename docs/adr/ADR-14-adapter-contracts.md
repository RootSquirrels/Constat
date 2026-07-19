# ADR-14 — Adapter contracts: integrations return canonical objects, never write tables

**Status:** accepted (2026-07-19) — closes review item 5 (adapter contracts for
future integrations).

## Context

Every external system we connect (today: AWS RDS/EC2 APIs and FOCUS exports;
tomorrow per the review: ServiceNow, Azure, Prisma) must produce the same
canonical objects — `Resource`, `Observation`, `Fact`, and the FOCUS charge
object — so that persistence, rule evaluation, and the restitution never care
which system a piece of data came from. Until now this boundary was only a
convention in connector docstrings ("This module only translates AWS API
responses into canonical Resources / Facts / Observations"). Nothing formal
defined the contract, nothing proved the existing connectors satisfy it, and
nothing stopped a future integration from writing directly to the interface
tables or findings — the exact failure mode that would couple `packages/*` to
`apps/api` and break the monorepo ownership rules in `AGENTS.md`.

## Decision

`constat_core.adapters` defines six `typing.Protocol` contracts
(`@runtime_checkable`, structural — no inheritance required):

- **`InventoryAdapter`** — discovers resources and turns raw cloud items into
  canonical objects. Methods: `collect(session, regions) -> Iterator[dict]`,
  `to_resource(raw, account_id) -> Resource`,
  `to_facts(resource_id, account_id, raw, observed_at) -> list[Fact]`,
  `to_observation(resource_id, raw, observed_at) -> Observation`, plus a
  `source_name` attribute. Shaped to what the aws_rds / aws_ec2 collectors
  actually expose today (module-level collect + factory functions), not to an
  idealized shape.
- **`CostAdapter[ChargeT]`** — ingests cost data into canonical charge
  objects. Methods: `load(path, on_skip=...) -> Iterator[ChargeT]`, plus
  `source_name`. Generic over the charge type because the canonical charge
  object (`constat_focus.loader.FocusCharge`) lives in the connector package
  and core imports nothing.
- **`EvidenceAdapter`**, **`RelationshipAdapter`**, **`WorkflowAdapter`**,
  **`ActionAdapter`** — contracts only, no V1 implementation. Their docstrings
  fix what a future implementation must produce: evidence as canonical
  Facts + Observations, relationships as canonical edge Facts on the source
  resource (ADR-08 stands: no graph database), workflow status normalized to
  the insight ack vocabulary, action outcomes returned as canonical
  Observations.

The architectural rule, stated in the module docstring and enforced by tests:

- **Adapters return canonical objects; they never persist.** Repositories,
  interface tables, and findings rows are the orchestrator's job in
  `apps/api`. An adapter must never import `constat_api` or write to a
  database.
- **Import direction:** `packages/*` never imports `apps/*`
  (`tests/test_adapter_contracts.py` AST-scans every module under
  `packages/connectors` and `packages/insights` for `constat_api` imports).
- **Conformance is proven, not asserted:** the conformance tests wrap each
  connector's module-level functions in a thin adapter view, check
  `isinstance` against the runtime-checkable protocol, and smoke-call every
  factory. A negative control (`constat_rds_eol.resolver`, an insight, not a
  connector) must NOT satisfy `InventoryAdapter`.

The review's integration-to-contract mapping:

| Integration | Contract(s) | V1 status |
|---|---|---|
| AWS EC2 / RDS APIs | `InventoryAdapter` | implemented (`constat_aws_rds`, `constat_aws_ec2`) |
| FOCUS 1.0 exports | `CostAdapter` | implemented (`constat_focus`) |
| ServiceNow CMDB | `EvidenceAdapter` + `RelationshipAdapter` | contract only |
| Azure Resource Graph | `InventoryAdapter` + `RelationshipAdapter` | contract only |
| Prisma | `EvidenceAdapter` | contract only |
| ServiceNow ITSM | `WorkflowAdapter` | contract only |
| Azure Update Manager | `EvidenceAdapter` | contract only |

**Non-goal:** no new connector is built as part of this decision. The four
contract-only protocols define the shape future integrations must fill; each
remains its own scoped work item (and, per `AGENTS.md`, Azure/ServiceNow/Prisma
are V2/V3).

## Consequences

- A new integration = a package under `packages/connectors/` exposing
  functions that satisfy the relevant protocol(s) + a conformance test cloned
  from `tests/test_adapter_contracts.py`. The contract is checked by CI, not
  by code review memory.
- `packages/core` gains a module — a stable-contract change, which is exactly
  why this ADR exists. The canonical models (`models.py`) are untouched.
- Existing connector code is untouched: conformance is proven by wrapping,
  not by editing the collectors.
- The four contract-only protocols may evolve when their first implementation
  lands; until then, changing them is cheap (no consumers) but still goes
  through the core-contract process.
