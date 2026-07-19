# SQS: async collection queue (roadmap chantier 1.1) — UNVALIDATED, see infra/README.md.
#
# This file is the first deviation from the "a Fargate task + cron, no
# Step Functions/SQS" line in AGENTS.md. Justification (one paragraph, as
# required): at ICP scale (~35 accounts x N regions), a single inline
# POST /collect/aws call serializes hundreds of region scans behind one
# HTTP request and one Fargate task. WorkItems (account x region) on a
# queue let a worker service consume regions in parallel with at-least-
# once delivery and a DLQ for poison items, without buying Step
# Functions. The queue is the smallest primitive that removes the
# serialization; we add nothing else.
#
# Delivery semantics: the worker is idempotent per WorkItem (re-running
# a region scan is delete-and-replace at the fact layer), so standard
# (not FIFO) SQS is sufficient and cheaper to reason about.

resource "aws_sqs_queue" "collect" {
  name = "${local.name}-collect"

  # One WorkItem = one (account, region) scan. The worker's per-region
  # scan budget is ~15 minutes (adaptive retry absorbs throttling before
  # the budget is spent); the visibility timeout must cover the WORST
  # case so a slow region is not redelivered mid-scan and executed twice.
  # 900s = 15 min, matching that budget exactly.
  visibility_timeout_seconds = 900

  # Long polling: the worker's ReceiveMessage waits up to 20s for items,
  # which collapses empty-queue polls (cost + hot-loop noise) without
  # delaying real work.
  receive_wait_time_seconds = 20

  # Daily scan cadence + operator-triggered re-scans: 4 days of retention
  # is far beyond any realistic consumer outage we would tolerate without
  # noticing via the DLQ alarm below.
  message_retention_seconds = 345600

  # Encryption at rest via SQS-managed SSE (SSE-SQS, AWS-owned key).
  # This matches rds.tf, which sets storage_encrypted = true and accepts
  # the AWS-managed key: a customer-managed KMS key would add key-policy
  # surface (SQS + ECS task roles + SNS) for a queue whose payloads
  # contain only account IDs and region names. Revisit if WorkItems ever
  # carry secrets (they must not — ExternalId stays in Secrets Manager).
  sqs_managed_sse_enabled = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.collect_dlq.arn
    # 3 receives without a delete = the item consistently crashes or
    # times out the worker. Beyond that it is a poison item: park it in
    # the DLQ and alert, rather than burning scan budget forever.
    maxReceiveCount = 3
  })
}

resource "aws_sqs_queue" "collect_dlq" {
  name = "${local.name}-collect-dlq"

  # DLQ items are diagnostic evidence: keep them 14 days so an operator
  # can inspect/redrive after a weekend without losing the failure.
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = true
}

# --- Alerting: anything in the DLQ is a permanently failed region scan ---
#
# This CloudWatch alarm is the PRIMARY alerting path for the async
# collection pipeline (the Prometheus rule in deploy/prometheus/alerts.yml
# is the secondary, in-process signal). Any visible DLQ message means a
# WorkItem failed 3 times: a region's inventory is silently unproven
# until an operator re-scans it (runbook: docs/operations/alerting.md).

resource "aws_sns_topic" "ops_alerts" {
  name = "${local.name}-ops-alerts"
}

resource "aws_sns_topic_subscription" "ops_alerts_email" {
  # Empty var.ops_alert_email (the default) = no subscription: the topic
  # and alarm still exist and the alarm state is visible in the console,
  # but nobody is emailed. Set the variable to wire a real mailbox.
  count     = var.ops_alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.ops_alert_email
}

resource "aws_cloudwatch_metric_alarm" "collect_dlq_depth" {
  alarm_name        = "${local.name}-collect-dlq-not-empty"
  alarm_description = "WorkItems in the collect DLQ: a (account, region) scan failed 3 times. Runbook: docs/operations/alerting.md."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = aws_sqs_queue.collect_dlq.name
  }

  # > 0 for 5 consecutive minutes. ApproximateNumberOfMessagesVisible is
  # emitted per minute; 5 periods filter the redrive-in-progress window
  # where a message lands and is immediately drained by an operator.
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # An idle DLQ emits no datapoints; "no data" must mean OK, not alarm.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}
