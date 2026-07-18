# STATUS (2026-07-18): UNAPPLIED and UNVALIDATED.
# No terraform/tofu binary and no AWS account on the dev machine — this
# configuration has never been through `terraform validate`, `plan`, or
# `apply`. Treat it as a careful draft to validate on first real use.

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state for the pilot: a single operator applies from their own
  # machine. An S3 backend + DynamoDB locking is on the post-pilot
  # hardening list (docs/operations/deployment.md), not V1.
}
