# Operations runbook (PLAN §5, §5.6 "without a monitoring stack", §5.7 "office deployment")

Companion docs: `docs/ops/secrets.md` (secrets and rotation), `docs/ops/log-queries.md` (jq one-liners),
`docs/ops/audit-log.md` (audit roles and partitions). Commands assume `cd /srv/flarize/app` (a checkout of this
repository at the deployed SHA) and `alias dc='docker compose -f deploy/docker-compose.yml'` with
`IMAGE_TAG=$(cat /srv/flarize/releases/current)` exported.

## 1. The stack (PLAN §5.2)

| Service | What | Scale |
|---|---|---|
| `nginx` | TLS, routing (§5.4), rate limits, `/media/private/` X-Accel, plain-HTTP `:8080` for `/iclock/` only | 1 (+ `certbot`) |
| `api-a`, `api-b` | Django + gunicorn gthread 4 × 8, `/healthz` readiness | 2 (rolling deploys) |
| `worker-default` | Celery `-Q default,ingest -c 8`: outbox, e-mail, thumbnails, imports, attendance | 1 |
| `worker-documents` | Celery `-Q documents -c 2` + Chromium + Noto fonts | 1 |
| `beat` | django-celery-beat (DatabaseScheduler) | exactly 1 |
| `pgbouncer` → `db` | transaction pooling (500 clients, pool 40) → PostgreSQL 16 | 1 |
| `redis` / `redis-broker` | cache (allkeys-lru, no persistence) / Celery broker (noeviction, AOF) — DV-12 | 1 each |
| `frontend` | Next.js (website + Studio) | 1 |
| `legacy-backend`, `legacy-cms` | old containers, profile `legacy`, until W26 | 1 each |

VM files (root-only): `/srv/flarize/.env`, `owner.env`, `db.env`, `smoke.env`, `frontend.env`, `keys/`,
`pgbouncer/userlist.txt`, `nginx/legacy-switch.conf`, `releases/{current,history}`.
Networks: compose network `172.28.0.0/16` → set `TRUSTED_PROXIES=172.28.0.0/16` in `.env`.

## 2. First installation

1. Create the database roles and `/srv/flarize/*.env` files (secrets.md, audit-log.md §1). Required in `.env`:
   `SECRET_KEY`, `ALLOWED_HOSTS=flarize.com,api-a,api-b,127.0.0.1,localhost` (the container health checks call
   `http://127.0.0.1:8000/healthz`), `TRUSTED_PROXIES`, `JWT_*_PATH=/run/flarize-keys/…`, `FERNET_KEYS`,
   `DB_NAME/DB_USER/DB_PASSWORD`, `DB_APP_ROLE`, `EMAIL_*`, `PASSWORD_RESET_URL`, `CORS_ALLOWED_ORIGINS`,
   `BUNNY_*` (until the Bunny integration is stored in Studio), `DOCUMENTS_ALLOWED_ASSET_HOSTS=<cdn host>`,
   `OPS_EMAILS`. `prod.py` refuses to start on anything unsafe and names each problem.
2. `cp deploy/nginx/legacy/switch.conf /srv/flarize/nginx/legacy-switch.conf` and set
   `LEGACY_SWITCH_FILE=/srv/flarize/nginx/legacy-switch.conf` in the shell profile used for `dc`.
3. TLS: `dc run --rm certbot certonly --webroot -w /var/www/certbot -d flarize.com -d www.flarize.com` (nginx must
   answer on :80 first — start it with the HTTP server only, or copy the existing certificates into the volume).
4. `SKIP_AUTH_SMOKE=1 deploy/release.sh <sha>`; then `dc exec api-a python manage.py seed_roles` and
   `dc exec api-a python manage.py bootstrap_admin --email <admin>` (prints a one-time link); create the smoke user
   (dashboard-only role) and fill `smoke.env`.
5. Install the cron entries (§4).

## 3. Deploying (PLAN §5.5)

