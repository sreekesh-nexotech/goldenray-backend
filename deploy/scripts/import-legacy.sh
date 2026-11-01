#!/usr/bin/env bash
# deploy/scripts/import-legacy.sh --source backend|cms [options] <legacy-dump> — restore a legacy website pg_dump into
# its own database next to the platform's, then import it with `manage.py import_backend` / `import_cms` (PLAN §7.2,
# §7.3, docs/ops/runbook.md §7). Safe to re-run: the restore replaces only the legacy database (--replace) and the
# importers are idempotent (a second run reports `unchanged`). Wrappers: import-legacy-backend.sh, import-legacy-cms.sh.
#
#   1. checks the dump (pg_dump custom format) and backs up the platform database
#   2. creates the legacy database and restores the dump into it (pg_restore 18 → SQL → psql inside the db container:
#      no database port, no superuser password; the pg_dump 17+ `SET transaction_timeout` line is dropped because
#      Postgres 15/16 have no such setting — any other restore error stops the script)
#   3. opens it to a read-only login role `legacy_reader` with a fresh random password (closed again on exit)
#   4. seed_roles, then the importer's --dry-run; stops if the report is not ok
#   5. after confirmation: the real import (+ the legacy media, reset links if asked), then verify_migration
#      cms: when the backend was already imported (runbook order is CMS first), its CMS-dependent steps are re-run —
#      job applications → their CMS job positions, quotation offer settings → the company profile the CMS creates
#   6. every report goes to the reports directory (mode 0600: the reports contain personal data)
#
# Sources:
#   backend (default)  the main backend + BOM database (GoldenApp)      → legacy database legacy_backend
#   cms                the website CMS database (blog_cms)              → legacy database legacy_cms
#
# Targets:
#   prod (default)  deploy/docker-compose.yml on the VM, service api-a, image tag from /srv/flarize/releases/current
#   dev             deploy/docker-compose.dev.yml + the root .env (the local stack), service api
#   docker          any other deployment, addressed by container name (e.g. a hand-written compose file):
#                   --db-container goldenray-db --app-container goldenray-backend (the defaults)
#
# Options:
#   --source backend|cms     which legacy database the dump is (default backend)
#   --target prod|dev|docker which stack (default prod)
#   --db-container NAME      docker target: the Postgres container (default goldenray-db)
#   --app-container NAME     docker target: a running backend container of this repo (default goldenray-backend)
#   --via-pgbouncer          docker target: let the import write through the app's configured DB_HOST/DB_PORT
#                            (default: straight to the db container on 5432, around a transaction-mode PgBouncer)
#   --legacy-db NAME         database the dump is restored into (default legacy_backend / legacy_cms)
#   --backend-legacy-db NAME cms: the restored backend database for the re-run (default legacy_backend)
#   --replace                drop and re-create the legacy database when it already exists (a newer dump)
#   --media-volume NAME      docker volume with the legacy media (backend: resumes, offer images; cms: /uploads)
#   --media-dir PATH         same, from a host directory
#   --site-url URL           cms: the legacy site URL for company_profile.website when the CMS has none
#   --send-reset-links       e-mail set-password links to the migrated accounts (CUTOVER ONLY: real e-mails in prod)
#   --dry-run-only           stop after the dry run
#   --offline                verify without the CDN HEAD checks (default for dev)
#   --reports-dir PATH       where reports go (prod /srv/flarize/legacy/reports/<ts>, dev var/legacy-import/<ts>,
#                            docker ~/legacy-import/reports/<ts>)
#   --backup-dir PATH        where the pre-import backup goes (prod /srv/flarize/backups, dev var/backups,
#                            docker ~/legacy-import/backups)
#   --pg-client-image IMAGE  image whose pg_restore reads the dump (default postgres:18-alpine)
#   -y, --yes                do not ask before the real import
set -Eeuo pipefail

