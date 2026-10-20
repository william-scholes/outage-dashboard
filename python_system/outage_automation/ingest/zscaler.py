"""Zscaler ingester (RSS 2.0).

Source: Zscaler Trust Portal RSS feed (URL configurable via
`OA_ZSCALER_URL`).

Severity inference (matches the legacy JS parser):
  - content (title + description) contains resolved / completed → resolved
  - content contains outage / unavailable / down / critical → critical
  - content contains degraded / degradation / issue / impact / delay → warning
  - content contains maintenance → info
  - else info

Region inference — Zscaler has no real geographic regions in the
source feed, so `regions` is always `["global"]`. (Zscaler sub-product codes ZIA / ZPA / ZDX / ZCC are no longer parsed from text)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import re
import json
from typing import List, Set, Tuple, Optional

from .rss import RssAdapter, _find_child, _text
from .base import fetch_url
from ..paths import resolve_paths
from ..dashboard.regions_store import load_regions_csv, write_regions_csv, RegionRow
from ..logging_setup import get_logger

SERVICE = "zscaler"
DEFAULT_URL = "https://trust.zscaler.com/rss-feed"
URL_ENV_VAR = "OA_ZSCALER_URL"

# Severity keyword maps (content lowercased).
_SEV_RESOLVED = ("resolved", "completed")
_SEV_CRITICAL = ("outage", "unavailable", "down", "critical")
_SEV_WARNING = ("degraded", "degradation", "issue", "impact", "delay")
_SEV_INFO_MAINT = ("maintenance",)


def _content(item: ET.Element) -> str:
    title = _text(_find_child(item, "title"))
    desc = _text(_find_child(item, "description"))
    return f"{title} {desc}".lower()


def _infer_severity(item: ET.Element) -> str:
    # Check custom status/ResolvedDate elements first
    status_el = _find_child(item, "Status")
    status = _text(status_el).strip().lower() if status_el is not None else ""
    resolved_date_el = _find_child(item, "ResolvedDate")
    resolved_date = _text(resolved_date_el).strip() if resolved_date_el is not None else ""

    if resolved_date or status in ("resolved", "completed", "cancelled"):
        return "resolved"

    body = _content(item)
    if any(w in body for w in _SEV_RESOLVED):
        return "resolved"

    categories = [c.text.lower() for c in item.findall("category") if c.text]
    link_el = _find_child(item, "link")
    link = _text(link_el).lower() if link_el is not None else ""

    # Check category tags first (modern Zscaler unified feed format)
    if "scheduled maintenance" in categories:
        return "info"
    if "advisory" in categories:
        return "info"
    if "recent incident" in categories:
        if any(w in body for w in _SEV_CRITICAL):
            return "critical"
        return "warning"

    # Fall back to URL or keyword checks (for legacy mock files or feeds without categories)
    if "/maintenance/" in link or "/advisories/" in link:
        return "info"
    if "/incidents/" in link:
        if any(w in body for w in _SEV_CRITICAL):
            return "critical"
        if any(w in body for w in _SEV_INFO_MAINT):
            return "info"
        return "warning"

    # Last resort fallback (pure keyword checking)
    if any(w in body for w in _SEV_CRITICAL):
        return "critical"
    if any(w in body for w in _SEV_WARNING):
        return "warning"
    return "info"


ZSCALER_CONFIG_URLS = [
    "https://config.zscaler.com/api/zscaler.net/cenr/json",
    "https://config.zscaler.com/api/zscalertwo.net/cenr/json",
    "https://config.zscaler.com/api/zscloud.net/cenr/json",
    "https://config.zscaler.com/api/zscalerthree.net/cenr/json"
]

DEFAULT_APAC_CITIES = {
    "Auckland II", "Canberra I", "Melbourne II", "Perth I", "Sydney III", "Sydney V"
}

DEFAULT_NON_APAC_CITIES = {
    "Abu Dhabi II", "Amsterdam II", "Amsterdam III", "Brussels II", "Capetown IV",
    "Copenhagen II", "Dubai I", "Dusseldorf I", "Frankfurt IV", "Frankfurt VI",
    "Helsinki I", "Johannesburg III", "Kingdom of Saudi Arabia I", "Lagos II",
    "Lagos III", "Lisbon I", "London III", "London V", "Madrid III", "Madrid IV",
    "Manchester I", "Manchester II", "Marseille I", "Milan III", "Milan IV",
    "Munich I", "Oslo III", "Paris II", "Paris IV", "Rouen I", "Stockholm III",
    "Tel Aviv II", "Vienna I", "Warsaw II", "Zurich",
    "Atlanta II", "Atlanta III", "Bogota I", "Bogota II", "Boston I", "Buenos Aires I",
    "Buenos Aires II", "Chicago", "Chicago II", "Dallas I", "Dallas II", "Denver III",
    "Honolulu I", "Los Angeles", "Los Angeles II", "Mexico City I", "Mexico City II",
    "Miami III", "Miami IV", "Montreal I", "New York III", "New York IV", "Nuevo Laredo I",
    "Rio de Janeiro I", "San Francisco IV", "Santiago I", "Santiago II", "Sao Paulo",
    "Sao Paulo II", "Sao Paulo IV", "Seattle", "Toronto III", "Vancouver I",
    "Washington", "Washington DC", "Washington DC IV",
    # Non-approved APAC cities (mapped to [] to prevent spamming/global fallback)
    "Beijing", "Beijing III", "Chennai", "Chennai II", "Chennai III", "Hong Kong III",
    "Hong Kong IV", "Hyderabad I", "Jakarta I", "Kolkata I", "Kuala Lumpur I",
    "Kuala Lumpur II", "Mumbai IV", "Mumbai VI", "Mumbai VII", "New Delhi I",
    "Osaka I", "Seoul I", "Shanghai", "Shanghai II", "Singapore IV", "Singapore V",
    "Taipei", "Tianjin", "Tokyo IV", "Tokyo V", "Tokyo VI"
}

NON_APAC_KEYWORDS = ("amer", "emea", "europe", "americas", "latin america", "middle east", "africa")


def fetch_zscaler_cities() -> Tuple[Set[str], Set[str]]:
    """Fetch Zscaler cities dynamically from the config APIs.
    
    Returns:
        Tuple[apac_cities, non_apac_cities]
    """
    apac_cities: Set[str] = set()
    non_apac_cities: Set[str] = set()
    for url in ZSCALER_CONFIG_URLS:
        try:
            domain = url.split("/")[4]
            data_bytes = fetch_url(url)
            data = json.loads(data_bytes.decode("utf-8"))
            domain_data = data.get(domain, {})
            for continent_key, continent_data in domain_data.items():
                if not continent_key.startswith("continent : "):
                    continue
                is_apac = "APAC" in continent_key
                for key in continent_data.keys():
                    if key.startswith("city : "):
                        city = key.replace("city : ", "").strip()
                        if city:
                            if is_apac and city in DEFAULT_APAC_CITIES:
                                apac_cities.add(city)
                            else:
                                non_apac_cities.add(city)
        except Exception as e:
            log = get_logger("zscaler")
            log.warning(f"Could not fetch/parse config from {url}: {e}")
    return apac_cities, non_apac_cities


def update_approved_regions_with_zscaler(apac_cities: Set[str], csv_path: Optional[Path] = None) -> None:
    """Dynamically update approved-regions.csv with Zscaler APAC regions, pruning obsolete ones."""
    if csv_path is None:
        paths = resolve_paths()
        csv_path = paths.approved_regions_csv
    
    try:
        rows = load_regions_csv(csv_path)
    except Exception:
        rows = []
        
    allowed_zscaler_codes = {re.sub(r'[^a-z0-9]+', '-', c.lower()).strip('-') for c in apac_cities}
    allowed_zscaler_codes.add("global")
    
    new_rows = []
    removed_any = False
    for r in rows:
        if r.service == "zscaler" and r.region_code not in allowed_zscaler_codes:
            removed_any = True
            continue
        new_rows.append(r)
        
    existing_keys = {(r.service, r.region_code) for r in new_rows}
    added_any = False
    for city in sorted(apac_cities):
        code = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
        if not code:
            continue
        key = ("zscaler", code)
        if key not in existing_keys:
            new_rows.append(RegionRow(service="zscaler", region_code=code, display_name=city))
            existing_keys.add(key)
            added_any = True
            
    if added_any or removed_any:
        try:
            write_regions_csv(csv_path, new_rows)
            log = get_logger("zscaler")
            log.info(f"Updated approved-regions.csv with Zscaler APAC regions (added={added_any}, removed={removed_any})")
        except Exception as e:
            log = get_logger("zscaler")
            log.warning(f"Failed to write updated approved-regions.csv: {e}")


class ZscalerAdapter(RssAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        super().__init__(url)
        self.apac_cities = set(DEFAULT_APAC_CITIES)
        self.non_apac_cities = set(DEFAULT_NON_APAC_CITIES)

    def fetch(self, state_dir=None) -> IngestResult:
        # Fetch Zscaler config info to update region mappings dynamically.
        try:
            apac, non_apac = fetch_zscaler_cities()
            if apac:
                self.apac_cities.update(apac)
            if non_apac:
                self.non_apac_cities.update(non_apac)
        except Exception as e:
            log = get_logger("zscaler")
            log.warning(f"Failed to fetch dynamic Zscaler cities: {e}")

        # Update approved-regions.csv on disk.
        try:
            if state_dir is not None:
                root_path = state_dir.parent if state_dir.name == "state" else state_dir
                target_csv = resolve_paths(root=str(root_path)).approved_regions_csv
            else:
                target_csv = resolve_paths().approved_regions_csv
            update_approved_regions_with_zscaler(self.apac_cities, csv_path=target_csv)
        except Exception as e:
            log = get_logger("zscaler")
            log.warning(f"Failed to dynamically update approved regions list: {e}")

        return super().fetch(state_dir=state_dir)

    def parse_severity(self, item: ET.Element) -> str:
        return _infer_severity(item)

    def parse_regions(self, item: ET.Element) -> List[str]:
        body = _content(item)

        # 1. Exact APAC matches
        exact_apac = set()
        for city in self.apac_cities:
            pattern = r'\b' + re.escape(city.lower()) + r'\b'
            if re.search(pattern, body):
                exact_apac.add(city)

        # 2. Base APAC matches (only if no exact APAC matches)
        base_apac = set()
        if not exact_apac:
            for city in self.apac_cities:
                base_city = re.sub(r'\s+[IVXLCDM]+$', '', city, flags=re.IGNORECASE)
                pattern = r'\b' + re.escape(base_city.lower()) + r'\b'
                if re.search(pattern, body):
                    base_apac.add(city)

        # 3. Exact non-APAC matches
        exact_non_apac = set()
        for city in self.non_apac_cities:
            pattern = r'\b' + re.escape(city.lower()) + r'\b'
            if re.search(pattern, body):
                exact_non_apac.add(city)

        # 4. Base non-APAC matches (only if no exact non-APAC matches)
        base_non_apac = set()
        if not exact_non_apac:
            for city in self.non_apac_cities:
                base_city = re.sub(r'\s+[IVXLCDM]+$', '', city, flags=re.IGNORECASE)
                pattern = r'\b' + re.escape(base_city.lower()) + r'\b'
                if re.search(pattern, body):
                    base_non_apac.add(city)

        # 5. Non-APAC region keywords
        matched_non_apac_keyword = False
        for kw in NON_APAC_KEYWORDS:
            if re.search(r'\b' + re.escape(kw) + r'\b', body):
                matched_non_apac_keyword = True
                break

        # Decision
        if exact_apac:
            return sorted(list({re.sub(r'[^a-z0-9]+', '-', c.lower()).strip('-') for c in exact_apac}))
        elif base_apac:
            return sorted(list({re.sub(r'[^a-z0-9]+', '-', c.lower()).strip('-') for c in base_apac}))
        elif exact_non_apac or base_non_apac or matched_non_apac_keyword:
            # Localized non-APAC incident
            return []
        else:
            # General / Global incident
            return ["global"]
