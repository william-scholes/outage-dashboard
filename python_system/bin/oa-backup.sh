#!/usr/bin/env bash
# Snapshot the state directory + approved-regions.csv to a timestamped .tar.gz.
#
# Usage: oa-backup.sh [dest-dir]
# Default dest-dir is the parent of the state directory.
#
# Both the state directory (alert_history.json, customers.json, events.ndjson,
# reports/) and the approved-regions CSV at the repo root are needed to
# fully restore an outage-automation install. The dashboard editor can now
# mutate both, so both belong in the backup.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PY_SYSTEM_DIR="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

# Resolve the install root the same way the Python code does.
OA_ROOT="${OA_ROOT:-$(cd -- "${PY_SYSTEM_DIR}/.." >/dev/null 2>&1 && pwd)}"
STATE_DIR="${OA_ROOT}/python_system/state"
APPROVED_REGIONS_CSV="${OA_ROOT}/approved-regions.csv"
# Destination: positional arg > OA_BACKUP_DEST env > parent of state dir.
DEST_DIR="${1:-${OA_BACKUP_DEST:-$(dirname "${STATE_DIR}")}}"

# If the state dir doesn't exist, we still want to back up the CSV. The
# state dir is created on first `init`; the CSV is shipped in the repo.
BACKUP_PATHS=()
if [[ -d "${STATE_DIR}" ]]; then
    BACKUP_PATHS+=("python_system/state")
fi
if [[ -f "${APPROVED_REGIONS_CSV}" ]]; then
    BACKUP_PATHS+=("approved-regions.csv")
fi
if [[ ${#BACKUP_PATHS[@]} -eq 0 ]]; then
    echo "oa-backup: nothing to back up (state dir and approved-regions.csv both missing)" >&2
    exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${DEST_DIR%/}/outage-state-${TS}.tar.gz"

mkdir -p "${DEST_DIR}"
# `cd` to OA_ROOT so the tar paths are relative — lets a restore
# untar into a fresh checkout of the repo and get the layout right.
# Exclude the e2e scratch dir (created by examples/test_e2e.py).
tar -C "${OA_ROOT}" -czf "${BACKUP_FILE}" \
    --exclude='python_system/state_e2e_scratch' \
    --exclude='python_system/state/_tmp_*' \
    "${BACKUP_PATHS[@]}"

echo "${BACKUP_FILE}"
