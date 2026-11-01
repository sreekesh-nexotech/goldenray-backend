#!/usr/bin/env bash
# Regenerate the Postman collection from the code (run from the repo root with the dev stack up: `make up`).
#
#   bash docs/postman/_work/regenerate.sh
#
# Sources -> inventory -> seeds -> fragments (synced; new endpoints started from seeds) -> collection -> checks -> report.
# Exits non-zero if any check fails. New endpoints come out as seed-only fragments flagged "needs review" in
# COVERAGE_REPORT.md: enrich them from the code (see AGENT_BRIEF.md), then run this again.
set -euo pipefail
W=docs/postman/_work
CONTAINER="${API_CONTAINER:-flarize-dev-api-1}"

docker exec -w /app "$CONTAINER" python "$W/dump_urls.py"          # source 1: live URL resolver
API_CONTAINER="$CONTAINER" sh "$W/dump_schema.sh"                   # source 2: drf-spectacular schema
python3 "$W/static_scan.py" > /dev/null                             # source 3: static scan (exit 1 on problems)
python3 "$W/build_inventory.py"                                     # merge -> inventory.json
python3 "$W/seed_fragments.py" > /dev/null                          # schema-derived seeds
python3 "$W/seed_fragments.py" --add-missing                        # fragments for endpoints that have none
python3 "$W/seed_fragments.py" --sync-fragments                     # inventory-owned fields -> fragments
python3 "$W/reconcile.py" | tail -1                                 # Phase 3 (exit 1 on any gap)
python3 "$W/build_collection.py"                                    # Phase 4
python3 "$W/verify_coverage.py" | grep -E "inventory endpoints|MISSING|EXTRA|MISROUTE|resolved like"   # Phase 5 (exit 1 if anything missing)
python3 "$W/validate_collection.py"                                 # Postman v2.1 JSON schema
python3 "$W/write_report.py"
