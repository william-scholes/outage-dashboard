"""Duo Security (Cisco Duo) ingest adapter.

Source: https://status.duo.com/
Powered by Atlassian Statuspage v2 API at https://status.duo.com/api/v2/incidents.json.
Provides coverage for Duo MFA push delivery, telephony, admin panel, and SSO.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents
from ..model import OutageEvent

SERVICE = "duo"
DEFAULT_URL = "https://status.duo.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_DUO_URL"

_AU_PATTERNS = re.compile(r"\b(australia|sydney|melbourne|au|anz)\b", re.IGNORECASE)
_NZ_PATTERNS = re.compile(r"\b(new zealand|nz|auckland|wellington)\b", re.IGNORECASE)


def extract_duo_regions(incident: Dict[str, Any]) -> List[str]:
    """Infer regions from Duo incident text."""
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
    if _NZ_PATTERNS.search(combined):
        regions.add("new-zealand")

    return sorted(regions)


def extract_duo_sub_services(incident: Dict[str, Any]) -> List[str]:
    """Extract sub-services for Duo (push, telephony, sso, admin)."""
    sub_services = set()
    text = str(incident.get("name") or "").lower()
    for c in incident.get("components") or []:
        if isinstance(c, dict):
            text += " " + str(c.get("name") or "").lower()

    if "push" in text:
        sub_services.add("push")
    if "phone" in text or "sms" in text or "telephony" in text:
        sub_services.add("telephony")
    if "sso" in text or "single sign" in text:
        sub_services.add("sso")
    if "admin" in text:
        sub_services.add("admin-panel")

    return sorted(sub_services)


class DuoAdapter(StatuspageV2Adapter):
    """Adapter for Duo Security statuspage."""

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
                    regions=extract_duo_regions(inc),
                    sub_services=extract_duo_sub_services(inc),
                )
            out.append(ev)
        return out
