# ECS Fargate: one cluster, three task definitions from the same image
# (API service, worker service, one-off scan task).
#
# EXPOSURE DECISION (P0-3 hardening): the API service runs without
# a public IP and is reachable only through the ALB (see alb.tf).
#   Why an ALB: the X-API-Key cannot cross the internet in plaintext.
#     TLS termination at the ALB keeps the API key encrypted on the
#     browser ↔ ALB leg; the ALB ↔ ECS leg is HTTP inside the VPC
#     (private subnets, no internet egress). Without an ALB the
#     "API key in HTTPS" pitch to a SOC2 questionnaire falls apart.
#   The honest trade-off: ~$16/month fixed + LCU charges. We pay it
#     because TLS is non-negotiable for the operator dashboard.

resource "aws_ecs_cluster" "main" {
  name = local.name
}

# --- Security groups ---
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "ECS tasks (API service + scheduled scan)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "API from the ALB only (no public internet ingress)"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    # Tasks call AWS APIs (ECR, Secrets Manager, STS, CloudWatch) and RDS.
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "RDS PostgreSQL — reachable from the ECS tasks only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "PostgreSQL from ECS tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

# --- API task definition (long-running service) ---
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256 # 0.25 vCPU / 512 MiB is the smallest Fargate shape
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [
      { name = "CONSTAT_ENV", value = "pilot" },
      { name = "CONSTAT_LOG_JSON", value = "1" },
      # Async collection (chantier 1.1): in "sqs" mode POST /collect/aws
      # enqueues WorkItems (account x region) and returns 202 + job_id
      # instead of scanning inline. The worker service below consumes
      # them. "inline" (the code default) keeps the old synchronous path
      # for local dev.
      { name = "CONSTAT_COLLECT_MODE", value = "sqs" },
      { name = "CONSTAT_COLLECT_QUEUE_URL", value = aws_sqs_queue.collect.url },
    ]

    secrets = [
      { name = "CONSTAT_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "CONSTAT_API_KEY", valueFrom = aws_secretsmanager_secret.api_key.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    # python is in the image; curl is not (slim base).
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/health',timeout=5)\" || exit 1"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 30
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    # In sqs collect mode the API task must EGRESS to SQS (SendMessage),
    # and the pilot has no NAT gateway or VPC endpoints, so it needs a
    # public IP exactly like the scan task does for AWS API egress. This
    # does not re-open inbound exposure: the SG's only ingress rule is
    # from the ALB SG (see the EXPOSURE DECISION above).
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Rolling deploys: ECS will register the new task, wait for the
  # health check to pass, then drain the old one. Combined with the
  # health_check on the target group, this gives a zero-downtime
  # deploy for free (the target group's health_check on /health
  # is the gate).
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

# --- Scan task definition (one-off, run daily by the scheduler) ---
# Same image, command overridden to a single enqueue call: the scan task
# no longer collects directly. It creates the collect job for ALL
# persisted collect_targets and enqueues account x region WorkItems on
# the SQS collect queue; the worker service drains them and rule
# evaluation chains automatically when the job completes. No more
# in-task run_insights, no more targets JSON file — the queue + worker
# do the rest, so a re-scan is one path (the queue) instead of two.
resource "aws_ecs_task_definition" "scan" {
  family                   = "${local.name}-scan"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  # The scan task now only talks to the DB + SQS (SendMessage). It no
  # longer assumes prospect roles — the WORKER does the scanning. It
  # shares the API's task role for sqs:SendMessage (that role still
  # carries sts:AssumeRole the scan task doesn't use; a dedicated
  # narrower role is post-pilot hardening, noted in iam.tf).
  task_role_arn = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "scan"
    image     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
    essential = true

    command = ["python", "-m", "constat_api.cli.aws", "--enqueue-all"]

    environment = [
      { name = "CONSTAT_ENV", value = "pilot" },
      { name = "CONSTAT_LOG_JSON", value = "1" },
      { name = "CONSTAT_COLLECT_MODE", value = "sqs" },
      { name = "CONSTAT_COLLECT_QUEUE_URL", value = aws_sqs_queue.collect.url },
    ]

    secrets = [
      { name = "CONSTAT_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "scan"
      }
    }
  }])
}

# --- Worker service (long-running, consumes the collect queue) ---
# Same image as the API, command overridden to the queue consumer
# (python -m constat_api.worker). WorkItems are account x region; the
# worker scans the region, writes facts, and deletes the message. See
# sqs.tf for the queue and its visibility-timeout rationale.
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  # Dedicated task role: SQS consume permissions + sts:AssumeRole into
  # prospect accounts (the worker is the process that actually scans).
  task_role_arn = aws_iam_role.task_worker.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
    essential = true

    command = ["python", "-m", "constat_api.worker"]

    environment = [
      { name = "CONSTAT_ENV", value = "pilot" },
      { name = "CONSTAT_LOG_JSON", value = "1" },
      { name = "CONSTAT_COLLECT_MODE", value = "sqs" },
      { name = "CONSTAT_COLLECT_QUEUE_URL", value = aws_sqs_queue.collect.url },
      # In-process concurrency of WorkItem handlers within ONE worker
      # task. 4 concurrent region scans per task is a conservative
      # starting point for boto3 I/O-bound work on 0.25 vCPU.
      { name = "CONSTAT_WORKER_CONCURRENCY", value = "4" },
    ]

    secrets = [
      { name = "CONSTAT_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  # SCALING NOTE: desired_count is the concurrency knob at ~35 accounts.
  # Total region-scan parallelism = desired_count x
  # CONSTAT_WORKER_CONCURRENCY (currently 1 x 4). Scale THIS (or add
  # queue-depth-based autoscaling) only after the real staging bench
  # (chantier 1.5, scripts/bench_real.py + docs/operations/benchmarks.md)
  # shows where the wall time actually goes — do not pre-scale on the
  # sqlite numbers.
  desired_count = 1
  launch_type   = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true # no NAT gateway in the pilot; the worker needs SQS/STS egress
  }

  # No load balancer: the worker has no inbound traffic. Crashes surface
  # via the circuit breaker below and via the DLQ alarm (sqs.tf).
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
