"""Kaseya ingest adapter.

Source: https://status.kaseya.com/
Powered by Atlassian Statuspage v2 API at https://status.kaseya.com/api/v2/incidents.json.
Provides unified coverage for the entire Kaseya MSP suite:
  - IT Glue
  - Datto (BCDR, SaaS Protection, RMM, Networking, Autotask PSA)
  - Kaseya VSA & BMS
  - Spanning Backup, Dark Web ID, RocketCyber
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "kaseya"
DEFAULT_URL = "https://status.kaseya.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_KASEYA_URL"

_AU_PATTERNS = re.compile(r"\b(australia|sydney|melbourne|syrah|apac|oceania|au|nz|new zealand)\b", re.IGNORECASE)

_SUB_SERVICES = {
    "it-glue": re.compile(r"\b(it glue|itglue)\b", re.IGNORECASE),
    "datto": re.compile(r"\b(datto|siris|alto|saas protection|bcdr)\b", re.IGNORECASE),
    "autotask": re.compile(r"\b(autotask|psa)\b", re.IGNORECASE),
    "vsa": re.compile(r"\b(vsa|vsa 9|vsa x)\b", re.IGNORECASE),
    "bms": re.compile(r"\b(bms)\b", re.IGNORECASE),
    "spanning": re.compile(r"\b(spanning)\b", re.IGNORECASE),
    "dark-web-id": re.compile(r"\b(dark web id|darkweb)\b", re.IGNORECASE),
    "rocketcyber": re.compile(r"\b(rocketcyber)\b", re.IGNORECASE),
}


def extract_kaseya_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from Kaseya incident components and updates."""
    regions = {"global"}
    text_to_check = [str(incident.get("name") or "")]
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            text_to_check.append(str(c.get("name") or ""))

    for u in incident.get("incident_updates") or []:
        if isinstance(u, dict):
            text_to_check.append(str(u.get("body") or ""))

    combined = " ".join(text_to_check)
    if _AU_PATTERNS.search(combined):
        regions.add("australia")
        regions.add("apac")

    return sorted(regions)


def extract_kaseya_sub_services(incident: Dict[str, Any]) -> List[str]:
    """Extract product lines within Kaseya stack."""
    sub_services = set()
    text_to_check = [str(incident.get("name") or "")]
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            text_to_check.append(str(c.get("name") or ""))

    combined = " ".join(text_to_check)
    for code, pattern in _SUB_SERVICES.items():
        if pattern.search(combined):
            sub_services.add(code)

    return sorted(sub_services)


class KaseyaAdapter(StatuspageV2Adapter):
    """Adapter for Kaseya statuspage."""

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
                inc = inc_by_id[ev.raw_id]
                ev = dataclasses.replace(
                    ev,
                    regions=extract_kaseya_regions(inc),
                    sub_services=extract_kaseya_sub_services(inc),
                )
            out.append(ev)
        return out
