"""Atlassian ingester.

Source: https://status.atlassian.com/
Under the hood, Atlassian's main status hub queries 25 individual product Statuspage
endpoints (Jira Software, Jira Service Management, Confluence, Bitbucket, Trello,
Opsgenie, Guard, Statuspage, Compass, Atlas, Analytics, Rovo, etc.).

When running against live feeds without an explicit URL override, this adapter
queries all 25 product Statuspage endpoints to provide complete portfolio coverage
with automatic sub-service tagging.
When `OA_ATLASSIAN_URL` is set or a custom URL is provided (e.g. fixtures/tests),
it fetches from that specific URL.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..model import OutageEvent
from .base import (
    IngestError,
    IngestResult,
    fetch_url,
    maybe_capture_raw,
    parse_json_bytes,
)
from .statuspage_v2 import (
    StatuspageV2Adapter,
    normalize_statuspage_v2_incident,
    parse_statuspage_v2_incidents,
)

SERVICE = "atlassian"
DEFAULT_URL = "https://confluence.status.atlassian.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_ATLASSIAN_URL"

# The 25 product status page codes embedded in https://status.atlassian.com/
ATLASSIAN_PAGE_CODES: List[Tuple[str, str, str]] = [
    ("7yh3h3y0c0x1", "jira", "https://7yh3h3y0c0x1.statuspage.io/api/v2/incidents.json"),
    ("pv54g7ltsc24", "jira-service-management", "https://pv54g7ltsc24.statuspage.io/api/v2/incidents.json"),
    ("s6f0gm5gcpmy", "jira-work-management", "https://s6f0gm5gcpmy.statuspage.io/api/v2/incidents.json"),
    ("qmzzdxyvmbmk", "jira-product-discovery", "https://qmzzdxyvmbmk.statuspage.io/api/v2/incidents.json"),
    ("4g2my7tbhjsq", "confluence", "https://4g2my7tbhjsq.statuspage.io/api/v2/incidents.json"),
    ("jrg4crndtsgz", "jira-align", "https://jrg4crndtsgz.statuspage.io/api/v2/incidents.json"),
    ("h5frqhb041yq", "trello", "https://h5frqhb041yq.statuspage.io/api/v2/incidents.json"),
    ("bqlf8qjztdtr", "bitbucket", "https://bqlf8qjztdtr.statuspage.io/api/v2/incidents.json"),
    ("t05vdsszxwtq", "opsgenie", "https://t05vdsszxwtq.statuspage.io/api/v2/incidents.json"),
    ("y2j98763l56x", "statuspage", "https://metastatuspage.com/api/v2/incidents.json"),
    ("5qmgwsxwsz78", "guard", "https://5qmgwsxwsz78.statuspage.io/api/v2/incidents.json"),
    ("6b7xgjptl20c", "compass", "https://6b7xgjptl20c.statuspage.io/api/v2/incidents.json"),
    ("g0wcwcmyx5lr", "atlas", "https://g0wcwcmyx5lr.statuspage.io/api/v2/incidents.json"),
    ("n7myhlx01bgx", "analytics", "https://n7myhlx01bgx.statuspage.io/api/v2/incidents.json"),
    ("ndlwhd94j2lv", "rovo", "https://ndlwhd94j2lv.statuspage.io/api/v2/incidents.json"),
    ("16gxbt4nsgst", "rovo", "https://16gxbt4nsgst.statuspage.io/api/v2/incidents.json"),
    ("vnj20rkgztf8", "talent", "https://vnj20rkgztf8.statuspage.io/api/v2/incidents.json"),
    ("w3zwfhw3ktfq", "csm", "https://w3zwfhw3ktfq.statuspage.io/api/v2/incidents.json"),
    ("3tbw4lb9t3qp", "support", "https://3tbw4lb9t3qp.statuspage.io/api/v2/incidents.json"),
    ("tfc0vzc0cq1n", "partners", "https://tfc0vzc0cq1n.statuspage.io/api/v2/incidents.json"),
    ("n8766lc3ybbs", "developer", "https://n8766lc3ybbs.statuspage.io/api/v2/incidents.json"),
    ("cgvlk9zzm52d", "admin", "https://cgvlk9zzm52d.statuspage.io/api/v2/incidents.json"),
    ("zr773hgyfn2s", "migrations", "https://zr773hgyfn2s.statuspage.io/api/v2/incidents.json"),
    ("73c2psx3zwgf", "migrations", "https://73c2psx3zwgf.statuspage.io/api/v2/incidents.json"),
    ("j9r9w5bcs26w", "migrations", "https://j9r9w5bcs26w.statuspage.io/api/v2/incidents.json"),
]

_ATLASSIAN_SUB_SERVICES = [
    (re.compile(r"\bjira service management\b|\bjsm\b", re.IGNORECASE), "jira-service-management"),
    (re.compile(r"\bjira work management\b", re.IGNORECASE), "jira-work-management"),
    (re.compile(r"\bjira product discovery\b", re.IGNORECASE), "jira-product-discovery"),
    (re.compile(r"\bjira align\b", re.IGNORECASE), "jira-align"),
    (re.compile(r"\bjira (?:cloud|software)?\b|\bjira\b", re.IGNORECASE), "jira"),
    (re.compile(r"\bconfluence\b", re.IGNORECASE), "confluence"),
    (re.compile(r"\bbitbucket\b", re.IGNORECASE), "bitbucket"),
    (re.compile(r"\btrello\b", re.IGNORECASE), "trello"),
    (re.compile(r"\bopsgenie\b", re.IGNORECASE), "opsgenie"),
    (re.compile(r"\batlassian access\b|\bguard\b", re.IGNORECASE), "guard"),
    (re.compile(r"\bcompass\b", re.IGNORECASE), "compass"),
    (re.compile(r"\batlas\b", re.IGNORECASE), "atlas"),
    (re.compile(r"\brovo\b", re.IGNORECASE), "rovo"),
    (re.compile(r"\bstatuspage\b", re.IGNORECASE), "statuspage"),
]

_ATLASSIAN_EXTRA_REGION_TOKENS: List[Tuple[str, str]] = []


def extract_atlassian_sub_services(incident: Dict[str, Any], default_sub_service: str = "") -> List[str]:
    texts = [str(incident.get("name") or "")]
    for u in incident.get("incident_updates") or []:
        if isinstance(u, dict):
            body = u.get("body")
            if body:
                texts.append(str(body))
    combined = " ".join(texts)
    matched = []
    seen = set()
    if default_sub_service:
        matched.append(default_sub_service)
        seen.add(default_sub_service)
    for pattern, slug in _ATLASSIAN_SUB_SERVICES:
        if pattern.search(combined) and slug not in seen:
            matched.append(slug)
            seen.add(slug)
    return matched


class AtlassianAdapter(StatuspageV2Adapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        super().__init__(url=url)
        self._is_url_overridden = bool(url or os.environ.get(self.url_env_var))

    def fetch(self, state_dir=None) -> IngestResult:
        if self._is_url_overridden:
            return super().fetch(state_dir=state_dir)

        result = IngestResult(provider=self.name)
        result.fetched_at = self._now_iso()

        events_by_id: Dict[str, OutageEvent] = {}
        total_bytes = 0
        raw_payloads = []
        errors = []

        for page_code, default_sub_svc, feed_url in ATLASSIAN_PAGE_CODES:
            try:
                data = fetch_url(feed_url)
            except IngestError as e:
                errors.append(f"{default_sub_svc} ({page_code}): {e}")
                continue
            total_bytes += len(data)
            try:
                payload = parse_json_bytes(data)
            except IngestError as e:
                errors.append(f"{default_sub_svc} ({page_code}) parse error: {e}")
                continue

            raw_payloads.append(payload)

            for inc in (payload.get("incidents") or []):
                if not isinstance(inc, dict):
                    continue
                ev = normalize_statuspage_v2_incident(
                    inc,
                    self.name,
                    extra_region_tokens=_ATLASSIAN_EXTRA_REGION_TOKENS,
                    sub_services_extractor=lambda item, svc=default_sub_svc: extract_atlassian_sub_services(item, svc),
                )
                if ev is not None:
                    if ev.raw_id not in events_by_id:
                        events_by_id[ev.raw_id] = ev
                    else:
                        existing = events_by_id[ev.raw_id]
                        merged_subs = list(dict.fromkeys(existing.sub_services + ev.sub_services))
                        if merged_subs != existing.sub_services:
                            events_by_id[ev.raw_id] = OutageEvent(
                                raw_id=existing.raw_id,
                                service=existing.service,
                                title=existing.title,
                                description=existing.description,
                                severity=existing.severity,
                                regions=existing.regions,
                                sub_services=merged_subs,
                                link=existing.link,
                                published_at=existing.published_at,
                            )

        result.raw_bytes = total_bytes
        if state_dir is not None and raw_payloads:
            cap = maybe_capture_raw(self.name, {"sources": len(raw_payloads), "incidents_count": len(events_by_id)}, result.raw_bytes, state_dir)
            if cap:
                result.raw_payload = cap

        result.events = list(events_by_id.values())
        if errors and not result.events:
            result.error = "; ".join(errors)
        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        return parse_statuspage_v2_incidents(
            payload,
            self.name,
            extra_region_tokens=_ATLASSIAN_EXTRA_REGION_TOKENS,
            sub_services_extractor=extract_atlassian_sub_services,
        )
