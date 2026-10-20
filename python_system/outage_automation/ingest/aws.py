r"""AWS ingester (Multi-Source: Regional RSS Feeds & Health Dashboard JSON).

AWS exposes outage data across fragmented regional feeds:
  - Regional & service-specific RSS feeds:
    https://status.aws.amazon.com/rss/{service}-{region}.rss
  - Global RSS feeds (all.rss, route53.rss, cloudfront.rss, etc.)
  - AWS Health Dashboard authoritative JSON:
    https://history-events-ap-northeast-1-prod.s3.amazonaws.com/historyevents.json
    https://status.aws.amazon.com/data.json

Coverage:
  - Australia: Sydney (ap-southeast-2), Melbourne (ap-southeast-4)
  - New Zealand: Auckland (ap-southeast-6 / ap-southeast-5)
  - Global AWS Infrastructure: Route 53, CloudFront, IAM, Billing, Management Console
"""
from __future__ import annotations

import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .base import (
    IngestError,
    IngestResult,
    fetch_url,
    maybe_capture_raw,
    parse_json_bytes,
)
from .rss import RssAdapter, _clean_description, _find_child, _text
from ..model import OutageEvent
from ..validate import parse_datetime

SERVICE = "aws"
DEFAULT_URL = "https://status.aws.amazon.com/rss/all.rss"
URL_ENV_VAR = "OA_AWS_URL"

HISTORY_EVENTS_URL = "https://history-events-ap-northeast-1-prod.s3.amazonaws.com/historyevents.json"
ACTIVE_DATA_URL = "https://status.aws.amazon.com/data.json"

TARGET_REGIONS = ["ap-southeast-2", "ap-southeast-4", "ap-southeast-5", "ap-southeast-6", "global"]

GLOBAL_SERVICES = frozenset({
    "cloudfront",
    "route53",
    "iam",
    "billingconsole",
    "management-console",
    "globalaccelerator",
    "chime",
    "waf",
    "shield",
})

_SEV_RESOLVED = ("[resolved]", "resolved")
_SEV_CRITICAL = ("outage", "unavailable", "disruption")
_SEV_WARNING = ("degraded", "increased error", "elevated", "delay")

_REGION_RE = re.compile(r"#[^-]+-([a-z]+-[a-z]+-\d+)_")
_REGION_IN_KEY_RE = re.compile(r"-(ap-southeast-\d|us-[a-z]+-\d|eu-[a-z]+-\d|me-[a-z]+-\d|sa-[a-z]+-\d|ca-[a-z]+-\d|af-[a-z]+-\d)")

# Curated regional and critical global feeds
REGIONAL_FEEDS = [
    "https://status.aws.amazon.com/rss/all.rss",
    "https://status.aws.amazon.com/rss/ec2-ap-southeast-2.rss",
    "https://status.aws.amazon.com/rss/s3-ap-southeast-2.rss",
    "https://status.aws.amazon.com/rss/rds-ap-southeast-2.rss",
    "https://status.aws.amazon.com/rss/lambda-ap-southeast-2.rss",
    "https://status.aws.amazon.com/rss/dynamodb-ap-southeast-2.rss",
    "https://status.aws.amazon.com/rss/ec2-ap-southeast-4.rss",
    "https://status.aws.amazon.com/rss/s3-ap-southeast-4.rss",
    "https://status.aws.amazon.com/rss/rds-ap-southeast-4.rss",
    "https://status.aws.amazon.com/rss/ec2-ap-southeast-6.rss",
    "https://status.aws.amazon.com/rss/s3-ap-southeast-6.rss",
    "https://status.aws.amazon.com/rss/route53.rss",
    "https://status.aws.amazon.com/rss/cloudfront.rss",
]


def _extract_region_from_guid(guid: str) -> Optional[str]:
    if not guid:
        return None
    m = _REGION_RE.search(guid)
    if not m:
        return None
    return m.group(1)


def _infer_severity(title_or_item: Any) -> str:
    if isinstance(title_or_item, ET.Element):
        title = _text(_find_child(title_or_item, "title")).lower()
    else:
        title = str(title_or_item).lower()

    if any(w in title for w in _SEV_RESOLVED):
        return "resolved"
    if any(w in title for w in _SEV_CRITICAL):
        return "critical"
    if any(w in title for w in _SEV_WARNING):
        return "warning"
    return "info"


def _infer_regions(item: ET.Element) -> List[str]:
    title = _text(_find_child(item, "title"))
    title_lower = title.lower()
    guid_elem = _find_child(item, "guid")
    guid = (guid_elem.text if guid_elem is not None and guid_elem.text else "").strip()

    found: List[str] = []
    seen = set()

    def _add(region: str) -> None:
        k = region.lower()
        if k not in seen:
            seen.add(k)
            found.append(region)

    extracted = _extract_region_from_guid(guid)
    if extracted in ("ap-southeast-2", "ap-southeast-4", "ap-southeast-5", "ap-southeast-6"):
        _add(extracted)
        if extracted in ("ap-southeast-5", "ap-southeast-6"):
            _add("new-zealand")

    if "asia pacific (sydney)" in title_lower or "ap-southeast-2" in title_lower:
        _add("ap-southeast-2")
    if "asia pacific (melbourne)" in title_lower or "ap-southeast-4" in title_lower:
        _add("ap-southeast-4")
    if "asia pacific (auckland)" in title_lower or "ap-southeast-6" in title_lower or "ap-southeast-5" in title_lower:
        _add("ap-southeast-6")
        _add("new-zealand")
    if "global" in title_lower or "all regions" in title_lower:
        _add("global")

    return found


