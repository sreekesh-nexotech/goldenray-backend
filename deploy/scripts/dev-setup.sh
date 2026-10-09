#!/usr/bin/env bash
# One-off database setup for the local stack (the `setup` service of deploy/docker-compose.dev.yml).
# Mirrors the migrate steps of deploy/release.sh, then seeds the system roles. Idempotent: safe on every `up`.
# Optionally creates a Super Admin when DEV_ADMIN_EMAIL is set (prints a one-time set-password link; no default password).
set -Eeuo pipefail

until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-postgres}" -q; do
  echo "waiting for postgres at ${DB_HOST:-db}:${DB_PORT:-5432}…"
  sleep 1
done

python manage.py migrate --noinput
python manage.py ensure_audit_partitions --months 3
python manage.py maintain_adms_evidence --months 3
python manage.py ensure_inventory_append_only
python manage.py maintain_attendance_punches --months 3
python manage.py seed_pages
python manage.py seed_roles

if [[ -n "${DEV_ADMIN_EMAIL:-}" ]]; then
  # Fails harmlessly once the user exists; use "Forgot password" (console e-mail) if the link has expired.
  python manage.py bootstrap_admin --email "$DEV_ADMIN_EMAIL" --first-name Dev --last-name Admin || echo "bootstrap_admin skipped (admin already exists?)"
fi

echo "setup complete"
