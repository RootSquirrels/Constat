# Deployment — local dev vs pilot

Scope: how the product runs locally and in the single pilot environment.
The IaC itself lives in `infra/` (see `infra/README.md` for the apply
runbook); this doc is the operator's mental model.

> IaC/Dockerfile status (2026-07-18): **unapplied and unvalidated** — no
> terraform binary, AWS account, or Docker daemon on the authoring machine.

## Local (docker-compose)

```
your machine
├── docker compose up: postgres:16 (migrations auto-applied from
│   db/migrations via docker-entrypoint-initdb.d) + minio (FOCUS files)
├── uv run uvicorn constat_api.main:app → API on :8000, auth OPEN if
│                                         CONSTAT_API_KEY unset (dev only)
│   (note: there is no __main__.py or console script yet — uvicorn is
│    the invocation; the CLIs below DO run as python -m)
├── uv run python -m constat_api.cli.aws --targets targets.json
├── uv run python -m constat_api.cli.run_insights --rule rds_eol
└── cd apps/web && npm run dev          → web on :3000
```

Config comes from `.env` (`CONSTAT_*`, see `.env.example`). AWS access for
collects uses `CONSTAT_AWS_PROFILE` → `~/.aws/credentials`.

## Pilot (infra/)

```
AWS account (single region, default VPC)
├── RDS PostgreSQL 16 — db.t4g.micro, single-AZ, private, 7-day backups
├── ECS Fargate cluster "constat-pilot"
│   ├── service constat-pilot-api (1 task, public IP, SG: port 8000 from
│   │   allowed_cidr only, NO ALB — plain HTTP, see below)
│   └── scan task def — same image, command overridden:
│       collect CLI → run_insights rds_eol → run_insights chargeback
├── EventBridge Scheduler — fires the scan task daily at 05:00 UTC
├── Secrets Manager — api-key, database-url, scan-targets
├── ECR — the one constat-api image
└── CloudWatch /ecs/constat-pilot — 30-day retention
```

What runs where: **everything stateful is in RDS**; Fargate tasks are
stateless. The API service and the scan task share one image
(repo-root `Dockerfile`); the scan task only differs by command override.
There is no worker fleet, no queue — one daily one-off task is the whole
orchestration (Step Functions/SQS are explicitly out of V1, AGENTS.md).

### The no-ALB decision

The API service gets a public IP with security-group ingress restricted
to `allowed_cidr`. An ALB costs ~$16+/month and only earns it with TLS
(ACM + DNS) — ceremony the pilot doesn't need for a few operator callers.
Accepted trade-offs: traffic is plain HTTP (the `X-API-Key` crosses the
internet unencrypted), and the service IP is **ephemeral** — look it up
after each deploy (`terraform output api_endpoint` prints the how). The
CIDR restriction + API key are the compensating controls. ALB + TLS is
the first hardening item below.

## How secrets flow

1. Operator sets `TF_VAR_db_password` / `TF_VAR_api_key` /
   `TF_VAR_scan_targets_json`; Terraform writes them into Secrets Manager
   (`constat-pilot/api-key`, `/database-url`, `/scan-targets`).
2. ECS injects them into containers as env vars (`secrets` blocks in the
   task definitions): the app reads the same `CONSTAT_*` variables as
   local dev — `settings.py` doesn't know Secrets Manager exists.
3. Nothing secret is baked into the image, the task-def JSON, or logs.
   Caveat: values do sit in the local terraform state file — protect it
   (and see the gitignore warning in `infra/README.md`).

Cross-account data access uses no stored credentials at all: the task
role calls `sts:AssumeRole` into the prospect's `constat-collector` role;
the **ExternalId is enforced by the prospect's trust policy** (F-06), and
the collector refuses to assume a role without one.

## How the daily scan fires

`aws_scheduler_schedule.daily_scan` — `cron(0 5 * * ? *)` UTC →
`ecs:RunTask` of the scan task definition (public IP so it can reach ECR
/Secrets Manager/STS without a NAT gateway). Retry: once, within 1 h.

Cadence rationale: the product's scope-freshness window is **24 h** — a
successful run older than that flips the scope to `scope_stale`
(INCONCLUSIVE). Daily is therefore the slowest cadence that keeps scopes
perpetually fresh; 05:00 UTC is off-peak and precedes the FR working day.
Because collect and both insight rules run in the same task, insights are
always computed from facts that are minutes old. Failures show up in the
`scan` CloudWatch log stream and in `source_runs` / the API status
endpoints (see `docs/operations/alerting.md`).

## Manual operations

**Trigger a collect / insight run manually** — two ways:

- API (from an allowed network): `POST /collect/aws` then
  `POST /insights/run` (rule name in the body), with the `X-API-Key` header.
- CLI, as a one-off Fargate task:

  ```bash
  aws ecs run-task --cluster constat-pilot --launch-type FARGATE \
    --task-definition <scan-task-definition-arn> \
    --network-configuration "awsvpcConfiguration={subnets=[...],securityGroups=[...],assignPublicIp=ENABLED}"
  ```

**Apply DB migrations**: raw SQL from `db/migrations/`, run from a
one-off Fargate task against the private RDS — exact command in
`infra/README.md`.

**Backups / restore**: RDS automated backups, 7-day retention, final
snapshot on destroy. Procedure: see `docs/operations/backup-restore.md`.

## Post-pilot hardening list (explicitly NOT done in V1)

- ALB + ACM + TLS (replaces public-IP + plain HTTP).
- Private subnets + NAT gateway or VPC endpoints (replaces public-IP tasks).
- WAF in front of the ALB.
- Remote terraform state (S3 + locking) once a second human applies.
- Secrets rotation (currently redeploy-to-rotate).
- Multi-AZ RDS, and pinning image digests instead of mutable tags.
