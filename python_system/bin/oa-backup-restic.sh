#!/usr/bin/env bash
# Restic-based backup of the outage-automation state directory and the
# approved-regions CSV. Run by the outage-automation-backup.service systemd unit.
#
# Env (set in /etc/systemd/system/outage-automation.env):
#   OA_RESTIC_REPO          — local path or restic URL: /var/backups/oa-restic,
#                              sftp:user@host:/path, s3:s3.amazonaws.com/bucket/...
#   OA_RESTIC_PASSWORD_FILE — path to the restic password file (chmod 600)
#   OA_ROOT                 — install root, used to resolve the state dir
#
# Tags each snapshot with "host:<hostname>" so multi-host repos can be
# filtered. The daily/weekly/monthly retention matches the convention
# recommended in docs/DEPLOYMENT.md.
#
# We back up the state dir AND the approved-regions.csv at the repo root.
# The dashboard editor can now mutate both, so both are needed for a
# full restore.
set -euo pipefail
: "${OA_RESTIC_REPO:?OA_RESTIC_REPO is required}"
: "${OA_RESTIC_PASSWORD_FILE:?OA_RESTIC_PASSWORD_FILE is required}"
: "${OA_ROOT:?OA_ROOT is required}"

STATE_DIR="${OA_ROOT}/python_system/state"
APPROVED_REGIONS_CSV="${OA_ROOT}/approved-regions.csv"

if [[ ! -d "${STATE_DIR}" && ! -f "${APPROVED_REGIONS_CSV}" ]]; then
  echo "oa-backup-restic: state dir and approved-regions.csv both missing at ${OA_ROOT}" >&2
  exit 1
fi

if ! command -v restic >/dev/null 2>&1; then
  echo "oa-backup-restic: restic not found in PATH" >&2
  exit 1
fi

# restic writes its cache to ${XDG_CACHE_HOME:-~/.cache}/restic. systemd's
# ProtectHome=read-only makes that path read-only, which spams the journal
# with "unable to open cache" warnings. Redirect to a writable location.
export XDG_CACHE_HOME="${OA_RESTIC_CACHE_DIR:-/var/cache/oa-restic}"
mkdir -p "${XDG_CACHE_HOME}"

HOST_TAG="host:$(hostname)"

# restic accepts multiple paths in a single invocation. Each path is
# backed up under its own prefix in the snapshot tree, so a `restic ls
# <snap>::python_system/state` or `restic ls <snap>::approved-regions.csv`
# retrieves either side independently.
BACKUP_PATHS=()
[[ -d "${STATE_DIR}" ]] && BACKUP_PATHS+=("${STATE_DIR}")
[[ -f "${APPROVED_REGIONS_CSV}" ]] && BACKUP_PATHS+=("${APPROVED_REGIONS_CSV}")

echo "oa-backup-restic: backing up ${BACKUP_PATHS[*]} to ${OA_RESTIC_REPO}"
restic -r "${OA_RESTIC_REPO}" \
       --password-file "${OA_RESTIC_PASSWORD_FILE}" \
       --tag "${HOST_TAG}" \
       backup "${BACKUP_PATHS[@]}"

echo "oa-backup-restic: applying retention (14 daily, 8 weekly, 6 monthly)"
restic -r "${OA_RESTIC_REPO}" \
       --password-file "${OA_RESTIC_PASSWORD_FILE}" \
       --tag "${HOST_TAG}" \
       forget --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
