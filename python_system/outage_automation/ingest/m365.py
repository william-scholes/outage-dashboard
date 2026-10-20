"""Microsoft 365 ingester (RSS 2.0).

Source: https://status.cloud.microsoft/api/feed/feed (Microsoft Admin
Center status RSS feed).

Severity inference (matches the legacy JS parser):
  - "resolved" / "restored" in content, or `<status>Available</status>` → resolved
  - outage / unavailable / down / unable  → critical
  - degraded / issue / impact / intermittent / delay  → warning
  - advisory / maintenance  → info

Region inference:
  - Always includes 'Global' (M365 is fundamentally a SaaS)
  - Adds 'Australia' if content mentions australia / apac / asia

Affected services are extracted from content keywords and added to
the title as `[Exchange Online, Microsoft Teams, ...]`.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List

from .rss import RssAdapter, _find_child, _text, _clean_description


SERVICE = "m365"
DEFAULT_URL = "https://status.cloud.microsoft/api/feed/mac"
URL_ENV_VAR = "OA_M365_URL"

_STATUS_EXPLICIT_RESOLVED = frozenset({
    "available",
    "resolved",
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
})

_SEV_KEYWORDS_CRITICAL = ("outage", "unavailable", "down", "unable")
_SEV_KEYWORDS_WARNING = (
    "degraded", "issue", "impact", "intermittent", "delay",
)
_SEV_KEYWORDS_RESOLVED = ("resolved", "restored", "mitigated")
_SEV_KEYWORDS_INFO = ("advisory", "maintenance", "information")

_REGION_KEYWORDS_NZ = ("new zealand", "new-zealand", "auckland", "wellington", "christchurch")
_REGION_KEYWORDS_AU = ("australia", "sydney", "melbourne", "canberra", "brisbane", "perth", "adelaide")
_REGION_KEYWORDS_APAC = ("apac", "asia pacific", "asia-pacific", "oceania", "australasia")

_SERVICE_KEYWORDS = {
    "exchange": "Exchange Online",
    "teams": "Microsoft Teams",
    "sharepoint": "SharePoint Online",
    "onedrive": "OneDrive",
    "outlook": "Outlook",
    "azure ad": "Azure AD",
    "intune": "Intune",
}


_SUB_SERVICE_CODES = {
    "defender": "defender-endpoint",
    "endpoint protection": "defender-endpoint",
    "defender for endpoint": "defender-endpoint",
    "teams phone": "teams-calling",
    "calling": "teams-calling",
    "pstn": "teams-calling",
    "direct routing": "teams-calling",
    "operator connect": "teams-calling",
    "audio conferencing": "teams-calling",
    "exchange": "exchange",
    "teams": "teams",
    "sharepoint": "sharepoint",
    "onedrive": "onedrive",
    "outlook": "exchange",
    "azure ad": "azure-ad",
    "entra": "azure-ad",
    "intune": "intune",
}


def _infer_sub_services(item: ET.Element) -> List[str]:
    body = _content(item)
    found = set()
    for keyword, code in _SUB_SERVICE_CODES.items():
        if keyword in body:
            found.add(code)
    return sorted(found)


def _content(item: ET.Element) -> str:
    title = _text(_find_child(item, "title"))
    desc = _text(_find_child(item, "description"))
    return f"{title} {desc}".lower()


def _status_text(item: ET.Element) -> str:
    return _text(_find_child(item, "status")).lower()


def _infer_severity(item: ET.Element) -> str:
    status = _status_text(item)
    title = _text(_find_child(item, "title")).lower()
    body = _content(item)

    # 1. Explicit status tag matching
    if status in _STATUS_EXPLICIT_RESOLVED:
        return "resolved"
    if status in _STATUS_EXPLICIT_CRITICAL:
        return "critical"
    if status in _STATUS_EXPLICIT_WARNING:
        return "warning"

    # 2. Check title for resolved keywords (avoid matching recovery notes inside description body)
    if any(k in title for k in _SEV_KEYWORDS_RESOLVED):
        return "resolved"

    # 3. Keyword-based inference from content and non-explicit status
    if any(k in body for k in _SEV_KEYWORDS_CRITICAL):
        return "critical"
    if any(k in body for k in _SEV_KEYWORDS_WARNING) or status in (
        "investigating", "restoringservice", "extendedrecovery", "degraded", "warning"
    ):
        return "warning"
    if any(k in body for k in _SEV_KEYWORDS_INFO) or status in (
        "advisory", "information", "maintenance"
    ):
        return "info"
    return "info"


def _infer_regions(item: ET.Element) -> List[str]:
    body = _content(item)
    regions = ["global"]
    has_nz = any(k in body for k in _REGION_KEYWORDS_NZ)
    has_au = any(k in body for k in _REGION_KEYWORDS_AU)
    has_apac = any(k in body for k in _REGION_KEYWORDS_APAC)
    if has_au or has_apac:
        regions.append("australia")
    if has_nz or has_apac:
        regions.append("new-zealand")
    return regions


def _infer_services(item: ET.Element) -> List[str]:
    body = _content(item)
    found: List[str] = []
    for keyword, name in _SERVICE_KEYWORDS.items():
        if keyword in body and name not in found:
            found.append(name)
    return found


def _augment_title(item: ET.Element) -> str:
    """Append affected services to the title so they're visible at a glance."""
    title = _text(_find_child(item, "title")).strip()
    services = _infer_services(item)
    if services and title:
        return f"{title} [{', '.join(services)}]"
    return title


class M365Adapter(RssAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse_severity(self, item: ET.Element) -> str:
        return _infer_severity(item)

    def parse_regions(self, item: ET.Element) -> List[str]:
        return _infer_regions(item)

    def parse_sub_services(self, item: ET.Element) -> List[str]:
        return _infer_sub_services(item)

    def parse_title(self, item: ET.Element) -> str:
        return _augment_title(item)

    def parse_description(self, item: ET.Element) -> str:
        return _clean_description(_text(_find_child(item, "description")))
