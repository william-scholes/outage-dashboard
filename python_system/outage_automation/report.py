"""Run report generation.

A run report summarizes what happened during a `run-all` cycle:
  - per-provider success/fail
  - event counts (incoming, new, updated, resolved)
  - top impacted customers
  - run duration
  - host + python version

Reports are written as JSON to `state/reports/<run_id>.json`. The most
recent report is also exposed via the dashboard API at
`/api/dashboard` payload (under the `last_run` key, when present).
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# Subdirectory under state_dir where per-run report JSON files are kept.
REPORTS_DIRNAME = "reports"

# The run_id format produced by `new_run_id()`. Used to validate user-supplied
# run_ids before interpolating them into a path (defends against path traversal
# in the dashboard's /api/runs/<run_id> endpoint).
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return _utcnow_iso().replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:8]


def make_report(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    providers: List[Dict[str, Any]],
    history: Dict[str, int],
    impact: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured report dict."""
    duration_ms = 0
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        duration_ms = int((f - s).total_seconds() * 1000)
    except Exception:
        pass
    return {
        "run_id": run_id,
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "pid": os.getpid(),
        },
        "providers": providers,
        "history": history,
        "impact_summary": _summarize_impact(impact) if impact else None,
        "extra": extra or {},
    }


def _summarize_impact(impact: Dict[str, Any]) -> Dict[str, Any]:
    by_customer = (impact or {}).get("by_customer") or {}
    ranked = sorted(
        by_customer.items(),
        key=lambda kv: (-int(kv[1].get("total", 0)), kv[0]),
    )
    return {
        "customer_count": len(by_customer),
        "outage_count": len((impact or {}).get("by_outage") or {}),
        "top_customers": [
            {"name": n, "total": int(v.get("total", 0))} for n, v in ranked[:5]
        ],
    }


def write_report(state_dir: Path, report: Dict[str, Any]) -> Path:
    """Persist the report to `state/reports/<run_id>.json` atomically."""
    reports_dir = state_dir / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report['run_id']}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_latest_report(state_dir: Path) -> Optional[Dict[str, Any]]:
    reports_dir = state_dir / REPORTS_DIRNAME
    if not reports_dir.is_dir():
        return None
    files = sorted(reports_dir.glob("*.json"), key=lambda p: p.name)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _summarise_run(report: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a small metadata dict from a full report for list views."""
    providers = report.get("providers") or []
    providers_ok = sum(1 for p in providers if p.get("ok"))
    error_count = len(providers) - providers_ok
    event_count = sum(int(p.get("event_count", 0) or 0) for p in providers)
    return {
        "run_id": report.get("run_id", ""),
        "started_at": report.get("started_at", ""),
        "finished_at": report.get("finished_at", ""),
        "duration_ms": int(report.get("duration_ms", 0) or 0),
        "ok": (error_count == 0 and len(providers) > 0),
        "error_count": error_count,
        "providers_ok": providers_ok,
        "provider_count": len(providers),
        "event_count": event_count,
    }


def list_reports(state_dir: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Return metadata for the most recent `limit` reports (newest first).

    Files that fail to parse are silently skipped — a corrupt single report
    should not break the entire runs list. Returns [] if the reports dir
    is missing or empty.
    """
    if limit < 1:
        limit = 1
    reports_dir = state_dir / REPORTS_DIRNAME
    if not reports_dir.is_dir():
        return []
    # Filenames are `YYYYMMDDTHHMMSSZ-<8hex>.json` (lexical == chronological).
    files = sorted(reports_dir.glob("*.json"), key=lambda p: p.name, reverse=True)
    out: List[Dict[str, Any]] = []
    for path in files[:limit]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(_summarise_run(report))
    return out


def read_report(state_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """Load a single report by run_id. Returns None if the id is malformed
    or the file is missing / unreadable. Run_id is validated against
    RUN_ID_RE to prevent path traversal.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return None
    path = state_dir / REPORTS_DIRNAME / f"{run_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
