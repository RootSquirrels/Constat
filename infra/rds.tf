# RDS PostgreSQL for the pilot.
#
# Pilot economics, stated plainly:
#   - db.t4g.micro + single-AZ + 20 GB gp3 is the smallest honest shape
#     (~$15/month). Multi-AZ would double the instance cost for an HA
#     guarantee the pilot does not need.
#   - deletion_protection = false BUT skip_final_snapshot = false: we want
#     `terraform destroy` to work during the pilot, and the mandatory final
#     snapshot is the safety net if we destroy by mistake.
#   - 7-day automated backups: enough to recover from a bad deploy during
#     the pilot. Restore procedure: docs/operations/backup-restore.md.

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "main" {
  identifier = local.name

  engine               = "postgres"
  engine_version       = var.db_engine_version
  instance_class       = "db.t4g.micro"
  allocated_storage    = 20
  storage_type         = "gp3"
  storage_encrypted    = true
  multi_az             = false
  db_name              = "constat"
  username             = "constat"
  password             = var.db_password
  port                 = 5432

  publicly_accessible    = false # reachable only from the ECS tasks' SG
  vpc_security_group_ids = [aws_security_group.db.id]
  db_subnet_group_name   = aws_db_subnet_group.main.name

  backup_retention_period   = 7
  deletion_protection       = false # pilot: allow destroy
  skip_final_snapshot       = false # ...but always leave a final snapshot
  final_snapshot_identifier = "${local.name}-final"
  auto_minor_version_upgrade = true
}
