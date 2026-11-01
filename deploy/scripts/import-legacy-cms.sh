#!/usr/bin/env bash
# deploy/scripts/import-legacy-cms.sh [options] <legacy-dump> — restore + import the legacy website CMS (blog_cms) dump.
# Same as `import-legacy.sh --source cms`; every option is documented there (`--help`). When the backend is already
# imported, its CMS-dependent steps (job applications → positions, offer settings → company profile) are re-run.
exec "$(dirname "${BASH_SOURCE[0]}")/import-legacy.sh" --source cms "$@"
