#!/usr/bin/env bash
# deploy/scripts/import-legacy-backend.sh [options] <legacy-dump> — restore a legacy main-backend (GoldenApp) pg_dump
# into its own database next to the platform's, then import it with `manage.py import_backend` (PLAN §7.3,
# docs/ops/runbook.md §7). Safe to re-run: the restore replaces only the legacy database (--replace) and the importer
# is idempotent (a second run reports `unchanged`).
#
#   1. checks the dump (pg_dump custom format) and backs up the platform database
#   2. creates the legacy database and restores the dump into it (pg_restore 18 → SQL → psql inside the db container:
#      no database port, no superuser password; the pg_dump 17+ `SET transaction_timeout` line is dropped because
#      Postgres 16 has no such setting — any other restore error stops the script)
#   3. opens it to a read-only login role `legacy_reader` with a fresh random password (closed again on exit)
#   4. seed_roles, then import_backend --dry-run; stops if the report is not ok
#   5. after confirmation: the real import (+ the legacy media volume, reset links if asked), then verify_migration
#   6. every report goes to the reports directory (mode 0600: the reports contain personal data)
#
# Targets:
#   prod (default)  deploy/docker-compose.yml on the VM, service api-a, image tag from /srv/flarize/releases/current
#   dev             deploy/docker-compose.dev.yml + the root .env (the local stack), service api
#
# Options:
#   --target prod|dev        which stack (default prod)
#   --legacy-db NAME         database the dump is restored into (default legacy_backend)
#   --replace                drop and re-create the legacy database when it already exists (a newer dump)
#   --media-volume NAME      docker volume with the legacy media (resumes, offer images), mounted read-only
#   --media-dir PATH         same, from a host directory
#   --send-reset-links       e-mail set-password links to the migrated accounts (CUTOVER ONLY: real e-mails in prod)
#   --dry-run-only           stop after the dry run
#   --offline                verify without the CDN HEAD checks (default for dev)
#   --reports-dir PATH       where reports go (prod /srv/flarize/legacy/reports/<ts>, dev var/legacy-import/<ts>)
#   --backup-dir PATH        where the pre-import backup goes (prod /srv/flarize/backups, dev var/backups)
#   --pg-client-image IMAGE  image whose pg_restore reads the dump (default postgres:18-alpine)
#   -y, --yes                do not ask before the real import
set -Eeuo pipefail

usage() { sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }
log() { printf '%s [legacy-import] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

TARGET=prod LEGACY_DB=legacy_backend REPLACE=0 MEDIA_VOLUME="" MEDIA_DIR="" RESET_LINKS=0 DRY_ONLY=0 OFFLINE="" YES=0
REPORTS_DIR="" BACKUP_DIR="" PG_CLIENT_IMAGE=postgres:18-alpine DUMP=""
while (($#)); do
  case "$1" in
    --target) TARGET="${2:?}"; shift 2 ;;
    --legacy-db) LEGACY_DB="${2:?}"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --media-volume) MEDIA_VOLUME="${2:?}"; shift 2 ;;
    --media-dir) MEDIA_DIR="${2:?}"; shift 2 ;;
    --send-reset-links) RESET_LINKS=1; shift ;;
    --dry-run-only) DRY_ONLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --reports-dir) REPORTS_DIR="${2:?}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?}"; shift 2 ;;
    --pg-client-image) PG_CLIENT_IMAGE="${2:?}"; shift 2 ;;
    -y | --yes) YES=1; shift ;;
    -h | --help) usage ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [[ -z "$DUMP" ]] || die "only one dump file"; DUMP="$1"; shift ;;
  esac
