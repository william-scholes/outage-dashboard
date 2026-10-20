"""Shared Statuspage v2 adapter (used by Cloudflare; intended as the
template for any future Statuspage.io-style provider).

Most status pages hosted by Atlassian Statuspage expose the same
`/api/v2/incidents.json` endpoint. The shape is:

    {
      "incidents": [
        {
          "id": "...",
          "name": "...",
          "status": "investigating|identified|monitoring|resolved|postmortem",
          "impact": "none|minor|major|critical",
          "shortlink": "https://stspg.io/...",
          "url": "https://...",
          "created_at": "2026-03-06T19:30:47.000Z",
          "incident_updates": [
            {"body": "..."}
          ],
          "components": [{"name": "..."}]
        }
      ]
    }

Subclasses just set `name` + `default_url` + the env-var override.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..model import OutageEvent
from ..validate import parse_datetime, severity_from_impact
from .base import (
    IngestError,
    IngestResult,
    ProviderAdapter,
    fetch_url,
    maybe_capture_raw,
    parse_json_bytes,
)


DEFAULT_REGIONS = ["global"]


def _pick_description(incident: Dict[str, Any]) -> str:
    updates = incident.get("incident_updates") or []
    if updates:
        body = updates[0].get("body") if isinstance(updates[0], dict) else None
        if body:
            return str(body)
    return str(incident.get("name") or "")


def _regions_for_incident(incident: Dict[str, Any], default: List[str]) -> List[str]:
    """Default region inference: always start with `default`, then add
    anything inferable from component names, incident title, and description.
    """
    regions = list(default)
    seen = {r.lower() for r in regions}

    text_sources = []
    for comp in incident.get("components") or []:
        if isinstance(comp, dict):
            text_sources.append(str(comp.get("name") or "").lower())
    title = str(incident.get("name") or "").lower()
    desc = _pick_description(incident).lower()
    text_sources.extend([title, desc])

    full_text = " ".join(text_sources)
    for token, region in _EXTRA_REGION_TOKENS:
        if token in full_text and region not in seen:
            regions.append(region)
            seen.add(region)
    return regions


# Default extra-region tokens used by the Cloudflare ingester. Subclasses
# can extend or replace this map via `_extra_region_tokens`.
_EXTRA_REGION_TOKENS = [
    ("asia", "australia"),
    ("apac", "australia"),
    ("oceania", "australia"),
    ("sydney", "australia"),
    ("melbourne", "australia"),
    ("perth", "australia"),
    ("brisbane", "australia"),
    ("adelaide", "australia"),
    ("australia", "australia"),
    ("auckland", "australia"),
    ("wellington", "australia"),
]


def normalize_statuspage_v2_incident(
    incident: Dict[str, Any],
    service: str,
    *,
    default_regions: List[str] = DEFAULT_REGIONS,
    extra_region_tokens: Optional[List] = None,
    sub_services_extractor: Optional[Any] = None,
) -> Optional[OutageEvent]:
    """Turn a Statuspage v2 incident dict into an `OutageEvent`.

    The `extra_region_tokens` argument lets subclasses add extra region
    inference without subclassing.
    """
    raw_id = str(incident.get("id") or "").strip()
    if not raw_id:
        return None

    status = str(incident.get("status") or "").strip().lower()
    impact = str(incident.get("impact") or "").strip().lower()

    severity = severity_from_impact(impact)
    if status in ("resolved", "postmortem", "completed"):
        severity = "resolved"

    title = str(incident.get("name") or "").strip()
    description = _pick_description(incident)[:500]
    link = str(incident.get("shortlink") or "").strip()
    if not link:
        link = str(incident.get("url") or "").strip()

    published_at = parse_datetime(incident.get("created_at"))
    if published_at is None:
        return None

    if extra_region_tokens is not None:
        regions = _regions_for_incident(incident, default_regions)
        # overlay the caller's tokens
        seen = {r.lower() for r in regions}
        for comp in incident.get("components") or []:
            if not isinstance(comp, dict):
                continue
            name = str(comp.get("name") or "").lower()
            for token, region in extra_region_tokens:
                if token in name and region not in seen:
                    regions.append(region)
                    seen.add(region)
    else:
        regions = _regions_for_incident(incident, default_regions)

    sub_services = sub_services_extractor(incident) if sub_services_extractor else []

    return OutageEvent(
        raw_id=raw_id,
        service=service,
        title=title or "(untitled incident)",
        description=description,
        severity=severity,
        regions=regions,
        sub_services=sub_services,
        link=link,
        published_at=published_at,
    )


def parse_statuspage_v2_incidents(
    payload: Any, service: str, **kwargs
) -> List[OutageEvent]:
    """Parse a Statuspage v2 incidents payload into `OutageEvent` objects."""
    if not isinstance(payload, dict):
        return []
    raw_incidents = payload.get("incidents") or []
    if not isinstance(raw_incidents, list):
        return []
    out: List[OutageEvent] = []
    for it in raw_incidents:
        if not isinstance(it, dict):
            continue
        ev = normalize_statuspage_v2_incident(it, service, **kwargs)
        if ev is not None:
            out.append(ev)
    return out


class StatuspageV2Adapter(ProviderAdapter):
    """Base class for any provider exposing the Statuspage v2 API.

    Subclasses set `name`, `default_url`, and `url_env_var`. The
    `parse` hook is exposed so subclasses can tweak normalization.
    """

    name: str = ""
    default_url: str = ""
    url_env_var: str = ""

    def __init__(self, url: Optional[str] = None) -> None:
        env_url = (
            os.environ.get(self.url_env_var) if self.url_env_var else None
        )
        self.url = url or env_url or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = (
            self._now_iso()
        )
        try:
            data = fetch_url(self.url)
        except IngestError as e:
            result.error = str(e)
            return result
        result.raw_bytes = len(data)
        try:
            payload = parse_json_bytes(data)
        except IngestError as e:
            result.error = str(e)
            return result
        if state_dir is not None:
            cap = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
            if cap:
                result.raw_payload = cap
        result.events = self.parse(payload)
        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        return parse_statuspage_v2_incidents(payload, self.name)

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
