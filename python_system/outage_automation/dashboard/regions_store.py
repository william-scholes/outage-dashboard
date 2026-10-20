"""Read/write/validate the approved-regions.csv used by the dashboard editor.

This module owns the 3-column shape of the CSV (Service, Region Code,
Display Name). The runtime matcher in `config.py` only reads the
(Service, Region Code) pair, but the editor and its API surface need
the human-friendly `Display Name` column to present in the UI.

Conventions:
- `service` and `region_code` are always stored lowercased + stripped.
- All writes are atomic (tempfile + os.replace), mirroring store.atomic_write_text.

Functions in this module are pure (no I/O outside load_regions_csv /
write_regions_csv). Callers — the dashboard server and the
`validate-regions` CLI — compose them into a read → validate → write
flow.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..config import DEFAULT_SERVICES
from ..validate import ValidationError


# Header columns the editor writes back. Order is load-bearing — the
# in-the-wild `approved-regions.csv` is hand-readable in this order, and
# the editor round-trips through it.
CSV_HEADER: Tuple[str, ...] = (
    "Service",
    "Region Code",
    "Display Name",
)

# region_code must be kebab-case / lowercased alphanumeric + dashes, or
# the literal "global". Operators who want to add e.g. "us_east_1" or
# "US East 1" can still hand-edit the CSV — the editor's job is to keep
# the constrained, lint-friendly form.
_REGION_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class RegionRow:
    """One row in approved-regions.csv.

    All string fields are normalised: stripped, service/region_code
    lowercased.
    """

    service: str
    region_code: str
    display_name: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "region_code": self.region_code,
            "display_name": self.display_name,
        }


# ---------------------------------------------------------------------------
# Slug / id helpers
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Convert a free-form customer name to a kebab-case customer_id.

    Examples:
        slugify("Acme Corp")            -> "acme-corp"
        slugify("  Test /  Client 2! ") -> "test-client-2"
        slugify("----")                 -> "customer"  (fallback)

    Pure: no I/O, deterministic.
    """
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower())
    s = s.strip("-")
    return s or "customer"


def unique_id(base: str, existing: Iterable[str]) -> str:
    """Return `base` if not in `existing`, else `base-2`, `base-3`, …"""
    seen = set(existing)
    if base not in seen:
        return base
    n = 2
    while f"{base}-{n}" in seen:
        n += 1
    return f"{base}-{n}"


# ---------------------------------------------------------------------------
# CSV load / write
# ---------------------------------------------------------------------------


def load_regions_csv(path: Path) -> List[RegionRow]:
    """Read the CSV at `path` into a list of RegionRow.

    Missing file → []. Rows that are entirely blank are skipped.
    Rows that are partially populated are kept and validated separately
    by `validate_regions` (so a typo shows up at the editor, not as a
    silent skip).

    Raises ValueError if the header row is missing one of the required
    columns. The editor treats that as a 500, since it indicates a
    hand-edited CSV that the system can no longer read.
    """
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        missing = [c for c in ("Service", "Region Code") if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"approved-regions.csv missing required column(s): {', '.join(missing)}"
            )

        rows: List[RegionRow] = []
        for raw in reader:
            svc = str(raw.get("Service") or "").strip().lower()
            code = str(raw.get("Region Code") or "").strip().lower()
            display = str(raw.get("Display Name") or "").strip()

            # Skip entirely-blank rows (e.g. trailing newline in the file).
            if not any([svc, code, display]):
                continue

            rows.append(
                RegionRow(
                    service=svc,
                    region_code=code,
                    display_name=display,
                )
            )
    return rows