def _infer_sub_services(text: str) -> List[str]:
    text_l = text.lower()
    subs = []
    for code in ("ec2", "s3", "rds", "lambda", "dynamodb", "route53", "cloudfront", "iam", "vpc", "billing"):
        if code in text_l:
            subs.append(code)
    return subs


class AwsAdapter(RssAdapter):
    """Adapter for AWS multi-regional RSS and Service Health Dashboard."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        super().__init__(url=url)
        self.is_custom_url = bool(url or os.environ.get(self.url_env_var))

    def parse_severity(self, item: ET.Element) -> str:
        return _infer_severity(item)

    def parse_regions(self, item: ET.Element) -> List[str]:
        return _infer_regions(item)

    def parse_sub_services(self, item: ET.Element) -> List[str]:
        title = _text(_find_child(item, "title"))
        guid = _text(_find_child(item, "guid"))
        return _infer_sub_services(f"{title} {guid}")

    def parse(self, payload: Any) -> List[OutageEvent]:
        # 1. XML ElementTree (e.g. from RssAdapter or fixture)
        if isinstance(payload, ET.Element):
            return super().parse(payload)

        # 2. Raw XML string or bytes
        if isinstance(payload, (str, bytes)):
            s = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
            s_strip = s.strip()
            if s_strip.startswith("<"):
                root = ET.fromstring(s)
                return super().parse(root)
            try:
                payload = json.loads(s)
            except Exception:
                return []

        # 3. AWS Health JSON (historyevents.json or data.json)
        events: List[OutageEvent] = []
        if isinstance(payload, dict):
            for svc_key, items in payload.items():
                if not isinstance(items, list):
                    continue

                # Identify region
                m = _REGION_IN_KEY_RE.search(svc_key)
                if m:
                    reg = m.group(1)
                    if reg not in TARGET_REGIONS:
                        continue
                    regions = [reg]
                    if reg in ("ap-southeast-5", "ap-southeast-6"):
                        regions.append("new-zealand")
                else:
                    svc_base = svc_key.lower()
                    if svc_base in GLOBAL_SERVICES or not any(x in svc_base for x in ("-", "east", "west", "central", "north", "south")):
                        regions = ["global"]
                    else:
                        continue

                for it in items:
                    if not isinstance(it, dict):
                        continue
                    raw_id = str(it.get("arn") or f"{svc_key}_{it.get('date')}")
                    summary = str(it.get("summary") or "AWS Service Event")
                    title = f"{svc_key}: {summary}" if not summary.lower().startswith(svc_key.lower()) else summary
                    date_ts = float(it.get("date") or 0)
                    published_at = datetime.fromtimestamp(date_ts, tz=timezone.utc) if date_ts else datetime.now(timezone.utc)

                    status_str = str(it.get("status") or "1")
                    if "[resolved]" in summary.lower() or "resolved" in summary.lower() or status_str in ("0", "1"):
                        severity = "resolved"
                    elif status_str == "3" or any(w in summary.lower() for w in _SEV_CRITICAL):
                        severity = "critical"
                    elif status_str == "2" or any(w in summary.lower() for w in _SEV_WARNING):
                        severity = "warning"
                    else:
                        severity = "info"

                    desc = summary
                    event_log = it.get("event_log") or []
                    if event_log and isinstance(event_log[0], dict):
                        desc = str(event_log[0].get("message") or summary)

                    sub_services = _infer_sub_services(svc_key)

                    events.append(
                        OutageEvent(
                            raw_id=raw_id,
                            service=self.name,
                            title=title,
                            description=desc[:500],
                            severity=severity,
                            regions=regions,
                            sub_services=sub_services,
                            link=f"https://health.aws.amazon.com/health/status",
                            published_at=published_at,
                        )
                    )

        return events

    def fetch(self, state_dir=None) -> IngestResult:
        # If user explicitly overrode the URL (e.g. testing with specific fixture or custom feed)
        if self.is_custom_url:
            return super().fetch(state_dir=state_dir)

        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        events_by_id: Dict[str, OutageEvent] = {}
        total_bytes = 0

        # 1. Fetch AWS Health Dashboard history events (gzip-decompressed JSON)
        try:
            raw_history_data = fetch_url(HISTORY_EVENTS_URL, headers={"User-Agent": "Mozilla/5.0 OutageAutomation/2.0"})
            total_bytes += len(raw_history_data)
            try:
                decompressed = gzip.decompress(raw_history_data)
            except Exception:
                decompressed = raw_history_data

            history_payload = json.loads(decompressed.decode("utf-8"))
            for ev in self.parse(history_payload):
                events_by_id[ev.raw_id] = ev
        except Exception:
            pass

        # 2. Fetch regional RSS feeds for ANZ & global
        for feed_url in REGIONAL_FEEDS:
            try:
                xml_data = fetch_url(feed_url, headers={"User-Agent": "Mozilla/5.0 OutageAutomation/2.0"})
                total_bytes += len(xml_data)
                root = ET.fromstring(xml_data.decode("utf-8", errors="replace"))
                feed_events = super().parse(root)
                for ev in feed_events:
                    events_by_id[ev.raw_id] = ev
            except Exception:
                continue

        result.raw_bytes = total_bytes
        result.events = list(events_by_id.values())

        if state_dir is not None and result.events:
            maybe_capture_raw(self.name, {"event_count": len(result.events)}, result.raw_bytes, state_dir)

        return result
