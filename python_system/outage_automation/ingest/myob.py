"""MYOB ingest adapter (StatusHub API).

Source: https://status.myob.com
API Endpoint: https://status.myob.com/api/blocks/calendar/v1
Provides coverage for MYOB Business, AccountRight, Practice, PayGlobal, Exo, and Greentree
across Australia and New Zealand.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
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

SERVICE = "myob"
BASE_URL = "https://status.myob.com"
URL_ENV_VAR = "OA_MYOB_URL"

_AU_PATTERNS = re.compile(r"\b(australia|ato|aest|aedt|superannuation|stp|melbourne|sydney|brisbane)\b", re.IGNORECASE)
_NZ_PATTERNS = re.compile(r"\b(new zealand|nz|ird|inland revenue|kiwisaver|nzst|nzdt|auckland|wellington)\b", re.IGNORECASE)

_SUB_SERVICES = {
    "accountright": re.compile(r"\b(accountright|arl)\b", re.IGNORECASE),
    "business": re.compile(r"\b(myob business|essentials)\b", re.IGNORECASE),
    "practice": re.compile(r"\b(practice|ae|ao)\b", re.IGNORECASE),
    "payglobal": re.compile(r"\b(payglobal)\b", re.IGNORECASE),
    "greentree": re.compile(r"\b(greentree)\b", re.IGNORECASE),
    "exo": re.compile(r"\b(exo|exo payroll)\b", re.IGNORECASE),
}


def extract_myob_regions(title: str) -> List[str]:
    """Extract ANZ regions from incident title."""
    regs = set()
    if _AU_PATTERNS.search(title):
        regs.add("australia")
    if _NZ_PATTERNS.search(title):
        regs.add("new-zealand")
    if not regs:
        # Default dual-market SaaS
        regs.update(["australia", "new-zealand"])
    regs.add("global")
    return sorted(regs)


def extract_myob_sub_services(title: str) -> List[str]:
    """Extract product lines from incident title."""
    subs = set()
    for code, pat in _SUB_SERVICES.items():
        if pat.search(title):
            subs.add(code)
    return sorted(subs)


class MyobAdapter(ProviderAdapter):
    """Adapter for MYOB StatusHub platform."""

    name = SERVICE
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        self._custom_url = url or os.environ.get(self.url_env_var)

    @property
    def url(self) -> str:
        if self._custom_url:
            return self._custom_url
        now = datetime.now(timezone.utc)
        s = (now - timedelta(days=14)).strftime("%Y-%m-%dT00:00:00.000Z")
        e = (now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59.999Z")
        return f"{BASE_URL}/api/blocks/calendar/v1?from={s}&to={e}&incidents=true&maintenances=true"

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            data = fetch_url(self.url, headers={"User-Agent": "OutageAutomation/2.0"})
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
        if not isinstance(payload, dict):
            return []

        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            return []

        now_ts = datetime.now(timezone.utc).timestamp()
        events: List[OutageEvent] = []

        for it in raw_items:
            if not isinstance(it, dict):
                continue

            raw_id = str(it.get("id") or "").strip()
            if not raw_id:
                continue

            title = str(it.get("title") or "").strip()
            event_type = it.get("event_type")  # 0 = incident, 1 = maintenance
            start_ts = it.get("start")
            end_ts = it.get("end")

            published_at = parse_datetime(start_ts)
            if published_at is None:
                continue

            # Determine severity
            if end_ts and float(end_ts) <= now_ts:
                severity = "resolved"
            elif event_type == 1:
                severity = "info"
            else:
                title_lower = title.lower()
                if "critical" in title_lower or "major" in title_lower or "outage" in title_lower:
                    severity = "critical"
                else:
                    severity = "warning"

            regions = extract_myob_regions(title)
            sub_services = extract_myob_sub_services(title)
            link = f"{BASE_URL}/"

            events.append(
                OutageEvent(
                    raw_id=raw_id,
                    service=self.name,
                    title=title or "(untitled incident)",
                    description=title,
                    severity=severity,
                    regions=regions,
                    sub_services=sub_services,
                    link=link,
                    published_at=published_at,
                )
            )

        return events
