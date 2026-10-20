# Production Deployment Guide

This guide covers deploying **Outage Automation** as a persistent background daemon and automated ingest pipeline on a production Linux server (Ubuntu/Debian Server, VM, Proxmox LXC container, or Cloud VM).

---

## 1. Prerequisites

- **Operating System:** Linux (Ubuntu 22.04+, Debian 12+, or equivalent systemd-based Linux distribution).
- **Python:** Python 3.10 or higher.
- **Network:** Outbound HTTPS (TCP 443) to provider status feeds.
- **Service Account:** Dedicated system user (e.g. `outage-automation` or your service account).

---

## 2. Directory & User Setup

```bash
# 1. Create a dedicated system user and group
sudo useradd -r -s /bin/false -d /opt/outage-automation outage-automation

# 2. Clone repository to /opt/outage-automation
sudo git clone https://github.com/william-scholes/outage-dashboard.git /opt/outage-automation
sudo chown -R outage-automation:outage-automation /opt/outage-automation

# 3. Ensure CLI scripts have execution permissions
sudo chmod +x /opt/outage-automation/python_system/bin/*

# 4. Initialize state directory
sudo -u outage-automation python3 /opt/outage-automation/python_system/run.py init
```

---

## 3. Authentication & Environment Configuration

### Create Password File
Store the dashboard password in a secure, restricted-permission file:

```bash
sudo mkdir -p /etc/outage-automation
sudo tee /etc/outage-automation/dashboard.pw << 'EOF'
your-secure-password-here
EOF

sudo chown outage-automation:outage-automation /etc/outage-automation/dashboard.pw
sudo chmod 600 /etc/outage-automation/dashboard.pw
```

### Configure Systemd Environment File
Create `/etc/systemd/system/outage-automation.env`:

```bash
sudo tee /etc/systemd/system/outage-automation.env << 'EOF'
OA_ROOT=/opt/outage-automation
OA_DASHBOARD_HOST=0.0.0.0
OA_DASHBOARD_PORT=8765
OA_DASHBOARD_USER=outage-viewer
OA_DASHBOARD_PASSWORD_FILE=/etc/outage-automation/dashboard.pw
OA_LOG_LEVEL=info
OA_LOG_FORMAT=json
OA_TTL_DAYS=7
EOF

sudo chmod 600 /etc/systemd/system/outage-automation.env
```

---

## 4. TLS Certificate Setup (HTTPS)

### Option A: Generate 10-Year Self-Signed SAN Certificate
The built-in CLI can generate a self-signed TLS certificate with Subject Alternative Names (SANs) for your server IP and hostnames:

```bash
sudo /opt/outage-automation/python_system/bin/oa generate-cert \
  --cert /etc/outage-automation/dashboard.crt \
  --key /etc/outage-automation/dashboard.key \
  --host <your-server-ip>

sudo chown -R outage-automation:outage-automation /etc/outage-automation/
sudo chmod 600 /etc/outage-automation/dashboard.key
```

Add the SSL variables to `/etc/systemd/system/outage-automation.env`:

```ini
OA_DASHBOARD_SSL=1
OA_DASHBOARD_SSL_CERT=/etc/outage-automation/dashboard.crt
OA_DASHBOARD_SSL_KEY=/etc/outage-automation/dashboard.key
OA_DASHBOARD_REDIRECT_PORT=8764
```

### Option B: Use Custom / CA-Signed Certificate
Place your PEM-encoded `.crt` and `.key` at `/etc/outage-automation/dashboard.crt` and `/etc/outage-automation/dashboard.key` with `chmod 600` permissions.

---

## 5. Systemd Units & Service Setup

The repository ships systemd unit files under `python_system/etc/systemd/`:

### Install Units
```bash
sudo cp /opt/outage-automation/python_system/etc/systemd/outage-automation.service \
        /opt/outage-automation/python_system/etc/systemd/outage-automation.timer \
        /opt/outage-automation/python_system/etc/systemd/outage-automation-dashboard.service \
        /opt/outage-automation/python_system/etc/systemd/outage-automation-backup.service \
        /opt/outage-automation/python_system/etc/systemd/outage-automation-backup.timer \
        /opt/outage-automation/python_system/etc/systemd/outage-automation-prune.service \
        /opt/outage-automation/python_system/etc/systemd/outage-automation-prune.timer \
        /etc/systemd/system/
```

### Configure Drop-In Overrides
Create drop-in overrides to specify `User=`, `Group=`, and `ReadWritePaths=`:

```bash
for svc in outage-automation outage-automation-dashboard outage-automation-backup outage-automation-prune; do
  sudo mkdir -p /etc/systemd/system/${svc}.service.d
  sudo tee /etc/systemd/system/${svc}.service.d/host.conf >/dev/null <<EOF
[Service]
User=outage-automation
Group=outage-automation
ReadWritePaths=/opt/outage-automation/python_system/state
EOF
done

# The backup service also needs access to the backup repository
sudo tee /etc/systemd/system/outage-automation-backup.service.d/host.conf >/dev/null <<EOF
[Service]
User=outage-automation
Group=outage-automation
ReadWritePaths=/var/backups/oa-restic /var/cache/oa-restic /opt/outage-automation/python_system/state
EOF
```

