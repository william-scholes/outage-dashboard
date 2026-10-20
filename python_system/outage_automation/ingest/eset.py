"""ESET Security ingest adapter.

Source: https://status.eset.com/
Powered by Atlassian Statuspage v2 API at https://status.eset.com/api/v2/incidents.json.
Provides coverage for ESET PROTECT Cloud, LiveGrid, LiveGuard, and MSP Administrator.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "eset"
DEFAULT_URL = "https://status.eset.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_ESET_URL"

_REGION_MAP = [
    (re.compile(r"\b(australia|oceania|apac|asia pacific)\b", re.IGNORECASE), "australia"),
    (re.compile(r"\b(apac|asia pacific)\b", re.IGNORECASE), "apac"),
    (re.compile(r"\b(europe|germany|slovakia|uk|london)\b", re.IGNORECASE), "europe"),
    (re.compile(r"\b(americas|north america|canada|usa|united states)\b", re.IGNORECASE), "north-america"),
]


def extract_eset_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from ESET components, title, and incident updates."""
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


class EsetAdapter(StatuspageV2Adapter):
    """Adapter for ESET status page."""

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
                ev = dataclasses.replace(ev, regions=extract_eset_regions(inc_by_id[ev.raw_id]))
            out.append(ev)
        return out
