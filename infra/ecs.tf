# ECS Fargate: one cluster, two task definitions from the same image.
#
# EXPOSURE DECISION (no ALB for V1): the API service runs with
# assign_public_ip = true and a security group that only allows port 8000
# from var.allowed_cidr.
#   Why not an ALB: ~$16/month fixed + LCU charges, and it only pays off
#     with TLS termination (ACM) — a whole certificate/DNS ceremony the
#     pilot does not need for a handful of operator callers on a known
#     network.
#   The honest trade-off: traffic is plain HTTP, so the X-API-Key header
#     crosses the internet unencrypted, and the service's public IP is
#     ephemeral (changes on redeploy — see outputs.tf for how to look it
#     up). The CIDR restriction + API key are the compensating controls.
#     ALB + ACM + TLS is the first item on the post-pilot hardening list.

resource "aws_ecs_cluster" "main" {
  name = local.name
}

# --- Security groups ---
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "ECS tasks (API service + scheduled scan)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "API from the allowed operator/prospect CIDR only"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
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
    assign_public_ip = true # see EXPOSURE DECISION above
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
