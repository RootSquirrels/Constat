variable "aws_region" {
  description = "AWS region for the pilot environment."
  type        = string
  default     = "eu-west-3" # Paris: pilot prospects are FR-based; keeps data in-region.
}

variable "db_password" {
  description = "Master password for the pilot RDS instance. Used to bootstrap the cluster and to ALTER the runtime role's password at apply time."
  type        = string
  sensitive   = true
  # No default on purpose: must be supplied via tfvars or TF_VAR_db_password.
}

variable "db_app_password" {
  description = <<-EOT
    Password for the `constat_app` runtime role (created by migration
    0012). The application (API, collector, CLI) connects as this role
    in prod, NOT as `constat` (the owner). Owner has DDL and ALTER
    POLICY rights; the runtime role has DML only and is bound by RLS.
    See architecture doc §11.2.
  EOT
  type        = string
  sensitive   = true
  # No default on purpose: must be supplied via tfvars or TF_VAR_db_app_password.
}

variable "api_key" {
  description = "Value of CONSTAT_API_KEY. Every API request (except /health) must carry it in X-API-Key."
  type        = string
  sensitive   = true
}

variable "image_tag" {
  description = "Tag of the constat-api image in ECR to deploy (built from the repo-root Dockerfile)."
  type        = string
  default     = "latest"
}

variable "allowed_cidr" {
  description = <<-EOT
    CIDR allowed to reach the ALB on 80/443 (operator/prospect network,
    e.g. an office egress IP as x.x.x.x/32). The ALB is the only
    public-facing surface; ECS tasks live in a private subnet reachable
    only from the ALB. Keep this tight.
  EOT
  type        = string
}

variable "public_domain" {
  description = <<-EOT
    Prospect-facing DNS name for the TLS endpoint. The ACM certificate
    is created for this name; DNS validation records must be added to
    the corresponding Route 53 zone (or external DNS) before the ALB
    listener will accept traffic. Set to a placeholder for the first
    apply (e.g. "pilot.constat.example.com") and update with the real
    prospect domain before go-live.
  EOT
  type        = string
  default     = "pilot.constat.example.com"
}

variable "scan_targets_json" {
  description = <<-EOT
    DEPRECATED (2026-07-19): no consumer left. The scheduled scan task now
    runs `python -m constat_api.cli.aws --enqueue-all`, which builds
    WorkItems from the persisted collect_targets (managed via
    POST /collect/targets/import or the onboard CLI) instead of a targets
    JSON file. The variable is still wired to the deprecated
    `scan_targets` secret (kept so the first apply doesn't destroy it —
    see secrets.tf); remove both in a follow-up.
  EOT
  type        = string
  sensitive   = true
}

variable "ops_alert_email" {
  description = <<-EOT
    Email address subscribed to the constat-ops-alerts SNS topic (DLQ
    depth alarm, see sqs.tf). Empty string (default) = no subscription:
    the topic and alarm are still created and the alarm state is visible
    in the console, but nobody is emailed. Set it before the staging
    gate so the DLQ alarm pages someone.
  EOT
  type        = string
  default     = ""
}

variable "db_engine_version" {
  description = <<-EOT
    PostgreSQL engine version for RDS. Minor versions rotate as AWS
    deprecates them; if apply fails with an invalid version, list current
    ones with: aws rds describe-db-engine-versions --engine postgres
    --query 'DBEngineVersions[].EngineVersion'
  EOT
  type        = string
  default     = "16.4"
}
