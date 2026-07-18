# Secrets Manager: all runtime secrets live here and are injected into
# ECS containers as environment variables (never baked into the image).
#
# Caveat accepted for the pilot: the secret VALUES are passed through
# Terraform variables, so they land in the (local) terraform state file.
# Protect tfstate accordingly; moving to out-of-band secret creation or
# RDS-managed master passwords is post-pilot hardening.

resource "aws_secretsmanager_secret" "api_key" {
  name = "${local.name}/api-key"
  # No rotation for V1: the API reads CONSTAT_API_KEY at process start, so
  # rotating underneath a running task would 401 live traffic. Rotate by
  # updating the variable + redeploying.
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = var.api_key
}

resource "aws_secretsmanager_secret" "database_url" {
  name = "${local.name}/database-url"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  # The runtime DSN uses `constat_app` (created by migration 0012),
  # NOT `constat` (the owner). The owner has DDL + ALTER POLICY
  # rights; the runtime role has DML only and is bound by RLS. See
  # architecture doc §11.2 and known-issues.md §2.
  secret_string = "postgresql://constat_app:${var.db_app_password}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/constat"
}

resource "aws_secretsmanager_secret" "scan_targets" {
  name = "${local.name}/scan-targets"
  # Contains the per-prospect ExternalId (a shared secret, F-06) — that is
  # why this is a secret and not plain config.
}

resource "aws_secretsmanager_secret_version" "scan_targets" {
  secret_id     = aws_secretsmanager_secret.scan_targets.id
  secret_string = var.scan_targets_json
}