`deploy/release.sh <git-sha>` — pulls the CI images for that SHA, runs `migrate` + `ensure_audit_partitions` as the
owner role, recreates `api-a` then waits for it to be healthy, then `api-b`, restarts workers and beat (warm
shutdown, up to 130 s for running tasks), runs the smoke tests (`/healthz`, `GET /api/public/v1/company/`, login →
`GET /api/v1/auth/me/` → logout) and records the SHA. On any failure it stops and prints the rollback command.

* **Rollback**: `deploy/release.sh <previous sha>` (`/srv/flarize/releases/history`). Schema changes are
  expand → migrate → contract (PLAN §2.10), so the previous code runs on the new schema. Never `migrate` backwards
  under pressure.
* Every prod deploy ran on staging first (with the parity harness during the cutover period).
* Frontend deploys are independent (`dc pull frontend && dc up -d frontend`).

## 4. Scheduled jobs

| When | What | Where |
|---|---|---|
| every 5 s | outbox drain | Beat → worker-default |
| every 5 min | render-job sweeper (requeue lost messages, fail dead RUNNING jobs) | Beat |
| daily 03:17 IST | auth retention (login attempts, expired tokens, reset rows) | Beat |
| 1st of month | `audit.tasks.ensure_partitions` (acts only when connected as owner) | Beat |
| Monday 08:05 IST | ops report e-mailed to `OPS_EMAILS` | Beat |
| every 2 min | `deploy/scripts/healthz-check.sh` → e-mail on state change | VM root cron |
| 20th monthly | `dc --profile ops run --rm migrate python manage.py ensure_audit_partitions --months 3` | VM root cron |
| nightly 01:30 | `pg_dump` (below) | VM root cron |
| weekly Sun 02:00 | base backup (below) | VM root cron |

```cron
*/2 * * * *  root  /srv/flarize/app/deploy/scripts/healthz-check.sh
30 1 * * *   root  cd /srv/flarize/app && docker compose -f deploy/docker-compose.yml exec -T db pg_dump -U flarize_owner -Fc flarize > /srv/backups/flarize-$(date +\%F).dump && find /srv/backups -name 'flarize-*.dump' -mtime +30 -delete
0 2 * * 0    root  cd /srv/flarize/app && docker compose -f deploy/docker-compose.yml exec -T db pg_basebackup -U postgres -D - -Ft -z -X fetch > /srv/backups/base-$(date +\%F).tar.gz
0 3 20 * *   root  cd /srv/flarize/app && IMAGE_TAG=$(cat /srv/flarize/releases/current) docker compose -f deploy/docker-compose.yml --profile ops run --rm migrate python manage.py ensure_audit_partitions --months 3
```

Backups (PLAN §5.2): nightly `pg_dump` + weekly base backup + WAL archive (`wal_archive` volume), 30-day retention,
copied off the VM (`rclone` to the backup bucket). **Restore drill monthly**: restore the latest dump into a scratch
database on staging (`pg_restore -d flarize_restore …`), run `python manage.py check` and `verify_migration` against
it, record the result in the ops log.

## 5. Watching the system without a monitoring stack (PLAN §5.6)

* **`/healthz`** (`dc exec api-a curl -s localhost:8000/healthz | jq`): `database`, `cache` (critical → HTTP 503,
  instance leaves rotation), `outbox` (lag/parked), `render_queue` (oldest QUEUED render job),
  `audit_partitions` (non-critical → `degraded`, HTTP 200). The healthz cron e-mails on every state change.
* **Weekly ops report** (`dc exec api-a python manage.py ops_report [--days 7] [--email|--json]`): outbox backlog,
  render jobs by status with recent failures, audit volume and top actions, media volume; HR adds agent-offline
  hours and unknown ADMS devices when those packages land.
* **Logs**: `docs/ops/log-queries.md` (5xx rate, slow requests, Celery failures, slow SQL, legacy hits).
* **Errors**: `core_system_exception` rows (never raises; request id links to the logs). Rejected request bodies
  (too many fields/files, oversized) are client errors: 400/413, a `flarize.security` warning, no row.
