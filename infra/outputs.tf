output "ecr_repository_url" {
  description = "Push the API image here (see infra/README.md)."
  value       = aws_ecr_repository.api.repository_url
}

output "db_endpoint" {
  description = "RDS endpoint (host:port). Private — reachable from ECS tasks only."
  value       = aws_db_instance.main.endpoint
}

output "secret_arns" {
  description = "Secrets Manager ARNs for the runtime secrets."
  value = {
    api_key      = aws_secretsmanager_secret.api_key.arn
    database_url = aws_secretsmanager_secret.database_url.arn
    scan_targets = aws_secretsmanager_secret.scan_targets.arn
  }
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "collect_queue_url" {
  description = "URL of the async-collection SQS queue (CONSTAT_COLLECT_QUEUE_URL for API + worker tasks)."
  value       = aws_sqs_queue.collect.url
}

output "collect_dlq_url" {
  description = "URL of the collect dead-letter queue. Non-empty DLQ = a region scan failed 3 times (runbook: docs/operations/alerting.md)."
  value       = aws_sqs_queue.collect_dlq.url
}

output "ops_alerts_topic_arn" {
  description = "SNS topic for ops alerts (DLQ alarm). Subscribe an email via var.ops_alert_email."
  value       = aws_sns_topic.ops_alerts.arn
}

output "worker_service_name" {
  description = "ECS service consuming the collect queue (python -m constat_api.worker)."
  value       = aws_ecs_service.worker.name
}

output "scan_task_definition_arn" {
  description = "Use with `aws ecs run-task` to trigger a scan manually."
  value       = aws_ecs_task_definition.scan.arn
}

output "api_endpoint" {
  description = <<-EOT
    HTTPS endpoint for the operator dashboard. The ALB DNS name is
    stable across deploys (only the underlying tasks rotate). Point
    a Route 53 alias (or external DNS CNAME) at this name and the
    ACM certificate (var.public_domain) lights up.

    Until DNS is configured, this resolves to an AWS-owned
    *.elb.amazonaws.com hostname that won't accept traffic for your
    domain. Apply Terraform, then add the DNS record.
  EOT
  value       = "https://${aws_lb.main.dns_name}"
}

output "api_certificate_status" {
  description = <<-EOT
    The ACM certificate must be ISSUED before the ALB listener will
    accept traffic. Status goes PENDING_VALIDATION → ISSUED once the
    DNS validation records are in place. Watch with:
      aws acm describe-certificate \
        --certificate-arn <the certificate_arn output> \
        --query 'Certificate.Status' --output text
    (terraform validate: output descriptions cannot interpolate
    unknown resource attributes — the ARN stays a placeholder here.)
  EOT
  value       = aws_acm_certificate.main.status
}
