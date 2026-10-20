"""Status.io adapter (used by ConnectWise).

ConnectWise's status page is hosted on Status.io (not Atlassian
Statuspage). The endpoint shape is:

    {
      "result": {
        "incidents": [
          {
            "_id": "...",
            "name": "Support Case Submissions Currently Unavailable",
            "datetime_open": "2026-03-03T08:54:00.000Z",
            "datetime_close": "...",          // optional
            "current_active": true,
            "messages": [
              {
                "details": "...",
                "state": 100|300|400|500,     // impact level
                "status": 100|200|300|400,    // phase (400=Resolved)
                "datetime": "..."
              }
            ],
            "containers_affected": [{"_id": "...", "name": "APAC"}, ...],
            "components_affected": [{"_id": "...", "name": "x360Cloud"}, ...]
          }
        ],
        "status": [...],
        "status_overall": {...},
        "maintenance": {...}
      }
    }

State codes (impact level):
  100 = Operational, 300 = Degraded, 400 = Partial Outage, 500 = Major Outage
Status codes (phase):
  100 = Investigating, 200 = Identified, 300 = Monitoring, 400 = Resolved
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..model import OutageEvent
from ..validate import parse_datetime
from .base import (
    IngestError,
    IngestResult,
    ProviderAdapter,
    fetch_url,
    maybe_capture_raw,
    parse_json_bytes,
)


# Status.io state code → our severity
_SEVERITY_FROM_STATE = {
    100: "info",      # Operational
    200: "info",      # (not commonly used as impact)
    300: "warning",   # Degraded
    400: "warning",   # Partial Outage
    500: "critical",  # Major Outage
}

# Status.io status code → phase label (for description only)
_PHASE_LABEL = {
    100: "Investigating",
    200: "Identified",
    300: "Monitoring",
    400: "Resolved",
}


def _classify_severity(state: int) -> str:
    return _SEVERITY_FROM_STATE.get(int(state or 0), "info")


def _is_resolved(incident: Dict[str, Any], latest_message: Dict[str, Any]) -> bool:
    """An incident is resolved if any of these hold:
      - `current_active` is false
      - `datetime_close` is set
      - the latest message has `status: 400` (Status.io's "Resolved" phase)
      - the latest message details indicate resolution (e.g. "fully resolved")
    """
    if not incident.get("current_active", True):
        return True
    if incident.get("datetime_close"):
        return True
    if int(latest_message.get("status") or 0) == 400:
        return True

    # Check for resolution keywords in the message details to handle cases
    # where Status.io API records are stale but explicitly state resolution.
    details = str(latest_message.get("details") or "").lower()
    resolution_phrases = (
        "fully resolved",
        "has been resolved",
        "is now resolved",
        "was resolved",
        "resolved.",
        "operating as expected",
        "operating normally",
        "returned to normal",
    )
    if any(phrase in details for phrase in resolution_phrases):
        # Avoid false positives from ongoing update phrasing (e.g. "working to get this resolved")
        false_positives = ("working to get", "expected to be", "striving to get", "hope to have")
        if not any(fp in details for fp in false_positives):
            return True

    return False


def _regions_for_incident(incident: Dict[str, Any]) -> List[str]:
    """Extract affected regions from `containers_affected`.

    Falls back to `['global']` if no containers are listed, since
    ConnectWise is fundamentally a SaaS that's regional only in the
    sense of "which datacenters are affected".
    """
    containers = incident.get("containers_affected") or []
    if not containers:
        return ["global"]
    out: List[str] = []
    seen = set()
    for c in containers:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out or ["global"]


def _affected_products(incident: Dict[str, Any]) -> List[str]:
    comps = incident.get("components_affected") or []
    return [str(c.get("name") or "").strip() for c in comps if isinstance(c, dict) and c.get("name")]


def _pick_description(incident: Dict[str, Any], latest_message: Dict[str, Any]) -> str:
    body = str(latest_message.get("details") or "").strip()
    if body:
        return body
    return str(incident.get("name") or "")


def normalize_status_io_incident(
    incident: Dict[str, Any],
    service: str,
    *,
    default_link_template: str = "https://status.{service}.com/pages/incident/{id}",
) -> Optional[OutageEvent]:
    raw_id = str(incident.get("_id") or "").strip()
    if not raw_id:
        return None

    messages = incident.get("messages") or []
    if messages and isinstance(messages[-1], dict):
        latest = messages[-1]
    else:
        latest = {}

    state = int(latest.get("state") or 0)
    severity = _classify_severity(state)
    if _is_resolved(incident, latest):
        severity = "resolved"

    title = str(incident.get("name") or "").strip()
    description = _pick_description(incident, latest)[:500]
    regions = _regions_for_incident(incident)

    published_at = parse_datetime(incident.get("datetime_open"))
    if published_at is None:
        return None

    link = default_link_template.format(service=service, id=raw_id)
    products = _affected_products(incident)
    if products:
        title_with_products = f"{title} [{', '.join(products)}]" if title else ", ".join(products)
    else:
        title_with_products = title

    return OutageEvent(
        raw_id=raw_id,
        service=service,
        title=title_with_products or "(untitled incident)",
        description=description,
        severity=severity,
        regions=regions,
        sub_services=[],
        link=link,
        published_at=published_at,
    )


def parse_status_io_incidents(
    payload: Any,
    service: str,
    **kwargs,
) -> List[OutageEvent]:
    """Parse a Status.io response payload into `OutageEvent` objects.

    Accepts both shapes:
      - `{"result": {"incidents": [...]}}` (Status.io wrapped)
      - `{"incidents": [...]}` (unwrapped, for fixtures/tests)
    """
    incidents_raw: List[Any]
    if isinstance(payload, dict):
        if isinstance(payload.get("result"), dict):
            incidents_raw = payload["result"].get("incidents") or []
        else:
            incidents_raw = payload.get("incidents") or []
    else:
        return []

    if not isinstance(incidents_raw, list):
        return []
    out: List[OutageEvent] = []
    for it in incidents_raw:
        if not isinstance(it, dict):
            continue
        ev = normalize_status_io_incident(it, service, **kwargs)
        if ev is not None:
            out.append(ev)
    return out


class StatusIoAdapter(ProviderAdapter):
    """Base class for any provider exposing the Status.io API.

    Subclasses set `name`, `default_url`, and `url_env_var`.
    """

    name: str = ""
    default_url: str = ""
    url_env_var: str = ""

    def __init__(self, url: Optional[str] = None) -> None:
        env_url = (
            os.environ.get(self.url_env_var) if self.url_env_var else None
        )
        self.url = url or env_url or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = fetch_url(self.url)
        except IngestError as e:
            result.error = str(e)
            return result
        result.raw_bytes = len(data)
        try:
            payload = parse_json_bytes(data)
        except IngestError as e:
            result.error = str(e)
            return result
        if state_dir is not None:
            cap = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
            if cap:
                result.raw_payload = cap
        result.events = self.parse(payload)
        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        return parse_status_io_incidents(payload, self.name)
