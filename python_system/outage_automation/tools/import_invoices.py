"""Import customer subscriptions from billing invoice export CSV.

Parses billing invoice CSV (Customer, Group Header, Description), maps billed
services and geolocates local telco/power circuits, and outputs a validated customers.json.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from ..config import RegionRules, load_region_rules
from ..paths import Paths, resolve_paths
from ..validate import validate_customers

# Product matching rules: (service_key, regex_patterns, default_regions)
PRODUCT_RULES: List[Tuple[str, List[str], List[str]]] = [
    ("m365", [r"office\s*365", r"m365", r"microsoft\s*365", r"powerbi", r"teams calling", r"exchange online", r"intune"], ["australia", "global", "new-zealand"]),
    ("azure", [r"\bazure\b"], ["australia-east", "australia-southeast", "global"]),
    ("aws", [r"\baws\b", r"amazon web services"], ["ap-southeast-2", "global"]),
    ("wasabi", [r"wasabi"], ["ap-southeast-2", "global"]),
    ("tenable", [r"tenable", r"vulnerability"], ["australia", "global"]),
    ("eset", [r"\beset\b"], ["australia", "global"]),
    ("crowdstrike", [r"crowdstrike", r"falcon"], ["australia", "global"]),
    ("connectwise", [r"connectwise", r"automate rmm", r"cw automate", r"\bautomate\b"], ["global"]),
    ("halopsa", [r"halopsa", r"\bhalo\b"], ["australia", "global"]),
    ("avepoint", [r"avepoint"], ["australia", "global", "new-zealand"]),
    ("cloudflare", [r"cloudflare"], ["australia", "global"]),
    ("googleworkspace", [r"google workspace", r"google apps", r"gsuite"], ["australia", "global", "new-zealand"]),
    ("atlassian", [r"atlassian", r"jira", r"confluence", r"bitbucket", r"trello"], ["global"]),
    ("kaseya", [r"kaseya", r"datto", r"itglue", r"it-glue"], ["australia", "global"]),
    ("nbn", [r"\bnbn\b"], ["australia", "global"]),
]

IGNORE_GEO_NAMES: Set[str] = {"global", "australia", "new zealand", "east", "rem"}


def slugify_customer_name(text: str) -> str:
    """Generate clean URL-safe slug from customer name."""
    s = re.sub(r"\(.*?\)", "", text)
    s = re.sub(r"\b(ltd|limited|pty|lp|inc|corporation|group|co|holdings)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "cust"


def parse_invoices_csv(
    csv_path: Path,
    approved_regions_path: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Tuple[str, str]]]]:
    """Parse invoice lines and construct Customer objects."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Invoices CSV not found: {csv_path}")

    # Load approved regions
    approved_by_svc: Dict[str, Dict[str, str]] = defaultdict(dict)
    with approved_regions_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            svc = (r.get("Service") or "").strip().lower()
            code = (r.get("Region Code") or "").strip().lower()
            display = (r.get("Display Name") or "").strip().lower()
            if svc and code:
                approved_by_svc[svc][code] = display

    chorus_approved = approved_by_svc.get("chorus", {})
    vector_approved = approved_by_svc.get("vector", {})

    with csv_path.open("r", encoding="utf-8-sig") as f:
        invoices = list(csv.DictReader(f))

    customers: Dict[str, Dict[str, Any]] = {}
    unmapped_lines_by_cust: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for inv in invoices:
        cust_raw = (inv.get("Customer") or "").strip()
        if not cust_raw:
            continue
        gh = (inv.get("Group Header") or "").strip()
        desc = (inv.get("Description") or "").strip()
        line_text = f"{gh} {desc}".lower()

        cid = slugify_customer_name(cust_raw)
        if cid not in customers:
            customers[cid] = {
                "customer_id": cid,
                "name": cust_raw,
                "subscriptions": defaultdict(set),
                "sub_services": defaultdict(set),
            }

        c_entry = customers[cid]
        matched_any = False

        # 1. Direct software / cloud products
        for svc, patterns, default_regs in PRODUCT_RULES:
            if any(re.search(p, line_text) for p in patterns):
                matched_any = True
                for r in default_regs:
                    c_entry["subscriptions"][svc].add(r)

        # 2. Zscaler
        if any(k in line_text for k in ["zscaler", "zia", "zpa", "secure private access", "cloud web security", "zero trust"]):
            matched_any = True
            c_entry["subscriptions"]["zscaler"].add("global")
            if any(k in line_text for k in ["zia", "cloud web security"]):
                c_entry["sub_services"]["zscaler"].add("zia")
            if any(k in line_text for k in ["zpa", "secure private access"]):
                c_entry["sub_services"]["zscaler"].add("zpa")

        # 3. Lightwire
        if "lightwire" in line_text or "light wire" in line_text:
            matched_any = True
            c_entry["subscriptions"]["lightwire"].add("global")
            c_entry["subscriptions"]["lightwire"].add("new-zealand")
            for lw_reg in ["waikato", "bay-of-plenty", "auckland", "hawkes-bay", "taranaki", "manawatu", "wellington", "canterbury"]:
                if lw_reg in line_text or lw_reg.replace("-", " ") in line_text:
                    c_entry["subscriptions"]["lightwire"].add(lw_reg)

        # 4. Telco, WAN, and local power circuit geolocation
        is_circuit = any(k in line_text for k in ["wan", "broadband", "fibre", "circuit", "ufb", "bs2", "bs3", "hyperfibre", "sbf", "devoli", "scada"])
        if is_circuit or "chorus" in line_text or "vector" in line_text or "enable" in line_text:
            if "christchurch" in line_text or "canterbury" in line_text:
                matched_any = True
                c_entry["subscriptions"]["enable"].add("christchurch")
                c_entry["subscriptions"]["enable"].add("canterbury")

            for code, display in chorus_approved.items():
                if code in IGNORE_GEO_NAMES or display in IGNORE_GEO_NAMES:
                    continue
                pat = r"\b" + re.escape(display) + r"\b"
                if re.search(pat, line_text) or (len(code) > 3 and re.search(r"\b" + re.escape(code) + r"\b", line_text)):
                    matched_any = True
                    c_entry["subscriptions"]["chorus"].add(code)
                    if any(f in line_text for f in ["hyperfibre", "bs2", "bs3", "ufb", "fibre"]):
                        c_entry["sub_services"]["chorus"].add("fibre")
                    if any(c in line_text for c in ["copper", "vdsl", "adsl", "dsl"]):
                        c_entry["sub_services"]["chorus"].add("copper")
                    if code in vector_approved:
                        c_entry["subscriptions"]["vector"].add(code)
                        c_entry["sub_services"]["vector"].add("power")

            for code, display in vector_approved.items():
                if code in IGNORE_GEO_NAMES or display in IGNORE_GEO_NAMES:
                    continue
                pat = r"\b" + re.escape(display) + r"\b"
                if re.search(pat, line_text) or (len(code) > 3 and re.search(r"\b" + re.escape(code) + r"\b", line_text)):
                    matched_any = True
                    c_entry["subscriptions"]["vector"].add(code)
                    c_entry["sub_services"]["vector"].add("power")
                    if code in chorus_approved:
                        c_entry["subscriptions"]["chorus"].add(code)
                        c_entry["sub_services"]["chorus"].add("fibre")

        if not matched_any:
            unmapped_lines_by_cust[cust_raw].append((gh, desc))

    final_customers: List[Dict[str, Any]] = []
    for c_data in customers.values():
        if not c_data["subscriptions"]:
            continue
        c_obj = {
            "customer_id": c_data["customer_id"],
            "name": c_data["name"],
            "subscriptions": {s: sorted(list(regs)) for s, regs in sorted(c_data["subscriptions"].items())},
        }
        if c_data["sub_services"]:
            non_empty_subs = {s: sorted(list(subs)) for s, subs in sorted(c_data["sub_services"].items()) if subs}
            if non_empty_subs:
                c_obj["sub_services"] = non_empty_subs
        final_customers.append(c_obj)

    final_customers.sort(key=lambda x: str(x["name"]))
    return final_customers, unmapped_lines_by_cust


def import_invoices_to_state(
    csv_path: Path,
    paths: Paths,
    backup: bool = True,
) -> int:
    """Execute customer import from invoices CSV."""
    rules = load_region_rules(paths.approved_regions_csv)
    customers, unmapped = parse_invoices_csv(csv_path, paths.approved_regions_csv)

    errs = validate_customers(customers, rules=rules)
    if errs:
        sys.stderr.write(f"Validation failed for generated customer records ({len(errs)} errors):\n")
        for e in errs[:10]:
            sys.stderr.write(f" - {e}\n")
        return 1

    if backup and paths.customers_path.exists():
        backup_path = paths.customers_path.with_name("customers.backup.json")
        backup_path.write_text(paths.customers_path.read_text(encoding="utf-8"), encoding="utf-8")
        sys.stdout.write(f"Backed up existing customers to {backup_path}\n")

    paths.customers_path.write_text(json.dumps(customers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(f"Successfully imported {len(customers)} real customer records into {paths.customers_path}\n")
    return 0
