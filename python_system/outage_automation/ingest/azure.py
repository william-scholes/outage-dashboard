"""Azure ingester (RSS 2.0).

Source: https://rssfeed.azure.status.microsoft/en-us/status/feed/

Severity inference (matches the legacy JS parser):
  - title contains critical/outage/down/unavailable → critical
  - title contains degraded/degradation/issue/problem → warning
  - title contains resolved/mitigated/recovery → resolved
  - else info

Region inference (matches the legacy JS parser):
  - Search the combined title + description text for the target regions.
  - Capitalize each matched region.
  - If no regions match but the content mentions "all regions" / "worldwide"
    / "global" / "multiple regions", use `Global`.
  - Else, fall back to `[]` (no regions) so we don't fabricate.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List

from .rss import RssAdapter, _find_child, _text


SERVICE = "azure"
DEFAULT_URL = "https://rssfeed.azure.status.microsoft/en-us/status/feed/"
URL_ENV_VAR = "OA_AZURE_URL"

# Lowercase region patterns to look for in content.
TARGET_REGIONS = [
    "australia-east",
    "australia-southeast",
    "australia-central",
    "australia-central-2",
    "global",
    "worldwide",
    "all-regions",
]

_GLOBAL_REGION_LABELS = frozenset({"global", "worldwide", "all-regions", "all regions", "multiple regions"})

_STATUS_EXPLICIT_RESOLVED = frozenset({
    "available",
    "resolved",
    "mitigated",
    "restored",
    "servicerestored",
    "service restored",
    "healthy",
    "normal",
})

_STATUS_EXPLICIT_CRITICAL = frozenset({
    "serviceinterruption",
    "service interruption",
    "critical",
    "outage",
})

_STATUS_EXPLICIT_WARNING = frozenset({
    "servicedegradation",
    "service degradation",
    "degraded",
    "warning",
    "investigating",
})

# Severity keyword maps.
_SEV_CRITICAL = (
    "critical", "outage", "down", "unavailable", "unable", "disruption", "offline",
)
_SEV_WARNING = (
    "degraded", "degradation", "issue", "problem", "impact", "intermittent",
    "delay", "latency", "error", "errors", "failure", "failures", "elevated",
    "investigating",
)
_SEV_RESOLVED = ("resolved", "mitigated", "recovery", "restored", "completed")
_SEV_INFO = ("advisory", "information", "maintenance", "notice")


def _content(item: ET.Element) -> str:
    title = _text(_find_child(item, "title"))
    desc = _text(_find_child(item, "description"))
    return f"{title} {desc}".lower()


def _status_text(item: ET.Element) -> str:
    return _text(_find_child(item, "status")).lower()


def _infer_severity(item: ET.Element) -> str:
    status = _status_text(item)
    title = _text(_find_child(item, "title")).lower()
    desc = _text(_find_child(item, "description")).lower()

    # 1. Explicit status tag matching (if present)
    if status in _STATUS_EXPLICIT_RESOLVED:
        return "resolved"
    if status in _STATUS_EXPLICIT_CRITICAL:
        return "critical"
    if status in _STATUS_EXPLICIT_WARNING:
        return "warning"

    # 2. Check title for explicit keywords (priority: resolved > critical > warning > info)
    if any(w in title for w in _SEV_RESOLVED):
        return "resolved"
    if any(w in title for w in _SEV_CRITICAL):
        return "critical"
    if any(w in title for w in _SEV_WARNING):
        return "warning"
    if any(w in title for w in _SEV_INFO):
        return "info"

    # 3. Fallback to description keywords when title is generic / uninformative
    if any(w in desc for w in _SEV_CRITICAL):
        return "critical"
    if any(w in desc for w in _SEV_WARNING):
        return "warning"
    if any(w in desc for w in _SEV_INFO):
        return "info"

    return "info"


def _infer_regions(item: ET.Element) -> List[str]:
    body = _content(item)
    found: List[str] = []
    seen = set()
    for region in TARGET_REGIONS:
        # Canonical form uses hyphens (matching approved-regions.csv).
        # Azure RSS content uses spaces in human-readable titles (e.g.
        # "Australia East"), so check both hyphen and space forms.
        search_key = region.replace("-", " ")
        if search_key in body or region in body:
            key = region.lower()
            if key not in seen:
                seen.add(key)
                found.append(region)
    # Collapse all global-ish labels to the canonical "global" label.
    if any(label in seen for label in _GLOBAL_REGION_LABELS):
        return ["global"]
    return found


def _infer_guid(item: ET.Element) -> str:
    """Status.io-style `<guid isPermaLink="false">…</guid>` is uncommon in
    the Azure feed; the legacy parser preferred `guid['#text']` but in
    practice the Azure feed uses the link as the unique id. We mirror
    that fallback chain here.
    """
    guid_elem = _find_child(item, "guid")
    if guid_elem is None:
        link = _text(_find_child(item, "link"))
        return link
    text = (guid_elem.text or "").strip()
    if text:
        return text
    return _text(_find_child(item, "link"))


class AzureAdapter(RssAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse_severity(self, item: ET.Element) -> str:
        return _infer_severity(item)

    def parse_regions(self, item: ET.Element) -> List[str]:
        return _infer_regions(item)

    def parse_guid(self, item: ET.Element) -> str:
        return _infer_guid(item)
