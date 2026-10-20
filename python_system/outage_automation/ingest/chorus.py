"""Chorus (NZ) internet outages ingester.

Source: Chorus API (https://api.chorus.co.nz/events/v3/incidents)
Severity inference: based on total customers impacted (total_impact) and incident status.
Region inference: exchange name normalized to kebab-case.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .base import IngestError, IngestResult, ProviderAdapter, fetch_url, maybe_capture_raw
from ..dashboard.regions_store import RegionRow, load_regions_csv, write_regions_csv
from ..logging_setup import get_logger
from ..model import OutageEvent
from ..paths import resolve_paths

SERVICE = "chorus"
DEFAULT_URL = "https://api.chorus.co.nz/events/v3/incidents?includePolygons=false"
URL_ENV_VAR = "OA_CHORUS_URL"

# Public client credentials (Basic auth) from Chorus public site config
CLIENT_ID = "cad522f8cd6f4dbe91742c92b957f8ec"
CLIENT_SECRET = "cc1aE6d9afcc44D48764913a4269FfD6"

DEFAULT_EXCHANGES = {
    "Hibiscus Coast",
    "Avondale",
    "Kelburn",
    "Taneatua",
    "Ngongotaha"
}


def _basic_auth_header() -> str:
    token = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return f"Basic {token}"


def update_approved_regions_with_chorus(exchanges: Set[str], csv_path: Optional[Path] = None) -> None:
    """Dynamically update approved-regions.csv with Chorus exchanges (append-only)."""
    if csv_path is None:
        paths = resolve_paths()
        csv_path = paths.approved_regions_csv
    
    try:
        rows = load_regions_csv(csv_path)
    except Exception:
        rows = []
        
    existing_keys = {(r.service, r.region_code) for r in rows}
    added_any = False
    
    # Include fallback default exchanges to ensure they are seeded
    exchanges_to_check = set(exchanges)
    for default_exch in DEFAULT_EXCHANGES:
        exchanges_to_check.add(default_exch)
        
    for exchange in sorted(exchanges_to_check):
        code = re.sub(r'[^a-z0-9]+', '-', exchange.lower()).strip('-')
        if not code:
            continue
        key = ("chorus", code)
        if key not in existing_keys:
            # Capitalize display name
            display_name = " ".join(word.capitalize() for word in exchange.replace("-", " ").split())
            rows.append(RegionRow(service="chorus", region_code=code, display_name=display_name))
            existing_keys.add(key)
            added_any = True
            
    if added_any:
        try:
            write_regions_csv(csv_path, rows)
            log = get_logger("chorus")
            log.info(f"Updated approved-regions.csv with Chorus exchanges (added new)")
        except Exception as e:
            log = get_logger("chorus")
            log.warning(f"Failed to write updated approved-regions.csv: {e}")

COPPER_ROLES = {"DSL", "FRE_CABLEPAIR", "FRE_CABLE", "FRE_TERMINAL", "FRE_CHORUS_EN"}


def infer_access_mediums(inc: Dict[str, Any]) -> List[str]:
    """Infer access medium sub-services (fibre, copper) from incident sites and descriptions.

    Chorus frontend (Rl function) uses site roles:
      - GPON -> fibre
      - DSL, FRE_CABLEPAIR, FRE_CABLE, FRE_TERMINAL, FRE_CHORUS_EN -> copper
    Fallback to description text if site roles are absent or ambiguous.
    """
    mediums: Set[str] = set()
    sites = inc.get("sites") or []
    for s in sites:
        if not isinstance(s, dict):
            continue
        role = str(s.get("role") or "").strip().upper()
        if role == "GPON":
            mediums.add("fibre")
        elif role in COPPER_ROLES:
            mediums.add("copper")

    if not mediums:
        desc = f"{inc.get('description', '')} {inc.get('add_desc', '')}".lower()
        if "fibre" in desc or "fiber" in desc:
            mediums.add("fibre")
        elif any(k in desc for k in ["copper", "dsl", "broadband"]):
            mediums.add("copper")

    return sorted(list(mediums))


class ChorusAdapter(ProviderAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        env_url = (
            os.environ.get(self.url_env_var) if self.url_env_var else None
        )
        self.url = url or env_url or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Chorus API requires gzip Accept-Encoding and basic authentication
        headers = {
            "Authorization": _basic_auth_header(),
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; chorus-outage-puller/1.0)",
        }

        try:
            raw_data = fetch_url(self.url, headers=headers)
        except Exception as e:
            result.error = str(e)
            return result

        result.raw_bytes = len(raw_data)

        # Decompress gzip response if it has the gzip magic prefix
        if raw_data.startswith(b'\x1f\x8b'):
            try:
                raw_data = gzip.decompress(raw_data)
            except Exception as e:
                result.error = f"Failed to decompress gzip response: {e}"
                return result

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

        events = []
        seen_exchanges = set()
        for inc in payload:
            if not isinstance(inc, dict):
                continue
            ev = self.parse_incident(inc)
            if ev:
                events.append(ev)
            exchange = inc.get("exchange")
            if exchange and str(exchange).strip():
                seen_exchanges.add(str(exchange).strip())
            elif ev and ev.regions:
                for r in ev.regions:
                    if r != "global":
                        seen_exchanges.add(r)

        result.events = events

        # Sync seen exchanges to approved-regions.csv (always including default seeds)
        try:
            if state_dir is not None:
                root_path = state_dir.parent if state_dir.name == "state" else state_dir
                target_csv = resolve_paths(root=str(root_path)).approved_regions_csv
            else:
                target_csv = resolve_paths().approved_regions_csv
            update_approved_regions_with_chorus(seen_exchanges, csv_path=target_csv)
        except Exception as e:
            log = get_logger("chorus")
            log.warning(f"Failed to dynamically update approved regions: {e}")

        return result

    def parse_incident(self, inc: Dict[str, Any]) -> Optional[OutageEvent]:
        ref = inc.get("reference")
        if not ref:
            return None

        # Access medium inference based on Chorus site roles and description fallback
        sub_services = infer_access_mediums(inc)


        exchange = inc.get("exchange")
        if exchange and str(exchange).strip():
            exchange_clean = str(exchange).strip()
            region_code = re.sub(r'[^a-z0-9]+', '-', exchange_clean.lower()).strip('-')
            regions = [region_code]
        else:
            regions = ["global"]

        title_desc = inc.get("description", "").strip()
        title = f"{exchange} - {title_desc}" if exchange else (title_desc or "Chorus Outage")

        # Build description from updates
        desc_parts = []
        add_desc = inc.get("add_desc")
        if add_desc and str(add_desc).strip():
            desc_parts.append(str(add_desc).strip())

        updates = inc.get("updates") or []
        if updates and not add_desc:
            for up in updates:
                ts = up.get("timestamp") or ""
                udesc = up.get("description") or ""
                desc_parts.append(f"{ts}\n{udesc}")

        description = "\n\n".join(desc_parts) if desc_parts else title_desc

        # Determine severity based on sites impact sum
        sites = inc.get("sites") or []
        total_impact = sum(s.get("impact") or 0 for s in sites if isinstance(s, dict))

        status = str(inc.get("status") or "").strip().lower()
        rest_time = inc.get("rest_time")

        is_resolved = False
        if status in ("r", "resolved", "restored"):
            is_resolved = True
        elif rest_time:
            try:
                rest_dt = datetime.fromtimestamp(float(rest_time), tz=timezone.utc)
                if datetime.now(timezone.utc) >= rest_dt:
                    is_resolved = True
            except Exception:
                pass

        if is_resolved:
            severity = "resolved"
        elif total_impact >= 100:
            severity = "critical"
        elif total_impact >= 10:
            severity = "warning"
        else:
            severity = "info"

        published_raw = inc.get("start_time")
        try:
            published_at = datetime.fromtimestamp(float(published_raw), tz=timezone.utc) if published_raw else datetime.now(timezone.utc)
        except Exception:
            published_at = datetime.now(timezone.utc)

        link = "https://www.chorus.co.nz/optimise/internet-outages-map"

        return OutageEvent(
            raw_id=ref,
            service=self.name,
            title=title,
            description=description or title_desc or title or "(no description)",
            severity=severity,
            regions=regions,
            sub_services=sub_services,
            link=link,
            published_at=published_at,
        )
