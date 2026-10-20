#!/usr/bin/env bash
# Prune stale entries from events.ndjson (default: 90-day retention).
# Suitable for cron and systemd.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
: "${OA_ROOT:=$(cd -- "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd)}"
export OA_ROOT
exec "${SCRIPT_DIR}/oa" prune-events "$@"
