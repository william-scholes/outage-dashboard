"""REWST Ingestion Adapter.

Source: https://status-au.rewst.io and https://engine.rewst.io
Provides coverage for Rewst Robotic Process Automation (RPA), Workflow Engine,
and App Builder for MSPs.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import (
    IngestError,
    IngestResult,
    ProviderAdapter,
    fetch_url,
    maybe_capture_raw,
    parse_json_bytes,
)
from ..model import OutageEvent
from ..validate import parse_datetime

SERVICE = "rewst"
DEFAULT_URL = "https://status-au.rewst.io"
URL_ENV_VAR = "OA_REWST_URL"
API_KEY_ENV_VAR = "OA_REWST_API_KEY"

_SEVERITY_MAP = {
    "unavailable": "critical",
    "degraded": "warning",
    "investigating": "warning",
    "identified": "warning",
    "monitoring": "warning",
    "operational": "resolved",
    "resolved": "resolved",
    "available": "resolved",
}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


class RewstAdapter(ProviderAdapter):
    """Adapter for ingesting Rewst platform status and workflow health."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.environ.get(self.url_env_var) or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {"User-Agent": "Mozilla/5.0 OutageAutomation/2.0"}
        api_key = os.environ.get(API_KEY_ENV_VAR)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            data = fetch_url(self.url, headers=headers)
        except IngestError as e:
            result.error = str(e)
            return result

        result.raw_bytes = len(data)

        # Check if response is HTML with Next.js embedded data
        payload: Any = None
        decoded = data.decode("utf-8", errors="replace")
        match = _NEXT_DATA_RE.search(decoded)
        if match:
            try:
                payload = json.loads(match.group(1))
            except Exception as e:
                result.error = f"failed to parse Next.js payload: {e}"
                return result
        else:
            try:
                payload = parse_json_bytes(data)
            except Exception as e:
                result.error = f"invalid JSON: {e}"
                return result

        if state_dir is not None:
            cap = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
            if cap:
                result.raw_payload = cap

        result.events = self.parse(payload)
        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        # Handle Next.js Cronitor structure
        if isinstance(payload, dict) and "props" in payload:
            data = payload.get("props", {}).get("pageProps", {}).get("data", {})
            incidents = data.get("incidents", [])
            events: List[OutageEvent] = []
            for inc in incidents:
                if not isinstance(inc, dict):
                    continue
                raw_id = str(inc.get("id") or "")
                title = str(inc.get("name") or inc.get("title") or "Rewst Incident")
                state = str(inc.get("state") or inc.get("status") or "").lower()
                severity = _SEVERITY_MAP.get(state, "warning")

                desc = ""
                updates = inc.get("updates") or []
                if updates and isinstance(updates[0], dict):
                    desc = str(updates[0].get("body") or "")
                if not desc:
                    desc = title

                pub_raw = inc.get("created_at") or inc.get("created")
                published_at = parse_datetime(pub_raw) or datetime.now(timezone.utc)

                sub_services = []
                for comp in inc.get("components") or []:
                    cname = str(comp if isinstance(comp, str) else comp.get("name") or "").lower()
                    if "workflow" in cname:
                        sub_services.append("workflows")
                    elif "app builder" in cname:
                        sub_services.append("app-builder")
                    elif "roborewsty" in cname:
                        sub_services.append("roborewsty")

                events.append(
                    OutageEvent(
                        raw_id=raw_id,
                        service=self.name,
                        title=title,
                        description=desc[:500],
                        severity=severity,
                        regions=["australia", "global"],
                        sub_services=sub_services,
                        link="https://status-au.rewst.io",
                        published_at=published_at,
                    )
                )
            return events

        # Fallback to direct incident/events/value array
        items: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = payload.get("value") or payload.get("events") or payload.get("incidents") or []
        elif isinstance(payload, list):
            items = payload

        events = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or item.get("name") or "")
            if not raw_id:
                continue

            props = item.get("properties") or item
            avail_state = str(props.get("availabilityState") or props.get("status") or "").lower()
            severity = _SEVERITY_MAP.get(avail_state, "warning")

            title = str(props.get("title") or props.get("summary") or item.get("name") or "Rewst Event")
            desc = str(props.get("summary") or props.get("details") or title)

            pub_raw = props.get("occurredTime") or props.get("timestamp") or item.get("created_at")
            published_at = parse_datetime(str(pub_raw)) if pub_raw else datetime.now(timezone.utc)

            events.append(
                OutageEvent(
                    raw_id=raw_id,
                    service=self.name,
                    title=title,
                    description=desc[:500],
                    severity=severity,
                    regions=["australia", "global"],
                    sub_services=[],
                    link="https://status-au.rewst.io",
                    published_at=published_at,
                )
            )

        return events
