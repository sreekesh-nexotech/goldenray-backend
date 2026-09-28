#!/usr/bin/env bash
# deploy/scripts/healthz-check.sh — VM cron probe of /healthz (PLAN §5.6: no monitoring stack).
#
#   */2 * * * * root /srv/flarize/app/deploy/scripts/healthz-check.sh
#
# Alerts OPS_EMAILS (comma-separated; `mail` from mailutils/bsd-mailx) when /healthz is unreachable, answers 503
# ("fail": database or cache down) or reports "degraded" (outbox lag, render queue, audit partitions) — once per
# state change, not every two minutes; a recovery is announced too. Always logs to syslog (tag flarize-healthz).
set -uo pipefail

URL="${HEALTHZ_URL:-https://flarize.com/healthz}"
RESOLVE="${HEALTHZ_RESOLVE:-127.0.0.1}"
STATE_FILE="${HEALTHZ_STATE_FILE:-/var/lib/flarize/healthz.state}"
OPS_EMAILS="${OPS_EMAILS:-$(sed -n 's/^OPS_EMAILS=//p' /srv/flarize/.env 2>/dev/null)}"

host="$(sed -E 's#^https?://([^/:]+).*#\1#' <<<"$URL")"
body="$(curl --silent --max-time 15 --resolve "${host}:443:${RESOLVE}" --write-out '\n%{http_code}' "$URL" 2>&1)"
code="$(tail -n1 <<<"$body")"
payload="$(sed '$d' <<<"$body")"

if [[ "$code" == "200" ]] && grep -Eq '"status": ?"ok"' <<<"$payload"; then
  state="ok"
elif [[ "$code" == "200" ]]; then
  state="degraded"
else
  state="fail"
fi

mkdir -p "$(dirname "$STATE_FILE")"
previous="$(cat "$STATE_FILE" 2>/dev/null || echo ok)"
printf '%s\n' "$state" >"$STATE_FILE"
logger -t flarize-healthz "state=$state http=$code"

if [[ "$state" != "$previous" && -n "$OPS_EMAILS" ]] && command -v mail >/dev/null 2>&1; then
  printf 'healthz on %s changed %s -> %s (HTTP %s)\n\n%s\n' "$(hostname)" "$previous" "$state" "$code" "$payload" \
    | mail -s "[flarize] healthz ${state^^} on $(hostname)" "${OPS_EMAILS//,/ }"
fi
[[ "$state" != "fail" ]]
