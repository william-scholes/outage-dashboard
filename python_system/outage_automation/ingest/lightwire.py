"""Lightwire ingest adapter (Cachet API).

Source: https://status.lightwire.co.nz
API Endpoint: https://status.lightwire.co.nz/api/v1/incidents?sort=id&order=desc&per_page=50

Provides coverage for Lightwire Business Internet, WAN, and Voice services across New Zealand.
"""
from __future__ import annotations

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

SERVICE = "lightwire"
DEFAULT_URL = "https://status.lightwire.co.nz/api/v1/incidents?sort=id&order=desc&per_page=50"
URL_ENV_VAR = "OA_LIGHTWIRE_URL"
BASE_STATUS_URL = "https://status.lightwire.co.nz"

_REGION_PATTERNS = [
    (re.compile(r"\b(waikato|hamilton|cambridge|te awamutu)\b", re.IGNORECASE), "waikato"),
    (re.compile(r"\b(bay of plenty|tauranga|rotorua|whakatane)\b", re.IGNORECASE), "bay-of-plenty"),
    (re.compile(r"\b(auckland)\b", re.IGNORECASE), "auckland"),
    (re.compile(r"\b(hawke'?s bay|napier|hastings)\b", re.IGNORECASE), "hawkes-bay"),
    (re.compile(r"\b(taranaki|new plymouth)\b", re.IGNORECASE), "taranaki"),
    (re.compile(r"\b(manawatu|palmerston north|whanganui)\b", re.IGNORECASE), "manawatu"),
    (re.compile(r"\b(wellington)\b", re.IGNORECASE), "wellington"),
    (re.compile(r"\b(canterbury|christchurch)\b", re.IGNORECASE), "canterbury"),
]

_PLANNED_WORDS = re.compile(r"\b(planned|scheduled|maintenance)\b", re.IGNORECASE)
_CRITICAL_WORDS = re.compile(r"\b(major|critical|total outage|down|emergency)\b", re.IGNORECASE)


def extract_lightwire_regions(text: str) -> List[str]:
    """Extract New Zealand regions from Lightwire incident text."""
    regions = {"new-zealand", "global"}
    for pattern, region_code in _REGION_PATTERNS:
        if pattern.search(text):
            regions.add(region_code)
    return sorted(regions)


def extract_lightwire_severity(status: int, text: str) -> str:
    """Map Cachet status code and incident wording to standard severity."""
    # Cachet status 4 = Fixed / Resolved
    if status == 4:
        return "resolved"
    if _PLANNED_WORDS.search(text) and "unplanned" not in text.lower():
        return "info"
    if _CRITICAL_WORDS.search(text):
        return "critical"
    return "warning"


class LightwireAdapter(ProviderAdapter):
    """Adapter for Lightwire (Cachet status platform)."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.environ.get(self.url_env_var) or self.default_url

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
        if not isinstance(payload, dict):
            return []

        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            return []

        events: List[OutageEvent] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue

            raw_id = str(it.get("id") or "").strip()
            if not raw_id:
                continue

            name = str(it.get("name") or "").strip()
            message = str(it.get("message") or "").strip()
            status_code = it.get("status")
            try:
                status_int = int(status_code) if status_code is not None else 1
            except (ValueError, TypeError):
                status_int = 1

            full_text = f"{name} {message}"
            severity = extract_lightwire_severity(status_int, full_text)
            regions = extract_lightwire_regions(full_text)

            published_at = parse_datetime(it.get("created_at"))
            if published_at is None:
                continue

            permalink = str(it.get("permalink") or "").strip()
            link = permalink or f"{BASE_STATUS_URL}/incidents/{raw_id}"

            # Infer voice or data sub-service
            sub_services = []
            if "voice" in full_text.lower() or "calling" in full_text.lower() or "sip" in full_text.lower():
                sub_services.append("voice")
            if "internet" in full_text.lower() or "fibre" in full_text.lower() or "carrier" in full_text.lower():
                sub_services.append("data")

            events.append(
                OutageEvent(
                    raw_id=raw_id,
                    service=self.name,
                    title=name or "(untitled incident)",
                    description=message[:500],
                    severity=severity,
                    regions=regions,
                    sub_services=sub_services,
                    link=link,
                    published_at=published_at,
                )
            )

        return events
