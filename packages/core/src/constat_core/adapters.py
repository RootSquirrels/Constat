"""Adapter contracts — the boundary every external integration must respect.

Architectural rule (review item 5, ADR-14): an integration (AWS, FOCUS,
and tomorrow ServiceNow, Azure, Prisma, ...) implements one or more of the
protocols below and **returns canonical objects** — `Resource`,
`Observation`, `Fact`, or the cost-adapter charge object. Persistence
(repositories, interface tables, findings/insights rows) is the
orchestrator's job in `apps/api`, never the adapter's. An adapter must
never import `constat_api` or write to a database; conversely,
`packages/*` never imports `apps/*` (guarded by
`tests/test_adapter_contracts.py`).

The protocols are structural (`typing.Protocol`, `@runtime_checkable`):
a connector conforms by exposing the right callables, not by inheriting
anything. The V1 connectors ship as plain module-level functions; the
conformance tests wrap them in a thin object that satisfies the protocol,
which proves the contracts describe what the collectors actually do —
not an idealized shape.

Only `InventoryAdapter` and `CostAdapter` have a V1 implementation
(`constat_aws_rds` / `constat_aws_ec2`, and `constat_focus`). The other
four are contracts only: their docstrings define what a future
implementation must produce, per the review's integration mapping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from constat_core.models import Fact, Insight, Observation, Resource

# The canonical charge object of the V1 cost adapter is
# `constat_focus.loader.FocusCharge`. Core imports nothing (AGENTS.md),
# so `CostAdapter` is generic over the charge type; the conformance test
# pins the V1 binding to FocusCharge.
ChargeT = TypeVar("ChargeT", covariant=True)

# Row-level skip callback, mirroring the FOCUS loader: (row index, error).
OnSkip = Callable[[int, Exception], None]


@runtime_checkable
class InventoryAdapter(Protocol):
    """Discovers resources and turns raw cloud items into canonical objects.

    V1 implementations: `constat_aws_rds.collector` (one resource type),
    `constat_aws_ec2.collector` (three: volume, snapshot, instance — one
    adapter view per resource type). Future: Azure Resource Graph
    (Inventory + Relationship per the review's mapping).

    `collect` yields raw provider dicts; the caller owns the connection
    object (`session` — a boto3 Session in V1, kept as `Any` here because
    core imports nothing) so cross-account AssumeRole stays an
    orchestrator concern. Each raw item carries the adapter-injected
    region (e.g. the `_region` key) so the factories can emit the region
    fact the pricing rules gate on.

    The factories are pure functions: raw dict in, canonical
    `Resource` / `Fact` / `Observation` out. No DB access, no imports
    from `apps/*`.
    """

    source_name: str  # e.g. "aws_rds" — stamped on every emitted Fact/Observation

    def collect(
        self, session: Any, regions: Sequence[str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield raw provider items across the given regions (default: adapter's)."""
        ...

    def to_resource(self, raw: dict[str, Any], account_id: str) -> Resource:
        """Build the canonical Resource (stable identity) from one raw item."""
        ...

    def to_facts(
        self, resource_id: UUID, account_id: str, raw: dict[str, Any], observed_at: datetime
    ) -> list[Fact]:
        """Convert one raw item to canonical namespaced Facts (KNOWN/UNKNOWN states)."""
        ...

    def to_observation(
        self, resource_id: UUID, raw: dict[str, Any], observed_at: datetime
    ) -> Observation:
        """Convert one raw item to an immutable Observation (replayable payload)."""
        ...


@runtime_checkable
class CostAdapter(Protocol[ChargeT]):
    """Ingests cost data into canonical charge objects.

    V1 implementation: `constat_focus.loader` — `load_focus` /
    `load_focus_csv` / `load_focus_parquet` stream `FocusCharge` rows from
    a FOCUS 1.0 export, validating required columns up front and skipping
    (not dying on) malformed rows via `on_skip`. The charge type is a
    type parameter because the canonical charge object lives in the
    connector package, and core imports nothing.
    """

    source_name: str  # e.g. "focus"

    def load(self, path: str | Path, *, on_skip: OnSkip | None = None) -> Iterator[ChargeT]:
        """Stream canonical charge objects from a cost export at `path`."""
        ...


@runtime_checkable
class EvidenceAdapter(Protocol):
    """Pulls evidence from an external system of record into canonical objects.

    Contract only — no V1 implementation. Per the review's mapping:
    ServiceNow CMDB → Evidence + Relationship, Prisma → Evidence,
    Azure Update Manager → Evidence.

    "Evidence" is emitted exactly like inventory facts: canonical `Fact`s
    in the adapter's own namespace (e.g. `servicenow.cmdb.*`) with an
    explicit `value_state`, plus an immutable `Observation` per raw
    record so the assessment stays replayable. An EvidenceAdapter never
    writes to the facts/observations tables itself — the orchestrator
    persists what the adapter returns.
    """

    source_name: str

    def collect_evidence(self, session: Any) -> Iterator[dict[str, Any]]:
        """Yield raw evidence records from the external system."""
        ...

    def to_facts(
        self, resource_id: UUID | None, account_id: str, raw: dict[str, Any], observed_at: datetime
    ) -> list[Fact]:
        """Convert one raw evidence record to canonical Facts."""
        ...

    def to_observation(
        self, resource_id: UUID | None, raw: dict[str, Any], observed_at: datetime
    ) -> Observation:
        """Convert one raw evidence record to an immutable Observation."""
        ...


@runtime_checkable
class RelationshipAdapter(Protocol):
    """Emits relationships between resources as canonical objects.

    Contract only — no V1 implementation. Per the review's mapping:
    ServiceNow CMDB → Evidence + Relationship, Azure Resource Graph →
    Inventory + Relationship. ADR-08 stands: no graph database; a
    relationship is data, not a query engine.

    Relationships are emitted as canonical `Fact`s on the source
    resource: the fact key is the relation type (e.g. `depends_on`) and
    the value carries the target's native identity (e.g. ARN, Azure
    resource id). `resource_id`/`account_id` anchor the edge's source;
    the orchestrator resolves the target's `Resource` at persistence
    time. An absent relationship is never guessed: no fact is written
    unless the source system proved the edge.
    """

    source_name: str

    def collect_relationships(self, session: Any) -> Iterator[dict[str, Any]]:
        """Yield raw relationship records from the external system."""
        ...

    def to_facts(
        self, resource_id: UUID, account_id: str, raw: dict[str, Any], observed_at: datetime
    ) -> list[Fact]:
        """Convert one raw relationship record to canonical edge Facts."""
        ...


@runtime_checkable
class WorkflowAdapter(Protocol):
    """Opens and tracks external work items for insights.

    Contract only — no V1 implementation. Per the review's mapping:
    ServiceNow ITSM → Workflow.

    The adapter translates a canonical `Insight` into an external case
    and returns the external reference (a string); it never writes the
    reference back itself — persistence is the orchestrator's job. Status
    reads are normalized to a small closed vocabulary (`open`,
    `in_progress`, `resolved`, `dismissed`) mirroring the insight ack
    states, so the product never leaks vendor-specific state machines.
    """

    source_name: str

    def create_case(self, insight: Insight) -> str:
        """Create an external case for the insight; return its external reference."""
        ...

    def get_case_status(self, external_ref: str) -> str:
        """Return the normalized status of an external case."""
        ...


@runtime_checkable
class ActionAdapter(Protocol):
    """Executes a remediation action and returns evidence of the outcome.

    Contract only — no V1 implementation, and no V1 integration is mapped
    to it (remediation is post-V1). The contract exists so that a future
    action capability is born on the right side of the boundary.

    `execute` performs the action against the provider and returns a
    canonical `Observation` recording what was requested and what the
    provider answered — the outcome is evidence, replayable and auditable
    like any other observation. The adapter never touches the resources
    or insights tables; the orchestrator persists the observation and
    decides what the action means for the originating insight.
    """

    source_name: str

    def supported_actions(self) -> list[str]:
        """Return the action identifiers this adapter can execute."""
        ...

    def execute(self, action: str, resource: Resource, params: dict[str, Any]) -> Observation:
        """Run `action` against `resource`; return the outcome as an Observation."""
        ...
