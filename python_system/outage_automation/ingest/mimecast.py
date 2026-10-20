"""Mimecast ingester (Status.io).

Source: https://status.mimecast.com
Powered by Status.io with page ID 5d849b1c02e65b3ec45369d4.
API Endpoint: https://api.status.io/1.0/status/5d849b1c02e65b3ec45369d4

Provides coverage for Mimecast Email Security, Cloud Integrated, and Australian Grid.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .status_io import StatusIoAdapter, parse_status_io_incidents
from ..model import OutageEvent

SERVICE = "mimecast"
PAGE_ID = "5d849b1c02e65b3ec45369d4"
DEFAULT_URL = f"https://api.status.io/1.0/status/{PAGE_ID}"
URL_ENV_VAR = "OA_MIMECAST_URL"
DEFAULT_LINK_TEMPLATE = f"https://status.mimecast.com/pages/incident/{PAGE_ID}/{{id}}"

_AU_CONTAINER_PATTERNS = re.compile(r"\b(australia|australian|sydney|melbourne|au)\b", re.IGNORECASE)


class MimecastAdapter(StatusIoAdapter):
    """Adapter for Mimecast Status.io feed."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload: Any) -> List[OutageEvent]:
        events = parse_status_io_incidents(
            payload,
            self.name,
            default_link_template=DEFAULT_LINK_TEMPLATE,
        )
        # Status.io containers normalization:
        # Check containers_affected in raw incidents to add australia/nz
        raw_result = payload.get("result", {}) if isinstance(payload, dict) else {}
        raw_incidents = raw_result.get("incidents", []) if isinstance(raw_result, dict) else []
        inc_by_id = {inc.get("_id"): inc for inc in raw_incidents if isinstance(inc, dict)}

        import dataclasses
        out = []
        for ev in events:
            inc = inc_by_id.get(ev.raw_id)
            if inc:
                regs = set(ev.regions)
                for cont in inc.get("containers_affected") or []:
                    if isinstance(cont, dict):
                        cname = str(cont.get("name") or "")
                        if _AU_CONTAINER_PATTERNS.search(cname):
                            regs.add("australia")
                for comp in inc.get("components_affected") or []:
                    if isinstance(comp, dict):
                        cname = str(comp.get("name") or "")
                        if _AU_CONTAINER_PATTERNS.search(cname):
                            regs.add("australia")
                ev = dataclasses.replace(ev, regions=sorted(regs))
            out.append(ev)
        return out
