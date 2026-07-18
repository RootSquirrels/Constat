# ECS Fargate: one cluster, two task definitions from the same image.
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
    assign_public_ip = false # private; reachable only via the ALB SG
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
# Same image, command overridden: write the targets JSON from the injected
# secret to a file, run the AWS collect CLI, then both insight rules.
# Insights run AFTER collect in the same task so they always score fresh
# facts (the 24h scope-freshness window then never expires between scans).
resource "aws_ecs_task_definition" "scan" {
  family                   = "${local.name}-scan"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn # needs sts:AssumeRole into prospect accounts

  container_definitions = jsonencode([{
    name      = "scan"
    image     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
    essential = true

    command = [
      "sh", "-c",
      join(" && ", [
        "printf '%s' \"$CONSTAT_SCAN_TARGETS_JSON\" > /tmp/targets.json",
        "python -m constat_api.cli.aws --targets /tmp/targets.json",
        "python -m constat_api.cli.run_insights --rule rds_eol",
        "python -m constat_api.cli.run_insights --rule chargeback",
      ])
    ]

    environment = [
      { name = "CONSTAT_ENV", value = "pilot" },
      { name = "CONSTAT_LOG_JSON", value = "1" },
    ]

    secrets = [
      { name = "CONSTAT_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "CONSTAT_SCAN_TARGETS_JSON", valueFrom = aws_secretsmanager_secret.scan_targets.arn },
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
