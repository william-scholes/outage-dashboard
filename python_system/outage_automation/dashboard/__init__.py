"""Standalone outage dashboard.

The dashboard is intentionally dependency-free: it ships with stdlib
`http.server` plus a small static file handler. The UI is plain HTML/JS
that fetches `/api/dashboard` and renders itself.

Endpoints
---------
- `GET /`              → HTML (redirects to /dashboard)
- `GET /dashboard`     → HTML
- `GET /api/dashboard` → JSON (rows + impact + meta)
- `GET /api/health`    → JSON (liveness)
- `GET /api/customers` → JSON (sanitized customer list)
- `GET /static/*`      → CSS / JS / favicon
"""
from .server import run_server, build_dashboard_payload, ensure_self_signed_cert

__all__ = ["run_server", "build_dashboard_payload", "ensure_self_signed_cert"]
