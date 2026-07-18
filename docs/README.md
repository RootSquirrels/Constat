# Constat — documentation

This directory holds the V1 documentation set. It is written for **engineering
and for the pilot customer** — it is not a strategic brief and not an
aspirational architecture deck. Anything that is not in V1 is explicitly
marked as such.

## Start here

- **[overview.md](./overview.md)** — what Constat is, the value, who it is for
  (5 minutes).
- **[architecture.md](./architecture.md)** — how the V1 system is shaped:
  sources, ingestion, core, product. One read.
- **[concepts.md](./concepts.md)** — the 9 core concepts (Resource, Fact,
  SourceRun, Insight, …) with code references.
- **[concept-registry.md](./concepts/fact-registry.md)** — the V1
  fact registry (YAML + CI guard). The data contract: every fact
  is declared before it ships.
- **[data-model.md](./data-model.md)** — the 7 tables, FK chains, and the
  invariants you must respect when touching the schema.

## Topic docs

- **Insights**
  - **[rds-extended-support.md](./insights/rds-extended-support.md)** — the
    V1 hero insight. The EOL dates, the pricing tiers, the resolution logic.
  - **[chargeback.md](./insights/chargeback.md)** — the FOCUS-derived
    insight. Conceptual model and data flow. The aggregation contract is
    still being iterated; see the source.
- **API**
  - **[endpoints.md](./api/endpoints.md)** — the 9 routers, request/response
    shapes, error semantics, auth, request_id.
- **Operations**
  - **[logging.md](./operations/logging.md)** — structlog + request_id
    middleware. JSON output in prod, colored in dev.
  - **[metrics.md](./operations/metrics.md)** — Prometheus `/metrics`
    endpoint, the SLO counters/histograms, PromQL examples, OTel
    migration path.
  - **[inconclusive-cleanup.md](./operations/inconclusive-cleanup.md)** —
    scheduled cleanup of the `inconclusive` table. CLI + endpoint.
- **Development**
  - **[setup.md](./development/setup.md)** — local dev environment.
  - **[running-the-stack.md](./development/running-the-stack.md)** — the
    end-to-end V1 demo path (ingest → scan → run insights → see in UI).
  - **[known-issues.md](./development/known-issues.md)** — drift between ORM
    and SQL migrations, and other traps.
- **GTM**
  - **[positioning.md](./gtm/positioning.md)** — the customer-facing
    positioning. Use this for decks and one-pagers.

## What this docs set is NOT

- **Not a V2/V3 roadmap.** The V1 ship is the priority. The big strategic
  architecture document from the 2nd LLM is preserved in
  `docs/_strategic/` (not added yet — the V2/V3 parts don't need to be
  inside the V1 surface). When we move to V2, we promote the relevant
  sections here.
- **Not a tutorial.** Read the code. Read the tests. The tests document the
  contract.
- **Not auto-generated.** Each doc has an owner (see commit history).

## Conventions

- Code references are `path:line` or `path::ClassName` (e.g.
  `packages/core/src/constat_core/models.py::Resource`).
- `criterion n°X` refers to the V1 acceptance criteria (see AGENTS.md and
  `architecture.md`).
- ADRs (Architecture Decision Records) are a separate file each, under
  `docs/adr/`. We don't have any in V1 yet — the V1 decisions are inline
  in `architecture.md`. We split when a decision needs a defense of its
  own.
