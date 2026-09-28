# Log queries (no monitoring stack — PLAN §5.6)

Every container logs **JSON lines to stdout**; Docker keeps them (json-file driver, 10 × 50 MB per container, see
`deploy/docker-compose.yml`) and journald/`docker compose logs` reads them. Run these from `/srv/flarize/app`
with `alias dc='docker compose -f deploy/docker-compose.yml'`. `jq` is installed on the VM.

## Shapes

* **Django** (api, workers, beat) — `flarize.logging.JsonFormatter`:
  `{"ts", "level", "logger", "message", "request_id", "user_uid"?, …extra}`.
  One access line per request from `flarize.request`: `{"message": "request", "method", "path", "status", "duration_ms"}`.
  Unhandled exceptions: `logger = "flarize.errors"` (also stored in `core_system_exception`).
* **nginx** — `flarize_json` access log: `{"ts", "request_id", "remote", "xff", "method", "uri", "status", "bytes",
  "request_time", "upstream", "upstream_time", "legacy_group", "ua"}`. The same `request_id` is passed to Django as
  `X-Request-ID`, so one id follows a request through nginx, Django, audit rows and error records.
* **Never in a log line:** capability tokens in paths (signed media/document downloads, `/iclock/<device token>/`,
  customer links) appear as `[redacted]` in `path`/`uri`/messages and in `core_system_exception.path`; Celery task
  arguments appear as `[redacted]` (e-mail tasks publish only their category and recipient count).
* **Postgres** — plain text; statements slower than 500 ms are logged (`log_min_duration_statement=500`).

`--no-log-prefix` strips the `service |` prefix so `jq` gets pure JSON; `2>/dev/null` hides non-JSON startup lines.

## API health

```bash
# 5xx responses in the last hour, per path
dc logs --since 1h --no-log-prefix api-a api-b | jq -Rr 'fromjson? | select(.logger=="flarize.request" and .status>=500) | .path' | sort | uniq -c | sort -rn

# 5xx rate per minute (last 3 h)
dc logs --since 3h --no-log-prefix api-a api-b | jq -Rr 'fromjson? | select(.logger=="flarize.request" and .status>=500) | .ts[0:16]' | uniq -c

# Slowest requests (> 1 s) in the last hour
dc logs --since 1h --no-log-prefix api-a api-b | jq -Rc 'fromjson? | select(.logger=="flarize.request" and .duration_ms>1000) | {ts, duration_ms, method, path, status, request_id}' | sort -t: -k3 -rn | head -20

# p95 latency per path (last hour; needs ≥ 20 requests)
dc logs --since 1h --no-log-prefix api-a api-b | jq -Rs '[split("\n")[] | fromjson? | select(.logger=="flarize.request")] | group_by(.path) | map(select(length>=20) | {path: .[0].path, n: length, p95: (map(.duration_ms) | sort | .[(length*0.95|floor)])}) | sort_by(-.p95) | .[:15]'

# Unhandled exceptions, logged once each (then look the request id up in core_system_exception): API views on
# flarize.errors, plain Django views (/iclock/, /healthz) on django.request
dc logs --since 24h --no-log-prefix api-a api-b | jq -Rc 'fromjson? | select(.logger=="flarize.errors" or (.logger=="django.request" and .level=="ERROR")) | {ts, request_id, message}'

# Rejected request bodies (too many fields/files, body too large: 400/413, not server errors), per path
dc logs --since 24h --no-log-prefix api-a api-b | jq -Rr 'fromjson? | select(.logger=="flarize.security") | "\(.message) \(.path)"' | sort | uniq -c | sort -rn

# Everything about one request (nginx + api + workers)
RID=<request id>; dc logs --since 24h --no-log-prefix nginx api-a api-b worker-default | grep "$RID" | jq -R 'fromjson? // .'
```

## Rate limiting and abuse

```bash
# 429s from nginx limit_req, by client and URI
dc logs --since 1h --no-log-prefix nginx | jq -Rr 'fromjson? | select(.status==429) | "\(.remote) \(.uri)"' | sort | uniq -c | sort -rn | head
# 429s from Django throttles (per scope, per user/IP)
dc logs --since 1h --no-log-prefix api-a api-b | jq -Rc 'fromjson? | select(.logger=="flarize.request" and .status==429) | {path, user_uid}' | sort | uniq -c | sort -rn | head
# Failed / locked logins
dc logs --since 24h --no-log-prefix api-a api-b | jq -Rc 'fromjson? | select(.logger=="flarize.auth")'
```

## Celery

```bash
# Task failures and retries
dc logs --since 24h --no-log-prefix worker-default worker-documents | jq -Rc 'fromjson? | select(.level=="ERROR" or .level=="CRITICAL") | {ts, logger, message}'
# Render jobs that failed (also: ops_report, /healthz render_queue)
dc logs --since 24h --no-log-prefix worker-documents | jq -Rc 'fromjson? | select(.logger=="flarize.documents") | {ts, message, job_uid, kind}'
# Outbox problems (parked events, handler failures)
dc logs --since 24h --no-log-prefix worker-default | jq -Rc 'fromjson? | select(.logger=="flarize.outbox" and .level!="INFO")'
# Is beat ticking? (drain_outbox every 5 s)
dc logs --since 5m --no-log-prefix beat | tail -5
```

## Database

```bash
# Slow statements (> 500 ms) today
dc logs --since 24h db | grep -E 'duration: [0-9]+' | sed -E 's/.*duration: ([0-9.]+) ms.*statement: (.{0,160}).*/\1 ms  \2/' | sort -rn | head -20
# Connection pressure (PgBouncer pools)
dc exec pgbouncer psql -h 127.0.0.1 -p 5432 -U pgbouncer_stats pgbouncer -c 'SHOW POOLS;'
```

## Legacy cutover (PLAN §6.4 C9: "7 days of zero legacy hits")

```bash
# Old-URL traffic per group over the last 7 days (empty output = ready to switch the group off)
dc logs --since 168h --no-log-prefix nginx | jq -Rr 'fromjson? | select(.legacy_group != "") | .legacy_group' | sort | uniq -c | sort -rn
# Which old URLs still get hits
dc logs --since 24h --no-log-prefix nginx | jq -Rr 'fromjson? | select(.legacy_group != "") | "\(.legacy_group) \(.uri | split("?")[0])"' | sort | uniq -c | sort -rn | head -30
```
