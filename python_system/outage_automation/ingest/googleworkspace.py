"""Google Workspace ingester.

Source: https://www.google.com/appsstatus/dashboard/incidents.json
Fallback: Atom feed https://www.google.com/appsstatus/dashboard/en-NZ/feed.atom

Severity inference:
  - Incident has end timestamp, or most_recent_update.status == "AVAILABLE" -> resolved
  - severity == "high" or status_impact == "SERVICE_OUTAGE" -> critical
  - severity == "medium" or status_impact == "SERVICE_DISRUPTION" -> warning
  - severity == "low" or status_impact == "SERVICE_INFORMATION" -> info

Region inference:
  - Always includes 'global' (Google Workspace is a global SaaS)
  - Adds 'australia' if content mentions australia, new zealand, or apac

Sub-services:
  - Extracted from affected_products titles or service_name (e.g. google-drive, gmail, google-meet)
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import IngestResult, ProviderAdapter, fetch_url, maybe_capture_raw
from ..model import OutageEvent
from ..validate import parse_datetime

SERVICE = "googleworkspace"
DEFAULT_URL = "https://www.google.com/appsstatus/dashboard/incidents.json"
URL_ENV_VAR = "OA_GOOGLEWORKSPACE_URL"

_REGION_KEYWORDS_NZ = ("new zealand", "new-zealand", "auckland", "wellington", "christchurch")
_REGION_KEYWORDS_AU = ("australia", "sydney", "melbourne", "canberra", "brisbane", "perth", "adelaide")
_REGION_KEYWORDS_APAC = ("apac", "asia pacific", "asia-pacific", "oceania", "australasia")


def _clean_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _parse_google_incident_json(inc: Dict[str, Any]) -> Optional[OutageEvent]:
    raw_id = str(inc.get("id") or inc.get("number") or "").strip()
    if not raw_id:
        return None

    svc_name = str(inc.get("service_name") or "Google Workspace").strip()

    # Sub services
    sub_services: List[str] = []
    affected_products = inc.get("affected_products")
    if isinstance(affected_products, list):
        for p in affected_products:
            if isinstance(p, dict):
                t = p.get("title")
                if t:
                    slug = _clean_slug(t)
                    if slug and slug not in sub_services:
                        sub_services.append(slug)

    if not sub_services and svc_name:
        slug = _clean_slug(svc_name)
        if slug:
            sub_services.append(slug)

    mru = inc.get("most_recent_update")
    mru_text = mru.get("text", "") if isinstance(mru, dict) else ""
    ext_desc = str(inc.get("external_desc") or "")
    full_text = mru_text or ext_desc or ""

    # Title extraction
    m = re.search(r"(?i)(?:\*\*|##\s*)(?:summary|title):?\s*(?:\*\*)?\s*\n+([^\n]+)", full_text)
    if not m:
        m = re.search(r"(?i)(?:\*\*|##\s*)(?:summary|title):?\s*(?:\*\*)?\s*([^\n]+)", full_text)

    if m:
        raw_title = m.group(1).strip()
        raw_title = re.sub(r"^[#\*_\s]+|[#\*_\s]+$", "", raw_title).strip()
    else:
        lines = [re.sub(r"^[#\*_\s]+|[#\*_\s]+$", "", l).strip() for l in full_text.splitlines() if l.strip()]
        raw_title = ""
        for l in lines:
            if l.lower() not in ("incident report", "mini incident report", "summary", "title", "description"):
                raw_title = l
                break
        if not raw_title:
            raw_title = svc_name

    raw_title = re.sub(r"^\[?Mitigated\]?\s*", "[Mitigated] ", raw_title, flags=re.I)
    raw_title = re.sub(r"^\[?Resolved\]?\s*", "[Resolved] ", raw_title, flags=re.I)
    raw_title = raw_title.strip()

    if svc_name and svc_name.lower() not in raw_title.lower():
        title = f"{svc_name}: {raw_title}"
    else:
        title = raw_title or svc_name

    # Severity inference
    end_raw = inc.get("end")
    mru_status = (mru.get("status") or "").upper() if isinstance(mru, dict) else ""
    sev_raw = str(inc.get("severity") or "").lower()
    impact_raw = str(inc.get("status_impact") or "").upper()

    if end_raw or mru_status == "AVAILABLE":
        severity = "resolved"
    elif sev_raw == "high" or impact_raw == "SERVICE_OUTAGE":
        severity = "critical"
    elif sev_raw == "medium" or impact_raw == "SERVICE_DISRUPTION":
        severity = "warning"
    else:
        severity = "info"

    # Region inference
    regions = ["global"]
    combined_search = f"{svc_name} {title} {full_text}".lower()
    has_nz = any(k in combined_search for k in _REGION_KEYWORDS_NZ)
    has_au = any(k in combined_search for k in _REGION_KEYWORDS_AU)
    has_apac = any(k in combined_search for k in _REGION_KEYWORDS_APAC)

    if has_nz or has_apac:
        if "new-zealand" not in regions:
            regions.append("new-zealand")
    if has_au or has_apac:
        if "australia" not in regions:
            regions.append("australia")

    # Link
    uri = str(inc.get("uri") or f"incidents/{raw_id}").strip().lstrip("/")
    link = f"https://www.google.com/appsstatus/dashboard/{uri}"

    # Timestamp
    pub_raw = inc.get("begin") or inc.get("created") or inc.get("modified")
    pub_dt = parse_datetime(pub_raw) if pub_raw else datetime.now(timezone.utc)
    if pub_dt is None:
        pub_dt = datetime.now(timezone.utc)

    return OutageEvent(
        raw_id=raw_id,
        service=SERVICE,
        title=title,
        description=full_text or title,
        severity=severity,
        regions=regions,
        sub_services=sub_services,
        link=link,
        published_at=pub_dt,
    )


def _parse_google_atom_xml(xml_bytes: bytes) -> List[OutageEvent]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    events: List[OutageEvent] = []

    for entry in root.findall("atom:entry", ns):
        raw_id = (entry.findtext("atom:id", "", ns) or "").strip()
        title_raw = (entry.findtext("atom:title", "", ns) or "").strip()
        updated_raw = (entry.findtext("atom:updated", "", ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href") if link_el is not None else "https://www.google.com/appsstatus/dashboard/"
        summary_html = entry.findtext("atom:summary", "", ns) or ""

        desc_plain = re.sub(r"<[^>]+>", " ", summary_html)
        desc_plain = re.sub(r"\s+", " ", desc_plain).strip()

        # Extract incident ID suffix if raw_id is a tag URN
        m_id = re.search(r"feed:([^.]+)", raw_id)
        clean_id = m_id.group(1) if m_id else raw_id

        title_lower = title_raw.lower()
        if "resolved" in title_lower or "ended at" in desc_plain.lower():
            severity = "resolved"
        elif "outage" in title_lower or "disruption" in title_lower:
            severity = "warning"
        else:
            severity = "info"

        regions = ["global"]
        has_nz = any(k in desc_plain.lower() for k in _REGION_KEYWORDS_NZ)
        has_au = any(k in desc_plain.lower() for k in _REGION_KEYWORDS_AU)
        has_apac = any(k in desc_plain.lower() for k in _REGION_KEYWORDS_APAC)
        if has_nz or has_apac:
            regions.append("new-zealand")
        if has_au or has_apac:
            regions.append("australia")

        pub_dt = parse_datetime(updated_raw) if updated_raw else datetime.now(timezone.utc)
        if pub_dt is None:
            pub_dt = datetime.now(timezone.utc)

        events.append(
            OutageEvent(
                raw_id=clean_id or raw_id,
                service=SERVICE,
                title=title_raw or "Google Workspace Outage",
                description=desc_plain or title_raw,
                severity=severity,
                regions=regions,
                sub_services=["googleworkspace"],
                link=link,
                published_at=pub_dt,
            )
        )

    return events


class GoogleWorkspaceAdapter(ProviderAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        env_url = (
            os.environ.get(self.url_env_var)
            or os.environ.get("OA_GOOGLE_WORKSPACE_URL")
            or os.environ.get("OA_GOOGLE_URL")
        )
        self.url = url or env_url or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GoogleWorkspaceStatusParser/1.0)",
            "Accept": "application/json, application/atom+xml, text/xml, */*",
        }

        try:
            raw_data = fetch_url(self.url, headers=headers)
        except Exception as e:
            result.error = str(e)
            return result

        result.raw_bytes = len(raw_data)

        # Detect JSON vs Atom XML
        text_start = raw_data.lstrip()[:50].decode("utf-8", errors="ignore").lower()
        events: List[OutageEvent] = []

        if text_start.startswith("<") or "<feed" in text_start or "xmlns" in text_start:
            events = _parse_google_atom_xml(raw_data)
        else:
            try:
                payload = json.loads(raw_data.decode("utf-8"))
            except Exception as e:
                result.error = f"Failed to parse JSON response: {e}"
                return result

            if not isinstance(payload, list):
                result.error = f"Unexpected payload shape (expected list, got {type(payload).__name__})"
                return result

            if state_dir is not None:
                cap = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
                if cap:
                    result.raw_payload = cap

            for inc in payload:
                if isinstance(inc, dict):
                    ev = _parse_google_incident_json(inc)
                    if ev:
                        events.append(ev)

        result.events = events
        return result
