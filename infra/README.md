# infra/ — pilot environment (Terraform)

> **STATUS (2026-07-18): UNAPPLIED and UNVALIDATED.**
> No `terraform`/`tofu` binary and no AWS account exist on the dev machine
> this was written on, so none of this configuration has been through
> `terraform validate`, `plan`, or `apply`. The same applies to the
> repo-root `Dockerfile` (never built — no Docker daemon available).
> Expect a short fix-up pass on first real use.

Minimal IaC for the **single pilot environment**. No modules, no remote
state, no multi-env ceremony — deliberately. V1 deployment philosophy
(AGENTS.md): a Fargate task + cron, no Step Functions/SQS.

## What it creates

- **RDS PostgreSQL 16** (`db.t4g.micro`, single-AZ, 20 GB gp3, 7-day
  backups, private) — replaces local docker-compose Postgres.
- **ECR** repo for the API image.
- **ECS Fargate cluster** with:
  - an **API service** (1 task, 0.25 vCPU/512 MiB, public IP, SG ingress
    restricted to `allowed_cidr` on port 8000 — no ALB, see the EXPOSURE
    DECISION comment in `ecs.tf`),
  - a **scan task definition** (same image, command overridden) run
    **daily at 05:00 UTC** by **EventBridge Scheduler**: AWS collect CLI
    then the `rds_eol` and `chargeback` insight rules.
- **Secrets Manager**: `CONSTAT_API_KEY`, `CONSTAT_DATABASE_URL`, and the
  scan-targets JSON — injected into containers as env vars.
- **IAM**: execution role (ECR/secrets/logs), task role (`sts:AssumeRole`
  into prospect `constat-collector*` roles; ExternalId is enforced by the
  prospect's trust policy, F-06), scheduler role.
- **CloudWatch** log group `/ecs/constat-pilot`, 30-day retention.

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

Migrations are raw SQL in `db/migrations/` (no Alembic yet) and are NOT
applied at container start. RDS is private, so run them from a one-off
Fargate task in the cluster — the image ships `psycopg2` and the SQL
files under `/app/db/migrations/`. Write this override file locally:

```json
{
  "containerOverrides": [{
    "name": "scan",
    "command": [
      "python", "-c",
      "import glob,os,psycopg2; con=psycopg2.connect(os.environ['CONSTAT_DATABASE_URL']); [con.cursor().execute(open(f).read()) for f in sorted(glob.glob('/app/db/migrations/*.sql'))]; con.commit()"
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

(Watch the task's `scan` log stream for errors; each migration file is
executed in name order, same as docker-entrypoint-initdb.d does locally.)

After migrations land, force a fresh API deployment so it boots against a
migrated schema:

```bash
aws ecs update-service --cluster constat-pilot --service constat-pilot-api --force-new-deployment
```

## Day to day

- **New image**: build/push as above, then `update-service
  --force-new-deployment`.
- **Manual scan**: `aws ecs run-task` with the scan task definition (see
  `docs/operations/deployment.md`), or `POST /collect/aws` +
  `POST /insights/run` on the API.
- **Find the API's current public IP**: see the `api_endpoint` output
  description (`terraform output api_endpoint`).

## Cost ballpark (estimate — NOT measured)

On-demand list prices, eu-west-3-ish, 2026-07; verify with the AWS
Pricing Calculator before quoting anyone:

| Item | ~USD/month |
|---|---|
| RDS db.t4g.micro single-AZ + 20 GB gp3 | ~15 |
| Fargate API task 24/7 (0.25 vCPU / 0.5 GB) | ~9 |
| Daily scan task (~15 min/day) | <1 |
| Secrets Manager (3 secrets) | ~1.2 |
| ECR + CloudWatch logs (<1 GB) | ~1 |
| **Total** | **~27** |

Sources: aws.amazon.com/rds/postgresql/pricing, /fargate/pricing,
/secrets-manager/pricing. The avoided costs are the point: no NAT gateway
(~$32), no ALB (~$16+) — both are on the post-pilot hardening list.
