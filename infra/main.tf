# Shared foundations: provider, network data sources, ECR, logs.
#
# Network decision (pilot): we use the DEFAULT VPC and its public subnets
# instead of building a VPC. Reasons:
#   - Default-VPC subnets auto-assign public IPs, so Fargate tasks can pull
#     from ECR and reach Secrets Manager/RDS without a NAT gateway
#     (~$32/month each — the single biggest fixed cost we would otherwise
#     add for a pilot that must stay cheap).
#   - RDS stays `publicly_accessible = false`; "public subnet" only means
#     it CAN have a public route, not that the DB gets one.
# Private subnets + NAT (or VPC endpoints) are post-pilot hardening.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = "constat"
      Env     = "pilot"
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  name = "constat-pilot"
}

# --- ECR: one repo for the API image (same image serves the long-running
# API service and the one-off daily scan task, with a command override).
resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE" # pilot: we re-push `latest`; pin digests post-pilot

  image_scanning_configuration {
    scan_on_push = true # free basic scanning; cheap signal for a pilot
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images; expire the rest"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# --- Logs: one group for all task families, 30-day retention.
# 30 days matches the pilot's "look back a few weeks when something went
# wrong" need without paying for open-ended log storage.
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}
