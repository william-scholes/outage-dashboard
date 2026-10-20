#!/usr/bin/env bash
# oa-rotate-password.sh — Generate and rotate Outage Automation Dashboard password
set -euo pipefail

PW_FILE="${1:-/etc/outage-automation/dashboard.pw}"
PW_DIR="$(dirname "${PW_FILE}")"

echo "Rotating dashboard password for ${PW_FILE}..."

if [[ ! -d "${PW_DIR}" ]]; then
    mkdir -p "${PW_DIR}"
    chmod 700 "${PW_DIR}"
fi

# Generate 32-character URL-safe high-entropy password
NEW_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# Write atomically to temporary file then replace
TMP_FILE="${PW_FILE}.tmp.$$"
echo "${NEW_PW}" > "${TMP_FILE}"
chmod 600 "${TMP_FILE}"
mv -f "${TMP_FILE}" "${PW_FILE}"

echo "Password successfully updated at ${PW_FILE} (permissions: 600)."

# Restart systemd dashboard service if active
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet outage-automation-dashboard.service 2>/dev/null; then
        echo "Restarting outage-automation-dashboard.service..."
        systemctl restart outage-automation-dashboard.service
        echo "Dashboard service restarted with new password."
    else
        echo "outage-automation-dashboard.service is not currently active."
    fi
fi
