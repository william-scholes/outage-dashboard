#!/usr/bin/env bash
# Run the full ingest cycle and write a run report.
# Suitable for cron and systemd.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
exec "${SCRIPT_DIR}/oa" run-all "$@"
