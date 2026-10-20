"""Pia (aiDesk) Ingestion Adapter (Architecture & Partner API Stub).

Context:
  Pia (https://pia.ai) provides AI helpdesk automation for MSPs across ANZ and globally.
  Health status is provided through the Pia Partner Portal (aiDesk) and MSP partner webhooks.

Credentials / Config:
  - OA_PIA_API_KEY
  - OA_PIA_URL

When unconfigured, degrades gracefully without failing ingestion cycles.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import IngestResult, ProviderAdapter, fetch_url
from ..model import OutageEvent

SERVICE = "pia"
DEFAULT_URL = "https://api.pia.ai/v1/health"
URL_ENV_VAR = "OA_PIA_URL"
API_KEY_ENV_VAR = "OA_PIA_API_KEY"


class PiaAdapter(ProviderAdapter):
    """Adapter for Pia aiDesk MSP automation platform."""

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
            # Gated behind Pia MSP partner credentials
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
                title = str(inc.get("title") or inc.get("summary") or "Pia Platform Alert")
                events.append(
                    OutageEvent(
                        raw_id=raw_id,
                        service=self.name,
                        title=title,
                        description=str(inc.get("description") or title),
                        severity="warning",
                        regions=["australia", "new-zealand", "global"],
                        sub_services=["aidesk", "automations", "smart-triage"],
                        link="https://partner.pia.ai",
                        published_at=datetime.now(timezone.utc),
                    )
                )
        return events
