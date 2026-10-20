"""Vector (NZ) power & electricity network outages ingester.

Source: Vector Outage Centre API (https://outage-centre.api.vector.co.nz/v1/outages/planned-outages/future)
Outage Map: https://help.vector.co.nz/map
Severity inference:
  - Cancelled or Completed status -> resolved
  - In progress / active within outage window -> warning
  - Scheduled / Planning Approved -> info
Region inference:
  - Suburb name normalized to kebab-case, or 'global' if omitted.
Sub-services:
  - 'power', 'hv' (High Voltage), 'lv' (Low Voltage)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import IngestError, IngestResult, ProviderAdapter, fetch_url, maybe_capture_raw
from ..dashboard.regions_store import RegionRow, load_regions_csv, write_regions_csv
from ..logging_setup import get_logger
from ..model import OutageEvent
from ..paths import resolve_paths
from ..validate import parse_datetime

SERVICE = "vector"
DEFAULT_URL = "https://outage-centre.api.vector.co.nz/v1/outages/planned-outages/future"
URL_ENV_VAR = "OA_VECTOR_URL"

DEFAULT_APIKEY = "378657d8-674d-4e20-9fcc-4e23fdfdf224"
APIKEY_ENV_VAR = "OA_VECTOR_APIKEY"

DEFAULT_SUBURBS = {
    "Auckland Central",
    "Te Atatu Peninsula",
    "Takanini",
    "Silverdale",
    "Henderson",
    "Glen Eden",
    "Remuera",
    "Penrose",
    "Manurewa",
    "Papakura",
    "Albany",
    "Takapuna",
}


def _clean_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def update_approved_regions_with_vector(suburbs: Set[str], csv_path: Optional[Path] = None) -> None:
    """Dynamically update approved-regions.csv with Vector suburbs (append-only)."""
    if csv_path is None:
        paths = resolve_paths()
        csv_path = paths.approved_regions_csv

    try:
        rows = load_regions_csv(csv_path)
    except Exception:
        rows = []

    existing_keys = {(r.service, r.region_code) for r in rows}
    added_any = False

    # Ensure baseline global, new-zealand, and auckland rows exist
    baseline = [
        ("global", "Global"),
        ("new-zealand", "New Zealand"),
        ("auckland", "Auckland"),
    ]
    for code, display in baseline:
        key = (SERVICE, code)
        if key not in existing_keys:
            rows.append(RegionRow(service=SERVICE, region_code=code, display_name=display))
            existing_keys.add(key)
            added_any = True

    suburbs_to_check = set(suburbs)
    for s in DEFAULT_SUBURBS:
        suburbs_to_check.add(s)

    for suburb in sorted(suburbs_to_check):
        code = _clean_slug(suburb)
        if not code:
            continue
        key = (SERVICE, code)
        if key not in existing_keys:
            display_name = " ".join(word.capitalize() for word in suburb.replace("-", " ").split())
            rows.append(RegionRow(service=SERVICE, region_code=code, display_name=display_name))
            existing_keys.add(key)
            added_any = True

    if added_any:
        try:
            write_regions_csv(csv_path, rows)
            log = get_logger(SERVICE)
            log.info("Updated approved-regions.csv with Vector suburbs (added new)")
        except Exception as e:
            log = get_logger(SERVICE)
            log.warning(f"Failed to write updated approved-regions.csv: {e}")


class VectorAdapter(ProviderAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None, apikey: Optional[str] = None) -> None:
        env_url = os.environ.get(self.url_env_var) if self.url_env_var else None
        self.url = url or env_url or self.default_url
        self.apikey = apikey or os.environ.get(APIKEY_ENV_VAR) or DEFAULT_APIKEY

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {
            "apikey": self.apikey,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; VectorOutageParser/1.0)",
        }

        try:
            raw_data = fetch_url(self.url, headers=headers)
        except Exception as e:
            result.error = str(e)
            return result

        result.raw_bytes = len(raw_data)

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

        events: List[OutageEvent] = []
        seen_suburbs: Set[str] = set()

        for inc in payload:
            if not isinstance(inc, dict):
                continue
            ev = self.parse_incident(inc)
            if ev:
                events.append(ev)
            suburb = inc.get("suburb")
            if suburb and str(suburb).strip():
                seen_suburbs.add(str(suburb).strip())

        result.events = events

        # Sync seen suburbs to approved-regions.csv
        try:
            if state_dir is not None:
                root_path = state_dir.parent if state_dir.name == "state" else state_dir
                target_csv = resolve_paths(root=str(root_path)).approved_regions_csv
            else:
                target_csv = resolve_paths().approved_regions_csv
            update_approved_regions_with_vector(seen_suburbs, csv_path=target_csv)
        except Exception as e:
            log = get_logger(SERVICE)
            log.warning(f"Failed to dynamically update approved regions: {e}")

        return result

    def parse_incident(self, inc: Dict[str, Any]) -> Optional[OutageEvent]:
        raw_id = str(
            inc.get("switchingProposalNumber")
            or inc.get("plannedOutageId")
            or ""
        ).strip()
        if not raw_id:
            return None

        suburb = str(inc.get("suburb") or "").strip()
        suburb_slug = _clean_slug(suburb) if suburb else ""

        if suburb_slug:
            regions = [suburb_slug]
        else:
            regions = ["global"]

        outage_type = str(inc.get("type") or "HV").upper()
        sub_services = ["power"]
        if outage_type in ("HV", "LV"):
            sub_services.append(outage_type.lower())

        status_obj = inc.get("status")
        status_desc = str(
            (status_obj.get("statusDescription") if isinstance(status_obj, dict) else "")
            or inc.get("status")
            or ""
        ).strip()

        change_reason = inc.get("changeReason")
        reason_desc = str(
            (change_reason.get("reasonDescription") if isinstance(change_reason, dict) else "")
            or inc.get("changeReason")
            or "Planned Maintenance"
        ).strip()

        # Title
        if suburb and reason_desc:
            title = f"{suburb} - {reason_desc} ({raw_id})"
        elif suburb:
            title = f"{suburb} - Planned Power Outage ({raw_id})"
        else:
            title = f"Vector Planned Power Outage ({raw_id})"

        # Description with timing details
        blocks = inc.get("plannedOutageBlocks") or []
        desc_lines = [
            f"Reference: {raw_id}",
            f"Suburb: {suburb or 'Auckland'}",
            f"Type: {outage_type}",
            f"Status: {status_desc or 'Planned'}",
            f"Reason: {reason_desc}",
        ]

        now_dt = datetime.now(timezone.utc)
        is_active_window = False
        all_blocks_ended = True if blocks else False

        for b in blocks:
            if not isinstance(b, dict):
                continue
            start_raw = b.get("advertisedStartDateTime")
            end_raw = b.get("advertisedEndDateTime")
            desc_lines.append(f"Window: {start_raw or 'TBD'} to {end_raw or 'TBD'}")
            
            s_dt = parse_datetime(start_raw) if start_raw else None
            e_dt = parse_datetime(end_raw) if end_raw else None
            
            if s_dt and e_dt:
                if s_dt <= now_dt <= e_dt:
                    is_active_window = True
                if now_dt < e_dt:
                    all_blocks_ended = False

        description = "\n".join(desc_lines)

        # Severity
        cancelled = inc.get("cancelledReason") or (blocks and any(b.get("cancelledReason") for b in blocks if isinstance(b, dict)))
        status_lower = status_desc.lower()

        if cancelled or status_lower in ("cancelled", "completed") or all_blocks_ended:
            severity = "resolved"
        elif is_active_window or status_lower in ("work in progress", "in progress", "active"):
            severity = "warning"
        else:
            severity = "info"

        pub_raw = inc.get("updatedAt") or (blocks[0].get("advertisedStartDateTime") if blocks and isinstance(blocks[0], dict) else None)
        pub_dt = parse_datetime(pub_raw) if pub_raw else now_dt
        if pub_dt is None:
            pub_dt = now_dt

        link = "https://help.vector.co.nz/map"

        return OutageEvent(
            raw_id=raw_id,
            service=self.name,
            title=title,
            description=description,
            severity=severity,
            regions=regions,
            sub_services=sub_services,
            link=link,
            published_at=pub_dt,
        )
