from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from .config import DEFAULT_SERVICES
from .validate import parse_datetime


_SEVERITIES = {"critical", "warning", "info", "resolved"}
_SERVICES = set(DEFAULT_SERVICES)


def iso_utc(dt: datetime) -> str:
    """Return an ISO-8601 UTC string with a trailing 'Z'.

    Truncates microseconds for consistency with the rest of the system.
    """
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_str(v: Any) -> str:
    return str(v or "").strip()


def _normalize_service(v: Any) -> str:
    return _clean_str(v).lower()


def _normalize_severity(v: Any) -> str:
    s = _clean_str(v).lower()
    if s in _SEVERITIES:
        return s
    if s in ("warn",):
        return "warning"
    if s in ("crit",):
        return "critical"
    return s


def _normalize_regions_list(v: Any) -> List[str]:
    if not isinstance(v, list):
        return []
    out: List[str] = []
    for x in v:
        s = _clean_str(x)
        if s:
            out.append(s.lower())
    return out


def _normalize_sub_services_list(v: Any) -> List[str]:
    """Normalize the per-provider sub-service axis (e.g. zia/zpa/zdx/zcc
    for Zscaler). Same rules as regions: list of non-empty lowercased
    strings, deduped, original order preserved.
    """
    if not isinstance(v, list):
        return []
    out: List[str] = []
    seen = set()
    for x in v:
        s = _clean_str(x).lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


@dataclass(frozen=True)
class OutageEvent:
    raw_id: str
    service: str
    title: str
    description: str
    severity: str
    regions: List[str]
    sub_services: List[str]
    link: str
    published_at: datetime

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OutageEvent":
        published = d.get("published_at") or d.get("published")
        dt = parse_datetime(published)
        if dt is None:
            raise ValueError("invalid published_at")
        return OutageEvent(
            raw_id=_clean_str(d.get("raw_id")),
            service=_normalize_service(d.get("service")),
            title=_clean_str(d.get("title")),
            description=_clean_str(d.get("description")),
            severity=_normalize_severity(d.get("severity")),
            regions=_normalize_regions_list(d.get("regions")),
            sub_services=_normalize_sub_services_list(d.get("sub_services")),
            link=_clean_str(d.get("link")),
            published_at=dt,
        )


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    subscriptions: Dict[str, List[str]]
    # Per-service sub-services (e.g. Zscaler's zia/zpa/zdx/zcc).
    # `{}` (the default) means "no sub-service filter": the customer
    # matches every outage for the service, regardless of sub-service.
    # Used by `match.py:match_customers` to refine the Zscaler match
    # so a customer subscribed to "ZIA only" doesn't get paged about
    # a ZPA outage. (TODO #21)
    sub_services: Dict[str, List[str]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Customer":
        subs_raw = d.get("subscriptions") or {}
        subs: Dict[str, List[str]] = {}
        if isinstance(subs_raw, dict):
            for k, v in subs_raw.items():
                svc = str(k).strip().lower()
                if not svc:
                    continue
                if isinstance(v, list):
                    subs[svc] = [str(x).strip().lower() for x in v if str(x).strip()]
                else:
                    subs[svc] = []
        sub_services_raw = d.get("sub_services") or {}
        sub_services: Dict[str, List[str]] = {}
        if isinstance(sub_services_raw, dict):
            for k, v in sub_services_raw.items():
                svc = str(k).strip().lower()
                if not svc:
                    continue
                if isinstance(v, list):
                    sub_services[svc] = _normalize_sub_services_list(v)
                else:
                    sub_services[svc] = []
        return Customer(
            customer_id=_clean_str(d.get("customer_id") or d.get("customerId")),
            name=_clean_str(d.get("name") or d.get("customerName")),
            subscriptions=subs,
            sub_services=sub_services,
        )


@dataclass
class AlertHistoryRow:
    raw_id: str
    service: str
    severity: str
    title: str
    description: str
    regions: List[str]
    sub_services: List[str]
    link: str
    published_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "raw_id": self.raw_id,
            "service": self.service,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "regions": list(self.regions),
            "sub_services": list(self.sub_services),
            "link": self.link,
            "published_at": iso_utc(self.published_at),
            "last_seen_at": iso_utc(self.last_seen_at),
            "expires_at": iso_utc(self.expires_at),
        }
        for k, v in self.extra.items():
            d[k] = v
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AlertHistoryRow | None":
        try:
            published = parse_datetime(d.get("published_at"))
            last_seen = parse_datetime(d.get("last_seen_at")) or published
            expires = parse_datetime(d.get("expires_at"))
            if published is None or expires is None:
                return None
            return AlertHistoryRow(
                raw_id=_clean_str(d.get("raw_id")),
                service=_normalize_service(d.get("service")),
                severity=_normalize_severity(d.get("severity")),
                title=_clean_str(d.get("title")),
                description=_clean_str(d.get("description")),
                regions=_normalize_regions_list(d.get("regions")),
                sub_services=_normalize_sub_services_list(d.get("sub_services")),
                link=_clean_str(d.get("link")),
                published_at=published,
                last_seen_at=last_seen or published,
                expires_at=expires,
                extra={k: v for k, v in d.items() if k not in {
                    "raw_id", "service", "severity", "title", "description",
                    "regions", "sub_services", "link",
                    "published_at", "last_seen_at", "expires_at",
                }},
            )
        except Exception:
            return None


def is_valid_service(service: str) -> bool:
    return service in _SERVICES
