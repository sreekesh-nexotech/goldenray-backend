#!/usr/bin/env bash
# deploy/release.sh <git-sha> — zero-downtime release of the Flarize backend (PLAN §5.5).
#
#   1. pull the images built by CI for <git-sha>
#   2. migrate (expand phase only), extend the audit_log partitions and seed the maintained-pages registry — one-off,
#      as the owner role
#   3. rolling restart: api-a, wait until healthy, then api-b (nginx keeps serving from the other replica)
#   4. restart worker-default, worker-documents and beat (warm shutdown: running tasks finish first)
#   5. smoke tests: /healthz, one public GET, one authenticated GET (login → auth/me → logout)
#   6. record the release (STATE_DIR/current, STATE_DIR/history)
#
# Rollback = run this script with the previous SHA (printed on failure; STATE_DIR/current before this run). Schema
# changes are expand/contract, so the previous code runs on the new schema; never migrate backwards in a hurry.
#
# Environment (defaults suit the prod VM; see docs/ops/runbook.md):
#   IMAGE_REPO          image repository (compose default ghcr.io/flarize/platform-backend)
#   STATE_DIR           /srv/flarize/releases
#   SMOKE_BASE_URL      https://flarize.com   SMOKE_RESOLVE  127.0.0.1 (curl --resolve so the VM tests itself)
#   SMOKE_ENV_FILE      /srv/flarize/smoke.env with SMOKE_EMAIL / SMOKE_PASSWORD of a dashboard-only staff user
#   HEALTH_TIMEOUT      seconds to wait for a replica to become healthy (180)
#   SKIP_AUTH_SMOKE=1   first deploy only, before the smoke user exists
set -Eeuo pipefail

SHA="${1:-}"
if [[ ! "$SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "usage: deploy/release.sh <git-sha>" >&2
  exit 64
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STATE_DIR:-/srv/flarize/releases}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
SMOKE_BASE_URL="${SMOKE_BASE_URL:-https://flarize.com}"
SMOKE_RESOLVE="${SMOKE_RESOLVE:-127.0.0.1}"
SMOKE_ENV_FILE="${SMOKE_ENV_FILE:-/srv/flarize/smoke.env}"
export IMAGE_TAG="$SHA"
COMPOSE=(docker compose --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.yml")
PREVIOUS="$(cat "$STATE_DIR/current" 2>/dev/null || true)"
STEP="start"

log() { printf '%s [release %s] %s\n' "$(date -u +%FT%TZ)" "${SHA:0:12}" "$*"; }

rollback_instructions() {
  cat >&2 <<EOF

──────────────────────────────────────────────────────────────────────────────
Release ${SHA} FAILED during: ${STEP}
Nothing after that step ran. To roll back to the previous release:

    deploy/release.sh ${PREVIOUS:-<previous-sha from ${STATE_DIR}/history>}

Migrations are expand-only, so the previous code runs on the current schema.
Check: docker compose -f deploy/docker-compose.yml ps / logs --tail=200 api-a api-b
──────────────────────────────────────────────────────────────────────────────
EOF
}
trap 'rollback_instructions' ERR

container_of() { "${COMPOSE[@]}" ps -q "$1"; }

wait_healthy() {
  local service="$1" deadline=$((SECONDS + HEALTH_TIMEOUT)) status=""
  while ((SECONDS < deadline)); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$(container_of "$service")" 2>/dev/null || echo missing)"
    if [[ "$status" == "healthy" ]]; then
      log "$service is healthy"
      return 0
    fi
    sleep 3
  done
  log "$service did not become healthy within ${HEALTH_TIMEOUT}s (last status: $status)"
  return 1
}

smoke_curl() {
  local host
  host="$(sed -E 's#^https?://([^/:]+).*#\1#' <<<"$SMOKE_BASE_URL")"
  curl --silent --show-error --fail --max-time 20 --resolve "${host}:443:${SMOKE_RESOLVE}" "$@"
}

STEP="pull images"
log "pulling images for ${SHA} (previous release: ${PREVIOUS:-none})"
"${COMPOSE[@]}" --profile ops pull api-a api-b worker-default worker-documents beat migrate

STEP="migrate"
log "migrating (expand phase) as the owner role"
"${COMPOSE[@]}" --profile ops run --rm migrate python manage.py migrate --noinput
"${COMPOSE[@]}" --profile ops run --rm migrate python manage.py ensure_audit_partitions --months 3
# Register the website's maintained pages/slots the release's code knows about (additive, idempotent; sitepages).
"${COMPOSE[@]}" --profile ops run --rm migrate python manage.py seed_pages

for service in api-a api-b; do
  STEP="rolling restart of ${service}"
  log "recreating ${service}"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$service"
  wait_healthy "$service"
done

STEP="restart workers and beat"
log "restarting workers and beat"
"${COMPOSE[@]}" up -d --no-deps --force-recreate worker-default worker-documents beat
wait_healthy worker-default
wait_healthy worker-documents

STEP="smoke tests"
log "smoke: /healthz"
health="$(smoke_curl "$SMOKE_BASE_URL/healthz")"
grep -Eq '"status": ?"(ok|degraded)"' <<<"$health" || { log "healthz: $health"; false; }
log "smoke: public GET /api/public/v1/company/"
smoke_curl -o /dev/null "$SMOKE_BASE_URL/api/public/v1/company/"
if [[ "${SKIP_AUTH_SMOKE:-0}" == "1" ]]; then
  log "smoke: authenticated GET skipped (SKIP_AUTH_SMOKE=1)"
else
  # shellcheck source=/dev/null
  source "$SMOKE_ENV_FILE"
  : "${SMOKE_EMAIL:?SMOKE_EMAIL missing in $SMOKE_ENV_FILE}" "${SMOKE_PASSWORD:?SMOKE_PASSWORD missing in $SMOKE_ENV_FILE}"
  log "smoke: authenticated GET /api/v1/auth/me/"
  export SMOKE_EMAIL SMOKE_PASSWORD
  body="$(python3 -c 'import json, os; print(json.dumps({"email": os.environ["SMOKE_EMAIL"], "password": os.environ["SMOKE_PASSWORD"]}))')"
  login="$(smoke_curl -H 'Content-Type: application/json' --data "$body" "$SMOKE_BASE_URL/api/v1/auth/login/")"
  access="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["access"])' <<<"$login")"
  refresh="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["refresh"])' <<<"$login")"
  smoke_curl -o /dev/null -H "Authorization: Bearer ${access}" "$SMOKE_BASE_URL/api/v1/auth/me/"
  smoke_curl -o /dev/null -H "Authorization: Bearer ${access}" -H 'Content-Type: application/json' --data "{\"refresh\":\"${refresh}\"}" "$SMOKE_BASE_URL/api/v1/auth/logout/" || log "smoke: logout failed (session expires on its own)"
fi

STEP="record release"
mkdir -p "$STATE_DIR"
printf '%s\n' "$SHA" >"$STATE_DIR/current"
printf '%s %s previous=%s\n' "$(date -u +%FT%TZ)" "$SHA" "${PREVIOUS:-none}" >>"$STATE_DIR/history"
trap - ERR
log "release ${SHA} is live (rollback target: ${PREVIOUS:-none})"