def write_regions_csv(path: Path, rows: Sequence[RegionRow]) -> None:
    """Atomically write `rows` to `path` in the editor's column order.

    Mirrors `store.atomic_write_text`: writes to `<path>.tmp` (with
    newline="" so the csv writer controls line endings) then
    `os.replace`s into place. The parent directory is created on
    demand — useful for first-write in a fresh install.
    If `path.parent` is read-only (e.g. systemd ProtectHome), falls back
    to creating the temp file inside `state_dir`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        f = tmp.open("w", encoding="utf-8", newline="")
    except OSError:
        from ..paths import resolve_paths
        state_dir = resolve_paths(root=str(path.parent)).state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / f"{path.name}.tmp"
        f = tmp.open("w", encoding="utf-8", newline="")

    with f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_HEADER), lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "Service": r.service,
                "Region Code": r.region_code,
                "Display Name": r.display_name,
            })
    from ..store import _safe_replace_or_copy
    _safe_replace_or_copy(tmp, path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _coerce_region_code(code: Any) -> str:
    return str(code or "").strip().lower()


def _is_valid_region_code(code: str) -> bool:
    if not code:
        return False
    if code == "global":
        return True
    return bool(_REGION_CODE_RE.match(code))


def validate_regions(rows: Sequence[RegionRow]) -> List[ValidationError]:
    """Validate a list of RegionRow. Returns a list of errors (empty = OK).

    Rules:
      - service in DEFAULT_SERVICES
      - region_code matches the kebab-case regex, or is the literal "global"
      - display_name non-empty
      - no duplicate (service, region_code) pairs
    """
    errs: List[ValidationError] = []

    for i, r in enumerate(rows):
        if not r.service:
            errs.append(ValidationError(path=f"[{i}].service", message="required"))
        elif r.service not in DEFAULT_SERVICES:
            errs.append(
                ValidationError(
                    path=f"[{i}].service",
                    message=f"unknown service '{r.service}' (allowed: {', '.join(sorted(DEFAULT_SERVICES))})",
                )
            )

        code = _coerce_region_code(r.region_code)
        if not code:
            errs.append(ValidationError(path=f"[{i}].region_code", message="required"))
        elif not _is_valid_region_code(code):
            errs.append(
                ValidationError(
                    path=f"[{i}].region_code",
                    message=(
                        f"invalid region_code '{r.region_code}' "
                        f"(must be lowercase kebab-case like 'ap-southeast-2', or the literal 'global')"
                    ),
                )
            )

        if not str(r.display_name or "").strip():
            errs.append(
                ValidationError(path=f"[{i}].display_name", message="required")
            )

    # Duplicate (service, region_code) pairs.
    seen: Dict[Tuple[str, str], int] = {}
    for i, r in enumerate(rows):
        code = _coerce_region_code(r.region_code)
        if not r.service or not code:
            continue
        key = (r.service, code)
        if key in seen:
            errs.append(
                ValidationError(
                    path=f"[{i}]",
                    message=(
                        f"duplicate (service='{r.service}', region_code='{code}') "
                        f"(first at [{seen[key]}])"
                    ),
                )
            )
        else:
            seen[key] = i

    return errs


# ---------------------------------------------------------------------------
# Pure list mutations
# ---------------------------------------------------------------------------


def _norm_pair(service: Any, region_code: Any) -> Tuple[str, str] | None:
    svc = str(service or "").strip().lower()
    code = _coerce_region_code(region_code)
    if not svc or not code:
        return None
    return svc, code


def add_region(
    rows: Sequence[RegionRow],
    service: Any,
    region_code: Any,
    display_name: Any,
) -> Tuple[List[RegionRow], List[ValidationError]]:
    """Return (new_rows, errors). Pure: callers decide whether to write.

    Field-level validation (presence, service key, code shape, display
    name) is performed here and surfaced as `errors`. The caller
    typically returns 400 with these details.

    Duplicate (service, region_code) detection is a row-level concern
    and is also covered here.
    """
    errs: List[ValidationError] = []

    svc_raw = str(service or "").strip()
    code_raw = str(region_code or "").strip()
    display_raw = str(display_name or "").strip()

    if not svc_raw:
        errs.append(ValidationError(path="service", message="required"))
    elif svc_raw.lower() not in DEFAULT_SERVICES:
        errs.append(
            ValidationError(
                path="service",
                message=f"unknown service '{svc_raw}' (allowed: {', '.join(sorted(DEFAULT_SERVICES))})",
            )
        )

    if not code_raw:
        errs.append(ValidationError(path="region_code", message="required"))
    else:
        norm_code = code_raw.lower()
        if not _is_valid_region_code(norm_code):
            errs.append(
                ValidationError(
                    path="region_code",
                    message=(
                        f"invalid region_code '{code_raw}' "
                        f"(must be lowercase kebab-case like 'ap-southeast-2', or the literal 'global')"
                    ),
                )
            )

    if not display_raw:
        errs.append(ValidationError(path="display_name", message="required"))

    if errs:
        return list(rows), errs

    svc = svc_raw.lower()
    code = code_raw.lower()

    for existing in rows:
        if existing.service == svc and existing.region_code == code:
            errs.append(
                ValidationError(
                    path="",
                    message=f"duplicate (service='{svc}', region_code='{code}')",
                )
            )
            return list(rows), errs

    new_row = RegionRow(
        service=svc,
        region_code=code,
        display_name=display_raw,
    )
    return list(rows) + [new_row], []


def update_display_name(
    rows: Sequence[RegionRow],
    service: Any,
    region_code: Any,
    display_name: Any,
) -> Tuple[List[RegionRow], List[ValidationError]]:
    """Return (new_rows, errors). Replaces only `display_name` on the
    matching (service, region_code) row.
    """
    errs: List[ValidationError] = []
    pair = _norm_pair(service, region_code)
    display_raw = str(display_name or "").strip()
    if pair is None:
        errs.append(ValidationError(path="region", message="invalid service/region_code pair"))
    if not display_raw:
        errs.append(ValidationError(path="display_name", message="required"))
    if errs:
        return list(rows), errs

    if pair is None:
        return list(rows), errs
    svc, code = pair

    found = False
    out: List[RegionRow] = []
    for r in rows:
        if r.service == svc and r.region_code == code:
            out.append(
                RegionRow(
                    service=r.service,
                    region_code=r.region_code,
                    display_name=display_raw,
                )
            )
            found = True
        else:
            out.append(r)

    if not found:
        errs.append(
            ValidationError(
                path="",
                message=f"no row found at (service='{svc}', region_code='{code}')",
            )
        )
        return list(rows), errs

    return out, []


def remove_region(
    rows: Sequence[RegionRow],
    service: Any,
    region_code: Any,
) -> Tuple[List[RegionRow], List[ValidationError]]:
    """Return (new_rows, errors). Filters out the matching pair.

    No field-level validation beyond normalising the inputs; the caller
    is expected to have already 404'd on a missing pair (or this returns
    a no-op + an empty error list).
    """
    pair = _norm_pair(service, region_code)
    if pair is None:
        return list(rows), [ValidationError(path="region", message="invalid service/region_code pair")]
    svc, code = pair
    out = [r for r in rows if not (r.service == svc and r.region_code == code)]
    return out, []
