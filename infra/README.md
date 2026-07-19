# infra/ — pilot environment (Terraform)

> **STATUS (2026-07-19): UNAPPLIED and UNVALIDATED.**
> No `terraform`/`tofu` binary and no AWS account exist on the dev machine
> this was written on, so none of this configuration has been through
> `terraform validate`, `plan`, or `apply`. The same applies to the
> repo-root `Dockerfile` (never built — no Docker daemon available).
> Expect a short fix-up pass on first real use.

Minimal IaC for the **single pilot environment**. No modules, no remote
state, no multi-env ceremony — deliberately. V1 deployment philosophy
(AGENTS.md): a Fargate task + cron, plus — as of chantier 1.1
(2026-07-18) — one SQS queue for async collection at ICP scale
(justification inline in `sqs.tf`; still no Step Functions).

## What it creates

- **RDS PostgreSQL 16** (`db.t4g.micro`, single-AZ, 20 GB gp3, 7-day
  backups, private) — replaces local docker-compose Postgres.
- **ECR** repo for the API image.
- **ECS Fargate cluster** with:
  - an **ALB + ACM certificate** (`alb.tf`) — HTTPS :443 only, HTTP→HTTPS
    redirect; `api_endpoint` is `https://<alb-dns>`,
  - an **API service** (1 task, 0.25 vCPU/512 MiB, behind the ALB — SG
    ingress from the ALB security group only, no direct public exposure),
  - a **worker service** (1 task, same image, command override
    `python -m constat_api.worker`) consuming the collect queue
    (`sqs.tf`),
  - a **scan task definition** (same image, command overridden to
    `python -m constat_api.cli.aws --enqueue-all`) run **daily at 05:00
    UTC** by **EventBridge Scheduler**: it creates the collect job for all
    persisted collect_targets and enqueues account x region WorkItems on
    the SQS queue; the worker drains them and rule evaluation (all 8
    insight rules) chains automatically when the job completes.
- **SQS** (chantier 1.1): `constat-pilot-collect` queue (visibility
  timeout 900s, long polling, SSE-SQS encryption) + DLQ (redrive after
  3 receives). See "Collect modes" below.
- **CloudWatch alarm + SNS**: DLQ `ApproximateNumberOfMessagesVisible`
  > 0 for 5 min → `constat-pilot-ops-alerts` topic. Email subscription
  via `var.ops_alert_email` (empty default = alarm exists but emails
  nobody).
- **Secrets Manager**: `CONSTAT_API_KEY` and `CONSTAT_DATABASE_URL` —
  injected into containers as env vars. The `scan-targets` secret is
  **deprecated (2026-07-19, no consumer left)** but kept so the next
  `apply` doesn't destroy it (Secrets Manager deletion has a 7–30 day
  recovery window); see `secrets.tf`.
- **IAM**: execution role (ECR/secrets/logs), API task role
  (`sts:AssumeRole` into prospect `constat-collector*` roles — ExternalId
  is enforced by the prospect's trust policy, F-06 — plus
  `sqs:SendMessage` on the collect queue), worker task role (SQS consume
  + DLQ read + the same `sts:AssumeRole`), scheduler role.
- **CloudWatch** log group `/ecs/constat-pilot`, 30-day retention.

## Collect modes (inline vs sqs)

`CONSTAT_COLLECT_MODE` selects how `POST /collect/aws` executes:

- **`inline`** (code default; local dev): the request scans
  synchronously and returns 200 with per-account results. Fine for one
  account, one region.
- **`sqs`** (deployed here): the API enqueues one WorkItem per
  (account, region) on the collect queue and returns **202 + job_id**;
  the worker service consumes them with `CONSTAT_WORKER_CONCURRENCY=4`
  in-process concurrency. Job progress: `GET /collect/aws/jobs/{job_id}`.

Queue URLs come from the `collect_queue_url` / `collect_dlq_url`
outputs. A WorkItem that fails 3 receives lands in the DLQ and trips
the CloudWatch alarm above — the operator re-scans that one region via
the API (runbook: `docs/operations/alerting.md`), never via psql.
Scaling the worker = `desired_count` (see the SCALING NOTE in
`ecs.tf`); re-evaluate only after the real staging bench (chantier 1.5,
`scripts/bench_real.py`).

## Prerequisites

