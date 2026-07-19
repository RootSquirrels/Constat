# Scheduled daily scan — the roadmap's "Scans planifiés" item.
#
# Mechanism: EventBridge Scheduler -> ECS RunTask (one-off Fargate task).
# Chosen over a CloudWatch Events rule+target because Scheduler is the
# current API (native retry policy, no separate rule/target wiring).
# The task it starts does NOT scan: it enqueues account x region
# WorkItems on the SQS collect queue (`cli.aws --enqueue-all`, see
# ecs.tf), and the worker service drains the queue — one orchestration
# path (the queue, shipped in chantier 1.1) for both scheduled and
# API-triggered collection. Rule evaluation chains automatically when
# the collect job completes.
#
# Cadence: DAILY at 05:00 UTC. The product's scope-freshness window is
# 24 h (a successful run older than that makes the scope INCONCLUSIVE),
# so daily is the slowest cadence that keeps every scope perpetually
# "fresh", and it costs ~15 task-minutes/day. 05:00 UTC is off-peak for
# FR prospects and lands before the working day, so insights are current
# when someone opens the app.

resource "aws_scheduler_schedule" "daily_scan" {
  name = "${local.name}-daily-scan"

  schedule_expression          = "cron(0 5 * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF" # run at the scheduled time, not "sometime within a window"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.scan.arn
      launch_type         = "FARGATE"

      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.app.id]
        assign_public_ip = true # no NAT gateway in the pilot; tasks need AWS API egress
      }
    }

    retry_policy {
      # One retry within the hour covers transient Fargate placement/API
      # failures. Anything more persistent is a bug to look at in the
      # "scan" log stream, not something to keep retrying all day.
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}