* **API docs** (`/api/docs/`, `/api/schema/v1/`): prod serves them only to `API_DOCS_ALLOWED_NETWORKS` (api `.env`,
  CIDRs), which must match `deploy/nginx/snippets/docs-allow.conf`; no JWT is needed (a browser navigation cannot
  send one). An empty list closes the docs.

## 6. Incidents

| Symptom | Check | Fix |
|---|---|---|
| `/healthz` `fail`, 503s | `dc ps`; `dc logs --tail 100 db redis pgbouncer` | restart the failed dependency; the api recovers by itself |
| `degraded: outbox` | `/healthz` `checks.outbox`: `oldest_age_seconds` (lag), `stale_claims` (a drainer died mid-batch; its rows are re-claimed once the lease expires), `retrying` (backoff), `parked`; `dc logs worker-default`; parked rows: `select id, event_type, attempts, last_error from core_outbox_event where parked_at is not null` | fix the handler and deploy, then `dc exec api-a python manage.py drain_outbox --requeue-parked` (all parked rows) or `--requeue-parked --id <id>` (repeatable); handlers that already succeeded are not re-run. Stale claims need no action unless they persist (then check the worker) |
| `degraded: render_queue` | `dc ps worker-documents`; `dc logs worker-documents` | restart it; the sweeper re-enqueues lost QUEUED jobs within 5 min; FAILED jobs are re-requested from their record |
| `degraded: audit_partitions` | `\d+ audit_log` | run the partition cron line now (§4) |
| Upload returns 503 `storage_unavailable` | Bunny status; Settings → Integrations → Bunny | fix credentials/zone; nothing was written |
| Private download 404 behind nginx | `ls /srv/flarize/media/private/<key>` inside nginx | the `private_media` volume must be mounted in api, workers and (read-only) nginx |
| Mass 429s from one office | log-queries "Rate limiting" | check `TRUSTED_PROXIES` / BFF forwarding of client IPs (F2 note) before raising limits |
| Login lockout for a whole office | `accounts_login_attempt` by IP | same as above; `ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP` in `.env` if needed |

## 7. Legacy cutover (PLAN §6.4, DV-5)

nginx sends each **group** of old website URLs (`deploy/nginx/legacy/groups.conf`) to the old containers (`old`),
to the new API's adapters under `/legacy/<old path>` (`shim`, needs the `LEGACY_API_SHIM` flag on in Studio →
Settings → Flags), or nowhere (`gone`). The active switch is `/srv/flarize/nginx/legacy-switch.conf`:

```bash
vi /srv/flarize/nginx/legacy-switch.conf           # e.g. `cms_content shim;` for C1
dc exec nginx nginx -t && dc exec nginx nginx -s reload
```

Rollback of a step = flip the line back and reload (one command, no deploy). Before each step: parity diff empty
on staging for 24 h (§6.3), `verify_migration` green. C9: when `docs/ops/log-queries.md` "Legacy cutover" shows
no hits for 7 days, set every group to `gone`, turn `LEGACY_API_SHIM` off, stop the `legacy` profile.

## 8. Office deployment — HR (PLAN §5.7)

* One always-on PC per office runs `essl-agent` (Windows service `.exe` built with PyInstaller, or the Linux
  systemd unit). Studio → Devices → Agents → Download config gives `agent.ini` with the one-time token (only its
  hash is stored; rotate from the same screen).
* The agent talks to `https://flarize.com/api/agent/v1/` (service token, 2 r/s per IP at nginx, `agent` throttle).
* Terminals stay pointed at nothing new until the ADMS controlled test (D-10); then enable the `ADMS_RECEIVER`
  flag, issue a device token (Devices → ADMS enable) and point the terminal at `http://<vm>:8080/iclock/<token>/`
  (plain HTTP by necessity; the :8080 listener serves nothing else).
