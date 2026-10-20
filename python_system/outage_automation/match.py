from __future__ import annotations

from typing import Dict, Iterable, List, Set, Tuple

from .model import Customer, OutageEvent


def _normalize_regions(regions: Iterable[str]) -> Set[str]:
    return {r.strip().lower() for r in regions if r and r.strip()}


def _normalize_sub_services(subs: Iterable[str]) -> Set[str]:
    return {s.strip().lower() for s in subs if s and str(s).strip()}


def region_match(outage_regions: List[str], customer_regions: List[str]) -> bool:
    o = _normalize_regions(outage_regions)
    c = _normalize_regions(customer_regions)
    if not o or not c:
        return False
    if "global" in o:
        return True
    if "global" in c:
        return True
    return len(o.intersection(c)) > 0


def sub_service_match(outage_subs: List[str], customer_subs: List[str]) -> bool:
    """Decide whether an outage's sub_services satisfy a customer's
    sub_services filter.

    Rule (TODO #21): if EITHER side is empty, no sub-services check is
    applied (the customer opted in to "all sub-services" for that
    service, or the outage doesn't carry a sub-service axis). Only when
    BOTH sides are non-empty do we require the intersection to be
    non-empty. This keeps existing customer records (no sub_services
    field) working without modification.
    """
    o = _normalize_sub_services(outage_subs)
    c = _normalize_sub_services(customer_subs)
    if not o or not c:
        return True
    return len(o.intersection(c)) > 0


def match_customers(outage: OutageEvent, customers: List[Customer]) -> List[Tuple[Customer, List[str]]]:
    svc = outage.service.strip().lower()
    if not svc:
        return []
    matches: List[Tuple[Customer, List[str]]] = []
    o_regions = outage.regions or []
    o_subs = outage.sub_services or []
    for c in customers:
        regions = (c.subscriptions or {}).get(svc) or []
        if not regions:
            continue
        if not region_match(o_regions, regions):
            continue
        # New: also require sub_services to overlap (when both sides carry
        # sub-services). See sub_service_match for the rule. (TODO #21)
        c_subs = (c.sub_services or {}).get(svc) or []
        if not sub_service_match(o_subs, c_subs):
            continue
        c_regions_set = {r.lower() for r in regions}
        matched = [r for r in o_regions if r.lower() in c_regions_set or r.lower() == "global"]
        matches.append((c, matched or o_regions))
    return matches