- Terraform >= 1.7 (or OpenTofu), AWS credentials with admin-ish rights
  in the pilot account, Docker (to build/push the image).
- The prospect-side role (`constat-collector` in their account, trusting
  our account + ExternalId) is created by the prospect from
  [`customer-iam-role.yaml`](./customer-iam-role.yaml) (P1 item 2) —
  the deploy instructions are in
  [`customer-iam-role.md`](./customer-iam-role.md). We do not deploy
  it; the prospect does.

## First apply — before you start

1. **Add git ignores** (not covered by the repo `.gitignore` as of
   2026-07-18): `infra/.terraform/`, `infra/*.tfstate*`,
   `infra/terraform.tfvars`. Without this you will commit secrets.
2. `cp terraform.tfvars.example terraform.tfvars` and fill it in (or use
   `TF_VAR_*` env vars for the sensitive values).

## Build and push the image

The image is built from the repo-root `Dockerfile` (uv workspace, Python
3.13). The ECR repo must exist first, so apply Terraform once before the
first push — the API service will fail to pull until step 3; that is
expected and self-heals.

```bash
# 1. Create the infra (ECR repo included)
cd infra
terraform init
terraform plan
terraform apply

# 2. Build + push (from the repo root)
ECR_URL=$(terraform -chdir=infra output -raw ecr_repository_url)
aws ecr get-login-password --region eu-west-3 \
  | docker login --username AWS --password-stdin "$ECR_URL"
docker build -t "$ECR_URL:latest" .
docker push "$ECR_URL:latest"
```

## Apply the DB migrations

Migrations are managed by Alembic (`db/alembic/`, see ADR-17) and are
NOT applied at container start. RDS is private, so run `alembic
upgrade head` from a one-off Fargate task in the cluster — the image
ships the alembic CLI and the project source. Write this override file
locally:

```json
{
  "containerOverrides": [{
    "name": "scan",
    "command": [
      "alembic", "-c", "db/alembic.ini", "upgrade", "head"
    ]
  }]
}
```

then:

```bash
aws ecs run-task --cluster constat-pilot --launch-type FARGATE \
  --task-definition <scan-task-def-arn-from-outputs> \
  --network-configuration "awsvpcConfiguration={subnets=[<subnet>],securityGroups=[<app-sg>],assignPublicIp=ENABLED}" \
  --overrides file://migrate-override.json
```

(Watch the task's `scan` log stream for errors; alembic prints
each revision it applies. The bootstrap container does NOT pre-create
the schema — apply this one-off before the API task definition is
rolled out.)

After migrations land, force a fresh API deployment so it boots against a
migrated schema:

```bash
aws ecs update-service --cluster constat-pilot --service constat-pilot-api --force-new-deployment
```

## Day to day

- **New image**: build/push as above, then `update-service
  --force-new-deployment`.
- **Manual scan**: `aws ecs run-task` with the scan task definition (see
  `docs/operations/deployment.md`), or `POST /collect/aws` on the API —
  both go through the queue; rule evaluation chains automatically when
  the collect job completes (no separate `POST /insights/run` needed).
- **Find the API endpoint**: `terraform output api_endpoint` — stable
  (`https://<alb-dns>`), no ephemeral-IP lookup after redeploys.

## Cost ballpark (estimate — NOT measured)

On-demand list prices, eu-west-3-ish, 2026-07; verify with the AWS
Pricing Calculator before quoting anyone:

| Item | ~USD/month |
|---|---|
| RDS db.t4g.micro single-AZ + 20 GB gp3 | ~15 |
| Fargate API task 24/7 (0.25 vCPU / 0.5 GB) | ~9 |
| Fargate worker task 24/7 (0.25 vCPU / 0.5 GB) | ~9 |
| ALB + ACM (TLS termination) | ~16 |
| Daily scan task (~15 min/day) | <1 |
| SQS (standard queue, <1M requests/month) | <1 |
| SNS + CloudWatch alarm (1 topic, 1 alarm) | <1 |
| Secrets Manager (3 secrets) | ~1.2 |
| ECR + CloudWatch logs (<1 GB) | ~1 |
| **Total** | **~54** |

Sources: aws.amazon.com/rds/postgresql/pricing, /fargate/pricing,
/secrets-manager/pricing, /elasticloadbalancing/pricing, /sqs/pricing.
The avoided cost is the point: no NAT gateway (~$32) — it's on the
post-pilot hardening list.