usage() { sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }
log() { printf '%s [legacy-import] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

SOURCE=backend TARGET=prod LEGACY_DB="" BACKEND_LEGACY_DB=legacy_backend REPLACE=0 MEDIA_VOLUME="" MEDIA_DIR=""
SITE_URL="" RESET_LINKS=0 DRY_ONLY=0 OFFLINE="" YES=0 REPORTS_DIR="" BACKUP_DIR="" PG_CLIENT_IMAGE=postgres:18-alpine
DUMP="" DB_CTR=goldenray-db APP_CTR=goldenray-backend VIA_PGBOUNCER=0
while (($#)); do
  case "$1" in
    --source) SOURCE="${2:?}"; shift 2 ;;
    --target) TARGET="${2:?}"; shift 2 ;;
    --legacy-db) LEGACY_DB="${2:?}"; shift 2 ;;
    --backend-legacy-db) BACKEND_LEGACY_DB="${2:?}"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --media-volume) MEDIA_VOLUME="${2:?}"; shift 2 ;;
    --media-dir) MEDIA_DIR="${2:?}"; shift 2 ;;
    --site-url) SITE_URL="${2:?}"; shift 2 ;;
    --send-reset-links) RESET_LINKS=1; shift ;;
    --dry-run-only) DRY_ONLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --reports-dir) REPORTS_DIR="${2:?}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?}"; shift 2 ;;
    --pg-client-image) PG_CLIENT_IMAGE="${2:?}"; shift 2 ;;
    --db-container) DB_CTR="${2:?}"; shift 2 ;;
    --app-container) APP_CTR="${2:?}"; shift 2 ;;
    --via-pgbouncer) VIA_PGBOUNCER=1; shift ;;
    -y | --yes) YES=1; shift ;;
    -h | --help) usage ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [[ -z "$DUMP" ]] || die "only one dump file"; DUMP="$1"; shift ;;
  esac
done
[[ -n "$DUMP" ]] || usage 64
case "$SOURCE" in
  backend)
    IMPORT_CMD=import_backend
    LEGACY_DB="${LEGACY_DB:-legacy_backend}"
    [[ -z "$SITE_URL" ]] || die "--site-url is a cms option"
    ;;
  cms)
    IMPORT_CMD=import_cms
    LEGACY_DB="${LEGACY_DB:-legacy_cms}"
    [[ "$BACKEND_LEGACY_DB" =~ ^[a-z_][a-z0-9_]*$ ]] || die "--backend-legacy-db must be a plain lower-case identifier"
    [[ "$BACKEND_LEGACY_DB" != "$LEGACY_DB" ]] || die "--backend-legacy-db must differ from --legacy-db"
    ;;
  *) die "--source must be backend or cms" ;;
esac
[[ "$LEGACY_DB" =~ ^[a-z_][a-z0-9_]*$ ]] || die "--legacy-db must be a plain lower-case identifier"
[[ -z "$MEDIA_VOLUME" || -z "$MEDIA_DIR" ]] || die "--media-volume and --media-dir are exclusive"
[[ -z "$SITE_URL" || "$SITE_URL" =~ ^https?://[A-Za-z0-9.:/_-]+$ ]] || die "--site-url must be a plain http(s) URL"

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
  docker)
    API="$APP_CTR"
    REPORTS_DIR="${REPORTS_DIR:-$HOME/legacy-import/reports/$STAMP}"
    BACKUP_DIR="${BACKUP_DIR:-$HOME/legacy-import/backups}"
    ;;
  *) die "--target must be prod, dev or docker" ;;
esac
dc() { "${COMPOSE[@]}" "$@"; }
# Commands inside the db container use its local socket (trusted by the postgres image): no port, no password.
if [[ "$TARGET" == docker ]]; then
  dbsh() { docker exec -i "$DB_CTR" sh -c "$1"; }
  psql_su() { docker exec -i "$DB_CTR" sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-$DB_USER}" "$@"' _ "$@"; }
else
  dbsh() { dc exec -T db sh -c "$1"; }
  psql_su() { dc exec -T db sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-$DB_USER}" "$@"' _ "$@"; }
