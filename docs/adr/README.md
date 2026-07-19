# Architecture Decision Records (ADR)

Canonical home of Constat's architectural decisions — one file per decision.
ADR-01 → ADR-11 were extracted verbatim (French) from
`docs/design/architecture-cloud-assurance-v2.md` (§9) on 2026-07-19; that
section is kept as the historical source but must not receive new ADRs.

| # | Title | Status | Summary |
|---|---|---|---|
| [ADR-01](ADR-01-s3-parquet-before-iceberg.md) | S3 + Parquet avant Iceberg | accepté | S3/Parquet append-only for history and FOCUS; Iceberg only past explicit scale/maintenance thresholds. |
| [ADR-02](ADR-02-aurora-postgresql.md) | Aurora PostgreSQL | accepté | Aurora PostgreSQL for transactional state, tenant isolation, current facts and read models; tenant cells or a dedicated cluster past load/SLO thresholds. |
| [ADR-03](ADR-03-ecs-fargate-workers.md) | ECS Fargate workers | accepté | Containerized workers autoscaled on queue age/depth; Lambda for short tasks only; Glue/Spark only for oversized FOCUS/replay jobs. |
| [ADR-04](ADR-04-step-functions-coordinator-sqs-distributor.md) | Step Functions coordinateur + SQS distributeur | accepté | Step Functions coordinates a run, SQS distributes work units — avoids per-page workflows and double retry logic. |
| [ADR-05](ADR-05-rest-fastapi-nextjs-api-as-product-surface.md) | REST/FastAPI + Next.js, API comme surface produit | accepté | Versioned REST API with cursors and async long operations; the API is a product surface from V1, never bypassed by the UI. |
| [ADR-06](ADR-06-namespaced-facts-before-universal-model.md) | Facts namespacés avant modèle universel | accepté | Connectors publish namespaced facts; no universal owner/security/compliance fields in V1. |
| [ADR-07](ADR-07-no-permanent-streaming.md) | Pas de streaming permanent | accepté | Batch and micro-batch with mandatory periodic reconciliation; streaming only past freshness/throughput/cost thresholds. |
| [ADR-08](ADR-08-no-graph-database-in-v1-graph-ready.md) | Pas de graph database en V1, mais graph-ready | accepté | Relations stay in PostgreSQL/Parquet; a graph engine may be added later only as a derived projection, past explicit thresholds. |
| [ADR-09](ADR-09-ai-as-consumption-layer.md) | IA comme couche de consommation, jamais comme source de vérité | accepté | No AI inference in ingestion/normalization/projection; AI (V2+) is a consumption layer over read models that cites its sources. |
| [ADR-10](ADR-10-authentication-api-identities.md) | Authentification et identités API | accepté | No anonymous tenant-wide token: OIDC users with tenant membership, named service accounts with scoped expiring API keys. |
| [ADR-11](ADR-11-parquet-querying-duckdb-then-athena.md) | Requêtage Parquet : DuckDB puis Athena | accepté | Embedded DuckDB in workers for replays/FOCUS aggregates; Athena takes over per job past memory/duration thresholds. |
| [ADR-12](ADR-12-insights-first-pivot.md) | Insights-first pivot | accepted (2026-07-18) | V1 is sold as "insights + chargeback", not as a filterable inventory; the inventory capability is a V2 decision gated on pilot demand. |
| [ADR-13](ADR-13-monetary-extraction-registry.md) | Monetary extraction registry in core | accepted (2026-07-18) | `constat_core.monetary.MONETARY` is the single source of truth for rule monetary semantics; `ACCOUNTING_DELTA` amounts are never summed into savings. |