### Reload & Enable Services
```bash
sudo systemctl daemon-reload

# Start and enable the dashboard service
sudo systemctl enable --now outage-automation-dashboard.service

# Start and enable the ingest timer (runs every 5 min)
sudo systemctl enable --now outage-automation.timer

# Start and enable the backup timer (runs daily at 03:30)
sudo systemctl enable --now outage-automation-backup.timer

# Start and enable the prune timer (runs weekly, Sunday at 02:00)
sudo systemctl enable --now outage-automation-prune.timer
```

---

## 6. Backups & Disaster Recovery

The system provides two backup scripts to meet different operational requirements:

* **Automated Encrypted Backups (`python_system/bin/oa-backup-restic.sh`):** Used by the `outage-automation-backup.timer` systemd unit for automated daily snapshots using [Restic](https://restic.net/). Provides client-side encryption, deduplication, remote target support (local path, S3, SFTP), and retention pruning (`14 daily, 8 weekly, 6 monthly`).
* **Manual Zero-Dependency Snapshots (`python_system/bin/oa-backup.sh`):** Creates an immediate timestamped `.tar.gz` archive of `state/` and `approved-regions.csv` using standard POSIX `tar`. Requires no external tools; ideal for quick ad-hoc backups before manual upgrades or migrations:
  ```bash
  ./python_system/bin/oa-backup.sh /path/to/backup/dir
  ```

### Setting Up Automated Restic Backups

```bash
# 1. Install restic
sudo apt-get update && sudo apt-get install -y restic

# 2. Generate restic repository password
RPW=$(openssl rand -base64 36 | tr -d '/+=' | head -c 32)
printf '%s' "$RPW" | sudo tee /etc/outage-automation/restic.pw >/dev/null
sudo chown outage-automation:outage-automation /etc/outage-automation/restic.pw
sudo chmod 600 /etc/outage-automation/restic.pw

# 3. Create repository and cache directories
sudo install -d -m 755 -o outage-automation -g outage-automation /var/backups/oa-restic
sudo install -d -m 755 -o outage-automation -g outage-automation /var/cache/oa-restic

# 4. Initialize repository
sudo -u outage-automation \
  OA_RESTIC_PASSWORD_FILE=/etc/outage-automation/restic.pw \
  restic -r /var/backups/oa-restic init

# 5. Add backup configuration to environment file
cat <<EOF | sudo tee -a /etc/systemd/system/outage-automation.env >/dev/null
OA_RESTIC_REPO=/var/backups/oa-restic
OA_RESTIC_PASSWORD_FILE=/etc/outage-automation/restic.pw
EOF
```

---

## 7. Firewall Configuration (UFW)

If using UFW, restrict dashboard access to your management LAN / VPN subnet:

```bash
# Allow SSH
sudo ufw allow OpenSSH

# Allow dashboard port from corporate subnet only (e.g. 192.0.2.0/24)
sudo ufw allow proto tcp from 192.0.2.0/24 to any port 8765
sudo ufw allow proto tcp from 192.0.2.0/24 to any port 8764

# Enable firewall
sudo ufw enable
```

---

## 8. Automated Events Pruning & Storage Safeguards

The 5-minute ingest cycle continuously appends raw events to `state/events.ndjson`. Without pruning, this file grows indefinitely and can consume available disk space. The prune timer removes entries older than the retention window (default: 90 days) once per week.

| Timer | Schedule | What it does |
|---|---|---|
| `outage-automation.timer` | Every 5 min | Ingest from all providers |
| `outage-automation-backup.timer` | Daily 03:30 | Restic snapshot of state |
| `outage-automation-prune.timer` | Weekly Sun 02:00 | Prune events.ndjson (90-day retention) |

### Storage & Resilience Safeguards
- **Exception Cleanup:** The streaming pruner uses `try ... finally` to guarantee that `events.prune.tmp` is deleted even if interrupted or encountering an I/O error.
- **Pre-flight Headroom Check:** Verifies that available filesystem free space is at least 1.2x the size of `events.ndjson` before creating temporary files, aborting cleanly if headroom is insufficient.
- **Service Cleanup Hook:** `ExecStopPost=/bin/rm -f .../events.prune.tmp` ensures systemd cleans up temporary files on service exit under all exit codes.
- **Tmpfiles Aging:** Systemd-tmpfiles rule (`/etc/tmpfiles.d/outage-automation.conf`) sweeps stale `.tmp` files older than 1 hour.

### Manual Prune

```bash
# Preview what will be removed (no changes made)
oa prune-events --days 90 --dry-run

# Prune entries older than 90 days
oa prune-events --days 90
```

Override the retention window via the `OA_EVENTS_RETENTION_DAYS` environment variable or `--days` flag.

---

## 9. Verification & Health Monitoring

```bash
# Check timer statuses (ingest, backup, and prune)
systemctl list-timers 'outage-automation*'

# Check dashboard service status
systemctl status outage-automation-dashboard.service

# Check health probe
curl -kfsS https://127.0.0.1:8765/api/health

# Trigger a manual test ingest
sudo systemctl start outage-automation.service
journalctl -u outage-automation.service -n 30 --no-pager

# Trigger a manual prune
sudo systemctl start outage-automation-prune.service
journalctl -u outage-automation-prune.service -n 30 --no-pager
```
