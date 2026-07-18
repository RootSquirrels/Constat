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
    CIDR allowed to reach the API on port 8000 (operator/prospect network,
    e.g. an office egress IP as x.x.x.x/32). V1 has no ALB: the Fargate
    service gets a public IP and the security group is the only network
    control, so keep this tight.
  EOT
  type        = string
}

variable "scan_targets_json" {
  description = <<-EOT
    JSON array of AWS collection targets consumed by
    `python -m constat_api.cli.aws --targets`, e.g.
    [{"aws_account_id":"...","role_arn":"arn:aws:iam::<acct>:role/constat-collector","external_id":"...","name":"prospect","regions":["eu-west-3"]}]
    Stored as a Secrets Manager secret because it contains the ExternalId
    shared secret (F-06). The scheduled scan task writes it to a file and
    runs the collect CLI against it.
  EOT
  type        = string
  sensitive   = true
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
