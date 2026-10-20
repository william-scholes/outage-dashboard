"""Tenable Vulnerability Management ingest adapter.

Source: https://status.tenable.com/
Powered by Atlassian Statuspage v2 API at https://status.tenable.com/api/v2/incidents.json.
Provides coverage for Tenable Vulnerability Management, Tenable Cloud Security,
and Web Application Scanning.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "tenable"
DEFAULT_URL = "https://status.tenable.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_TENABLE_URL"

_REGION_MAP = [
    (re.compile(r"\b(australia|sydney|ap-syd-1|au1|apac)\b", re.IGNORECASE), "australia"),
    (re.compile(r"\b(united-states|us|usgov01|us01|us-east|us-west)\b", re.IGNORECASE), "united-states"),
    (re.compile(r"\b(europe|eu|eu1|fra|frankfurt|london)\b", re.IGNORECASE), "europe"),
]


def extract_tenable_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from Tenable components, title, and incident updates."""
    regions = {"global"}
    text_chunks = [str(incident.get("name") or "")]
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            text_chunks.append(str(c.get("name") or ""))

    for u in incident.get("incident_updates") or []:
        if isinstance(u, dict):
            text_chunks.append(str(u.get("body") or ""))

    combined = " ".join(text_chunks)
    for pattern, code in _REGION_MAP:
        if pattern.search(combined):
            regions.add(code)

    return sorted(regions)


class TenableAdapter(StatuspageV2Adapter):
    """Adapter for Tenable status page."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload: Any) -> List[OutageEvent]:
        events = parse_statuspage_v2_incidents(payload, self.name)
        raw_incidents = payload.get("incidents", []) if isinstance(payload, dict) else []
        inc_by_id = {inc.get("id"): inc for inc in raw_incidents if isinstance(inc, dict)}
        out = []
        for ev in events:
            if ev.raw_id in inc_by_id:
                ev = dataclasses.replace(ev, regions=extract_tenable_regions(inc_by_id[ev.raw_id]))
            out.append(ev)
        return out
