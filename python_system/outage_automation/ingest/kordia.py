"""Kordia Ingestion Adapter (Architecture & Partner Portal Stub).

Context:
  Kordia (New Zealand critical infrastructure, B2B WAN, and cyber security provider)
  does not publish an open unauthenticated public status RSS/JSON feed.
  Outages are communicated via the Kordia Customer Portal (Korpus), NOC ticket alerts,
  and dedicated B2B status notifications.

Credentials / Config:
  - OA_KORDIA_API_KEY
  - OA_KORDIA_URL (e.g. partner webhook or status gateway)

When unconfigured, degrades gracefully without failing ingestion cycles.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import IngestResult, ProviderAdapter, fetch_url
from ..model import OutageEvent

SERVICE = "kordia"
DEFAULT_URL = "https://portal.kordia.co.nz/api/v1/network/incidents"
URL_ENV_VAR = "OA_KORDIA_URL"
API_KEY_ENV_VAR = "OA_KORDIA_API_KEY"


class KordiaAdapter(ProviderAdapter):
    """Adapter for Kordia B2B network and cyber security services in New Zealand."""

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
            # Gated behind customer/partner NOC credentials
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
            raw_incidents = payload.get("incidents") or payload.get("data") or []
            for inc in raw_incidents:
                if not isinstance(inc, dict):
                    continue
                raw_id = str(inc.get("id") or "")
                title = str(inc.get("title") or inc.get("summary") or "Kordia Network Event")
                events.append(
                    OutageEvent(
                        raw_id=raw_id,
                        service=self.name,
                        title=title,
                        description=str(inc.get("description") or title),
                        severity="warning",
                        regions=["new-zealand", "global"],
                        sub_services=["wan", "voice", "security"],
                        link="https://www.kordia.co.nz",
                        published_at=datetime.now(timezone.utc),
                    )
                )
        return events
