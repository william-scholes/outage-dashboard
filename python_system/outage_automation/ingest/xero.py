"""Xero ingester (Statuspage.io v2).

Source: https://status.xero.com/api/v2/incidents.json
Xero publishes incidents via standard Atlassian Statuspage v2.

Region Inference:
  - Defaults to `global` (Xero is a multi-tenant cloud platform).
  - Inspects incident name, update bodies, and affected components:
    - Adds `australia` if 'AU:', 'Australia', 'ANZ', or Australian bank feeds are mentioned.
    - Adds `new-zealand` if 'NZ:', 'New Zealand', 'ANZ', or New Zealand bank feeds are mentioned.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .statuspage_v2 import StatuspageV2Adapter, parse_statuspage_v2_incidents

SERVICE = "xero"
DEFAULT_URL = "https://status.xero.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_XERO_URL"

_XERO_EXTRA_REGION_TOKENS: List[Tuple[str, str]] = [
    ("australia", "australia"),
    ("australian", "australia"),
    ("au:", "australia"),
    ("anz", "australia"),
    ("new zealand", "new-zealand"),
    ("nz:", "new-zealand"),
    ("anz", "new-zealand"),
    ("kiwi", "new-zealand"),
    ("inland revenue", "new-zealand"),
    ("ato", "australia"),
]


def extract_xero_regions(incident: Dict[str, Any]) -> List[str]:
    """Helper to extract regions from a raw Xero incident dict."""
    regions = ["global"]
    name = str(incident.get("name") or "").lower()
    comps = incident.get("components") or []
    comp_names = " ".join(str(c.get("name") or "").lower() for c in comps if isinstance(c, dict))
    updates = incident.get("incident_updates") or []
    update_bodies = " ".join(str(u.get("body") or "").lower() for u in updates if isinstance(u, dict))

    text = f"{name} {comp_names} {update_bodies}"
    seen = set(regions)

    for token, reg in _XERO_EXTRA_REGION_TOKENS:
        if token in text and reg not in seen:
            regions.append(reg)
            seen.add(reg)

    return regions


class XeroAdapter(StatuspageV2Adapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload: Any):
        return parse_statuspage_v2_incidents(
            payload,
            self.name,
            extra_region_tokens=_XERO_EXTRA_REGION_TOKENS,
        )
