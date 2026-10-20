"""NBN Co Ingestion Adapter (Australia National Broadband Network).

Context:
  NBN Co provides wholesale broadband infrastructure across Australia.
  Outages are tracked via the NBN Network Status Portal, Wholesale RSP B2B Gateway,
  and POI (Point of Interconnect) maintenance schedules.

Credentials / Config:
  - OA_NBN_API_KEY
  - OA_NBN_URL

When unconfigured, degrades gracefully without failing ingestion cycles.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import IngestResult, ProviderAdapter, fetch_url
from ..model import OutageEvent

SERVICE = "nbn"
DEFAULT_URL = "https://www.nbnco.com.au/api/v1/network-status"
URL_ENV_VAR = "OA_NBN_URL"
API_KEY_ENV_VAR = "OA_NBN_API_KEY"


class NbnAdapter(ProviderAdapter):
    """Adapter for NBN Co wholesale network outages across Australia."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.environ.get(self.url_env_var) or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        api_key = os.environ.get(API_KEY_ENV_VAR)
        if not api_key:
            # Gated behind wholesale RSP credentials
            result.events = []
            return result

        try:
            data = fetch_url(self.url, headers={"Authorization": f"Bearer {api_key}"})
            payload = json.loads(data.decode("utf-8"))
            result.events = self.parse(payload)
        except Exception as e:
            result.error = str(e)

        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        events: List[OutageEvent] = []
        if isinstance(payload, dict):
            raw_incidents = payload.get("outages") or payload.get("incidents") or []
            for inc in raw_incidents:
                if not isinstance(inc, dict):
                    continue
                raw_id = str(inc.get("id") or "")
                title = str(inc.get("title") or inc.get("description") or "NBN Network Outage")
                events.append(
                    OutageEvent(
                        raw_id=raw_id,
                        service=self.name,
                        title=title,
                        description=str(inc.get("impact") or title),
                        severity="warning",
                        regions=["australia", "global"],
                        sub_services=["fttp", "fttc", "fttn", "hfc", "fixed-wireless"],
                        link="https://www.nbnco.com.au/support/network-status",
                        published_at=datetime.now(timezone.utc),
                    )
                )
        return events