done
[[ -n "$DUMP" ]] || usage 64
[[ "$LEGACY_DB" =~ ^[a-z_][a-z0-9_]*$ ]] || die "--legacy-db must be a plain lower-case identifier"
[[ -z "$MEDIA_VOLUME" || -z "$MEDIA_DIR" ]] || die "--media-volume and --media-dir are exclusive"

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(dirname "$DEPLOY_DIR")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
case "$TARGET" in
  prod)
    export IMAGE_TAG="${IMAGE_TAG:-$(cat "${STATE_DIR:-/srv/flarize/releases}/current" 2>/dev/null || true)}"
    [[ -n "$IMAGE_TAG" ]] || die "IMAGE_TAG unknown: set it to the deployed SHA (no ${STATE_DIR:-/srv/flarize/releases}/current)"
    COMPOSE=(docker compose --progress quiet --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.yml")
    API=api-a
    REPORTS_DIR="${REPORTS_DIR:-/srv/flarize/legacy/reports/$STAMP}"
    BACKUP_DIR="${BACKUP_DIR:-/srv/flarize/backups}"
    ;;
  dev)
    [[ -f "$REPO_DIR/.env" && -f "$REPO_DIR/.env.docker" ]] || die "dev stack not set up: run \`make env\` (or \`make up\`) first"
    COMPOSE=(docker compose --progress quiet --env-file "$REPO_DIR/.env" -f "$DEPLOY_DIR/docker-compose.dev.yml")
    API=api
    REPORTS_DIR="${REPORTS_DIR:-$REPO_DIR/var/legacy-import/$STAMP}"
    BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/var/backups}"
    OFFLINE="${OFFLINE:-1}"
    ;;
  *) die "--target must be prod or dev" ;;
esac
dc() { "${COMPOSE[@]}" "$@"; }
dbsh() { dc exec -T db sh -c "$1"; }   # runs inside the db container: local socket, trusted, no password
psql_su() { dc exec -T db sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-$DB_USER}" "$@"' _ "$@"; }

