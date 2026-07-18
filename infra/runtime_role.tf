# Wire the `constat_app` runtime role into the deployed cluster.
#
# Migration 0012 creates the role with a dev-only password ('constat')
# so that the docker-compose and test fixtures work. In prod, that
# password is wrong (or, more precisely, it's the same one anyone with
# the public repo can read). Terraform rotates it to `var.db_app_password`
# via a one-shot ALTER ROLE.
#
# Why a null_resource and not a postgres provider: this repo is
# AWS-only. Adding a third-party provider is a bigger commitment
# than a 20-line shell-out. If/when we onboard a second deploy target
# (GCP, on-prem), this is the spot to revisit.
#
# Lifecycle:
# - depends_on rds: instance must be reachable.
# - depends_on secrets: the runtime secret must exist (the ECS task
#   reads it on first boot; if the secret has the old DSN with user=constat,
#   the app silently runs as owner until the secret is rotated).
# - trigger by var.db_app_password: re-running `terraform apply` with a
#   new password re-applies the ALTER ROLE.

resource "null_resource" "rotate_constat_app_password" {
  triggers = {
    password   = var.db_app_password
    rds_id     = aws_db_instance.main.id
    secret_arn = aws_secretsmanager_secret.database_url.arn
  }

  depends_on = [
    aws_db_instance.main,
    aws_secretsmanager_secret_version.database_url,
  ]

  provisioner "local-exec" {
    # The ALTER ROLE is idempotent. We pin to a single command (no
    # heredoc) so the failure mode is "psql error" not "shell
    # expansion gone wrong". The password travels as a TF_VAR, never
    # as a literal in this file.
    command = <<-EOT
      PGPASSWORD='${var.db_password}' psql \
        --host='${aws_db_instance.main.address}' \
        --port='${aws_db_instance.main.port}' \
        --username='constat' \
        --dbname='constat' \
        -c "ALTER ROLE constat_app WITH LOGIN PASSWORD '${var.db_app_password}'"
    EOT
  }

  # Local-exec on destroy: rotate the password back to the dev value
  # so a destroy-then-reapply cycle works without leaving the role
  # in a state where the secret DSN is wrong. The dev value matches
  # migration 0012 and docker-compose, so the cluster is back to a
  # known state for the next apply.
  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      PGPASSWORD='${var.db_password}' psql \
        --host='${aws_db_instance.main.address}' \
        --port='${aws_db_instance.main.port}' \
        --username='constat' \
        --dbname='constat' \
        -c "ALTER ROLE constat_app WITH LOGIN PASSWORD 'constat'" || true
    EOT
    # || true: best-effort cleanup on a cluster that may already be gone.
    # The destroy on the rds instance happens in parallel; the cluster
    # may be unreachable by the time we get here. The next apply
    # re-creates the cluster (and the role's password is set to
    # var.db_app_password at that point), so a failure here is recoverable.
  }
}
