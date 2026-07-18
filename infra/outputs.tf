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

output "scan_task_definition_arn" {
  description = "Use with `aws ecs run-task` to trigger a scan manually."
  value       = aws_ecs_task_definition.scan.arn
}

output "api_endpoint" {
  description = <<-EOT
    The API listens on port 8000 but Fargate public IPs are ephemeral, so
    there is no static endpoint to output. Resolve the current IP with:
      aws ecs list-tasks --cluster ${local.name} --service-name ${local.name}-api \
        --query 'taskArns[0]' --output text | xargs -I{} \
      aws ecs describe-tasks --cluster ${local.name} --tasks {} \
        --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text | xargs -I{} \
      aws ec2 describe-network-interfaces --network-interface-ids {} \
        --query 'NetworkInterfaces[0].Association.PublicIp' --output text
    Then: http://<ip>:8000 (plain HTTP — see EXPOSURE DECISION in ecs.tf).
  EOT
  value       = "http://<ephemeral-public-ip>:8000 — resolve via the commands in this output's description"
}
