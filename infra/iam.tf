# IAM: three roles with strictly separated concerns.
#
#   - task execution role: used by the ECS AGENT to pull the image, read
#     secrets, and write logs. Never seen by the application.
#   - task role: the APPLICATION's identity. Its only business permission
#     is sts:AssumeRole into prospect accounts (the SaaS cross-account
#     pattern, see apps/api/src/constat_api/collectors/aws.py).
#   - scheduler role: lets EventBridge Scheduler run the scan task.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Task execution role (ECS agent) ---
resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name = "read-app-secrets"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.api_key.arn,
        aws_secretsmanager_secret.database_url.arn,
        aws_secretsmanager_secret.scan_targets.arn,
      ]
    }]
  })
}

# --- Task role (application identity) ---
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "task_assume_prospect_roles" {
  name = "assume-prospect-collector-roles"
  role = aws_iam_role.task.id

  # The collector assumes a read-only role in each PROSPECT account.
  # The ExternalId is NOT conditioned here: it is enforced on the prospect
  # side, in their role's trust policy (Condition StringEquals
  # sts:ExternalId). The collector refuses to call STS without an
  # ExternalId (F-06, confused-deputy protection), so the IaC only needs
  # to scope WHICH roles may be assumed.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sts:AssumeRole"]
      Resource = ["arn:aws:iam::*:role/constat-collector*"]
    }]
  })
}

# --- Scheduler role (EventBridge Scheduler -> ECS RunTask) ---
resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["sts:AssumeRole"]
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_run_scan" {
  name = "run-daily-scan-task"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.scan.arn_without_revision]
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        # RunTask must be allowed to pass BOTH roles to the task.
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.task.arn, aws_iam_role.task_execution.arn]
      },
    ]
  })
}
