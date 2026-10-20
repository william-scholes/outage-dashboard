from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .match import match_customers
from .model import AlertHistoryRow, Customer, OutageEvent, iso_utc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_alert_history_rows(
    events: List[OutageEvent],
    existing_by_raw_id: Dict[str, Dict[str, Any]],
    ttl_days: int = 7,
    now: Optional[datetime] = None,
) -> List[AlertHistoryRow]:
    """Build (or refresh) alert-history rows from incoming events.

    - Sets `published_at` to the event's published time.
    - Computes `expires_at = published_at + ttl_days`.
    - Preserves `last_seen_at` from the existing row (always bumping forward
      to `now` if needed), so we can detect "still active" outages on the
      next run.
    """
    now = now or _utcnow()
    ttl = timedelta(days=ttl_days)
    rows: List[AlertHistoryRow] = []
    for ev in events:
        if not ev.raw_id:
            continue
        published_at = ev.published_at.astimezone(timezone.utc)
        expires_at = published_at + ttl
        prev = existing_by_raw_id.get(ev.raw_id) or {}
        prev_last_seen_raw = prev.get("last_seen_at")
        last_seen = now
        if isinstance(prev_last_seen_raw, str) and prev_last_seen_raw:
            try:
                ts = prev_last_seen_raw.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                last_seen = parsed.astimezone(timezone.utc)
            except Exception:
                last_seen = now
        if last_seen < now:
            last_seen = now
        rows.append(
            AlertHistoryRow(
                raw_id=ev.raw_id,
                service=ev.service,
                severity=ev.severity,
                title=ev.title,
                description=ev.description[:500],
                regions=list(ev.regions or []),
                sub_services=list(ev.sub_services or []),
                link=ev.link,
                published_at=published_at,
                last_seen_at=last_seen,
                expires_at=expires_at,
            )
        )
    return rows


def load_alert_history_rows(raw: Any) -> List[AlertHistoryRow]:
    """Parse stored alert_history.json content into AlertHistoryRow objects.

    Skips any rows that fail to parse (does not raise).
    """
    rows: List[AlertHistoryRow] = []
    if not isinstance(raw, list):
        return rows
    for d in raw:
        if not isinstance(d, dict):
            continue
        row = AlertHistoryRow.from_dict(d)
        if row is not None:
            rows.append(row)
    return rows


def filter_active_window(
    rows: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    ttl_days: int = 7,
) -> List[Dict[str, Any]]:
    """Drop rows we haven't seen in the last `ttl_days` from the dashboard feed.

    "Age" is measured from `last_seen_at` (the most recent time we observed
    the event in its source feed, bumped to "now" on every re-ingest by
    `build_alert_history_rows`), falling back to `published_at` when
    `last_seen_at` is missing or unparseable. Rows that haven't been seen
    within the window are assumed to be no longer active and dropped — so
    an outage whose source feed stops listing it after `ttl_days` falls
    off the dashboard, even if its `published_at` is still inside the
    window, and an outage whose feed still lists it remains visible even
    if its `published_at` is older than `ttl_days`.
    """
    now = now or _utcnow()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not r or not r.get("raw_id"):
            continue
        ref_raw = r.get("last_seen_at") or r.get("published_at")
        ref_dt: Optional[datetime] = None
        if isinstance(ref_raw, str) and ref_raw:
            try:
                ts = ref_raw.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                ref_dt = parsed.astimezone(timezone.utc)
            except Exception:
                ref_dt = None
        if ref_dt is not None and (now - ref_dt) > timedelta(days=ttl_days):
            continue
        out.append(r)
    return out


def compute_customer_impact(
    rows: List[Dict[str, Any]],
    customers: List[Customer],
) -> Dict[str, Any]:
    """Compute customer impact at request time.

    Returns a dict:
      {
        "by_customer": { customer_name: { "total": int, "by_service": { svc: count, ... } } },
        "by_outage":  { raw_id: [ customer_name, ... ] },
      }
    """
    impact: Dict[str, Any] = {
        "by_customer": {},
        "by_outage": {},
    }
    events: List[OutageEvent] = []
    for r in rows:
        try:
            events.append(OutageEvent.from_dict(r))
        except Exception:
            continue

    for ev in events:
        matches = match_customers(ev, customers)
        names = [c.name for (c, _) in matches]
        impact["by_outage"][ev.raw_id] = names
        for c, _regions in matches:
            entry = impact["by_customer"].setdefault(
                c.name,
                {
                    "customer_id": c.customer_id,
                    "total": 0,
                    "by_service": {},
                    "by_severity": {},
                },
            )
            entry["customer_id"] = entry.get("customer_id") or c.customer_id
            entry["total"] = entry.get("total", 0) + 1
            entry["by_service"][ev.service] = entry["by_service"].get(ev.service, 0) + 1
            entry["by_severity"][ev.severity] = entry["by_severity"].get(ev.severity, 0) + 1

    return impact

