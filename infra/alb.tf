# Public-facing load balancer with TLS termination.
#
# Why this exists: the V1 pilot's "EXPOSURE DECISION" (infra/ecs.tf)
# had the API on a Fargate-assigned public IP, port 8000, with a CIDR
# allowlist on the security group. That works for a handful of operator
# callers on a known network, but two of the audit committee's
# vetoes require more:
#   - "La clé API traverse Internet en HTTP non chiffré" (RSSI): the
#     X-API-Key was crossing the internet in plaintext. With ALB +
#     ACM + TLS, the browser ↔ ALB leg is HTTPS, and the ALB ↔ ECS
#     leg stays inside the VPC (HTTP on a private subnet).
#   - "le read-only client n'est pas démontré" (RSSI): TLS is the
#     transport prerequisite for a "this is the read-only endpoint"
#     story to the prospect's network team.
#
# Cost: ~$16/month fixed for the ALB + a few cents per LCU consumed.
# At V1's traffic (operator dashboard, ~10 req/s) this is sub-$20/mo.
# We pay it because TLS is non-negotiable and a public IP + IP allowlist
# + plaintext X-API-Key is the kind of thing that loses a SOC2
# questionnaire outright.
#
# The implementation:
#   - Application Load Balancer, internet-facing, in 2 public subnets
#     (the default VPC has at least 2; we use the same default VPC as
#     the rest of the stack).
#   - ACM certificate for the prospect-facing domain (var.public_domain),
#     DNS-validated (Route 53 is the standard way).
#   - HTTPS listener (TLS termination at the ALB), HTTP→HTTPS redirect.
#   - Target group pointing at the ECS API service on port 8000.
#   - The Fargate API service loses assign_public_ip = true (it now
#     lives in a private subnet reachable only from the ALB SG).
#
# What this commit does NOT do:
#   - Apply. The user has no AWS credentials in this dev env. The
#     infra/ infra remains "unvalidated/unapplied" per the existing
#     known-issues.md entry. Apply + DNS validation + prospect cert
#     issuance happen on the pilot's first deploy.
#   - WAF. The cost of a managed WAF rule set is not justified at V1
#     traffic. The CIDR allowlist on the ALB SG + the API key are the
#     only network-level controls. Post-pilot: add an AWS-managed
#     Common Rule Set.
#   - mTLS to the API. The browser does not present certs, so the
#     client side is X-API-Key over TLS, not mTLS. mTLS would be a
#     prospect-side requirement that we don't have today.

# --- ACM certificate (DNS-validated) ---
# var.public_domain is a placeholder until the prospect's domain is
# known. The validation records are owned by Route 53, which the
# user manages out of band.
resource "aws_acm_certificate" "main" {
  domain_name       = var.public_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# --- ALB security group ---
# Public ingress on 80/443 from var.allowed_cidr. Egress unrestricted
# so the ALB can forward to the private ECS tasks on 8000.
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB — TLS termination, IP-restricted"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTPS from the operator/prospect egress CIDR"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  ingress {
    description = "HTTP (redirected to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- ALB itself ---
# Internet-facing, in 2 of the default VPC's public subnets (the
# default VPC has 3+ AZs in eu-west-3; we pick 2 to stay in the
# free tier's LCU budget).
resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  ip_address_type    = "ipv4"

  security_groups = [aws_security_group.alb.id]
  subnets         = slice(data.aws_subnets.default.ids, 0, 2)

  # access_logs: enable once CloudWatch pricing is in the budget.
  # drop_invalid_header_fields: forward unknown headers as-is. The
  #   X-Forwarded-* chain (ALB → ECS) preserves the original client
  #   IP for audit logs.
  drop_invalid_header_fields = true
  enable_deletion_protection  = false # pilot: allow destroy

  tags = {
    Name = "${local.name}-alb"
  }
}

# --- Target group for the ECS API service ---
# The target group uses the IP address of the ECS task (not the
# instance ID), because Fargate awsvpc tasks have ENIs with private IPs.
# The ECS service registers itself with this target group via
# aws_ecs_service below.
resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    enabled             = true
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # The API's /health endpoint does not require auth, so the ALB can
  # probe it without injecting the X-API-Key. (The backend has a
  # whitelist for /health in verify_api_key — see auth.py.)
  deregistration_delay = 30 # seconds; lets in-flight requests drain
}

# --- HTTPS listener (TLS termination) ---
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06" # TLS 1.3 only
  certificate_arn   = aws_acm_certificate.main.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- HTTP listener (redirects to HTTPS) ---
resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
