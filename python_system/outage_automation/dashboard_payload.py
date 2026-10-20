"""Shared dashboard payload builder.

Used by both the `dashboard-data` CLI command and the dashboard HTTP server
(`/api/dashboard`) so the filter/impact logic stays in one place.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from .config import get_brand_title
from .model import Customer
from .paths import Paths
from .processor import compute_customer_impact, filter_active_window
from .report import read_latest_report
from .store import read_json

DEFAULT_TTL_DAYS = 7


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ttl_days() -> int:
    try:
        return int(os.environ.get("OA_TTL_DAYS") or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


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


def build_dashboard_payload(paths: Paths, *, include_last_run: bool = False) -> Dict[str, Any]:
    """Build the dashboard JSON payload.

    - Reads `alert_history.json` and `customers.json`.
    - Filters to the active window.
    - Computes customer matching on-the-fly.

    Args:
        paths: Resolved Paths for the state directory.
        include_last_run: If True, include the `last_run` report reference.
            Used by the HTTP server (which wants a live "last run" indicator)
            but not by the CLI (which is typically called by offline tooling).
    """
    rows_raw = read_json(paths.alert_history_path, [])
    customers = _load_customers(paths)
    rows: List[Dict[str, Any]] = rows_raw if isinstance(rows_raw, list) else []
    ttl = _ttl_days()
    visible = filter_active_window(rows, ttl_days=ttl)
    impact = compute_customer_impact(visible, customers)
    payload: Dict[str, Any] = {
        "brand_title": get_brand_title(paths),
        "rows": visible,
        "impact": impact,
        "ttl_days": ttl,
        "generated_at": _utcnow_iso(),
        "row_counts": {
            "stored": len(rows),
            "visible": len(visible),
        },
    }
    if include_last_run:
        payload["last_run"] = read_latest_report(paths.state_dir)
    return payload
