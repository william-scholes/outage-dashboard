from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path
    state_dir: Path
    events_path: Path
    alert_history_path: Path
    customers_path: Path
    dashboard_path: Path
    approved_regions_csv: Path
    raw_dir: Path


def _state_dir_for(base: Path) -> Path:
    """Locate the state directory for a given base path.

    Convention:
      - If `<base>/state` exists, use it (so the operator can `cd
        python_system && python run.py ...` and have the resolver find
        state without doubling the prefix).
      - Else if `<base>/python_system/state` exists, use it.
      - Else default to `<base>/python_system/state` (created on first
        `init`).
    """
    direct = base / "state"
    nested = base / "python_system" / "state"
    if direct.is_dir() and not nested.is_dir():
        return direct
    return nested


def resolve_paths(root: str | None = None) -> Paths:
    base = Path(root) if root else Path(os.environ.get("OA_ROOT") or Path.cwd())
    base = base.resolve()
    while base.name in ("state", "python_system"):
        base = base.parent
    state_dir = _state_dir_for(base)
    csv_path = Path(os.environ.get("OA_APPROVED_REGIONS_CSV") or (base / "approved-regions.csv"))
    return Paths(
        root=base,
        state_dir=state_dir,
        events_path=state_dir / "events.ndjson",
        alert_history_path=state_dir / "alert_history.json",
        customers_path=state_dir / "customers.json",
        dashboard_path=state_dir / "dashboard.json",
        approved_regions_csv=csv_path,
        raw_dir=state_dir / "raw",
    )
