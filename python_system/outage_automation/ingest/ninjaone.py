"""NinjaOne ingest adapter.

Source: https://status.ninjaone.com/
Powered by Atlassian Statuspage v2 API at https://status.ninjaone.com/api/v2/incidents.json.
Provides coverage for NinjaOne RMM, Backup, Ticketing, and Remote Access across global
and regional (Australia OC) deployments.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "ninjaone"
DEFAULT_URL = "https://status.ninjaone.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_NINJAONE_URL"

_AU_PATTERNS = re.compile(r"\b(australia|sydney|melbourne|oceania|\(oc\)|oc)\b", re.IGNORECASE)


def extract_ninjaone_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from NinjaOne incident components and text."""
    regions = {"global"}
    text_to_check = [str(incident.get("name") or "")]
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            c_name = str(c.get("name") or "")
            text_to_check.append(c_name)
            if _AU_PATTERNS.search(c_name):
                regions.add("australia")

    for u in incident.get("incident_updates") or []:
        if isinstance(u, dict):
            text_to_check.append(str(u.get("body") or ""))

    combined = " ".join(text_to_check)
    if _AU_PATTERNS.search(combined):
        regions.add("australia")

    return sorted(regions)


class NinjaOneAdapter(StatuspageV2Adapter):
    """Adapter for NinjaOne RMM statuspage."""

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
                ev = dataclasses.replace(ev, regions=extract_ninjaone_regions(inc_by_id[ev.raw_id]))
            out.append(ev)
        return out
