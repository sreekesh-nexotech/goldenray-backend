# Audit log operations: owner/app roles, partitions, append-only

`audit_log` (PLAN §2.1) is a PostgreSQL table **range-partitioned by month on `at`** (UTC month boundaries,
partitions `audit_log_yYYYYmMM`, plus `audit_log_default` for anything outside the created range). It is
**append-only**: the application may `INSERT` and `SELECT`, never `UPDATE`, `DELETE` or `TRUNCATE` — enforced by
database privileges, not by application code alone (standard §3.2).

## 1. Two database roles in staging and production

| Role | Used by | Privileges |
|---|---|---|
| `flarize_owner` | `manage.py migrate`, `ensure_audit_partitions`, retention/maintenance | owns every table (creates them) |
| `flarize_app` | gunicorn, Celery workers, beat (`DB_USER` of the running services) | DML on business tables; on `audit_log` only `SELECT, INSERT` |

The app role must **not** be a member of the owner role and must not be a superuser — otherwise a `REVOKE` has no
effect (`audit.services.partitions.check_app_role` refuses such a role and the migration fails closed).

One-time setup (as a superuser, once per cluster):

```sql
CREATE ROLE flarize_owner LOGIN PASSWORD '…';
CREATE ROLE flarize_app   LOGIN PASSWORD '…';
CREATE DATABASE flarize OWNER flarize_owner;
\c flarize
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO flarize_app;
-- Every table/sequence the owner creates from now on is usable by the app role…
ALTER DEFAULT PRIVILEGES FOR ROLE flarize_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO flarize_app;
ALTER DEFAULT PRIVILEGES FOR ROLE flarize_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO flarize_app;
-- …and audit_log is then narrowed back to append-only by the migration / command below.
```

Environment (`/srv/flarize/.env`): the services run with `DB_USER=flarize_app`; the deploy script runs migrations
and partition maintenance with `DB_USER=flarize_owner` (same settings module). **Both** set
`DB_APP_ROLE=flarize_app`.

## 2. What the code does

* `audit/migrations/0001_initial.py` creates the partitioned table, the default partition, the three query indexes
  and the partitions for the current month and the next two. When `DB_APP_ROLE` is set and differs from the role
  running the migration it grants `SELECT, INSERT` (and the id sequence) and revokes `UPDATE, DELETE, TRUNCATE`
  from it, and revokes **all** privileges on every partition (queries through the parent never check partition
  privileges, so default privileges can never re-open a partition for direct writes).
* `manage.py ensure_audit_partitions --months N` (default 3) creates the missing monthly partitions from the current
  month on, moves rows that landed in `audit_log_default` into their new month partition (in one transaction,
  with the default partition locked), closes new partitions to the app role and re-applies the revoke. Idempotent.
  With `DB_APP_ROLE` equal to the owner (single-role dev setups) it leaves privileges alone.
* `audit_log.actor_id` has no foreign-key constraint (DV-9): an `ON DELETE` action would rewrite ledger rows.

## 3. Schedule

* **Every deploy** (after `migrate`, as `flarize_owner`): `python manage.py ensure_audit_partitions --months 3`.
* **Monthly cron** on the VM, as `flarize_owner`, e.g. on the 20th:
  `docker compose run --rm -e DB_USER=flarize_owner -e DB_PASSWORD=… api python manage.py ensure_audit_partitions --months 3`.
  If the job is missed, rows go to `audit_log_default` (nothing is lost) and are moved on the next run.

## 4. Checks

```sql
-- privileges of the app role (expect SELECT/INSERT true, the rest false)
SELECT p, has_table_privilege('flarize_app', 'audit_log', p) FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p;
-- partitions and their row counts
SELECT c.relname, pg_stat_get_live_tuples(c.oid) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = 'audit_log'::regclass ORDER BY 1;
-- rows stranded in the default partition (should be 0 after the monthly run)
SELECT count(*) FROM audit_log_default;
```

## 5. Retention

Audit rows are kept. Should a retention period be decided, old months are removed by the **owner** with
`ALTER TABLE audit_log DETACH PARTITION audit_log_y2026m01; DROP TABLE audit_log_y2026m01;` (after archiving the
partition with `pg_dump -t`). The app role can never do this.
