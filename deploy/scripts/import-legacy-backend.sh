#!/usr/bin/env bash
# deploy/scripts/import-legacy-backend.sh [options] <legacy-dump> — restore + import the legacy main backend (GoldenApp)
# dump. Same as `import-legacy.sh --source backend`; every option is documented there (`--help`).
exec "$(dirname "${BASH_SOURCE[0]}")/import-legacy.sh" --source backend "$@"
