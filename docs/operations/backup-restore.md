# Backup & restore runbook

> Roadmap scoreboard "Exploitabilité": backup/restore executed once and a
> dated runbook. The procedure below is written against the
> docker-compose Postgres; the RDS section covers the pilot deployment.

## Execution log

| Date | Environment | Procedure | Result | Operator |
|---|---|---|---|---|
| — | — | — | **Not yet executed** — no Docker on the dev machine. First execution scheduled at pilot deploy. Owner: ship owner. | — |

Do not mark a row executed without having run both the backup AND the
restore verification below. A backup that was never restored is a
Schrodinger backup.

## Scope

The entire `constat` database: accounts, resources, facts, observations,
source_runs, insights, inconclusive, FOCUS rows, audit_events, PII
classifications. A full `pg_dump` covers all of it; there are no external
state stores (MinIO holds only FOCUS drop files, re-ingestible).

## Docker-compose Postgres (dev / single-node pilot)

### Backup

```bash
# Consistent logical dump, custom format (compressed, restorable per-table).
docker exec constat-postgres \
  pg_dump -U constat -d constat -Fc -f /tmp/constat-$(date +%Y%m%d-%H%M).dump

# Copy it out of the container.
docker cp constat-postgres:/tmp/constat-<timestamp>.dump ./backups/
```

Run this before every migration apply and every pilot demo. Cron it
daily once the pilot is live.

### Pre-restore checklist

1. **The dump matches the migration chain.** The app expects all
   migrations in `db/migrations/` (0001 through 0014 today) to have
   applied cleanly. CI proves the chain applies from scratch on every
   push; if you restore a dump taken at migration N onto a fresh DB,
   re-apply migrations N+1..latest afterwards.
2. **Stop the API** so no writes race the restore:
   `docker compose stop api` (or kill the local `uvicorn` process).
3. **Restore into a scratch database first** when validating a backup —
   never discover a corrupt dump by restoring over the only copy:
   ```bash
   docker exec constat-postgres \
     psql -U constat -d postgres -c "CREATE DATABASE constat_restore_check;"
   docker cp ./backups/constat-<timestamp>.dump constat-postgres:/tmp/
   docker exec constat-postgres \
     pg_restore -U constat -d constat_restore_check --no-owner -Fc /tmp/constat-<timestamp>.dump
   ```

### Restore (real)

```bash
docker exec constat-postgres \
  psql -U constat -d postgres -c "DROP DATABASE constat WITH (FORCE);" \
  -c "CREATE DATABASE constat;"
docker exec constat-postgres \
  pg_restore -U constat -d constat --no-owner -Fc /tmp/constat-<timestamp>.dump
```

`--no-owner` because the dump carries the same `constat` role anyway;
add `--clean --if-exists` instead of the DROP/CREATE if you prefer an
in-place restore.

### Verification

1. **Row counts per table** — compare against the pre-restore counts
   (take them before the restore with the same query):
   ```sql
   SELECT 'accounts' t, count(*) FROM accounts
   UNION ALL SELECT 'resources', count(*) FROM resources
   UNION ALL SELECT 'facts', count(*) FROM facts
   UNION ALL SELECT 'observations', count(*) FROM observations
   UNION ALL SELECT 'source_runs', count(*) FROM source_runs
   UNION ALL SELECT 'insights', count(*) FROM insights
   UNION ALL SELECT 'inconclusive', count(*) FROM inconclusive
   UNION ALL SELECT 'focus_charges', count(*) FROM focus_charges
   UNION ALL SELECT 'audit_events', count(*) FROM audit_events
   ORDER BY t;
   ```
2. **RLS spot check** — RLS policies (migrations 0007/0011) live in the
   schema, not the data, so a fresh DB re-created from migrations is
   covered; a `pg_restore` of schema+data preserves them. Verify:
   ```sql
   SELECT tablename, rowsecurity FROM pg_tables
   WHERE schemaname = 'public' AND tablename IN
     ('resources', 'facts', 'observations', 'source_runs');
   -- every row must report rowsecurity = true
   ```
   Then confirm the GUC path still works:
   ```sql
   SET app.current_tenant_id = '<tenant-uuid>';
   SELECT count(*) FROM resources;  -- must not error, must be tenant-scoped
   ```
3. **App smoke test** — restart the API, hit `/health`, then
   `GET /insights` and `GET /insights?rule_name=chargeback` and eyeball
   non-empty results. (There is no `GET /chargeback` API route —
   `/chargeback` is a web page; the chargeback restitution is the
   `rule_name=chargeback` filter on `/insights`.)
4. Record the execution in the table at the top of this file.

## AWS RDS (pilot deployment)

The pilot runs on RDS PostgreSQL. Do **not** use `pg_dump` as the primary
mechanism there:

- **Automated snapshots**: enable `backup_retention_period >= 7` days.
  Daily snapshots + continuous WAL archiving give **PITR** (point-in-time
  recovery) to any second within the window. Restore via
  `RestoreDBInstanceToPointInTime` into a NEW instance, verify with the
  same row-count + RLS queries above, then repoint the app's
  `DATABASE_URL` (or swap the endpoint in Secrets Manager).
- **Manual snapshot before risky operations**: migration applies,
  schema changes, demo days. Snapshots persist until deleted.
- **Cross-region copy**: out of V1 scope; add if the pilot customer's
  RPO demands it.

The docker-compose procedure above remains the fallback for logical
(e.g. single-table) restores — `pg_dump`/`pg_restore` work against RDS
endpoints unchanged, provided the security group allows it from the
operator host.

## What is NOT covered

- **MinIO / FOCUS drop files**: re-ingestible from the source CUR export;
  no backup in V1.
- **Secrets** (ExternalIds, API keys): live in `.env` (V1) / AWS Secrets
  Manager (V2), not in the database.
- **Point-in-time recovery on docker-compose Postgres**: no WAL
  archiving in dev; RPO is "last pg_dump".

## See also

- [`metrics.md`](./metrics.md) and [`alerting.md`](./alerting.md) — the
  alerts that tell you a restore might be needed
- `db/migrations/` — the migration chain CI proves applies cleanly
