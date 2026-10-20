"""Wiz ingester (RSS 2.0).

Source: Wiz Status RSS feed (https://status.wiz.io/history.rss).
Configurable via `OA_WIZ_URL`.

Severity inference:
  - Latest update in description is Resolved -> resolved
  - Disruption / outage / unavailable -> critical
  - Degradation / elevated error -> major
  - Issue / delays / investigation -> warning
  - Default -> info

Region inference:
  - Inferred from cloud regions (e.g. us-west-2, me-central-1), geographic tags (us, eu, apac),
    or data center references.
  - Defaults to ["global"].
  - Dynamically registers newly discovered regions into `approved-regions.csv` (append-only),
    matching the Chorus / Vector pattern.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import fetch_url
from .rss import RssAdapter, _clean_description, _find_child, _text
from ..dashboard.regions_store import RegionRow, load_regions_csv, write_regions_csv
from ..logging_setup import get_logger
from ..model import OutageEvent
from ..paths import resolve_paths

SERVICE = "wiz"
DEFAULT_URL = "https://status.wiz.io/history.rss"
URL_ENV_VAR = "OA_WIZ_URL"

DEFAULT_WIZ_REGIONS = {
    "global": "Global",
    "us": "United States",
    "eu": "Europe",
    "apac": "Asia Pacific",
}

_KNOWN_REGION_NAMES = {
    "global": "Global",
    "us": "United States",
    "eu": "Europe",
    "apac": "Asia Pacific",
    "us-west-2": "US West 2 (Oregon)",
    "us-east-1": "US East 1 (N. Virginia)",
    "us-east-2": "US East 2 (Ohio)",
    "me-central-1": "ME Central 1 (UAE)",
    "eu-west-1": "EU West 1 (Ireland)",
    "eu-central-1": "EU Central 1 (Frankfurt)",
    "ap-southeast-2": "AP Southeast 2 (Sydney)",
}

# Sub-services recognized for Wiz
_SUB_SERVICES = (
    ("authentication", ("auth", "login", "sso", "authentication")),
    ("portal", ("portal", "ui", "console")),
    ("ingestion", ("ingestion", "ingest", "data loss", "sensor atm", "vir")),
    ("defend", ("defend", "wiz defend")),
    ("sensor", ("sensor",)),
    ("cli", ("wiz cli", "cli")),
    ("code", ("wiz code", "code")),
    ("tenable", ("tenable",)),
)


def _content(item: ET.Element) -> str:
    title = _text(_find_child(item, "title"))
    desc = _text(_find_child(item, "description"))
    return f"{title} {desc}".lower()


def _infer_severity(item: ET.Element) -> str:
    desc = _text(_find_child(item, "description"))
    title = _text(_find_child(item, "title")).lower()

    # The top of Wiz description contains <strong>Status</strong>
    status_match = re.search(r"<strong>(Resolved|Monitoring|Identified|Investigating)</strong>", desc, re.IGNORECASE)
    latest_status = status_match.group(1).lower() if status_match else ""

    if latest_status == "resolved" or "resolved" in title or "has been resolved" in desc.lower():
        return "resolved"

    combined = f"{title} {desc}".lower()
    if any(k in combined for k in ("disruption", "outage", "unavailable", "critical", "down")):
        return "critical"
    if any(k in combined for k in ("degradation", "elevated error", "major", "data loss")):
        return "major"
    if any(k in combined for k in ("issue", "delay", "investigating", "delays", "slow")):
        return "warning"

    return "info"


def _infer_regions(item: ET.Element) -> List[str]:
    combined = _content(item)
    found: Set[str] = set()

    # Cloud region patterns (e.g. us-west-2, me-central-1)
    cloud_matches = re.findall(
        r"\b(us-west-[12]|us-east-[12]|me-central-[12]|eu-west-[123]|eu-central-[12]|ap-southeast-[123]|ap-northeast-[123])\b",
        combined,
    )
    for m in cloud_matches:
        found.add(m.lower())

    # Data center mentions: "Affected data centers: US2, US6..." or "US data centers"
    if "data center" in combined or "us data centers" in combined or re.search(r"\bus\d+\b", combined):
        found.add("us")

    if re.search(r"\b(us|united states|america)\b", combined) and not found:
        found.add("us")
    if re.search(r"\b(eu|europe)\b", combined):
        found.add("eu")
    if re.search(r"\b(apac|asia|australia|sydney)\b", combined):
        found.add("apac")

    if not found:
        found.add("global")

    return sorted(found)


def _infer_sub_services(item: ET.Element) -> List[str]:
    combined = _content(item)
    subs: List[str] = []
    for sub_id, aliases in _SUB_SERVICES:
        if any(alias in combined for alias in aliases):
            subs.append(sub_id)
    return subs


def update_approved_regions_with_wiz(regions: Set[str], csv_path: Optional[Path] = None) -> None:
    """Dynamically update approved-regions.csv with Wiz regions (append-only)."""
    if csv_path is None:
        paths = resolve_paths()
        csv_path = paths.approved_regions_csv

    try:
        rows = load_regions_csv(csv_path)
    except Exception:
        rows = []

    existing_keys = {(r.service, r.region_code) for r in rows}
    added_any = False

    regions_to_check = set(regions)
    for default_reg in DEFAULT_WIZ_REGIONS:
        regions_to_check.add(default_reg)

    for reg in sorted(regions_to_check):
        code = reg.strip().lower()
        if not code:
            continue
        key = (SERVICE, code)
        if key not in existing_keys:
            display_name = _KNOWN_REGION_NAMES.get(code)
            if not display_name:
                display_name = " ".join(part.capitalize() for part in code.replace("-", " ").split())
            rows.append(RegionRow(service=SERVICE, region_code=code, display_name=display_name))
            existing_keys.add(key)
            added_any = True

    if added_any:
        try:
            write_regions_csv(csv_path, rows)
            get_logger("wiz").info(
                f"Updated approved-regions.csv with Wiz regions (regions={sorted(regions_to_check)})"
            )
        except Exception as e:
            get_logger("wiz").warning(f"Could not update approved-regions.csv: {e}")


class WizAdapter(RssAdapter):
    """Ingester for Wiz Status RSS feed."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse_severity(self, item: ET.Element) -> str:
        return _infer_severity(item)

    def parse_regions(self, item: ET.Element) -> List[str]:
        return _infer_regions(item)

    def parse_sub_services(self, item: ET.Element) -> List[str]:
        return _infer_sub_services(item)

    def parse(self, root: ET.Element) -> List[OutageEvent]:
        events = super().parse(root)
        # Dynamically discover and register regions from ingested events
        all_regions: Set[str] = set()
        for ev in events:
            for r in ev.regions:
                all_regions.add(r)
        if all_regions:
            update_approved_regions_with_wiz(all_regions)
        return events