fi
db_exists() { [[ "$(psql_su -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$1'")" == 1 ]]; }

# ── 1. checks ───────────────────────────────────────────────────────────────────────────────────────────────────────
[[ -f "$DUMP" && -r "$DUMP" ]] || die "cannot read $DUMP"
DUMP="$(cd "$(dirname "$DUMP")" && pwd)/$(basename "$DUMP")"
[[ "$(head -c 5 "$DUMP")" == "PGDMP" ]] || die "$DUMP is not a pg_dump custom-format archive (expected the PGDMP header)"
if [[ "$TARGET" != dev && "$DUMP" == "$REPO_DIR"/* ]]; then
  die "the dump is inside the repository checkout ($REPO_DIR; image builds copy it): move it out (mode 0600) first"
fi
# The archive names the database it was taken from: refuse a CMS dump passed as --source backend and vice versa.
DUMPED_DB="$(docker run --rm -v "$DUMP:/dump:ro" "$PG_CLIENT_IMAGE" pg_restore -l /dump 2>/dev/null | sed -n 's/^;[[:space:]]*dbname: //p' | head -1)"
case "$SOURCE:$DUMPED_DB" in
  cms:*cms* | backend:*[Gg]olden* | backend:*backend* | *:) ;;
  cms:*) die "this dump is of database '$DUMPED_DB', which does not look like the CMS (blog_cms): wrong file or --source?" ;;
  backend:*) die "this dump is of database '$DUMPED_DB', which looks like the CMS: pass --source cms" ;;
esac
[[ -z "$MEDIA_VOLUME" ]] || docker volume inspect "$MEDIA_VOLUME" >/dev/null 2>&1 || die "no docker volume $MEDIA_VOLUME (docker volume ls)"
[[ -z "$MEDIA_DIR" || -d "$MEDIA_DIR" ]] || die "no directory $MEDIA_DIR"
if [[ "$TARGET" == docker ]]; then
  for c in "$DB_CTR" "$APP_CTR"; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" == true ]] || die "container $c is not running (docker ps; --db-container/--app-container)"
  done
  nets() { docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" | tr ' ' '\n' | sed '/^$/d' | sort; }
  [[ -n "$(comm -12 <(nets "$DB_CTR") <(nets "$APP_CTR"))" ]] || die "$APP_CTR and $DB_CTR share no docker network: the import could not reach the legacy database"
  docker exec "$APP_CTR" test -f manage.py || die "$APP_CTR does not run this backend (no manage.py in its working directory)"
else
  dc ps --status running --services 2>/dev/null | grep -qx db || die "the db service of the $TARGET stack is not running"
fi

SU="$(dbsh 'printf %s "${POSTGRES_USER:-$DB_USER}"')"
APP_DB="$(dbsh 'printf %s "${POSTGRES_DB:-$DB_NAME}"')"
[[ -n "$SU" && -n "$APP_DB" ]] || die "could not read the database superuser / name from the db container"
[[ "$LEGACY_DB" != "$APP_DB" ]] || die "--legacy-db must not be the platform database ($APP_DB)"
log "source=$SOURCE target=$TARGET service=$API platform-db=$APP_DB legacy-db=$LEGACY_DB dump=$DUMP (database $DUMPED_DB)"
EXISTS=0
if db_exists "$LEGACY_DB"; then
  EXISTS=1
  ((REPLACE)) || die "database $LEGACY_DB already exists: pass --replace to load this dump over it"
fi
# cms: re-run the backend's CMS-dependent steps when the backend is already in (needs its restored database too).
RELINK=0
if [[ "$SOURCE" == cms ]]; then
  BACKEND_ROWS="$(psql_su -d "$APP_DB" -tAc "SELECT count(*) FROM core_legacy_map WHERE source_system = 'BACKEND'" 2>/dev/null || echo 0)"
  if [[ "$BACKEND_ROWS" != 0 ]]; then
    if db_exists "$BACKEND_LEGACY_DB"; then RELINK=1
    else log "warning: the backend is imported but $BACKEND_LEGACY_DB is gone: after this, re-run import-legacy-backend.sh to link job applications and the offer settings"; fi
  fi
fi

install -d -m 700 "$REPORTS_DIR" "$BACKUP_DIR"
IN_CTR="/tmp/legacy-import-$STAMP"   # docker target: reports and media live here inside the app container, removed on exit
if [[ "$TARGET" == docker ]]; then
  docker exec "$APP_CTR" sh -c "umask 077 && mkdir -p $IN_CTR/reports"
else
  CONTAINER_UID="$(dc run --rm --no-deps -T "$API" id -u 2>/dev/null | tail -1 | tr -d '\r')"
  [[ "$CONTAINER_UID" =~ ^[0-9]+$ ]] || die "could not start a one-off $API container"
  if [[ "$CONTAINER_UID" != "$(id -u)" ]]; then
    chown "$CONTAINER_UID" "$REPORTS_DIR" 2>/dev/null || die "the $API container runs as uid $CONTAINER_UID: run as root so it can write $REPORTS_DIR"
  fi
fi

LEGACY_PW=""
cleanup() {
  # as root: `docker cp` creates the copied media as root inside the container
  if [[ "$TARGET" == docker ]]; then docker exec -u 0 "$APP_CTR" rm -rf "$IN_CTR" >/dev/null 2>&1 || true; fi
  if [[ -n "$LEGACY_PW" ]]; then
    psql_su -d postgres -c "ALTER ROLE legacy_reader NOLOGIN PASSWORD NULL" >/dev/null 2>&1 || true
    log "legacy_reader closed again (NOLOGIN)"
  fi
}
trap cleanup EXIT
trap 'log "FAILED at line $LINENO — nothing after this step ran; the platform backup is in $BACKUP_DIR"' ERR

# ── 2. backup + restore ─────────────────────────────────────────────────────────────────────────────────────────────
BACKUP="$BACKUP_DIR/$APP_DB-before-legacy-$SOURCE-import-$STAMP.dump"
log "backing up $APP_DB → $BACKUP"
(umask 077 && dbsh 'pg_dump -U "${POSTGRES_USER:-$DB_USER}" -Fc "${POSTGRES_DB:-$DB_NAME}"' >"$BACKUP")
[[ "$(head -c 5 "$BACKUP")" == "PGDMP" ]] || die "backup failed ($BACKUP)"

if ((EXISTS)); then
  log "dropping the existing $LEGACY_DB (--replace)"
  psql_su -d postgres -c "DROP DATABASE $LEGACY_DB WITH (FORCE)"
fi
psql_su -d postgres -c "CREATE DATABASE $LEGACY_DB"

# pg_restore 18 writes \restrict/\unrestrict (they block meta-commands smuggled into a dump); psql knows them from
# 13.22 / 14.19 / 15.14 / 16.10 / 17.6 / 18. An older psql in the db container gets those lines removed (with a warning).
PSQL_VERSION="$(dbsh 'psql -V' | grep -oE '[0-9]+\.[0-9]+' | head -1)"
STRIP='/^SET transaction_timeout = 0;$/d'
if ! python3 -c 'import sys; M, m = map(int, sys.argv[1].split(".")); sys.exit(0 if M >= 18 or m >= {13: 22, 14: 19, 15: 14, 16: 10, 17: 6}.get(M, 99) else 1)' "$PSQL_VERSION"; then
  log "warning: psql $PSQL_VERSION in the db container predates \\restrict: those lines are removed (update the postgres image)"
  STRIP="$STRIP"'; /^\\\(un\)\{0,1\}restrict /d'
fi
log "restoring with $PG_CLIENT_IMAGE (pg_restore → SQL → psql $PSQL_VERSION in the db container)"
docker run --rm -v "$DUMP:/dump:ro" "$PG_CLIENT_IMAGE" pg_restore --no-owner --no-acl -f - /dump \
  | sed "$STRIP" \
  | psql_su -d "$LEGACY_DB" >/dev/null
TABLES="$(psql_su -d "$LEGACY_DB" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
log "restored: $TABLES tables"
if [[ "$SOURCE" == backend ]]; then
  ROWS_SQL="SELECT 'rows:', (SELECT count(*) FROM auth_user) || ' users', (SELECT count(*) FROM bom_catalogitem) || ' catalog items',
    (SELECT count(*) FROM pincodes) || ' pincodes', (SELECT count(*) FROM lead_collection_home) || ' leads',
    (SELECT count(*) FROM job_application) || ' job applications'"
else
  ROWS_SQL="SELECT 'rows:', (SELECT count(*) FROM accounts_admin_user) || ' admin users', (SELECT count(*) FROM content_entry) || ' entries',
    (SELECT count(*) FROM sitepages_page) || ' pages', (SELECT count(*) FROM faqs_faq) || ' faqs',
    (SELECT count(*) FROM careers_job_position) || ' job positions', (SELECT count(*) FROM media_asset) || ' media assets'"
fi
psql_su -d "$LEGACY_DB" -tA -F' ' -c "$ROWS_SQL" | sed 's/^/    /' \
  || log "warning: some expected legacy tables are missing (the dry run lists them)"

# ── 3. read-only access for the importer ────────────────────────────────────────────────────────────────────────────
# Hex only, so it is safe inside the SQL text; sent on stdin, never as a process argument.
LEGACY_PW="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
psql_su -d postgres <<SQL
SELECT 'CREATE ROLE legacy_reader' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'legacy_reader') \gexec
ALTER ROLE legacy_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$LEGACY_PW';
SQL
grant_reader() {
  psql_su -d postgres -c "GRANT CONNECT ON DATABASE $1 TO legacy_reader"
  psql_su -d "$1" -c "GRANT USAGE ON SCHEMA public TO legacy_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO legacy_reader"
}
grant_reader "$LEGACY_DB"
((RELINK)) && grant_reader "$BACKEND_LEGACY_DB"
LEGACY_HOST=db
[[ "$TARGET" != docker ]] || LEGACY_HOST="$DB_CTR"
legacy_url() { printf 'postgresql://legacy_reader:%s@%s:5432/%s' "$LEGACY_PW" "$LEGACY_HOST" "$1"; }
LEGACY_URL="$(legacy_url "$LEGACY_DB")"
LEGACY_BACKEND_URL="$(legacy_url "$BACKEND_LEGACY_DB")"
export LEGACY_URL LEGACY_BACKEND_URL

MEDIA_ARGS=() MEDIA_OPT=""
if [[ "$TARGET" == docker ]]; then
  # `docker exec` cannot mount: the media is copied into the running container for this run and removed on exit.
  if [[ -n "$MEDIA_VOLUME" ]]; then MEDIA_DIR="$(docker volume inspect -f '{{.Mountpoint}}' "$MEDIA_VOLUME")"; fi
  if [[ -n "$MEDIA_DIR" ]]; then
    [[ -r "$MEDIA_DIR" ]] || die "cannot read $MEDIA_DIR (a docker volume's directory needs root)"
    log "copying the legacy media into $APP_CTR ($(du -sh "$MEDIA_DIR" | cut -f1))"
    docker cp "$MEDIA_DIR/." "$APP_CTR:$IN_CTR/media" >/dev/null
    MEDIA_OPT="--media-root $IN_CTR/media"
  fi
elif [[ -n "$MEDIA_VOLUME" ]]; then MEDIA_ARGS=(-v "$MEDIA_VOLUME:/legacy-media:ro"); MEDIA_OPT="--media-root /legacy-media"
elif [[ -n "$MEDIA_DIR" ]]; then MEDIA_ARGS=(-v "$(cd "$MEDIA_DIR" && pwd):/legacy-media:ro"); MEDIA_OPT="--media-root /legacy-media"
fi
if [[ -z "$MEDIA_OPT" ]]; then
  if [[ "$SOURCE" == backend ]]; then log "no --media-volume/--media-dir: resumes and offer images are imported as missing (listed in the report)"
  else log "no --media-volume/--media-dir: media already on the CDN is kept; files that only exist in the CMS /uploads are listed as missing"; fi
fi
EXTRA_OPT=""
[[ -z "$SITE_URL" ]] || EXTRA_OPT="--site-url $SITE_URL"

# The URLs travel as environment variables (never on a command line we print). Reports: $RIN inside the container,
# then $REPORTS_DIR on the host (a bind mount for the compose targets, `docker cp` for the docker target).
if [[ "$TARGET" == docker ]]; then
  RIN="$IN_CTR/reports"
  DB_ENV=()
  ((VIA_PGBOUNCER)) || DB_ENV=(-e DB_HOST="$DB_CTR" -e DB_PORT=5432)
  run_api() { docker exec -i -e LEGACY_URL -e LEGACY_BACKEND_URL "${DB_ENV[@]}" "$APP_CTR" sh -c "$1"; }
  fetch() { docker cp "$APP_CTR:$RIN/$1" "$REPORTS_DIR/$1" >/dev/null && chmod 600 "$REPORTS_DIR/$1"; }
else
  RIN=/reports
  run_api() { dc run --rm --no-deps -T -e LEGACY_URL -e LEGACY_BACKEND_URL -v "$REPORTS_DIR:/reports" "${MEDIA_ARGS[@]}" "$API" sh -c "$1"; }
  fetch() { :; }
fi

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
if [[ "$SOURCE" == backend ]]; then
  CMS_ROWS="$(psql_su -d "$APP_DB" -tAc "SELECT count(*) FROM core_legacy_map WHERE source_system = 'CMS'" 2>/dev/null || echo 0)"
  [[ "$CMS_ROWS" != 0 ]] || log "warning: the CMS has not been imported (runbook order: import_cms first) — job applications stay unlinked, the company profile is not created, verify #1 fails"
fi
log "seed_roles"
run_api 'python manage.py seed_roles' | tail -1 | sed 's/^/    /'

log "$IMPORT_CMD dry run (writes nothing)"
run_api "python manage.py $IMPORT_CMD --source-url \"\$LEGACY_URL\" $MEDIA_OPT $EXTRA_OPT --dry-run --json $RIN/$SOURCE-dryrun.json -v 0" >/dev/null
fetch "$SOURCE-dryrun.json"
summarise "$REPORTS_DIR/$SOURCE-dryrun.json" || die "dry run is not ok: read $REPORTS_DIR/$SOURCE-dryrun.json before importing"
if ((DRY_ONLY)); then log "--dry-run-only: stopping here (reports in $REPORTS_DIR)"; exit 0; fi

# ── 5. import + verify ──────────────────────────────────────────────────────────────────────────────────────────────
RESET_OPT=""
((RESET_LINKS)) && RESET_OPT="--send-reset-links"
if ((!YES)); then
  [[ -t 0 ]] || die "not a terminal: pass --yes to import without the confirmation prompt"
  ((RESET_LINKS)) && log "--send-reset-links: every migrated account that needs one gets a set-password e-mail"
  read -r -p "Import the $SOURCE dump into $APP_DB on $TARGET now? Type 'import' to continue: " answer
  [[ "$answer" == import ]] || die "aborted (nothing imported; the dry-run report is in $REPORTS_DIR)"
fi
log "$IMPORT_CMD"
run_api "python manage.py $IMPORT_CMD --source-url \"\$LEGACY_URL\" $MEDIA_OPT $EXTRA_OPT $RESET_OPT --json $RIN/$SOURCE.json -v 0" >/dev/null
fetch "$SOURCE.json"
summarise "$REPORTS_DIR/$SOURCE.json" || die "import reported problems: $REPORTS_DIR/$SOURCE.json"

VERIFY_SOURCES="--source $SOURCE --$SOURCE-url \"\$LEGACY_URL\""
if ((RELINK)); then
  log "import_backend --only backend.careers --only backend.company (re-run against $BACKEND_LEGACY_DB now that the CMS is in)"
  run_api "python manage.py import_backend --source-url \"\$LEGACY_BACKEND_URL\" --only backend.careers --only backend.company --json $RIN/backend-relink.json -v 0" >/dev/null
  fetch backend-relink.json
  summarise "$REPORTS_DIR/backend-relink.json" || die "backend re-run reported problems: $REPORTS_DIR/backend-relink.json"
  VERIFY_SOURCES="$VERIFY_SOURCES --source backend --backend-url \"\$LEGACY_BACKEND_URL\""
fi

log "verify_migration $(sed 's/ --[a-z]*-url "[^"]*"//g' <<<"$VERIFY_SOURCES")"
VERIFY_OPT=""
[[ -n "$OFFLINE" ]] && VERIFY_OPT="--offline"
# verify_migration exits non-zero whenever a check fails; which failures are expected is decided below.
run_api "python manage.py verify_migration $VERIFY_SOURCES $VERIFY_OPT --json $RIN/verify-$SOURCE.json" 2>&1 \
  | grep -E '^#[0-9]+ ' | sed 's/^/    /' || true
fetch "verify-$SOURCE.json"
VERIFY_STATUS=0
python3 - "$REPORTS_DIR/verify-$SOURCE.json" "$SOURCE" "$RELINK" <<'PY' || VERIFY_STATUS=$?
import json, sys
checks = json.load(open(sys.argv[1]))
source, relinked = sys.argv[2], sys.argv[3] == "1"
known = {6: "expected until --send-reset-links (cutover)"}
if source == "backend" or relinked:
    known[10] = "expected until import_flarize publishes PriceRelease #1"
if source == "backend":
    known[1] = "expected before import_cms (company profile)"
unexpected = 0
for c in checks:
    if c["status"] == "fail":
        unexpected += c["number"] not in known
        print(f"    #{c['number']} FAIL — {known.get(c['number'], 'NOT an expected failure: read the report')}")
sys.exit(2 if unexpected else 0)
PY

chmod 600 "$REPORTS_DIR"/*.json 2>/dev/null || true
log "done. reports: $REPORTS_DIR  platform backup: $BACKUP"
if ((VERIFY_STATUS)); then log "verification has failures beyond the expected ones: read $REPORTS_DIR/verify-$SOURCE.json"; fi
log "keep $LEGACY_DB until the cutover is signed off; then: DROP DATABASE $LEGACY_DB; (and DROP ROLE legacy_reader; once no legacy database is left)"
exit "$VERIFY_STATUS"
