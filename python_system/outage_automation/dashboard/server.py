"""Dashboard HTTP server (stdlib only).

Read endpoints
--------------
- `GET /api/dashboard`     → rows + computed customer impact
- `GET /api/health`        → liveness probe (always unauthenticated)
- `GET /api/customers`     → customers (sanitized, useful for debugging)
- `GET /api/regions`       → approved-regions.csv (full 5-column shape)
- `GET /api/runs`          → list of recent import-run metadata
- `GET /api/runs/<id>`     → full report for a single run
- `GET /dashboard`         → HTML
- `GET /`                  → redirect to /dashboard
- `GET /static/<file>`     → static assets (CSS / JS / favicon)

Write endpoints (auth-gated, lock-protected, atomic on disk)
-----------------------------------------------------------
- `POST   /api/customers`                              → create
- `PUT    /api/customers/<id>`                         → update
- `DELETE /api/customers/<id>`                         → delete
- `POST   /api/regions`                                → add (service, region_code)
- `PUT    /api/regions/<service>/<region_code>`        → rename display_name
- `DELETE /api/regions/<service>/<region_code>[?force]` → remove

Auth model
----------
When `auth` is configured on the handler (a `(user, password)` tuple), every
endpoint except `/api/health` requires HTTP Basic Auth. The handler compares
the provided credentials in constant time. When `auth` is `None`, the server
behaves exactly as it did before — but `run_server` will refuse to bind off
`127.0.0.1` in that case, so accidentally exposing an unauthenticated
dashboard requires an explicit CLI override.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..config import DEFAULT_SERVICES, get_brand_title, load_region_rules
from ..lock import FileLock, read_lock_info
from ..model import Customer
from ..paths import Paths
from ..processor import compute_customer_impact, filter_active_window
from ..report import list_reports, read_report
from ..store import read_json, write_json
from ..validate import validate_customers
from .regions_store import (
    RegionRow,
    add_region as add_region_row,
    load_regions_csv,
    remove_region as remove_region_row,
    slugify,
    unique_id,
    update_display_name as update_display_name_row,
    validate_regions,
    write_regions_csv,
)


DEFAULT_TTL_DAYS = 7
STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE_NAME = "oa_session"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 days

# Endpoints that must remain reachable without credentials. Health is a
# liveness probe; we want monitoring/load-balancer checks to work even if
# the operator forgets the password. Favicons are public so browsers can load
# icons without triggering auth challenges. Login and logout manage session state.
PUBLIC_PATHS = frozenset({
    "/api/health",
    "/favicon.ico",
    "/static/favicon.svg",
    "/login",
    "/logout",
})

# Hosts that are considered "loopback" — safe to bind without auth.
# NOTE: `0.0.0.0` and `::` are NOT loopback; they're wildcards that bind on
# every interface and will expose the dashboard to the network. Refuse that
# unless auth is configured.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

AUTH_REALM = "Outage Automation Dashboard"

# Max bytes accepted on a write endpoint. Generous for hundreds of customers
# or a single region row; bulk imports belong on the CLI, not the dashboard.
MAX_WRITE_BODY_BYTES = 256 * 1024

# URL path-segment validation for customer_id / service / region_code.
# Kebab-case / lowercase alphanumeric, plus 'global' for region_code.
_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_REGION_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


class RateLimiter:
    """In-memory rate limiter tracking failed authentication attempts per client IP.

    Configurable via:
      - OA_AUTH_MAX_FAILURES: Max failed attempts before lockout (default: 5)
      - OA_AUTH_FAIL_WINDOW: Window in seconds to track failures (default: 60)
      - OA_AUTH_LOCKOUT_SECONDS: Lockout duration in seconds (default: 900 / 15m)
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 60.0,
        lockout_seconds: float = 900.0,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._failures: Dict[str, List[float]] = {}
        self._lockouts: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, ip: str) -> Tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            expires = self._lockouts.get(ip)
            if expires is not None:
                if now < expires:
                    return True, max(1, int(expires - now))
                del self._lockouts[ip]
            return False, 0

    def record_failure(self, ip: str, user: str = "") -> Tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            expires = self._lockouts.get(ip)
            if expires is not None and now < expires:
                return True, max(1, int(expires - now))

            history = self._failures.setdefault(ip, [])
            history[:] = [t for t in history if now - t <= self.window_seconds]
            history.append(now)

            sys.stderr.write(
                f"[dashboard] [AUTH_FAIL] ip={ip} user={user} attempts={len(history)}/{self.max_failures}\n"
            )

            if len(history) >= self.max_failures:
                self._lockouts[ip] = now + self.lockout_seconds
                self._failures.pop(ip, None)
                sys.stderr.write(
                    f"[dashboard] [AUTH_LOCKOUT] ip={ip} locked out for {int(self.lockout_seconds)}s\n"
                )
                return True, int(self.lockout_seconds)

            return False, 0

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._lockouts.pop(ip, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._lockouts.clear()


RATE_LIMITER = RateLimiter(
    max_failures=int(os.environ.get("OA_AUTH_MAX_FAILURES", "5")),
    window_seconds=float(os.environ.get("OA_AUTH_FAIL_WINDOW", "60")),
    lockout_seconds=float(os.environ.get("OA_AUTH_LOCKOUT_SECONDS", "900")),
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ttl_days() -> int:
    try:
        return int(os.environ.get("OA_TTL_DAYS") or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def _lock_path(paths: Paths) -> Path:
    """Return the canonical state-dir lock path. Mirrors `cli._lock_paths`."""
    return paths.state_dir / ".lock"


def _lock_error_message(lock_path: Path) -> str:
    info = read_lock_info(lock_path)
    if info and info.pid:
        return (
            f"locked by pid {info.pid} since "
            f"{_dt.datetime.fromtimestamp(info.created_at, tz=_dt.timezone.utc).isoformat()}"
        )
    return "state directory is currently locked by another operation"


def _load_customers(paths: Paths) -> List[Customer]:
    raw = read_json(paths.customers_path, [])
    out: List[Customer] = []
    if isinstance(raw, list):
        for d in raw:
            if isinstance(d, dict):
                try:
                    out.append(Customer.from_dict(d))
                except Exception:
                    continue
    return out


def build_dashboard_payload(paths: Paths) -> Dict[str, Any]:
    """Backwards-compatible shim. The real implementation lives in
    `dashboard_payload.build_dashboard_payload`. Server callers get
    `include_last_run=True` so the dashboard's "last run" indicator keeps working.
    """
    from ..dashboard_payload import build_dashboard_payload as _build
    return _build(paths, include_last_run=True)


def _customer_payload(paths: Paths) -> List[Dict[str, Any]]:
    customers = _load_customers(paths)
    return [
        {
            "customer_id": c.customer_id,
            "name": c.name,
            "subscriptions": c.subscriptions,
            "sub_services": c.sub_services,
        }
        for c in customers
    ]


def _regions_payload(paths: Paths) -> List[Dict[str, Any]]:
    rows = load_regions_csv(paths.approved_regions_csv)
    return [r.to_dict() for r in rows]


# Caps for the /api/runs `limit` query parameter.
RUNS_LIMIT_DEFAULT = 20
RUNS_LIMIT_MIN = 1
RUNS_LIMIT_MAX = 100


def _handle_runs_list(paths: Paths, query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
    """Parse `?limit=N` and return the list payload, or (status, error)."""
    raw = query.get("limit", [str(RUNS_LIMIT_DEFAULT)])[0]
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return 400, {"error": "limit must be an integer"}
    if limit < RUNS_LIMIT_MIN:
        limit = RUNS_LIMIT_MIN
    elif limit > RUNS_LIMIT_MAX:
        limit = RUNS_LIMIT_MAX
    runs = list_reports(paths.state_dir, limit=limit)
    return 200, {"runs": runs, "limit": limit}


def _handle_runs_detail(paths: Paths, run_id: str) -> Tuple[int, Dict[str, Any]]:
    """Return the full report for a single run, or (status, error)."""
    # read_report validates run_id via RUN_ID_RE and returns None on any
    # failure (malformed id, missing file, parse error). The HTTP layer
    # turns None into a 404 so callers see the same error for "no such run"
    # and "malformed id" — we don't want to leak id-validation details.
    report = read_report(paths.state_dir, run_id)
    if report is None:
        return 404, {"error": "run not found", "run_id": run_id}
    return 200, report


def _validation_details(errs: Sequence[Any]) -> List[Dict[str, str]]:
    return [{"path": e.path, "message": e.message} for e in errs]


# ---------------------------------------------------------------------------
# Customer write handlers
# ---------------------------------------------------------------------------


def _read_customers_raw(paths: Paths) -> List[Dict[str, Any]]:
    raw = read_json(paths.customers_path, [])
    return raw if isinstance(raw, list) else []


def _validate_sub_services_body(
    sub_services_in: Any, *, path_prefix: str = "sub_services"
) -> List[Dict[str, str]]:
    """Validate the raw sub_services body shape before normalization.

    `Customer.from_dict` strips empty strings and dedupes, so by the time
    `validate_customers` sees the row, a payload like `{"zscaler": [""]}`
    has already collapsed to `{"zscaler": []}`. This helper catches
    empty / non-string codes in the *raw* body, returning a list of
    structured errors in the same shape `validate_customers` uses.
    """
    errs: List[Dict[str, str]] = []
    if sub_services_in is None:
        return errs
    if not isinstance(sub_services_in, dict):
        errs.append({
            "path": path_prefix,
            "message": "must be an object",
        })
        return errs
    for svc_key, sub_list in sub_services_in.items():
        svc = str(svc_key or "").strip().lower()
        if not svc:
            errs.append({
                "path": path_prefix,
                "message": "service key must be non-empty",
            })
            continue
        if svc not in DEFAULT_SERVICES:
            errs.append({
                "path": f"{path_prefix}.{svc}",
                "message": (
                    f"unknown service '{svc}' (allowed: "
                    f"{', '.join(sorted(DEFAULT_SERVICES))})"
                ),
            })
        if not isinstance(sub_list, list):
            errs.append({
                "path": f"{path_prefix}.{svc}",
                "message": "must be a list",
            })
            continue
        for k, ss in enumerate(sub_list):
            if not str(ss or "").strip():
                errs.append({
                    "path": f"{path_prefix}.{svc}[{k}]",
                    "message": "sub-service code must be non-empty",
                })
    return errs


def _normalize_customer_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Round-trip a customer dict through `Customer.from_dict` and back to
    a plain dict, so on-disk casing is canonical (lowercased service
    keys, lowercased region strings, stripped). Preserves the original
    `customer_id` and `name` (which `from_dict` strips but doesn't
    lowercase). The `sub_services` field is optional and round-tripped
    only when present (TODO #21)."""
    c = Customer.from_dict(d)
    out: Dict[str, Any] = {
        "customer_id": c.customer_id,
        "name": c.name,
        "subscriptions": c.subscriptions,
    }
    if c.sub_services:
        out["sub_services"] = c.sub_services
    return out


def _handle_customers_create(
    paths: Paths, body: Any
) -> Tuple[int, Dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "invalid body", "detail": "JSON object required"}

    name_raw = str(body.get("name") or "").strip()
    if not name_raw:
        return 400, {
            "error": "invalid",
            "details": [{"path": "name", "message": "required"}],
        }

    subs_in = body.get("subscriptions") or {}
    if not isinstance(subs_in, dict):
        return 400, {
            "error": "invalid",
            "details": [{"path": "subscriptions", "message": "must be an object"}],
        }

    # `sub_services` is optional (TODO #21). When omitted, the customer
    # matches every sub-service for the services they're subscribed to
    # (backward-compatible with customers.json files written before
    # sub_services existed). When present, it must be a dict of
    # {service: [sub_service_codes]}; shape is validated here on the
    # raw body (before normalization would strip empty strings).
    sub_services_in = body.get("sub_services")
    if sub_services_in is not None and not isinstance(sub_services_in, dict):
        return 400, {
            "error": "invalid",
            "details": [{"path": "sub_services", "message": "must be an object"}],
        }
    sub_services_errs = _validate_sub_services_body(sub_services_in)
    if sub_services_errs:
        return 400, {"error": "invalid", "details": sub_services_errs}

    existing = _read_customers_raw(paths)
    existing_ids = {
        str(c.get("customer_id") or "").strip()
        for c in existing
        if isinstance(c, dict)
    }
    existing_names = {
        str(c.get("name") or "").strip()
        for c in existing
        if isinstance(c, dict)
    }

    client_id_raw = str(body.get("customer_id") or "").strip()
    if client_id_raw:
        if not _ID_SEGMENT_RE.match(client_id_raw):
            return 409, {
                "error": "invalid customer_id",
                "detail": (
                    "must match [A-Za-z0-9][A-Za-z0-9_.-]*; got "
                    f"{client_id_raw!r}"
                ),
            }
        if client_id_raw in existing_ids:
            return 409, {
                "error": "customer_id already exists",
                "id": client_id_raw,
            }
        new_id = client_id_raw
    else:
        # Auto-generate from name. Slugify then ensure unique.
        new_id = unique_id(slugify(name_raw), existing_ids)
        # If the slug collides with an *existing name* (case-insensitive),
        # still allow it — the customer_id namespace is separate from
        # the name namespace. We only need to avoid id collision.

    new_entry: Dict[str, Any] = {
        "customer_id": new_id,
        "name": name_raw,
        "subscriptions": subs_in,
    }
    if sub_services_in is not None:
        new_entry["sub_services"] = sub_services_in
    new_entry = _normalize_customer_dict(new_entry)

    if new_entry["name"] in existing_names:
        return 409, {
            "error": "customer name already exists",
            "name": new_entry["name"],
        }

    merged = existing + [new_entry]
    rules = load_region_rules(paths.approved_regions_csv)
    errs = validate_customers(merged, rules=rules)
    if errs:
        return 400, {"error": "invalid", "details": _validation_details(errs)}

    write_json(paths.customers_path, merged)
    return 201, {"customers": _customer_payload(paths)}


def _handle_customers_update(
    paths: Paths, cid: str, body: Any
) -> Tuple[int, Dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "invalid body", "detail": "JSON object required"}

    name_raw = str(body.get("name") or "").strip()
    if not name_raw:
        return 400, {
            "error": "invalid",
            "details": [{"path": "name", "message": "required"}],
        }
    subs_in = body.get("subscriptions") or {}
    if not isinstance(subs_in, dict):
        return 400, {
            "error": "invalid",
            "details": [{"path": "subscriptions", "message": "must be an object"}],
        }
    # `sub_services` is optional. When omitted, the customer's existing
    # sub_services (if any) is preserved — the PUT body is a full
    # replace of (name, subscriptions, sub_services). Pass an empty
    # dict to clear the customer's sub_services entirely. (TODO #21)
    sub_services_in = body.get("sub_services")
    if sub_services_in is not None and not isinstance(sub_services_in, dict):
        return 400, {
            "error": "invalid",
            "details": [{"path": "sub_services", "message": "must be an object"}],
        }
    sub_services_errs = _validate_sub_services_body(sub_services_in)
    if sub_services_errs:
        return 400, {"error": "invalid", "details": sub_services_errs}

    existing = _read_customers_raw(paths)
    found = False
    merged: List[Dict[str, Any]] = []
    for c in existing:
        if isinstance(c, dict) and str(c.get("customer_id") or "").strip() == cid:
            updated: Dict[str, Any] = {
                "customer_id": cid,
                "name": name_raw,
                "subscriptions": subs_in,
            }
            # sub_services: explicit body value wins. If the body omits
            # the field, preserve the row's existing sub_services —
            # older editor versions that don't know about sub_services
            # shouldn't wipe them on a routine PUT.
            if sub_services_in is not None:
                updated["sub_services"] = sub_services_in
            elif "sub_services" in c and c["sub_services"]:
                updated["sub_services"] = c["sub_services"]
            merged.append(updated)
            found = True
        else:
            merged.append(c)
    if not found:
        return 404, {"error": "customer not found", "id": cid}

    # Normalize just the updated row, but also dedupe whitespace inside
    # other rows so the file stays canonical. The full-list validator
    # runs after normalization.
    merged = [
        _normalize_customer_dict(c) if isinstance(c, dict) else c
        for c in merged
    ]

    # Name uniqueness check — compare the new name against every other
    # row's name. `validate_customers` will also catch this, but we want
    # a clear "name already exists" error to surface to the UI.
    others = [c for c in merged if c.get("customer_id") != cid]
    other_names = {c.get("name") for c in others}
    target_name = next(c["name"] for c in merged if c.get("customer_id") == cid)
    if target_name in other_names:
        return 409, {
            "error": "customer name already exists",
            "name": target_name,
        }

    rules = load_region_rules(paths.approved_regions_csv)
    errs = validate_customers(merged, rules=rules)
    if errs:
        return 400, {"error": "invalid", "details": _validation_details(errs)}

    write_json(paths.customers_path, merged)
    return 200, {"customers": _customer_payload(paths)}


def _handle_customers_delete(paths: Paths, cid: str) -> Tuple[int, Dict[str, Any]]:
    existing = _read_customers_raw(paths)
    found = any(
        isinstance(c, dict) and str(c.get("customer_id") or "").strip() == cid
        for c in existing
    )
    if not found:
        return 404, {"error": "customer not found", "id": cid}
    remaining = [
        c for c in existing
        if not (isinstance(c, dict) and str(c.get("customer_id") or "").strip() == cid)
    ]
    write_json(paths.customers_path, remaining)
    return 200, {"customers": _customer_payload(paths)}


# ---------------------------------------------------------------------------
# Region write handlers
# ---------------------------------------------------------------------------


def _handle_regions_create(
    paths: Paths, body: Any
) -> Tuple[int, Dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "invalid body", "detail": "JSON object required"}

    rows = load_regions_csv(paths.approved_regions_csv)
    new_rows, errs = add_region_row(
        rows,
        body.get("service"),
        body.get("region_code"),
        body.get("display_name"),
    )
    if errs:
        # Treat the duplicate-pair error as a 409 (more semantic than 400)
        # and the rest as 400. `add_region_row` is the single source of
        # truth on this classification.
        if any("duplicate" in e.message for e in errs):
            return 409, {
                "error": "duplicate",
                "details": _validation_details(errs),
            }
        return 400, {"error": "invalid", "details": _validation_details(errs)}

    full_errs = validate_regions(new_rows)
    if full_errs:
        return 400, {"error": "invalid", "details": _validation_details(full_errs)}

    write_regions_csv(paths.approved_regions_csv, new_rows)
    return 201, {"regions": [r.to_dict() for r in new_rows]}


def _handle_regions_update(
    paths: Paths, svc: str, code: str, body: Any
) -> Tuple[int, Dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "invalid body", "detail": "JSON object required"}

    rows = load_regions_csv(paths.approved_regions_csv)
    if not any(r.service == svc and r.region_code == code for r in rows):
        return 404, {
            "error": "region not found",
            "service": svc,
            "region_code": code,
        }

    new_rows, errs = update_display_name_row(
        rows, svc, code, body.get("display_name")
    )
    if errs:
        return 400, {"error": "invalid", "details": _validation_details(errs)}

    write_regions_csv(paths.approved_regions_csv, new_rows)
    return 200, {"regions": [r.to_dict() for r in new_rows]}


def _handle_regions_delete(
    paths: Paths, svc: str, code: str, force: bool
) -> Tuple[int, Dict[str, Any]]:
    rows = load_regions_csv(paths.approved_regions_csv)
    if not any(r.service == svc and r.region_code == code for r in rows):
        return 404, {
            "error": "region not found",
            "service": svc,
            "region_code": code,
        }

    if not force:
        # Block delete when any customer's subscription references the
        # (service, region_code) pair. Without this, `lint-customers` would
        # start failing with a less helpful error and the operator wouldn't
        # know which customers to update.
        customers = _read_customers_raw(paths)
        used_by: List[str] = []
        for c in customers:
            if not isinstance(c, dict):
                continue
            subs = c.get("subscriptions") or {}
            if not isinstance(subs, dict):
                continue
            regions = subs.get(svc)
            if not isinstance(regions, list):
                continue
            if code in [str(r).strip().lower() for r in regions]:
                used_by.append(str(c.get("name") or ""))
        if used_by:
            return 409, {
                "error": "in use",
                "service": svc,
                "region_code": code,
                "customer_count": len(used_by),
                "customers": used_by,
            }

    new_rows, _errs = remove_region_row(rows, svc, code)
    write_regions_csv(paths.approved_regions_csv, new_rows)
    return 200, {"regions": [r.to_dict() for r in new_rows]}


# ---------------------------------------------------------------------------
# Path-segment parsing
# ---------------------------------------------------------------------------


def _parse_path_segment(value: str, pattern: re.Pattern[str], label: str) -> Optional[str]:
    """URL-decode a single path segment and validate it against `pattern`.

    Returns the decoded segment on success, or None on failure.
    The handler turns None into a 400 with a clear message.
    """
    decoded = unquote(value)
    if not pattern.match(decoded):
        return None
    return decoded


def _derive_session_secret(auth: Tuple[str, str]) -> bytes:
    user, pw = auth
    return hashlib.sha256(f"oa_dash_session_salt_v1:{user}:{pw}".encode("utf-8")).digest()


def _generate_session_token(user: str, secret: bytes) -> str:
    b64_user = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
    ts = int(time.time())
    payload = f"{b64_user}.{ts}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(
    token: str,
    secret: bytes,
    expected_user: str,
    max_age: int = SESSION_MAX_AGE_SECONDS,
) -> bool:
    if not token or token.count(".") != 2:
        return False
    b64_user, ts_str, sig = token.split(".")
    pad = len(b64_user) % 4
    if pad:
        b64_user_padded = b64_user + "=" * (4 - pad)
    else:
        b64_user_padded = b64_user
    try:
        user = base64.urlsafe_b64decode(b64_user_padded.encode("ascii")).decode("utf-8")
        ts = int(ts_str)
    except (ValueError, UnicodeDecodeError):
        return False

    if not hmac.compare_digest(user, expected_user):
        return False

    now = int(time.time())
    if ts > now + 300:
        return False
    if now - ts > max_age:
        return False

    expected_payload = f"{b64_user}.{ts_str}"
    expected_sig = hmac.new(secret, expected_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def _build_session_cookie(token: str, max_age: int = SESSION_MAX_AGE_SECONDS, secure: bool = False) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _build_clear_cookie() -> str:
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; "
        "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
    )


def _render_login_html(
    error: Optional[str] = None,
    username: str = "",
    next_url: str = "/dashboard",
    locked_remaining: Optional[int] = None,
    brand_title: Optional[str] = None,
) -> bytes:
    login_file = STATIC_DIR / "login.html"
    template = login_file.read_text(encoding="utf-8")

    error_html = ""
    if locked_remaining is not None and locked_remaining > 0:
        error_html = (
            f'<div class="login-alert warning" role="alert">'
            f"Too many failed attempts. Try again in {locked_remaining}s."
            f"</div>"
        )
    elif error:
        safe_err = html.escape(error)
        error_html = f'<div class="login-alert error" role="alert">{safe_err}</div>'

    safe_next = html.escape(next_url)
    safe_user = html.escape(username)
    safe_brand = html.escape(brand_title or get_brand_title())

    content = (
        template.replace("{{ERROR_HTML}}", error_html)
        .replace("{{NEXT}}", safe_next)
        .replace("{{USERNAME}}", safe_user)
        .replace("{{BRAND_TITLE}}", safe_brand)
    )
    return content.encode("utf-8")


def _render_dashboard_html(brand_title: Optional[str] = None) -> bytes:
    index = STATIC_DIR / "index.html"
    template = index.read_text(encoding="utf-8")
    safe_brand = html.escape(brand_title or get_brand_title())
    content = template.replace("{{BRAND_TITLE}}", safe_brand)
    return content.encode("utf-8")


def _safe_static_path(rel: str) -> Optional[Path]:
    if not rel:
        return None
    rel = rel.lstrip("/")
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15.0
    paths: Paths = None  # type: ignore[assignment]
    # `auth` is either None (server is open) or a (user, password) tuple.
    auth: Optional[Tuple[str, str]] = None
    # Held during a write request. Released in the request's `finally:`.
    _write_lock: Optional[FileLock] = None
    server_version = "OutageDashboard/1.0"

    # Silence default access logging in favour of our own.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        if args and any(isinstance(a, (TimeoutError, socket.timeout)) or "timed out" in str(a).lower() for a in args):
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_redirect(self, location: str, cookie: Optional[str] = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _is_secure_request(self) -> bool:
        if isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return True
        proto = self.headers.get("X-Forwarded-Proto", "").lower()
        return proto == "https"

    def _get_cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            morsel = c.get(name)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _check_session_auth(self) -> bool:
        if self.auth is None:
            return True
        cookie_val = self._get_cookie(SESSION_COOKIE_NAME)
        if not cookie_val:
            return False
        expected_user, _ = self.auth
        secret = _derive_session_secret(self.auth)
        return _verify_session_token(cookie_val, secret, expected_user)

    def _send_unauthorized(self) -> None:
        body = b"unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def _parse_basic_auth(self, header: Optional[str]) -> Optional[Tuple[str, str]]:
        """Parse an `Authorization: Basic ...` header. Returns (user, pw) or None."""
        if not header:
            return None
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "basic":
            return None
        try:
            decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        user, _, pw = decoded.partition(":")
        return user, pw

    def _is_authorized(self) -> bool:
        """Constant-time check of session cookie or Authorization header against self.auth."""
        if self.auth is None:
            return True
        if self._check_session_auth():
            return True
        expected_user, expected_pw = self.auth
        header = self.headers.get("Authorization")
        parsed = self._parse_basic_auth(header)
        if parsed is None:
            return False
        user, pw = parsed
        # compare_digest requires both args to be the same type; both are str here.
        user_ok = hmac.compare_digest(user, expected_user)
        pw_ok = hmac.compare_digest(pw, expected_pw)
        return user_ok and pw_ok

    def _get_client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        if self.client_address and len(self.client_address) > 0:
            return str(self.client_address[0])
        return "127.0.0.1"

    def _send_rate_limited(self, retry_after: int) -> None:
        body = b"too many failed authentication attempts. try again later."
        self.send_response(429)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def _enforce_auth(self, path: str) -> bool:
        """Enforces authentication and brute-force rate-limiting.
        If unauthorized or locked out, sends 401, 429, or redirects to /login and returns False.
        Returns True if authorized to proceed.
        """
        if path in PUBLIC_PATHS:
            return True
        if self.auth is None:
            return True

        client_ip = self._get_client_ip()
        is_locked, remaining = RATE_LIMITER.is_locked(client_ip)
        if is_locked:
            self._send_rate_limited(remaining)
            return False

        # 1. Valid session cookie
        if self._check_session_auth():
            return True

        # 2. HTTP Basic Auth header
        header = self.headers.get("Authorization")
        parsed = self._parse_basic_auth(header)
        expected_user, expected_pw = self.auth

        if parsed is not None:
            user, pw = parsed
            user_ok = hmac.compare_digest(user, expected_user)
            pw_ok = hmac.compare_digest(pw, expected_pw)
            if user_ok and pw_ok:
                RATE_LIMITER.record_success(client_ip)
                return True

            # Bad credentials provided -> record failure and potentially lock out
            locked, retry_after = RATE_LIMITER.record_failure(client_ip, user=user)
            if locked:
                self._send_rate_limited(retry_after)
            else:
                self._send_unauthorized()
            return False

        # 3. Neither valid cookie nor Basic Auth header was provided.
        # Browser page visits -> redirect to /login
        accept = self.headers.get("Accept", "")
        if path in ("/", "/dashboard") or "text/html" in accept:
            next_target = path if path and path != "/" else "/dashboard"
            self._send_redirect(f"/login?next={quote(next_target, safe='/')}")
            return False

        # Headless API or asset requests without auth -> HTTP 401 Basic Auth challenge
        self._send_unauthorized()
        return False

    def _check_auth(self, path: str) -> bool:
        """Returns True if the request is authorized for `path`. Public
        paths (/api/health) are always authorized."""
        if path in PUBLIC_PATHS:
            return True
        return self._is_authorized()

    def _read_json_body(self) -> Tuple[Optional[Any], Optional[Tuple[int, Dict[str, Any]]]]:
        """Read up to MAX_WRITE_BODY_BYTES from the request body and parse as JSON.

        Returns (parsed, None) on success, or (None, (status, body)) on
        any error. The handler short-circuits on the (status, body) tuple.
        """
        cl = self.headers.get("Content-Length")
        if cl is None:
            return None, (400, {"error": "Content-Length required"})
        try:
            length = int(cl)
        except (TypeError, ValueError):
            return None, (400, {"error": "Content-Length must be an integer"})
        if length < 0:
            return None, (400, {"error": "Content-Length must be non-negative"})
        if length > MAX_WRITE_BODY_BYTES:
            return None, (413, {"error": "body too large", "max_bytes": MAX_WRITE_BODY_BYTES})
        if length == 0:
            return None, (400, {"error": "empty body"})
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None, (400, {"error": "could not read request body"})
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return None, (400, {"error": "invalid JSON", "detail": str(e)})

    def _acquire_lock(self) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Try to acquire the state-dir lock. On success, stores the
        `FileLock` on `self._write_lock` for `_release_lock` to clean up.
        On contention, returns the (status, body) tuple for the handler
        to send."""
        if self._write_lock is not None:
            # Defensive: shouldn't happen because do_POST/PUT/DELETE release
            # in their `finally:`, but if it does we want to fail loud
            # rather than silently overwrite.
            return (
                500,
                {
                    "error": "internal",
                    "detail": "write lock already held by this handler",
                },
            )
        lock = FileLock(_lock_path(self.paths), timeout=10.0)
        try:
            lock.acquire()
        except RuntimeError:
            return (
                503,
                {
                    "error": "locked",
                    "detail": _lock_error_message(_lock_path(self.paths)),
                },
            )
        self._write_lock = lock
        return None

    def _release_lock(self) -> None:
        if self._write_lock is None:
            return
        try:
            self._write_lock.release()
        finally:
            self._write_lock = None

    # -- method dispatch --------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            if not self._enforce_auth(path):
                return
            if path == "/" or path == "":
                self._send_redirect("/dashboard")
                return
            if path == "/login":
                query = parse_qs(url.query)
                next_url = query.get("next", ["/dashboard"])[0]
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = "/dashboard"

                if self._check_session_auth():
                    self._send_redirect(next_url)
                    return

                client_ip = self._get_client_ip()
                is_locked, remaining = RATE_LIMITER.is_locked(client_ip)
                error = "Invalid username or password." if "invalid" in query.get("error", []) else None
                html_bytes = _render_login_html(
                    error=error,
                    next_url=next_url,
                    locked_remaining=remaining if is_locked else None,
                    brand_title=get_brand_title(getattr(self, "paths", None)),
                )
                self._send(200, html_bytes, "text/html; charset=utf-8")
                return
            if path == "/logout":
                cookie_hdr = _build_clear_cookie()
                self._send_redirect("/login", cookie=cookie_hdr)
                return
            if path == "/dashboard":
                self._send(200, _render_dashboard_html(brand_title=get_brand_title(getattr(self, "paths", None))), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self._send_json(200, {"status": "ok", "time": _utcnow_iso()})
                return
            if path == "/favicon.ico":
                ico_path = _safe_static_path("favicon.ico")
                if ico_path and ico_path.is_file():
                    self._send(200, ico_path.read_bytes(), "image/x-icon")
                else:
                    self._send(204, b"", "image/x-icon")
                return
            if path == "/api/dashboard":
                payload = build_dashboard_payload(self.paths)
                self._send_json(200, payload)
                return
            if path == "/api/customers":
                self._send_json(200, _customer_payload(self.paths))
                return
            if path == "/api/regions":
                self._send_json(200, {"regions": _regions_payload(self.paths)})
                return
            if path == "/api/runs":
                status, body = _handle_runs_list(self.paths, parse_qs(url.query))
                self._send_json(status, body)
                return
            if path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/"):]
                if not run_id:
                    self._send_json(400, {"error": "run_id required"})
                    return
                status, body = _handle_runs_detail(self.paths, run_id)
                self._send_json(status, body)
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = _safe_static_path(rel)
                if target is None:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                ctype, _enc = mimetypes.guess_type(str(target))
                ctype = ctype or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except BrokenPipeError:
            # Client gave up; nothing to do.
            pass
        except Exception as e:  # pragma: no cover - defensive
            self._send_json(500, {"error": "internal", "detail": repr(e)})

    def _handle_login_post(self, url: Any) -> None:
        client_ip = self._get_client_ip()
        is_locked, remaining = RATE_LIMITER.is_locked(client_ip)
        if is_locked:
            body = _render_login_html(
                locked_remaining=remaining,
                brand_title=get_brand_title(getattr(self, "paths", None)),
            )
            self._send(429, body, "text/html; charset=utf-8")
            return

        cl = self.headers.get("Content-Length")
        if cl is None:
            self._send_json(400, {"error": "Content-Length required"})
            return
        try:
            length = int(cl)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "Content-Length must be an integer"})
            return
        if length < 0:
            self._send_json(400, {"error": "Content-Length must be non-negative"})
            return
        if length > MAX_WRITE_BODY_BYTES:
            self._send_json(413, {"error": "body too large", "max_bytes": MAX_WRITE_BODY_BYTES})
            return

        try:
            raw = self.rfile.read(length)
        except OSError:
            self._send_json(400, {"error": "could not read request body"})
            return

        content_type = self.headers.get("Content-Type", "")
        username = ""
        password = ""
        next_url = "/dashboard"

        if "application/json" in content_type:
            try:
                json_data = json.loads(raw.decode("utf-8"))
                if isinstance(json_data, dict):
                    username = str(json_data.get("username", ""))
                    password = str(json_data.get("password", ""))
                    next_url = str(json_data.get("next", "/dashboard"))
            except Exception:
                pass
        else:
            try:
                body_str = raw.decode("utf-8")
                form_data = parse_qs(body_str)
                username = form_data.get("username", [""])[0]
                password = form_data.get("password", [""])[0]
                next_url = form_data.get("next", ["/dashboard"])[0]
            except Exception:
                pass

        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/dashboard"

        if self.auth is None:
            self._send_redirect(next_url)
            return

        expected_user, expected_pw = self.auth
        user_ok = hmac.compare_digest(username, expected_user)
        pw_ok = hmac.compare_digest(password, expected_pw)

        is_json = "application/json" in content_type and "application/json" in self.headers.get("Accept", "")

        if user_ok and pw_ok:
            RATE_LIMITER.record_success(client_ip)
            secret = _derive_session_secret(self.auth)
            token = _generate_session_token(expected_user, secret)
            secure = self._is_secure_request()
            cookie_hdr = _build_session_cookie(token, secure=secure)
            if is_json:
                body = json.dumps({"status": "ok", "next": next_url}).encode("utf-8")
                self.send_response(200)
                self.send_header("Set-Cookie", cookie_hdr)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_redirect(next_url, cookie=cookie_hdr)
            return

        # Bad credentials provided
        locked, retry_after = RATE_LIMITER.record_failure(client_ip, user=username)
        if is_json:
            status = 429 if locked else 401
            self._send_json(status, {
                "error": "invalid_credentials",
                "locked": locked,
                "retry_after": retry_after,
            })
            return

        if locked:
            body = _render_login_html(
                locked_remaining=retry_after,
                next_url=next_url,
                username=username,
                brand_title=get_brand_title(getattr(self, "paths", None)),
            )
            self._send(429, body, "text/html; charset=utf-8")
        else:
            body = _render_login_html(
                error="Invalid username or password.",
                next_url=next_url,
                username=username,
                brand_title=get_brand_title(getattr(self, "paths", None)),
            )
            self._send(401, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            if not self._enforce_auth(path):
                return
            if path == "/logout" or path == "/api/logout":
                cookie_hdr = _build_clear_cookie()
                if "application/json" in self.headers.get("Accept", ""):
                    body = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Set-Cookie", cookie_hdr)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_redirect("/login", cookie=cookie_hdr)
                return
            if path == "/login":
                self._handle_login_post(url)
                return
            body, err = self._read_json_body()
            if err is not None:
                self._send_json(*err)
                return
            err = self._acquire_lock()
            if err is not None:
                self._send_json(*err)
                return
            try:
                if path == "/api/customers":
                    status, resp = _handle_customers_create(self.paths, body)
                elif path == "/api/regions":
                    status, resp = _handle_regions_create(self.paths, body)
                else:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(status, resp)
            finally:
                self._release_lock()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            self._send_json(500, {"error": "internal", "detail": repr(e)})

    def do_PUT(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            if not self._enforce_auth(path):
                return
            body, err = self._read_json_body()
            if err is not None:
                self._send_json(*err)
                return
            err = self._acquire_lock()
            if err is not None:
                self._send_json(*err)
                return
            try:
                if path.startswith("/api/customers/"):
                    raw_id = path[len("/api/customers/"):]
                    cid = _parse_path_segment(raw_id, _ID_SEGMENT_RE, "customer_id")
                    if cid is None or not raw_id:
                        self._send_json(400, {"error": "invalid customer_id in path"})
                        return
                    status, resp = _handle_customers_update(self.paths, cid, body)
                    self._send_json(status, resp)
                    return
                if path.startswith("/api/regions/"):
                    rest = path[len("/api/regions/"):]
                    parts = rest.split("/")
                    if len(parts) != 2:
                        self._send_json(400, {"error": "expected /api/regions/<service>/<region_code>"})
                        return
                    svc = _parse_path_segment(parts[0], _ID_SEGMENT_RE, "service")
                    code = _parse_path_segment(parts[1], _REGION_SEGMENT_RE, "region_code")
                    if svc is None or code is None:
                        self._send_json(400, {"error": "invalid service/region_code in path"})
                        return
                    # region_code "global" passes the regex check above
                    # (the regex allows [a-z0-9][a-z0-9-]*, which matches
                    # the literal "global"). No special-case needed.
                    status, resp = _handle_regions_update(self.paths, svc, code, body)
                    self._send_json(status, resp)
                    return
                self._send_json(404, {"error": "not found"})
            finally:
                self._release_lock()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            self._send_json(500, {"error": "internal", "detail": repr(e)})

    def do_DELETE(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            if not self._enforce_auth(path):
                return
            err = self._acquire_lock()
            if err is not None:
                self._send_json(*err)
                return
            try:
                if path.startswith("/api/customers/"):
                    raw_id = path[len("/api/customers/"):]
                    cid = _parse_path_segment(raw_id, _ID_SEGMENT_RE, "customer_id")
                    if cid is None or not raw_id:
                        self._send_json(400, {"error": "invalid customer_id in path"})
                        return
                    status, resp = _handle_customers_delete(self.paths, cid)
                    self._send_json(status, resp)
                    return
                if path.startswith("/api/regions/"):
                    rest = path[len("/api/regions/"):]
                    parts = rest.split("/")
                    if len(parts) != 2:
                        self._send_json(400, {"error": "expected /api/regions/<service>/<region_code>"})
                        return
                    svc = _parse_path_segment(parts[0], _ID_SEGMENT_RE, "service")
                    code = _parse_path_segment(parts[1], _REGION_SEGMENT_RE, "region_code")
                    if svc is None or code is None:
                        self._send_json(400, {"error": "invalid service/region_code in path"})
                        return
                    qs = parse_qs(url.query)
                    force = (qs.get("force", ["false"])[0] or "").lower() in ("1", "true", "yes")
                    status, resp = _handle_regions_delete(self.paths, svc, code, force)
                    self._send_json(status, resp)
                    return
                self._send_json(404, {"error": "not found"})
            finally:
                self._release_lock()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            self._send_json(500, {"error": "internal", "detail": repr(e)})


def make_handler(paths: Paths, auth: Optional[Tuple[str, str]] = None):
    """Bind `paths` and optional `auth` into a handler class.

    `auth=None` leaves the server open (only safe for loopback binds).
    `auth=(user, password)` enables HTTP Basic Auth on every endpoint
    except those in `PUBLIC_PATHS`.
    """
    class _Bound(DashboardHandler):
        pass
    _Bound.paths = paths  # type: ignore[assignment]
    _Bound.auth = auth
    return _Bound


def ensure_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    host: str = "127.0.0.1",
    days: int = 3650,
) -> None:
    """Generate a self-signed SAN TLS certificate and private key if not already present.

    Uses `openssl req` to produce an RSA 2048-bit key and X.509 certificate
    with Subject Alternative Names for localhost, 127.0.0.1, and the target host/IP.
    """
    if cert_path.exists() and key_path.exists():
        return

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    if host and host not in ("127.0.0.1", "localhost", "0.0.0.0", "::", "::1"):
        try:
            import ipaddress
            ipaddress.ip_address(host)
            san_entries.append(f"IP:{host}")
        except ValueError:
            san_entries.append(f"DNS:{host}")

    san_str = ",".join(san_entries)
    subj = f"/CN={host or '127.0.0.1'}"

    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-nodes",
        "-keyout", str(key_path),
        "-out", str(cert_path),
        "-days", str(days),
        "-subj", subj,
        "-addext", f"subjectAltName={san_str}",
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "openssl binary not found in PATH; cannot auto-generate self-signed certificate. "
            "Please provide existing certificate and key paths."
        )
    except subprocess.CalledProcessError:
        cmd_fallback = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-nodes",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", str(days),
            "-subj", subj,
        ]
        try:
            subprocess.run(cmd_fallback, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as fallback_err:
            raise RuntimeError(
                f"failed to generate self-signed certificate with openssl: "
                f"{fallback_err.stderr.decode('utf-8', errors='replace')}"
            )

    try:
        key_path.chmod(0o600)
    except OSError:
        pass


class HTTPRedirectHandler(BaseHTTPRequestHandler):
    """HTTP redirector that returns 301 to the HTTPS dashboard."""

    target_port: int = 8765

    def do_GET(self) -> None:
        self._redirect()

    def do_HEAD(self) -> None:
        self._redirect()

    def do_POST(self) -> None:
        self._redirect()

    def _redirect(self) -> None:
        host_header = self.headers.get("Host", "127.0.0.1").split(":")[0]
        target_host = f"{host_header}:{self.target_port}" if self.target_port != 443 else host_header
        location = f"https://{target_host}{self.path}"
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class InsecureBindError(RuntimeError):
    """Raised when the dashboard would be exposed without authentication."""


class DashboardServer(ThreadingHTTPServer):
    """Threading HTTPServer with non-blocking per-connection TLS handshakes.

    TLS handshakes are performed in individual worker threads rather than on the
    main listener thread. This prevents slow or stalling clients (e.g. Windows
    Schannel attempting online CRL / Windows Update root certificate validation)
    from blocking the main accept() loop and starving other connections.
    """

    ssl_context: Optional[ssl.SSLContext] = None
    handshake_timeout: float = 15.0

    def finish_request(self, request: Any, client_address: Any) -> None:
        if self.ssl_context is not None:
            try:
                request.settimeout(self.handshake_timeout)
                ssl_request = self.ssl_context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError, TimeoutError):
                return
            try:
                super().finish_request(ssl_request, client_address)
            finally:
                try:
                    ssl_request.close()
                except Exception:
                    pass
        else:
            super().finish_request(request, client_address)


def run_server(
    paths: Paths,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auth: Optional[Tuple[str, str]] = None,
    allow_insecure_bind: bool = False,
    ssl_enabled: bool = False,
    ssl_cert: Optional[str] = None,
    ssl_key: Optional[str] = None,
    redirect_port: Optional[int] = None,
    block: bool = True,
) -> ThreadingHTTPServer:
    """Start the dashboard HTTP server.

    Parameters
    ----------
    paths:
        State directory paths.
    host, port:
        Bind address. Default is loopback (`127.0.0.1:8765`).
    auth:
        `(user, password)` tuple to enable HTTP Basic Auth, or `None` to
        leave the server open. When set, every endpoint except
        `/api/health` requires the credentials.
    allow_insecure_bind:
        If False (default), refuses to bind to a non-loopback host when
        `auth` is None. Set True to override (e.g. for local LAN
        deployments behind another auth layer).
    ssl_enabled:
        If True, serve over HTTPS with TLS encryption. If cert/key paths
        are not provided, auto-generates a self-signed SAN certificate.
    ssl_cert, ssl_key:
        Optional paths to TLS certificate and private key files.
    redirect_port:
        Optional HTTP port to listen on and return 301 redirects to HTTPS.
    block:
        If False, start the server on a background thread (useful for
        tests) and return immediately. The returned server can be
        `shutdown()`-ed by the caller.

    Returns the `ThreadingHTTPServer` instance.
    """
    if not STATIC_DIR.exists():
        raise RuntimeError(f"dashboard static assets missing: {STATIC_DIR}")

    if auth is None and host not in LOOPBACK_HOSTS and not allow_insecure_bind:
        raise InsecureBindError(
            f"refusing to bind dashboard to {host}:{port} without auth. "
            f"Loopback hosts ({', '.join(sorted(LOOPBACK_HOSTS))}) are "
            f"allowed unauthenticated; for any other host, configure "
            f"auth (--user/--password on `serve`, or "
            f"OA_DASHBOARD_USER/OA_DASHBOARD_PASSWORD env vars). "
            f"Pass allow_insecure_bind=True to override."
        )

    server = DashboardServer((host, port), make_handler(paths, auth=auth))
    server.daemon_threads = True

    redirect_server: Optional[ThreadingHTTPServer] = None
    if ssl_enabled or ssl_cert or ssl_key:
        if ssl_cert and not ssl_key:
            raise RuntimeError("ssl_cert was provided without ssl_key")
        if ssl_key and not ssl_cert:
            raise RuntimeError("ssl_key was provided without ssl_cert")

        if not ssl_cert and not ssl_key:
            etc_dir = Path("/etc/outage-automation")
            if etc_dir.is_dir() and os.access(etc_dir, os.W_OK):
                default_cert = etc_dir / "dashboard.crt"
                default_key = etc_dir / "dashboard.key"
            else:
                default_ssl_dir = paths.state_dir / "ssl"
                default_cert = default_ssl_dir / "dashboard.crt"
                default_key = default_ssl_dir / "dashboard.key"

            ensure_self_signed_cert(default_cert, default_key, host=host)
            cert_path = str(default_cert)
            key_path = str(default_key)
        else:
            cert_path = str(ssl_cert)
            key_path = str(ssl_key)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server.ssl_context = ctx
        proto = "https"
        ssl_status = f"ssl=on ({cert_path})"
    else:
        proto = "http"
        ssl_status = "ssl=off"

    if redirect_port:
        class _BoundRedirectHandler(HTTPRedirectHandler):
            pass
        _BoundRedirectHandler.target_port = port
        try:
            redirect_server = DashboardServer((host, redirect_port), _BoundRedirectHandler)
            redirect_server.daemon_threads = True
            t_red = threading.Thread(target=redirect_server.serve_forever, name="dashboard-redirect", daemon=True)
            t_red.start()
            sys.stderr.write(
                f"[dashboard] HTTP redirector listening on http://{host}:{redirect_port} -> https://{host}:{port}\n"
            )
        except Exception as e:
            sys.stderr.write(
                f"[dashboard] warning: failed to start HTTP redirector on {host}:{redirect_port}: {e}\n"
            )

    setattr(server, "_redirect_server", redirect_server)
    orig_server_close = server.server_close
    def _close_all() -> None:
        if redirect_server:
            try:
                redirect_server.shutdown()
                redirect_server.server_close()
            except Exception:
                pass
        orig_server_close()
    server.server_close = _close_all  # type: ignore[method-assign]

    auth_status = "auth=on" if auth else "auth=off"
    sys.stderr.write(
        f"[dashboard] listening on {proto}://{host}:{port} "
        f"({auth_status}, {ssl_status}, state_dir={paths.state_dir})\n"
    )
    if not block:
        t = threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True)
        t.start()
        return server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[dashboard] shutting down\n")
    finally:
        server.server_close()
    return server