# ── 1. checks ───────────────────────────────────────────────────────────────────────────────────────────────────────
[[ -f "$DUMP" && -r "$DUMP" ]] || die "cannot read $DUMP"
DUMP="$(cd "$(dirname "$DUMP")" && pwd)/$(basename "$DUMP")"
[[ "$(head -c 5 "$DUMP")" == "PGDMP" ]] || die "$DUMP is not a pg_dump custom-format archive (expected the PGDMP header)"
if [[ "$TARGET" == prod && "$DUMP" == "$REPO_DIR"/* ]]; then
  die "the dump is inside the repository checkout ($REPO_DIR): move it to e.g. /srv/flarize/legacy/ (mode 0600) first"
fi
[[ -z "$MEDIA_VOLUME" ]] || docker volume inspect "$MEDIA_VOLUME" >/dev/null 2>&1 || die "no docker volume $MEDIA_VOLUME (docker volume ls)"
[[ -z "$MEDIA_DIR" || -d "$MEDIA_DIR" ]] || die "no directory $MEDIA_DIR"
dc ps --status running --services 2>/dev/null | grep -qx db || die "the db service of the $TARGET stack is not running"

SU="$(dbsh 'printf %s "${POSTGRES_USER:-$DB_USER}"')"
APP_DB="$(dbsh 'printf %s "${POSTGRES_DB:-$DB_NAME}"')"
[[ -n "$SU" && -n "$APP_DB" ]] || die "could not read the database superuser / name from the db container"
[[ "$LEGACY_DB" != "$APP_DB" ]] || die "--legacy-db must not be the platform database ($APP_DB)"
log "target=$TARGET service=$API platform-db=$APP_DB legacy-db=$LEGACY_DB dump=$DUMP"
EXISTS="$(psql_su -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$LEGACY_DB'")"
[[ "$EXISTS" != 1 ]] || ((REPLACE)) || die "database $LEGACY_DB already exists: pass --replace to load this dump over it"

install -d -m 700 "$REPORTS_DIR" "$BACKUP_DIR"
CONTAINER_UID="$(dc run --rm --no-deps -T "$API" id -u 2>/dev/null | tail -1 | tr -d '\r')"
[[ "$CONTAINER_UID" =~ ^[0-9]+$ ]] || die "could not start a one-off $API container"
if [[ "$CONTAINER_UID" != "$(id -u)" ]]; then
  chown "$CONTAINER_UID" "$REPORTS_DIR" 2>/dev/null || die "the $API container runs as uid $CONTAINER_UID: run as root so it can write $REPORTS_DIR"
fi

LEGACY_PW=""
cleanup() {
  if [[ -n "$LEGACY_PW" ]]; then
    psql_su -d postgres -c "ALTER ROLE legacy_reader NOLOGIN PASSWORD NULL" >/dev/null 2>&1 || true
    log "legacy_reader closed again (NOLOGIN)"
  fi
}
trap cleanup EXIT
trap 'log "FAILED at line $LINENO — nothing after this step ran; the platform backup is in $BACKUP_DIR"' ERR

# ── 2. backup + restore ─────────────────────────────────────────────────────────────────────────────────────────────
BACKUP="$BACKUP_DIR/$APP_DB-before-legacy-import-$STAMP.dump"
log "backing up $APP_DB → $BACKUP"
(umask 077 && dbsh 'pg_dump -U "${POSTGRES_USER:-$DB_USER}" -Fc "${POSTGRES_DB:-$DB_NAME}"' >"$BACKUP")
[[ "$(head -c 5 "$BACKUP")" == "PGDMP" ]] || die "backup failed ($BACKUP)"

if [[ "$EXISTS" == 1 ]]; then
  log "dropping the existing $LEGACY_DB (--replace)"
  psql_su -d postgres -c "DROP DATABASE $LEGACY_DB WITH (FORCE)"
fi
psql_su -d postgres -c "CREATE DATABASE $LEGACY_DB"

log "restoring with $PG_CLIENT_IMAGE (pg_restore → SQL → psql in the db container)"
docker run --rm -v "$DUMP:/dump:ro" "$PG_CLIENT_IMAGE" pg_restore --no-owner --no-acl -f - /dump \
  | sed '/^SET transaction_timeout = 0;$/d' \
  | psql_su -d "$LEGACY_DB" >/dev/null
TABLES="$(psql_su -d "$LEGACY_DB" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
log "restored: $TABLES tables"
psql_su -d "$LEGACY_DB" -tA -F' ' -c "
  SELECT 'rows:', (SELECT count(*) FROM auth_user) || ' users', (SELECT count(*) FROM bom_catalogitem) || ' catalog items',
         (SELECT count(*) FROM pincodes) || ' pincodes', (SELECT count(*) FROM lead_collection_home) || ' leads',
         (SELECT count(*) FROM job_application) || ' job applications'" \
  | sed 's/^/    /' || log "warning: some expected legacy tables are missing (the dry run lists them)"

# ── 3. read-only access for the importer ────────────────────────────────────────────────────────────────────────────
# Hex only, so it is safe inside the SQL text; sent on stdin, never as a process argument.
LEGACY_PW="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
psql_su -d postgres <<SQL
SELECT 'CREATE ROLE legacy_reader' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'legacy_reader') \gexec
ALTER ROLE legacy_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$LEGACY_PW';
SQL
psql_su -d postgres -c "GRANT CONNECT ON DATABASE $LEGACY_DB TO legacy_reader"
psql_su -d "$LEGACY_DB" -c "GRANT USAGE ON SCHEMA public TO legacy_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO legacy_reader"
export LEGACY_BACKEND_URL="postgresql://legacy_reader:$LEGACY_PW@db:5432/$LEGACY_DB"

MEDIA_ARGS=() MEDIA_OPT=""
if [[ -n "$MEDIA_VOLUME" ]]; then MEDIA_ARGS=(-v "$MEDIA_VOLUME:/legacy-media:ro"); MEDIA_OPT="--media-root /legacy-media"
elif [[ -n "$MEDIA_DIR" ]]; then MEDIA_ARGS=(-v "$(cd "$MEDIA_DIR" && pwd):/legacy-media:ro"); MEDIA_OPT="--media-root /legacy-media"
else log "no --media-volume/--media-dir: resumes and offer images are imported as missing (listed in the report)"; fi

# The URL travels as an environment variable (never on a command line we print); reports land in $REPORTS_DIR.
run_api() { dc run --rm --no-deps -T -e LEGACY_BACKEND_URL -v "$REPORTS_DIR:/reports" "${MEDIA_ARGS[@]}" "$API" sh -c "$1"; }

summarise() {
  python3 - "$1" <<'PY'
import collections, json, sys
r = json.load(open(sys.argv[1]))
t = r.get("totals", {})
print(f"    ok={r.get('ok')}  created={t.get('created')} updated={t.get('updated')} unchanged={t.get('unchanged')} violations={t.get('violations')}")
codes = collections.Counter()
for step in r.get("steps", []):
    if step.get("status") not in ("imported", "skipped", "dry_run", None):
        print(f"    step {step.get('step')}: {step.get('status')} {step.get('error', '')}")
    for res in step.get("results", {}).values():
        for v in res.get("violations", []) if isinstance(res, dict) else []:
            codes[(v.get("severity", "warning"), v.get("code") or v.get("field") or "?")] += 1
for (severity, code), n in codes.most_common():
    print(f"      {n:4d} {severity:8s} {code}")
sys.exit(0 if r.get("ok") else 3)
PY
}

# ── 4. prerequisites + dry run ──────────────────────────────────────────────────────────────────────────────────────
CMS_ROWS="$(psql_su -d "$APP_DB" -tAc "SELECT count(*) FROM core_legacy_map WHERE source_system = 'CMS'" 2>/dev/null || echo 0)"
[[ "$CMS_ROWS" != 0 ]] || log "warning: the CMS has not been imported (runbook order: import_cms first) — job applications stay unlinked, the company profile is not created, verify #1 fails"
log "seed_roles"
run_api 'python manage.py seed_roles' | tail -1 | sed 's/^/    /'

log "dry run (writes nothing)"
run_api "python manage.py import_backend --source-url \"\$LEGACY_BACKEND_URL\" $MEDIA_OPT --dry-run --json /reports/backend-dryrun.json -v 0" >/dev/null
summarise "$REPORTS_DIR/backend-dryrun.json" || die "dry run is not ok: read $REPORTS_DIR/backend-dryrun.json before importing"
if ((DRY_ONLY)); then log "--dry-run-only: stopping here (reports in $REPORTS_DIR)"; exit 0; fi

# ── 5. import + verify ──────────────────────────────────────────────────────────────────────────────────────────────
RESET_OPT=""
((RESET_LINKS)) && RESET_OPT="--send-reset-links"
if ((!YES)); then
  [[ -t 0 ]] || die "not a terminal: pass --yes to import without the confirmation prompt"
  ((RESET_LINKS)) && log "--send-reset-links: every migrated account that needs one gets a set-password e-mail"
  read -r -p "Import into $APP_DB on $TARGET now? Type 'import' to continue: " answer
  [[ "$answer" == import ]] || die "aborted (nothing imported; the dry-run report is in $REPORTS_DIR)"
fi
log "importing"
run_api "python manage.py import_backend --source-url \"\$LEGACY_BACKEND_URL\" $MEDIA_OPT $RESET_OPT --json /reports/backend.json -v 0" >/dev/null
summarise "$REPORTS_DIR/backend.json" || die "import reported problems: $REPORTS_DIR/backend.json"

log "verify_migration --source backend"
VERIFY_OPT=""
[[ -n "$OFFLINE" ]] && VERIFY_OPT="--offline"
# verify_migration exits non-zero whenever a check fails; which failures are expected is decided below.
run_api "python manage.py verify_migration --source backend --backend-url \"\$LEGACY_BACKEND_URL\" $VERIFY_OPT --json /reports/verify-backend.json" 2>&1 \
  | grep -E '^#[0-9]+ ' | sed 's/^/    /' || true
VERIFY_STATUS=0
python3 - "$REPORTS_DIR/verify-backend.json" <<'PY' || VERIFY_STATUS=$?
import json, sys
checks = json.load(open(sys.argv[1]))
known = {1: "expected before import_cms (company profile)", 6: "expected until --send-reset-links (cutover)", 10: "expected until import_flarize publishes PriceRelease #1"}
unexpected = 0
for c in checks:
    if c["status"] == "fail":
        unexpected += c["number"] not in known
        print(f"    #{c['number']} FAIL — {known.get(c['number'], 'NOT an expected failure: read the report')}")
sys.exit(2 if unexpected else 0)
PY

chmod 600 "$REPORTS_DIR"/*.json 2>/dev/null || true
log "done. reports: $REPORTS_DIR  platform backup: $BACKUP"
if ((VERIFY_STATUS)); then log "verification has failures beyond the expected ones: read $REPORTS_DIR/verify-backend.json"; fi
log "keep $LEGACY_DB until the cutover is signed off; then: DROP DATABASE $LEGACY_DB; DROP ROLE legacy_reader;"
exit "$VERIFY_STATUS"
