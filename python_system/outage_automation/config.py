from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Set

DEFAULT_BRAND_TITLE = "Outage Dashboard"


def get_brand_title(paths: Any = None) -> str:
    """Resolve the display brand title for UI, HTML templates, and payloads.

    Resolution order:
    1. OA_BRAND_TITLE env var (e.g. "TIG Outage Dashboard", "Acme NOC Dashboard")
    2. OA_BRAND_NAME env var (e.g. "TIG" -> "TIG Outage Dashboard")
    3. branding.json file in state directory or install root:
       {"brand_title": "..."} or {"brand_name": "..."}
    4. Default: "Outage Dashboard"
    """
    env_title = os.environ.get("OA_BRAND_TITLE", "").strip()
    if env_title:
        return env_title
    env_name = os.environ.get("OA_BRAND_NAME", "").strip()
    if env_name:
        return f"{env_name} Outage Dashboard"

    candidates: list[Path] = []
    if paths is not None:
        if hasattr(paths, "state_dir") and paths.state_dir:
            candidates.append(Path(paths.state_dir) / "branding.json")
        if hasattr(paths, "root") and paths.root:
            candidates.append(Path(paths.root) / "branding.json")
        if isinstance(paths, (str, Path)):
            p = Path(paths)
            candidates.append(p / "branding.json")
            candidates.append(p / "state" / "branding.json")
            candidates.append(p / "python_system" / "state" / "branding.json")

    candidates.append(Path("branding.json"))
    candidates.append(Path("python_system/state/branding.json"))

    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if data.get("brand_title"):
                        return str(data["brand_title"]).strip()
                    if data.get("brand_name"):
                        return f"{str(data['brand_name']).strip()} Outage Dashboard"
            except Exception:
                pass

    return DEFAULT_BRAND_TITLE



DEFAULT_SERVICES = (
    "azure",
    "aws",
    "cloudflare",
    "connectwise",
    "m365",
    "zscaler",
    "chorus",
    "googleworkspace",
    "vector",
    "atlassian",
    "wiz",
    "xero",
    "halopsa",
    "ninjaone",
    "duo",
    "kaseya",
    "enable",
    "mimecast",
    "lightwire",
    "myob",
    "rewst",
    "crowdstrike",
    "kordia",
    "pia",
    "nbn",
    "avepoint",
    "wasabi",
    "tenable",
    "eset",
    "googleai",
)


@dataclass(frozen=True)
class RegionRules:
    by_service: Dict[str, Set[str]]


def load_region_rules(csv_path: Path) -> RegionRules:
    by_service: Dict[str, Set[str]] = {}
    if not csv_path.exists():
        return RegionRules(by_service={})

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            svc = str(row.get("Service") or "").strip().lower()
            region = str(row.get("Region Code") or "").strip().lower()
            if not svc or not region:
                continue
            by_service.setdefault(svc, set()).add(region)
    return RegionRules(by_service=by_service)
