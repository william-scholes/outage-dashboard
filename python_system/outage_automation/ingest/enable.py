"""Enable Networks ingest adapter.

Source: https://enable.statuspage.io/
Powered by Atlassian Statuspage v2 API at https://enable.statuspage.io/api/v2/incidents.json.
Provides coverage for Enable Networks (Christchurch & Canterbury LFC Fibre Network in New Zealand).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional

from .statuspage_v2 import (
    StatuspageV2Adapter,
    parse_statuspage_v2_incidents,
)
from ..model import OutageEvent

SERVICE = "enable"
DEFAULT_URL = "https://enable.statuspage.io/api/v2/incidents.json"
URL_ENV_VAR = "OA_ENABLE_URL"

_CANTERBURY_PATTERNS = re.compile(r"\b(christchurch|canterbury|selwyn|waimakariri|ashburton|rolleston|rangiora)\b", re.IGNORECASE)


def extract_enable_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions for Enable Networks fibre outages."""
    regions = {"new-zealand", "canterbury", "global"}
    text_to_check = [str(incident.get("name") or "")]
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            text_to_check.append(str(c.get("name") or ""))

    for u in incident.get("incident_updates") or []:
        if isinstance(u, dict):
            text_to_check.append(str(u.get("body") or ""))

    combined = " ".join(text_to_check)
    if _CANTERBURY_PATTERNS.search(combined):
        regions.add("christchurch")

    return sorted(regions)


class EnableAdapter(StatuspageV2Adapter):
    """Adapter for Enable Networks statuspage."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload: Any) -> List[OutageEvent]:
        events = parse_statuspage_v2_incidents(
            payload,
            self.name,
            default_regions=["new-zealand", "canterbury", "global"],
        )
        raw_incidents = payload.get("incidents", []) if isinstance(payload, dict) else []
        inc_by_id = {inc.get("id"): inc for inc in raw_incidents if isinstance(inc, dict)}
        out = []
        for ev in events:
            if ev.raw_id in inc_by_id:
                ev = dataclasses.replace(ev, regions=extract_enable_regions(inc_by_id[ev.raw_id]))
            out.append(ev)
        return out
