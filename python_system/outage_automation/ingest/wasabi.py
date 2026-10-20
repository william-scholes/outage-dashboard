"""Wasabi Cloud Storage ingest adapter.

Source: https://status.wasabi.com/
Powered by Atlassian Statuspage v2 API at https://status.wasabi.com/api/v2/incidents.json.
Provides coverage for Wasabi Hot Cloud Storage across global regions,
including AP-Southeast-2 (Sydney) and AP-Southeast-1 (Singapore).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "wasabi"
DEFAULT_URL = "https://status.wasabi.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_WASABI_URL"

_REGION_MAP = [
    (re.compile(r"\b(ap-southeast-2|sydney|australia)\b", re.IGNORECASE), "ap-southeast-2"),
    (re.compile(r"\b(ap-southeast-1|singapore)\b", re.IGNORECASE), "ap-southeast-1"),
    (re.compile(r"\b(us-east-1|virginia)\b", re.IGNORECASE), "us-east-1"),
    (re.compile(r"\b(us-west-1|oregon)\b", re.IGNORECASE), "us-west-1"),
    (re.compile(r"\b(us-central-1|texas)\b", re.IGNORECASE), "us-central-1"),
    (re.compile(r"\b(eu-central-1|amsterdam)\b", re.IGNORECASE), "eu-central-1"),
    (re.compile(r"\b(eu-west-1|london)\b", re.IGNORECASE), "eu-west-1"),
]


def extract_wasabi_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from Wasabi components, title, and incident updates."""
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


class WasabiAdapter(StatuspageV2Adapter):
    """Adapter for Wasabi Technologies status page."""

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
                ev = dataclasses.replace(ev, regions=extract_wasabi_regions(inc_by_id[ev.raw_id]))
            out.append(ev)
        return out
