# IAM: four roles with strictly separated concerns.
#
#   - task execution role: used by the ECS AGENT to pull the image, read
#     secrets, and write logs. Never seen by the application.
#   - task role: the API's identity. Business permissions: sts:AssumeRole
#     into prospect accounts (the SaaS cross-account pattern, see
#     apps/api/src/constat_api/collectors/aws.py) and sqs:SendMessage on
#     the collect queue (async collection, see sqs.tf). The scheduled
#     scan task shares this role but uses only sqs:SendMessage — it
#     enqueues WorkItems (--enqueue-all) and never scans, so its
#     sts:AssumeRole grant is now unused by that task. A dedicated,
#     narrower scan-task role is post-pilot hardening (it would also
#     need the scheduler's iam:PassRole updated).
#   - worker task role: the queue consumer's identity. SQS consume on the
#     collect queue (+ read on the DLQ) and the same sts:AssumeRole —
#     the worker is the process that actually scans.
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
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.api_key.arn,
        aws_secretsmanager_secret.database_url.arn,
        # scan_targets is deliberately NOT here anymore: the secret is
        # deprecated (see secrets.tf) and no container injects it.
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

# In sqs collect mode the API enqueues WorkItems (POST /collect/aws ->
# 202 + job_id). It needs SendMessage ONLY: it never consumes.
resource "aws_iam_role_policy" "task_enqueue_collect" {
  name = "enqueue-collect-workitems"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = [aws_sqs_queue.collect.arn]
    }]
  })
}

# --- Worker task role (queue consumer identity) ---
# Separate role from the API's: the API enqueues, the worker consumes.
# Splitting them keeps each side's SQS permissions minimal (least
# privilege) and makes CloudTrail attribution unambiguous.
resource "aws_iam_role" "task_worker" {
  name               = "${local.name}-task-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# The worker runs the actual region scans, so it needs the cross-account
# AssumeRole into prospect accounts (the scan task only enqueues now).
resource "aws_iam_role_policy" "task_worker_assume_prospect_roles" {
  name = "assume-prospect-collector-roles"
  role = aws_iam_role.task_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sts:AssumeRole"]
      Resource = ["arn:aws:iam::*:role/constat-collector*"]
    }]
  })
}

resource "aws_iam_role_policy" "task_worker_consume_collect" {
  name = "consume-collect-workitems"
  role = aws_iam_role.task_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The consume triplet: receive, delete on success, extend
        # visibility while a slow region scan is in flight.
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = [aws_sqs_queue.collect.arn]
      },
      {
        # DLQ read access so the worker (or an in-image redrive helper)
        # can inspect failed items. No DeleteMessage here: draining the
        # DLQ is a deliberate operator action, not a code path.
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [aws_sqs_queue.collect_dlq.arn]
      },
    ]
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
