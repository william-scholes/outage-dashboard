from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_SERVICES, RegionRules


_SEV_ALLOWED = {"critical", "warning", "info", "resolved"}
_SEV_FROM_IMPACT = {
    "critical": "critical",
    "major": "critical",
    "minor": "warning",
    "none": "info",
}
_NONEMPTY_RE = re.compile(r"\S")


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 timestamps, including `Z` and naive datetimes.

    Returns a timezone-aware datetime in UTC, or None when the input cannot
    be parsed. Accepts strings, ints/floats (epoch seconds), and existing
    datetime objects.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
            except (ValueError, TypeError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _has_text(v: Any) -> bool:
    return bool(v) and bool(_NONEMPTY_RE.search(str(v)))


def validate_outage_event(d: Dict[str, Any], idx: int) -> List[ValidationError]:
    errs: List[ValidationError] = []

    raw_id = str(d.get("raw_id") or "").strip()
    if not raw_id:
        errs.append(ValidationError(path=f"[{idx}].raw_id", message="required"))

    service = str(d.get("service") or "").strip().lower()
    if not service:
        errs.append(ValidationError(path=f"[{idx}].service", message="required"))
    elif service not in DEFAULT_SERVICES:
        errs.append(
            ValidationError(
                path=f"[{idx}].service",
                message=f"unknown service '{service}' (allowed: {', '.join(sorted(DEFAULT_SERVICES))})",
            )
        )

    title = str(d.get("title") or "").strip()
    if not title:
        errs.append(ValidationError(path=f"[{idx}].title", message="required"))

    severity = str(d.get("severity") or "").strip().lower()
    if not severity:
        errs.append(ValidationError(path=f"[{idx}].severity", message="required"))
    elif severity not in _SEV_ALLOWED:
        errs.append(
            ValidationError(
                path=f"[{idx}].severity",
                message=f"must be one of {', '.join(sorted(_SEV_ALLOWED))}",
            )
        )

    regions = d.get("regions")
    if not isinstance(regions, list):
        errs.append(ValidationError(path=f"[{idx}].regions", message="must be a list"))
    elif len([x for x in regions if str(x).strip()]) == 0:
        errs.append(ValidationError(path=f"[{idx}].regions", message="must be a non-empty list"))

    link = str(d.get("link") or "").strip()
    if not link:
        errs.append(ValidationError(path=f"[{idx}].link", message="required"))

    published = d.get("published_at") or d.get("published")
    if parse_datetime(published) is None:
        errs.append(ValidationError(path=f"[{idx}].published_at", message="must be ISO-8601 datetime"))

    return errs


def validate_event_batch(batch: Any) -> Tuple[List[Dict[str, Any]], List[ValidationError]]:
    if not isinstance(batch, list):
        return [], [ValidationError(path="$", message="must be a JSON array")]

    errs: List[ValidationError] = []
    items: List[Dict[str, Any]] = []
    for i, it in enumerate(batch):
        if not isinstance(it, dict):
            errs.append(ValidationError(path=f"[{i}]", message="must be an object"))
            continue
        errs.extend(validate_outage_event(it, i))
        items.append(it)
    return items, errs


def validate_customers(
    raw: Any,
    rules: RegionRules | None = None,
    *,
    require_customer_id: bool = True,
) -> List[ValidationError]:
    errs: List[ValidationError] = []
    if not isinstance(raw, list):
        return [ValidationError(path="$", message="must be a JSON array")]

    allowed_svcs = set(DEFAULT_SERVICES)
    regions_by_service = (rules.by_service if rules else {}) or {}

    seen_ids: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}

    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            errs.append(ValidationError(path=f"[{i}]", message="must be an object"))
            continue

        cid = str(it.get("customer_id") or it.get("customerId") or "").strip()
        name = str(it.get("name") or it.get("customerName") or "").strip()
        if require_customer_id and not cid:
            errs.append(ValidationError(path=f"[{i}].customer_id", message="required"))
        if not name:
            errs.append(ValidationError(path=f"[{i}].name", message="required"))

        if cid:
            if cid in seen_ids:
                errs.append(
                    ValidationError(
                        path=f"[{i}].customer_id",
                        message=f"duplicate customer_id '{cid}' (first at [{seen_ids[cid]}])",
                    )
                )
            else:
                seen_ids[cid] = i
        if name:
            if name in seen_names:
                errs.append(
                    ValidationError(
                        path=f"[{i}].name",
                        message=f"duplicate customer name '{name}' (first at [{seen_names[name]}])",
                    )
                )
            else:
                seen_names[name] = i

        subs = it.get("subscriptions")
        if not isinstance(subs, dict):
            errs.append(ValidationError(path=f"[{i}].subscriptions", message="must be an object"))
            continue

        # `sub_services` is optional. When present, it must be a dict
        # of {service: [sub_service_codes]}. We don't enforce a closed
        # set of codes (Zscaler may add new sub-products; other services
        # may carry their own axis) — only the shape. (TODO #21)
        sub_services_in = it.get("sub_services")
        if sub_services_in is not None:
            if not isinstance(sub_services_in, dict):
                errs.append(ValidationError(path=f"[{i}].sub_services", message="must be an object"))
            else:
                for svc_key, sub_list in sub_services_in.items():
                    svc = str(svc_key).strip().lower()
                    if not svc:
                        errs.append(ValidationError(path=f"[{i}].sub_services", message="service key must be non-empty"))
                        continue
                    if svc not in allowed_svcs:
                        errs.append(
                            ValidationError(
                                path=f"[{i}].sub_services.{svc}",
                                message=f"unknown service '{svc}' (allowed: {', '.join(sorted(allowed_svcs))})",
                            )
                        )
                    if not isinstance(sub_list, list):
                        errs.append(
                            ValidationError(
                                path=f"[{i}].sub_services.{svc}",
                                message="must be a list",
                            )
                        )
                        continue
                    for k, ss in enumerate(sub_list):
                        if not str(ss or "").strip():
                            errs.append(
                                ValidationError(
                                    path=f"[{i}].sub_services.{svc}[{k}]",
                                    message="sub-service code must be non-empty",
                                )
                            )

        for svc_key, regions in subs.items():
            svc = str(svc_key).strip().lower()
            if not svc:
                errs.append(
                    ValidationError(
                        path=f"[{i}].subscriptions",
                        message="service key must be non-empty",
                    )
                )
                continue
            if svc not in allowed_svcs:
                errs.append(
                    ValidationError(
                        path=f"[{i}].subscriptions.{svc}",
                        message=f"unknown service '{svc}' (allowed: {', '.join(sorted(allowed_svcs))})",
                    )
                )
            if not isinstance(regions, list) or len(regions) == 0:
                errs.append(
                    ValidationError(
                        path=f"[{i}].subscriptions.{svc}",
                        message="must be a non-empty list",
                    )
                )
                continue
            allowed_regions = regions_by_service.get(svc)
            for j, r in enumerate(regions):
                rv = str(r).strip()
                if not rv:
                    errs.append(
                        ValidationError(
                            path=f"[{i}].subscriptions.{svc}[{j}]",
                            message="region must be non-empty",
                        )
                    )
                    continue
                rv_norm = rv.lower()
                if rv_norm == "global":
                    continue
                if allowed_regions and rv_norm not in allowed_regions:
                    errs.append(
                        ValidationError(
                            path=f"[{i}].subscriptions.{svc}[{j}]",
                            message=(
                                f"unknown region '{rv}' for service '{svc}' "
                                f"(use 'global' or a code from approved-regions.csv)"
                            ),
                        )
                    )
    return errs


def severity_from_impact(impact: str | None) -> str:
    """Map a Statuspage-style impact string to a normalized severity."""
    if not impact:
        return "info"
    return _SEV_FROM_IMPACT.get(str(impact).lower(), "info")
