#!/usr/bin/env sh
# Source 2 of the endpoint inventory: the drf-spectacular OpenAPI schema for v1 (runs Django inside the API container).
#   sh docs/postman/_work/dump_schema.sh            (from the repo root, with the dev stack up: `make up`)
set -eu
CONTAINER="${API_CONTAINER:-flarize-dev-api-1}"
docker exec -w /app "$CONTAINER" python manage.py spectacular --api-version v1 --format openapi-json --file docs/postman/_work/openapi.v1.json
echo "wrote docs/postman/_work/openapi.v1.json"
